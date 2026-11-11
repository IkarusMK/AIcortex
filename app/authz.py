"""Authorization layer — per-credential roles, tool permissions, audit log.

The connector authenticates callers (OIDC and/or a static RUNNER_TOKEN) but, by
itself, authentication is all-or-nothing: any authenticated caller can invoke
every tool. This module adds least-privilege AUTHORIZATION on top, as a single
central policy gate (a FastMCP middleware) — the pattern from the
`securing-agentic-ai-tool-invocation` skill: deny-by-default tool allowlists,
identity binding, a policy decision before each call, and an audit log.

DESIGN SAFETY (so it can't lock a hands-off operator out):
- ON by default (secure by default); AUTH_ENFORCE=0 disables it entirely.
- Fail-open on POLICY glitches (degrade, don't brick) — but never on IDENTITY once
  sign-in is configured: with OIDC every /mcp caller carries a token, so a caller
  whose identity can't be resolved ("unknown"/empty) gets NO role and is denied
  (audited). Without OIDC, "unknown" is the local operator, exactly as before.
- Roles can come from the IdP: if the token carries a role/group claim
  (AUTH_ROLE_CLAIM, default "groups", e.g. PocketID groups), it drives the role.
- Secure defaults WHEN enabled: the RUNNER_TOKEN (headless/autonomy) becomes a
  non-admin "user" role — it can no longer register services/MCP servers, set
  secrets, add cron jobs, etc. (closes #2/#4/#5/#7/#15). An interactive person is
  admin only as an OPERATOR (policy "operators", or an explicit role); everyone
  else the IdP lets in starts as "user". OPERATORS (env) names operators outright;
  otherwise the first person to sign in to an install nobody is in charge of becomes
  the operator — only within OPERATOR_BOOTSTRAP_MINUTES of the start — and an upgrade
  keeps today's admins as operators (see ``migrate_operators``), so the human isn't
  locked out. The last person in charge can't be removed (``last_operator_refusal``).
- Policy is DATA: optional /data/auth/policy.json maps identities → roles.

Roles:
- admin  → every tool.
- user   → everything EXCEPT the admin tools (registration / secrets / identity).
- viewer → read-only tools only.
"""
import contextlib
import contextvars
import hashlib
import json
import os
import re
import time
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
    
    # Per-user data areas (who-may-see-what) — identity/policy management.
    "tenancy_set", "tenancy_unset", "tenancy_show", "tenancy_list", "tenancy_status",
    # REST API key control plane — minting/revoking credentials is admin-only.
    "apikey_create", "apikey_list", "apikey_revoke",
}

# Read/list/search/load — safe for the viewer role.
READ_ONLY_TOOLS = {
    "bootstrap", "ping", "guide", "cortex_start", "cortex_catalog", "integration_search",
    # the router itself; every call it makes is gated again by role (read-only here)
    "integration_call",
    "memory_read", "memory_list", "memory_search", "memory_candidates",
    "skill_search", "skill_list", "skill_load", "skill_resource",
    "playbook_list", "playbook_search", "playbook_load", "playbook_status", "playbook_runs",
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
    "cron_list", "runner_list",
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
    "mqtt_publish": ("mqtt", "broker"), "mqtt_get": ("mqtt", "broker"),
    "mcp_call": ("mcp", "server"), "mcp_tools": ("mcp", "server"),
    "ftp_list": ("ftp", "server"), "ftp_upload": ("ftp", "server"),
}
# tests/test_authz_endpoint_map.py checks every entry against the tool's real
# parameters — a wrong name here silently disables the per-user gate.

# Request-scoped effective identity for the REST layer (rest_api.py). Unlike the
# process-global cron act-as, a contextvar is per-async-task, so concurrent REST
# requests never bleed identity into one another. effective_identity() reads it FIRST.
_REST_IDENTITY: contextvars.ContextVar = contextvars.ContextVar("rest_identity", default=None)
# How a REST-layer call came in: "key" for an agent key on /api/v1, "" for the WebUI.
_REST_VIA: contextvars.ContextVar = contextvars.ContextVar("rest_via", default="")


