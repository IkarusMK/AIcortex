"""Authorization layer — per-credential roles, tool permissions, audit log.

The connector authenticates callers (OIDC and/or a static RUNNER_TOKEN) but, by
itself, authentication is all-or-nothing: any authenticated caller can invoke
every tool. This module adds least-privilege AUTHORIZATION on top, as a single
central policy gate (a FastMCP middleware) — the pattern from the
`securing-agentic-ai-tool-invocation` skill: deny-by-default tool allowlists,
identity binding, a policy decision before each call, and an audit log.

DESIGN SAFETY (so it can't lock a hands-off operator out):
- ON by default (secure by default); AUTH_ENFORCE=0 disables it entirely.
- Fail-open: any error resolving identity/policy → allow (degrade, don't brick).
- Roles can come from the IdP: if the token carries a role/group claim
  (AUTH_ROLE_CLAIM, default "groups", e.g. PocketID groups), it drives the role.
  Dormant today because the FastMCP OIDC proxy issues minimal-claim tokens — it
  activates automatically once the claim is forwarded, no rework needed.
- Secure defaults WHEN enabled: the RUNNER_TOKEN (headless/autonomy) becomes a
  non-admin "user" role — it can no longer register services/MCP servers, set
  secrets, add cron jobs, etc. (closes #2/#4/#5/#7/#15) — while an interactive
  OIDC operator stays "admin" by default, so the human isn't locked out.
- Policy is DATA: optional /data/auth/policy.json maps identities → roles.

Roles:
- admin  → every tool.
- user   → everything EXCEPT the admin tools (registration / secrets / identity).
- viewer → read-only tools only.
"""
import contextlib
import contextvars
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

AUTH_DIR = Path(os.environ.get("AUTH_STORE_DIR", "/data/auth"))
POLICY_FILE = AUTH_DIR / "policy.json"
AUDIT_FILE = AUTH_DIR / "audit.log"

# "Powerful" tools: register integrations/devices, manage secrets, identities,
# scheduled prompts. Restricted to the admin role (this is the core of #4/#5/#7).
ADMIN_TOOLS = {
    "service_add", "service_delete",
    "mqtt_add", "mqtt_delete",
    "ftp_add", "ftp_delete",
    "webdav_add", "webdav_delete_endpoint",
    "caldav_add", "caldav_delete_endpoint",
    "ssh_add", "ssh_delete_endpoint",
    "mail_add", "mail_delete_account",
    "imap_add", "imap_delete_account",
    "print_add", "print_delete",
    "scan_add", "scan_delete",
    "mcp_add", "mcp_delete",
    "webhook_add", "webhook_delete",
    # cron_add/cron_delete are NOT here: a non-admin may schedule/delete their OWN
    # jobs (forced act-as as themselves); the tools enforce owner-scoping in-tool.
    "secret_set", "secret_delete",
    "agent_register", "agent_remove",
    # Per-user data areas (who-may-see-what) — identity/policy management.
    "tenancy_set", "tenancy_unset", "tenancy_show", "tenancy_list", "tenancy_status",
    # REST API key control plane — minting/revoking credentials is admin-only.
    "apikey_create", "apikey_list", "apikey_revoke",
}

# Read/list/search/load — safe for the viewer role.
READ_ONLY_TOOLS = {
    "bootstrap", "ping", "guide",
    "memory_read", "memory_list", "memory_search", "memory_candidates",
    "skill_search", "skill_list", "skill_load", "skill_resource",
    "service_list", "mqtt_list", "mqtt_get",
    "ftp_list", "ftp_list_endpoints",
    "webdav_list", "webdav_list_endpoints",
    "caldav_list_endpoints", "caldav_list_calendars", "caldav_list_events",
    "ssh_list", "ssh_list_dir",
    "mail_list", "imap_list", "imap_search", "imap_fetch",
    "print_list", "scan_list",
    "mcp_list", "mcp_tools",
    "webhook_list",
    "fs_list", "fs_read", "fs_info",
    "session_list", "session_load",
    # cron_list is read-only (filtered to the caller's own jobs in-tool). cron_due
    # is NOT here — it mints act-as capability tokens, so it's runner/admin-only
    # (guarded in-tool), not a safe read for a viewer.
    "cron_list",
    "agent_list", "inbox_read",
    "task_list", "task_next",
    "secret_list",
}

_VALID_ROLES = ("admin", "user", "viewer")

