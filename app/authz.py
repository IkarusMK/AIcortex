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
    "ssh_add", "ssh_delete_endpoint",
    "mail_add", "mail_delete_account",
    "print_add", "print_delete",
    "scan_add", "scan_delete",
    "mcp_add", "mcp_delete",
    "cron_add", "cron_delete",
    "secret_set", "secret_delete",
    "agent_register", "agent_remove",
    # Per-user data areas (who-may-see-what) — identity/policy management.
    "tenancy_set", "tenancy_unset", "tenancy_show", "tenancy_list", "tenancy_status",
}

# Read/list/search/load — safe for the viewer role.
READ_ONLY_TOOLS = {
    "bootstrap", "ping", "guide",
    "memory_read", "memory_list", "memory_search", "memory_candidates",
    "skill_search", "skill_list", "skill_load", "skill_resource",
    "service_list", "mqtt_list", "mqtt_get",
    "ftp_list", "ftp_list_endpoints",
    "webdav_list", "webdav_list_endpoints",
    "ssh_list", "ssh_list_dir",
    "mail_list", "print_list", "scan_list",
    "mcp_list", "mcp_tools",
    "fs_list", "fs_read", "fs_info",
    "session_list", "session_load",
    "cron_list", "cron_due",
    "agent_list", "inbox_read",
    "task_list", "task_next",
    "secret_list",
}

_VALID_ROLES = ("admin", "user", "viewer")


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
                    identity, is_runner, claims = _identity()
                    role = role_for(identity, is_runner, claims)
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
                    # Per-user data isolation (tenancy, opt-in TENANCY_ISOLATE=1):
                    # confine a non-admin caller's memory_* calls to their OWN scope
                    # so two people on one brain don't read/overwrite each other.
                    # Fail-open: any glitch here leaves the requested scope untouched.
                    if isinstance(args, dict):
                        try:
                            import tenancy
                            if tool in tenancy.MEMORY_SCOPED_TOOLS:
                                args["scope"] = tenancy.confine_memory_scope(
                                    identity, role, args.get("scope", "shared"))
                        except Exception:
                            pass
            except Exception as exc:
                if exc.__class__ is ToolError or isinstance(exc, ToolError):
                    raise  # a real policy denial must propagate
                # anything else (identity/policy resolution) → fail-open
            return await call_next(context)

    try:
        return AuthzMiddleware()
    except Exception:
        return None