@contextlib.contextmanager
def rest_identity(sub: str, role: str, via: str = ""):
    """Bind (sub, role) as the effective identity for the current async task, so a
    tool invoked from a REST handler self-scopes (memory/vault/services/skills) as the
    key's owner. Auto-reset on exit; safe under concurrency (contextvar, not global).
    via="key" marks a call made with an agent key (rest_api), not by the person in
    the WebUI."""
    token, how = _REST_IDENTITY.set((sub, role)), _REST_VIA.set(via)
    try:
        yield
    finally:
        _REST_IDENTITY.reset(token)
        _REST_VIA.reset(how)


def is_agent_key() -> bool:
    """Is the current call made with an agent key (on /api/v1 or /mcp) rather than by
    the person themselves (the WebUI, or an app they signed in to)?"""
    if _REST_IDENTITY.get():
        return _REST_VIA.get() == "key"
    _, _, claims = _identity()
    return bool(isinstance(claims, dict) and claims.get("aicortex_key"))


def from_webui() -> bool:
    """Is the current call made by a person in the WebUI (a REST-layer identity that is
    not an agent key)? For the few settings only a person may loosen."""
    return bool(_REST_IDENTITY.get()) and _REST_VIA.get() != "key"


def enabled() -> bool:
    # Secure by default: enforce unless explicitly disabled.
    return os.environ.get("AUTH_ENFORCE", "1").strip().lower() not in ("0", "false", "no", "off")


def auth_configured() -> bool:
    """Is interactive sign-in (OIDC) configured? Then every caller of /mcp presents a
    token, so an identity that can't be resolved is a fault — never the local operator
    it stands for in a server without sign-in."""
    return bool(os.environ.get("OIDC_CONFIG_URL") and os.environ.get("OIDC_CLIENT_ID"))


def _unresolved(identity) -> bool:
    return not identity or identity == "unknown"


def _identity_required() -> bool:
    """Under enforcement WITH sign-in configured, an unresolved identity gets no role
    and no unrestricted area (fail-closed). Otherwise it keeps its old meaning."""
    return enabled() and auth_configured()


def _unresolved_role() -> str:
    """The role of a caller whose identity lookup failed: none at all when sign-in is
    configured (every tool denied), else the "user" fallback of a server without it."""
    return "" if _identity_required() else "user"


def audit_all() -> bool:
    return os.environ.get("AUTH_AUDIT_ALL", "0").strip().lower() in ("1", "true", "yes")


def _policy() -> dict:
    try:
        obj = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


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


def _explicit_role(pol, identity: str) -> str:
    """The identity's explicit policy role ("roles"), or "" — tolerating a hand-edited
    policy where "roles" is not an object."""
    roles = pol.get("roles") if isinstance(pol, dict) else None
    role = roles.get(identity) if isinstance(roles, dict) and identity else None
    return role if role in _VALID_ROLES else ""


def _configured_default(pol) -> str:
    """The operator's chosen role for interactive identities nobody configured:
    OIDC_DEFAULT_ROLE, then policy "default". "" when neither is set."""
    default = pol.get("default") if isinstance(pol, dict) else ""
    return str(os.environ.get("OIDC_DEFAULT_ROLE") or default or "")


def env_operators() -> frozenset:
    """Identities named in OPERATORS (comma-separated) — operators whatever the policy
    says, so an install can name its operator before anyone signs in."""
    raw = os.environ.get("OPERATORS", "")
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def _policy_operators(pol) -> frozenset:
    ops = pol.get("operators") if isinstance(pol, dict) else None
    if not isinstance(ops, list):
        return frozenset()
    return frozenset(o.strip() for o in ops if isinstance(o, str) and o.strip())


def operators(pol=None) -> frozenset:
    """The operators — the people who are admin when they sign in interactively
    (OIDC/WebUI): listed in policy "operators" or in OPERATORS. Never their agent keys
    or cron jobs."""
    pol = _policy() if pol is None else pol
    return _policy_operators(pol) | env_operators()