# Device/endpoint ACTION tools → (capability class, the arg naming the endpoint).
# Under enforce, a non-admin may invoke these only on endpoints an admin assigned
# them (tenancy.endpoint_allowed); this closes the gap where per-user areas covered
# only services/skills, not the caldav/imap/webdav/ssh/… registries.
_ENDPOINT_TOOLS = {
    "caldav_list_calendars": ("caldav", "endpoint"),
    "caldav_list_events": ("caldav", "endpoint"),
    "caldav_add_event": ("caldav", "endpoint"),
    "caldav_delete_event": ("caldav", "endpoint"),
    "imap_search": ("imap", "account"), "imap_fetch": ("imap", "account"),
    "webdav_list": ("webdav", "endpoint"), "webdav_upload": ("webdav", "endpoint"),
    "webdav_download": ("webdav", "endpoint"), "webdav_mkdir": ("webdav", "endpoint"),
    "webdav_delete": ("webdav", "endpoint"),
    "ssh_run": ("ssh", "host"), "ssh_upload": ("ssh", "host"),
    "ssh_download": ("ssh", "host"), "ssh_list_dir": ("ssh", "host"),
    "mail_send": ("mail", "account"),
    "print_document": ("print", "printer"),
    "scan_document": ("scan", "scanner"),
    "mqtt_publish": ("mqtt", "device"), "mqtt_get": ("mqtt", "device"),
    "mcp_call": ("mcp", "server"), "mcp_tools": ("mcp", "server"),
    "ftp_upload": ("ftp", "endpoint"),
}

# Request-scoped effective identity for the REST layer (rest_api.py). Unlike the
# process-global cron act-as, a contextvar is per-async-task, so concurrent REST
# requests never bleed identity into one another. effective_identity() reads it FIRST.
_REST_IDENTITY: contextvars.ContextVar = contextvars.ContextVar("rest_identity", default=None)


@contextlib.contextmanager
def rest_identity(sub: str, role: str):
    """Bind (sub, role) as the effective identity for the current async task, so a
    tool invoked from a REST handler self-scopes (memory/vault/services/skills) as the
    key's owner. Auto-reset on exit; safe under concurrency (contextvar, not global)."""
    token = _REST_IDENTITY.set((sub, role))
    try:
        yield
    finally:
        _REST_IDENTITY.reset(token)


def enabled() -> bool:
    # Secure by default: enforce unless explicitly disabled.
    return os.environ.get("AUTH_ENFORCE", "1").strip().lower() not in ("0", "false", "no", "off")


def audit_all() -> bool:
    return os.environ.get("AUTH_AUDIT_ALL", "0").strip().lower() in ("1", "true", "yes")


def _policy() -> dict:
    try:
        return json.loads(POLICY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


_RANK = {"viewer": 0, "user": 1, "admin": 2}


def _claim_value(claims, key):
    """Read a claim, preferring the forwarded upstream identity (PocketIDProxy
    nests the IdP's claims under 'upstream_claims'), then the top level."""
    if not isinstance(claims, dict):
        return None
    up = claims.get("upstream_claims")
    if isinstance(up, dict) and key in up:
        return up[key]
    return claims.get(key)


def _role_from_claims(claims) -> str:
    """Map an IdP role/group claim (e.g. PocketID groups) to a role, taking the
    highest privilege found. Claim name = AUTH_ROLE_CLAIM (default "groups", also
    tolerates "oc_groups"); group→role mapping can be set in policy.json under
    "groups". Returns "" if no usable claim is present."""
    if not claims:
        return ""
    claim_name = os.environ.get("AUTH_ROLE_CLAIM", "groups")
    val = _claim_value(claims, claim_name)
    if val is None and claim_name != "oc_groups":
        val = _claim_value(claims, "oc_groups")
    if val is None:
        return ""
    vals = val if isinstance(val, (list, tuple)) else re.split(r"[,\s]+", str(val))
    gmap = {str(k).lower(): v for k, v in (_policy().get("groups", {}) or {}).items()}
    best = ""
    for v in vals:
        v = str(v).strip().lower()
        if not v:
            continue
        r = v if v in _VALID_ROLES else gmap.get(v, "")
        if r in _VALID_ROLES and (not best or _RANK[r] > _RANK[best]):
            best = r
    return best


def role_for(identity: str, is_runner: bool, claims=None) -> str:
    """Resolve a caller's role. Precedence: explicit per-identity policy →
    IdP role/group claim → runner/default fallback. Secure defaults: runner→user,
    everyone else→admin (so the human operator is never locked out)."""
    pol = _policy()
    roles = pol.get("roles", {}) if isinstance(pol, dict) else {}
    if identity and identity in roles and roles[identity] in _VALID_ROLES:
        return roles[identity]
    claim_role = _role_from_claims(claims)
    if claim_role:
        return claim_role
    if is_runner:
        r = os.environ.get("RUNNER_ROLE") or pol.get("runner") or "user"
    else:
        r = os.environ.get("OIDC_DEFAULT_ROLE") or pol.get("default") or "admin"
    return r if r in _VALID_ROLES else "user"


def decide(role: str, tool: str) -> tuple[bool, str]:
    """Allow/deny a tool for a role (deny-by-default for unknown roles)."""
    if role == "admin":
        return True, "admin"
    if role == "viewer":
        if tool in READ_ONLY_TOOLS:
            return True, "viewer: read-only"
        return False, "viewer may only call read-only tools"
    if role == "user":
        if tool in ADMIN_TOOLS:
            return False, "admin-only tool (registration/secrets/identity)"
        return True, "user"
    return False, f"unknown role '{role}'"


def audit(identity: str, role: str, tool: str, decision: str, reason: str) -> None:
    try:
        AUTH_DIR.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "identity": identity or "unknown", "role": role,
            "tool": tool, "decision": decision, "reason": reason,
        }
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass  # never let auditing affect a call