def in_charge(pol) -> frozenset:
    """Who the policy (with OPERATORS) makes admin at sign-in on its own: operators
    without an explicit non-admin role, and explicit admins."""
    roles = pol.get("roles") if isinstance(pol, dict) else None
    roles = roles if isinstance(roles, dict) else {}
    admins = {str(i) for i, r in roles.items() if r == "admin"}
    return frozenset(admins | {o for o in operators(pol)
                               if _explicit_role(pol, o) in ("", "admin")})


def last_operator_refusal(old, new) -> str:
    """"" or why a policy change must be refused: it would remove the last person in
    charge, leaving an install nobody can manage (and, at the next start, open to
    whoever signs in first)."""
    before = in_charge(old)
    if not before or in_charge(new):
        return ""
    return (f"Refused: {', '.join(sorted(before))} is the last operator — nobody would be "
            f"admin any more. Make someone else an operator or an admin first "
            f"(OPERATORS, or role admin in the users view).")


def _has_explicit_admin(pol) -> bool:
    roles = pol.get("roles") if isinstance(pol, dict) else None
    return isinstance(roles, dict) and "admin" in roles.values()


def _bootstrap_open(pol) -> bool:
    """Would the next interactive identity nobody configured become the operator?
    Only while nobody is in charge yet: no operator, no explicit admin and no
    configured default role."""
    return not (operators(pol) or _has_explicit_admin(pol) or _configured_default(pol))


def session_role(identity: str, claim_role: str = "", pol=None) -> str:
    """Role of an interactive person (OIDC token, WebUI session), computed from the
    CURRENT policy — so a demotion or a policy change takes effect on the next request.

    Precedence: explicit ``roles`` → the IdP's claim role → named in OPERATORS → admin
    → OIDC_DEFAULT_ROLE → policy ``default`` → listed in policy ``operators`` → admin,
    otherwise "user". A configured default that is not a valid role is "user", never
    admin.

    Without sign-in configured, "unknown" is the local operator and keeps today's
    meaning (the configured default, else admin); with sign-in configured an
    unresolved identity gets no role at all."""
    if _unresolved(identity) and _identity_required():
        return ""
    pol = _policy() if pol is None else pol
    explicit = _explicit_role(pol, identity)
    if explicit:
        return explicit
    if claim_role in _VALID_ROLES:
        return claim_role
    if identity in env_operators():
        return "admin"            # named by the deployment: not one of "nobody configured"
    configured = _configured_default(pol)
    if configured:
        return configured if configured in _VALID_ROLES else "user"
    if _unresolved(identity) or identity in operators(pol):
        return "admin"
    return "user"


def default_role(pol=None) -> str:
    """What a NEW interactive identity (no policy entry, no IdP claim) gets right now —
    for the WebUI. While nobody is in charge and the bootstrap window is open, the next
    sign-in becomes the operator."""
    pol = _policy() if pol is None else pol
    configured = _configured_default(pol)
    if configured:
        return configured if configured in _VALID_ROLES else "user"
    return "admin" if _bootstrap_open(pol) and _bootstrap_window_open() else "user"


def _policy_strict() -> dict:
    """policy.json for a read-modify-write: {} when absent, but an unreadable or
    non-object file RAISES — rewriting it from {} would wipe everyone's roles."""
    if not POLICY_FILE.exists():
        return {}
    obj = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("policy.json is not an object")
    return obj


# The first-come bootstrap: on a start with nobody in charge, the first interactive
# sign-in within OPERATOR_BOOTSTRAP_MINUTES becomes the operator. Without a window,
# an install whose policy names nobody handed admin to whoever signed in first — at
# any time, months later. Opened only by ``start_bootstrap_window`` (server startup).
_BOOTSTRAP_MINUTES = 30
_BOOTSTRAP: dict = {"until": None}      # monotonic deadline; None = closed


def _clock() -> float:
    return time.monotonic()


def _bootstrap_window_open() -> bool:
    until = _BOOTSTRAP["until"]
    return until is not None and _clock() < until


def start_bootstrap_window(pol=None) -> int:
    """Startup step (server._build_auth, sign-in configured): open the first-come
    window when nobody is in charge, and say so loudly. Returns the minutes it is open
    (0 = closed: someone is in charge, or OPERATOR_BOOTSTRAP_MINUTES=0)."""
    pol = _policy() if pol is None else pol
    minutes = _env_int("OPERATOR_BOOTSTRAP_MINUTES", _BOOTSTRAP_MINUTES, 0)
    _BOOTSTRAP["until"] = None
    if not _bootstrap_open(pol):
        return 0
    if not minutes:
        print("[AICortex] WARNING: nobody is operator (admin) and the first-come bootstrap "
              "is off (OPERATOR_BOOTSTRAP_MINUTES=0). Name the operator with "
              "OPERATORS=<identity> (the IdP 'sub') and restart.")
        return 0
    _BOOTSTRAP["until"] = _clock() + minutes * 60
    print(f"[AICortex] WARNING: nobody is operator (admin) yet. The FIRST person to sign "
          f"in within the next {minutes} minutes becomes the operator. Name the operator "
          f"instead with OPERATORS=<identity> (the IdP 'sub'); "
          f"OPERATOR_BOOTSTRAP_MINUTES=0 switches this off.")
    return minutes


def _bootstrap_operator(identity: str) -> bool:
    """Record ``identity`` as the first operator, if nobody is in charge yet. Runs
    under the policy lock and re-checks there, so two first sign-ins at once can't
    both become operator. Never raises; a broken policy is left untouched."""
    try:
        import store
        import tenancy
        with store.locked(POLICY_FILE):
            pol = _policy_strict()
            if not _bootstrap_open(pol):
                return identity in operators(pol)
            tenancy._write_policy({**pol, "operators": [identity]})
    except Exception as exc:
        audit(identity, "user", "bootstrap", "deny",
              f"could not record the first operator: {type(exc).__name__}")
        return False
    _BOOTSTRAP["until"] = None
    audit(identity, "admin", "bootstrap", "allow",
          "first interactive sign-in recorded as operator (nobody was in charge)")
    print(f"[AICortex] WARNING: operator bootstrap: '{identity}' signed in first and is "
          f"now the operator (admin). If that is not you, remove them in the WebUI "
          f"(Users) or in policy.json and set OPERATORS.")
    return True


def migrate_operators():
    """Upgrade step, run once at startup (server._build_auth, sign-in configured).

    Up to 2.5.0 every interactive identity nobody configured was admin. Keep exactly
    those people admin by listing them as operators: everyone in policy "users" or
    "roles" without an explicit non-admin role. Only NEW identities then start as
    "user". Skipped when the policy already has "operators" or a default role is
    configured (OIDC_DEFAULT_ROLE / policy "default" keep deciding as before), and
    when nobody is listed (a fresh install bootstraps at the first sign-in instead).
    Returns the recorded list, or None when nothing was written. Never raises."""
    try:
        import store
        import tenancy
        with store.locked(POLICY_FILE):
            pol = _policy_strict()
            if "operators" in pol or _configured_default(pol):
                return None
            listed = set()
            for key in ("users", "roles"):
                section = pol.get(key)
                if isinstance(section, dict):
                    listed |= {str(k).strip() for k in section if str(k).strip()}
            roles = pol.get("roles") if isinstance(pol.get("roles"), dict) else {}
            admins = sorted(i for i in listed if roles.get(i) not in ("user", "viewer"))
            if not admins:
                return None
            tenancy._write_policy({**pol, "operators": admins})
    except Exception as exc:
        audit("system", "-", "migrate_operators", "deny",
              f"operators not migrated: {type(exc).__name__}: {exc}")
        return None
    audit("system", "-", "migrate_operators", "allow",
          f"today's admins kept as operators: {', '.join(admins)}")
    return admins