def _identity():
    """(identity, is_runner, claims) from the FastMCP auth context. Fail-open to
    ('unknown', False, {}) so a resolution problem can't lock anyone out."""
    try:
        from fastmcp.server.dependencies import get_access_token
        tok = get_access_token()
        cid = getattr(tok, "client_id", None) or "unknown"
        claims = getattr(tok, "claims", None) or {}
        # Per-person identity from the forwarded upstream 'sub' if present,
        # else the client_id. is_runner stays tied to the static token's client_id.
        person = _claim_value(claims, "sub")
        return (person or cid), (cid == "runner"), claims
    except Exception:
        return "unknown", False, {}


def _actas_role(owner: str) -> str:
    """Role for a cron act-as OWNER. Crucially this NEVER falls back to the default
    'admin' the way an interactive caller does — an owned job must run with the
    owner's OWN (least) privilege, so it resolves to the owner's explicit policy
    role or 'user'. Otherwise a scheduled job would silently run as admin."""
    pol = _policy()
    roles = pol.get("roles", {}) if isinstance(pol, dict) else {}
    r = roles.get(owner)
    return r if r in _VALID_ROLES else "user"


def role_for_apikey(sub: str) -> str:
    """Role for a REST API-key identity. Like the act-as owner, this NEVER falls back
    to the default 'admin' an interactive OIDC caller gets — a key resolves to the
    identity's explicit policy role, else 'user'. So a leaked key is never an
    accidental super-user; grant admin in policy.json if a key truly needs it."""
    return _actas_role(sub)


def effective_identity():
    """(identity, role) for the CURRENT call, honoring (in order) a REST per-task
    identity, then an active cron act-as binding, else the real caller. When the
    runner is executing a job as an owner, that owner (at the owner's own privilege) is
    the effective caller for both tool-gating and data-scoping — so the job is confined
    to the owner's area, never the runner's. Fail-open to ('unknown','user') only on a
    hard resolution error."""
    try:
        rest = _REST_IDENTITY.get()
        if rest:
            return rest
    except Exception:
        pass
    try:
        import actas
        owner = actas.current()
        if owner:
            return owner, _actas_role(owner)
    except Exception:
        pass
    try:
        ident, is_runner, claims = _identity()
        return ident, role_for(ident, is_runner, claims)
    except Exception:
        return "unknown", "user"


def enforce_rest(identity: str, role: str, tool: str, args) -> tuple[bool, str]:
    """Policy gate for the REST layer — the MCP middleware's equivalent, but it
    RETURNS ``(ok, reason)`` (a route can't raise ToolError) and audits the same way.
    Mirrors on_call_tool exactly: role decide → attribution stamp → memory-scope
    confine → per-user device-endpoint area. The caller wraps the subsequent
    ``tool.run()`` in ``rest_identity(identity, role)`` so in-tool self-scoping
    (service_list/secret_list/memory) resolves to the key's owner. `args` is mutated
    in place (scope/attribution), matching the middleware."""
    if not (enabled() and tool):
        return True, "enforce off"
    ok, reason = decide(role, tool)
    if not ok or audit_all():
        audit(identity, role, tool, "allow" if ok else "deny", reason)
    if not ok:
        return False, reason
    try:
        if isinstance(args, dict):
            if identity and identity != "unknown":
                if tool == "inbox_post":
                    args["sender"] = identity
                elif tool == "task_add":
                    args["created_by"] = identity
            import tenancy
            if tool in tenancy.MEMORY_SCOPED_TOOLS:
                args["scope"] = tenancy.confine_memory_scope(
                    identity, role, args.get("scope", "shared"))
            if tool in _ENDPOINT_TOOLS:
                kind, arg = _ENDPOINT_TOOLS[tool]
                if not tenancy.endpoint_allowed(identity, role, kind, args.get(arg, "")):
                    audit(identity, role, tool, "deny",
                          f"{kind} endpoint '{args.get(arg, '')}' not in caller's area")
                    return False, (f"{kind} endpoint '{args.get(arg, '')}' is not in your "
                                   f"allowed set (an admin grants it with tenancy_set)")
    except Exception as exc:
        # Fail-open like the middleware, but LEAVE A TRACE (a silent degrade-to-allow
        # is exactly what an attacker would want to pass unnoticed).
        audit(identity or "unknown", role or "?", tool or "?", "fail-open",
              f"rest authz error, allowed without full scoping: {type(exc).__name__}: {exc}")
    return True, "ok"