def role_for(identity: str, is_runner: bool, claims=None) -> str:
    """Resolve a caller's role. Precedence: explicit per-identity policy →
    IdP role/group claim → runner/default fallback. Secure defaults: runner→user,
    an agent key→its person's explicit role or user, an interactive person →
    ``session_role`` (operators are admin, everyone else user). The very first
    interactive identity of an install nobody is in charge of becomes the operator —
    within the bootstrap window after the start only (``start_bootstrap_window``).
    With sign-in configured, an unresolved identity gets no role ("")."""
    if _unresolved(identity) and _identity_required():
        return ""
    if isinstance(claims, dict) and claims.get("aicortex_key"):
        # A per-agent key (agent_auth.py) is never an accidental admin: the key's
        # person's explicit policy role, else "user" — same rule as on REST.
        return role_for_apikey(identity)
    pol = _policy()
    claim_role = _role_from_claims(claims)
    if is_runner:
        explicit = _explicit_role(pol, identity)
        if explicit or claim_role:
            return explicit or claim_role
        r = os.environ.get("RUNNER_ROLE") or pol.get("runner") or "user"
        return r if r in _VALID_ROLES else "user"
    role = session_role(identity, claim_role, pol)
    if (role == "user" and not claim_role and not _explicit_role(pol, identity)
            and not _unresolved(identity) and _bootstrap_open(pol)
            and _bootstrap_window_open() and _bootstrap_operator(identity)):
        return "admin"
    return role


def decide(role: str, tool: str) -> tuple[bool, str]:
    """Allow/deny a tool for a role (deny-by-default for unknown roles)."""
    if not role:
        return False, "no role: the caller's identity could not be resolved"
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


_AUDIT_MAX_BYTES = 5_000_000   # rotate the audit log at this size (AUDIT_MAX_BYTES; 0 = never)
_AUDIT_KEEP = 3                # rotated generations kept: audit.log.1 … .3 (AUDIT_KEEP)


def _env_int(name: str, default: int, minimum: int) -> int:
    try:
        value = int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
    return value if value >= minimum else default


def _rotate_audit() -> None:
    """Size-based rotation: audit.log → audit.log.1 → … → audit.log.<AUDIT_KEEP>, the
    oldest dropped. Unauthenticated callers can cause audit lines (refused /hooks
    deliveries), so an unbounded file could fill the data volume. Checked before each
    write; the lock + re-check keep two writers from rotating twice. Never raises."""
    try:
        limit = _env_int("AUDIT_MAX_BYTES", _AUDIT_MAX_BYTES, 0)
        if not limit or AUDIT_FILE.stat().st_size < limit:
            return
        keep = _env_int("AUDIT_KEEP", _AUDIT_KEEP, 1)
        import store
        with store.locked(AUDIT_FILE.with_name("audit-rotate")):
            if AUDIT_FILE.stat().st_size < limit:
                return                    # another writer rotated while we waited
            for n in range(keep - 1, 0, -1):
                older = AUDIT_FILE.with_name(f"{AUDIT_FILE.name}.{n}")
                if older.exists():
                    os.replace(older, AUDIT_FILE.with_name(f"{AUDIT_FILE.name}.{n + 1}"))
            os.replace(AUDIT_FILE, AUDIT_FILE.with_name(f"{AUDIT_FILE.name}.1"))
    except Exception:
        pass  # a full or read-only volume must not stop auditing (or the call)


def audit(identity: str, role: str, tool: str, decision: str, reason: str) -> None:
    try:
        AUTH_DIR.mkdir(parents=True, exist_ok=True)
        _rotate_audit()
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
    """(identity, is_runner, claims) from the FastMCP auth context, or
    ('unknown', False, {}) when there is none. What "unknown" may do depends on the
    server: without sign-in it is the local operator; with sign-in configured it gets
    no role at all (role_for), so a resolution problem fails CLOSED there."""
    try:
        from fastmcp.server.dependencies import get_access_token
        tok = get_access_token()
        # MultiAuth (fastmcp 4) rewrites client_id to a JSON-qualified string and
        # keeps the verifier's own id in original_client_id — compare on that one,
        # or the runner token stops being the (least-privilege) runner.
        cid = (getattr(tok, "original_client_id", None)
               or getattr(tok, "client_id", None) or "unknown")
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


def key_allows(tool: str) -> bool:
    """For a per-agent key (agent_auth.py): is ``tool`` inside the key's scopes?
    True for every other kind of caller."""
    _, _, claims = _identity()
    if not (isinstance(claims, dict) and claims.get("aicortex_key")):
        return True
    import apikeys
    return (apikeys.scope_allows(claims.get("key_scopes") or [], tool)
            and not apikeys.hard_denied(tool))


def caller_label() -> str:
    """'person' or 'person/agent' for attribution — taken from the token, never
    from tool arguments (so nobody can write in someone else's name)."""
    ident, _ = effective_identity()
    _, _, claims = _identity()
    agent = claims.get("agent") if isinstance(claims, dict) else ""
    return f"{ident}/{agent}" if agent else ident


def sees(owner_person: str) -> bool:
    """May the current caller see/change an item owned by `owner_person`? With
    enforcement off (homelab) or for admins: everything. Otherwise only their own
    items and unowned ones."""
    if not enabled():
        return True
    ident, role = effective_identity()
    return role == "admin" or not owner_person or owner_person == ident


def real_caller():
    """(identity, role, is_runner) of whoever is really calling — a REST key's person
    and role, else the token's identity — ignoring any cron act-as binding. Owner
    checks and runner-only gates need this, not the job owner. (Reading only the
    token made a REST call without one look like the unauthenticated default.)"""
    rest = _REST_IDENTITY.get()
    if rest:
        return rest[0], rest[1], False
    ident, is_runner, claims = _identity()
    return ident, role_for(ident, is_runner, claims), is_runner


def binding_key(ident: str = "") -> str:
    """What a cron act-as run is bound to: the caller's identity plus a short hash of
    the credential it called with. The built-in runner and an own runner (RUNNER_TOKEN)
    are both identity "runner" — keyed by identity alone, one runner's act-as run would
    confine (or block) the other's calls. A call without a token (REST, in-process)
    keys on the identity alone."""
    ident = ident or real_caller()[0]
    if _REST_IDENTITY.get():
        return ident
    try:
        from fastmcp.server.dependencies import get_access_token
        raw = str(getattr(get_access_token(), "token", "") or "")
    except Exception:
        raw = ""
    return f"{ident}#{hashlib.sha256(raw.encode()).hexdigest()[:16]}" if raw else ident


def effective_identity():
    """(identity, role) for the CURRENT call, honoring (in order) a REST per-task
    identity, then an active cron act-as binding, else the real caller. When the
    runner is executing a job as an owner, that owner (at the owner's own privilege) is
    the effective caller for both tool-gating and data-scoping — so the job is confined
    to the owner's area, never the runner's. On a hard resolution error: ('unknown',
    '') — no role, every tool denied — when sign-in is configured; ('unknown', 'user')
    on a server without sign-in, as before."""
    try:
        rest = _REST_IDENTITY.get()
        if rest:
            return rest
    except Exception:
        pass
    try:
        ident, is_runner, claims = _identity()
    except Exception:
        return "unknown", _unresolved_role()
    try:
        import actas
        owner = actas.current(binding_key(ident))    # only the caller that started the run
        if owner:
            return owner, _actas_role(owner)
    except Exception:
        pass
    try:
        return ident, role_for(ident, is_runner, claims)
    except Exception:
        return "unknown", _unresolved_role()


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
        async def on_list_tools(self, context, call_next):
            # Show a caller only what its role may call — otherwise a model sees
            # admin tools, tries them and gets refused.
            tools = await call_next(context)
            try:
                tools = [t for t in tools if key_allows(t.name)]
                if not enabled():
                    return tools
                _, role = effective_identity()
                return [t for t in tools if decide(role, t.name)[0]]
            except Exception as exc:
                audit("unknown", "?", "tools/list", "fail-open",
                      f"list filter error, full list shown: {type(exc).__name__}: {exc}")
                return tools

        async def on_call_tool(self, context, call_next):
            tool = getattr(getattr(context, "message", None), "name", None)
            if tool and not key_allows(tool):
                # A per-agent key's scopes apply regardless of AUTH_ENFORCE.
                raise ToolError(
                    f"Denied: this agent key may not call '{tool}' (outside the key's "
                    f"scopes). An admin can issue a key with more scopes.")
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
                            args["sender"] = caller_label()
                        elif tool == "task_add":
                            args["created_by"] = caller_label()
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