def build_middleware():
    """Return a FastMCP Middleware enforcing the policy, or None if unavailable."""
    try:
        from fastmcp.server.middleware import Middleware
    except Exception:
        return None

    try:
        from fastmcp.exceptions import ToolError
    except Exception:
        ToolError = RuntimeError

    class AuthzMiddleware(Middleware):
        async def on_call_tool(self, context, call_next):
            tool = getattr(getattr(context, "message", None), "name", None)
            try:
                if enabled() and tool:
                    # effective_identity() honors an active cron act-as binding, so a
                    # scheduled job is gated + scoped as its OWNER (least privilege),
                    # never as the runner.
                    identity, role = effective_identity()
                    ok, reason = decide(role, tool)
                    if not ok or audit_all():
                        audit(identity, role, tool, "allow" if ok else "deny", reason)
                    if not ok:
                        raise ToolError(
                            f"Denied by policy: role '{role}' may not call '{tool}' "
                            f"({reason}). An operator can grant access in "
                            f"/data/auth/policy.json or via the role env vars.")
                    # #16: stamp the authenticated identity onto attribution fields
                    # so a caller can't forge who a message/task came from.
                    args = getattr(context.message, "arguments", None)
                    if isinstance(args, dict) and identity and identity != "unknown":
                        if tool == "inbox_post":
                            args["sender"] = identity
                        elif tool == "task_add":
                            args["created_by"] = identity
                    # Per-user data isolation (tenancy, rides on AUTH_ENFORCE):
                    # confine a non-admin caller's memory_* calls to their OWN scope
                    # so two people on one brain don't read/overwrite each other. Under
                    # act-as, `identity`/`role` are already the OWNER, so a job's memory
                    # lands in the owner's scope. Fail-open: a glitch leaves scope as-is.
                    if isinstance(args, dict):
                        try:
                            import tenancy
                            if tool in tenancy.MEMORY_SCOPED_TOOLS:
                                args["scope"] = tenancy.confine_memory_scope(
                                    identity, role, args.get("scope", "shared"))
                        except Exception:
                            pass
                    # Per-user DEVICE/ENDPOINT areas (H1): confine a non-admin's use of
                    # caldav/imap/webdav/ssh/mail/… to the endpoints an admin assigned.
                    # endpoint_allowed is itself fail-closed under enforce / open in
                    # homelab, so a denial here is a real policy decision.
                    if tool in _ENDPOINT_TOOLS and isinstance(args, dict):
                        import tenancy
                        kind, arg = _ENDPOINT_TOOLS[tool]
                        if not tenancy.endpoint_allowed(identity, role, kind, args.get(arg, "")):
                            audit(identity, role, tool, "deny",
                                  f"{kind} endpoint '{args.get(arg, '')}' not in caller's area")
                            raise ToolError(
                                f"Denied: {kind} endpoint '{args.get(arg, '')}' is not in "
                                f"your allowed set. An admin grants it with "
                                f"tenancy_set(identity, {kind}='<name>'|'all').")
            except Exception as exc:
                if exc.__class__ is ToolError or isinstance(exc, ToolError):
                    raise  # a real policy denial must propagate
                # anything else (identity/policy resolution) → fail-open, but
                # LEAVE A TRACE: a silent degrade-to-allow is exactly what an
                # attacker would want to go unnoticed, so record it.
                audit("unknown", "?", tool or "?", "fail-open",
                      f"authz error, allowed without check: {type(exc).__name__}: {exc}")
            return await call_next(context)

    try:
        return AuthzMiddleware()
    except Exception:
        return None
