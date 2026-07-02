"""Multi-tenant data isolation — confine non-admin callers to their own area.

This sits ON TOP of the authorization layer (authz.py). Authz decides *which
tools* a caller may use; tenancy decides *which data* a caller sees within those
tools — so two people sharing one AICortex don't read or overwrite each other's
memory and secrets.

OPT-IN (``TENANCY_ISOLATE=1``; default OFF) — this is the "community homelab vs.
enterprise" switch:
- OFF (default): no confinement. Every authenticated caller uses data exactly as
  before (one trusted operator / a small homelab). Nothing changes.
- ON: each non-admin identity is confined to its OWN memory scope (``users/<sub>``)
  and OWN vault namespace, while ``admin`` keeps full access. Per-user widening or
  narrowing lives as DATA in ``data/auth/policy.json`` under ``"users"`` — no code,
  no redeploy.

DESIGN SAFETY (mirrors authz so it can't lock a hands-off operator out):
- Fail-open: any error resolving identity/policy/area → NO confinement (return the
  caller's requested scope unchanged), so a resolution glitch degrades to "full
  access", never to "locked out".
- Admins are never confined.
- The identity is the forwarded upstream ``sub`` (PocketIDProxy) when present, so
  isolation is per-PERSON, not per-OAuth-client.

policy.json shape (all optional):
    {
      "users": {
        "<sub-or-client-id>": {
          "memory":   "own"|"all",           # private data → default "own" (confined)
          "vault":    "own"|"all",           # private data → default "own" (confined)
          "services": "all"|"none"|[names…], # shared capability → default "all"
          "skills":   "all"|"none"|[names…], # shared capability → default "all"
          "note": "…"
        }
      }
    }

TWO DEFAULT STANCES, on purpose:
- PRIVATE data (memory, vault): default "own" — a non-admin is confined unless an
  admin widens them. Two people never see each other's notes/secrets by accident.
- SHARED capabilities (services, skills): default "all" — everyone can use every
  registered integration/skill unless an admin NARROWS them to an allow-list of
  names and/or categories ("none" locks all out). Capabilities are meant to be
  shared; you opt into restricting them.

Cron act-as: a scheduled job may carry an ``owner`` (a user's identity). Only an
admin may schedule a job to run AS another user; a non-admin can only schedule as
themselves (no privilege escalation). The NAS runner then executes that job in the
owner's area. See ``act_as_owner``.
"""
import hashlib
import json
import os
import re
from pathlib import Path

AUTH_DIR = Path(os.environ.get("AUTH_STORE_DIR", "/data/auth"))
POLICY_FILE = AUTH_DIR / "policy.json"

# Memory tools that take a ``scope`` argument we can rewrite to confine a caller.
# (memory_note / memory_candidates / memory_reject don't take a user scope — note
# stages into the reserved candidates scope, which is fine.)
MEMORY_SCOPED_TOOLS = {
    "memory_write", "memory_read", "memory_list", "memory_search",
    "memory_delete", "memory_promote",
}


def isolation_enabled() -> bool:
    """Master switch. OFF by default so single-operator setups are untouched."""
    return os.environ.get("TENANCY_ISOLATE", "0").strip().lower() in (
        "1", "true", "yes", "on")


def _policy() -> dict:
    try:
        return json.loads(POLICY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _safe(identity: str) -> str:
    """Filesystem-safe, INJECTIVE identity segment: a readable slug plus a short
    hash of the RAW identity. The slug alone is not injective (``bob@x.com`` and
    ``bob.x.com`` both sanitise to ``bob_x_com``) — collapsing two people onto one
    namespace would break vault/memory isolation — so the hash of the untouched
    identity disambiguates. Output stays within [A-Za-z0-9_-], matching
    memory._scope_dir's guard so the forced scope can't traverse out."""
    raw = identity or ""
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", raw).strip("_") or "user"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"{slug}-{digest}"


def area_for(identity: str, role: str) -> dict:
    """Resolve a caller's data area.

    Returns ``{"memory_scope", "vault_ns", "confined"}`` where ``memory_scope`` /
    ``vault_ns`` are either a concrete ``users/<id>`` string (confine here) or
    ``None`` (no confinement / full access).
    """
    unrestricted = {"memory_scope": None, "vault_ns": None, "confined": False}
    if not isolation_enabled():
        return unrestricted
    # Admins are never confined; an unresolved identity fails open (no confinement)
    # so a resolution glitch can't strand a caller in a bogus "unknown" bucket.
    if role == "admin" or not identity or identity == "unknown":
        return unrestricted
    users = _policy().get("users") or {}
    cfg = users.get(identity, {}) if isinstance(users, dict) else {}
    mem = str(cfg.get("memory", "own")).strip().lower()
    vault = str(cfg.get("vault", "own")).strip().lower()
    own = f"users/{_safe(identity)}"
    return {
        "memory_scope": None if mem == "all" else own,
        "vault_ns": None if vault == "all" else own,
        "confined": True,
    }


def confine_memory_scope(identity: str, role: str, requested_scope: str) -> str:
    """Return the scope a memory_* call must actually use. When the caller is
    confined, their OWN scope is forced regardless of what they requested (so they
    can neither read ``shared`` nor reach another user's scope). Fail-open: on any
    doubt, the requested scope is returned unchanged."""
    try:
        target = area_for(identity, role)["memory_scope"]
        return target if target else (requested_scope or "shared")
    except Exception:
        return requested_scope or "shared"


def vault_namespace(identity: str, role: str) -> str:
    """The vault key-prefix a caller writes under, or "" for the flat (shared)
    namespace. Used by secrets_store for per-user vault (P2)."""
    try:
        ns = area_for(identity, role)["vault_ns"]
        return ns or ""
    except Exception:
        return ""


def current_identity():
    """Resolve the CURRENT caller from the FastMCP auth context as
    ``(identity, role)`` — identity = the forwarded upstream ``sub`` (per-person)
    else the OAuth client_id. Fail-open to ``(None, None)`` so a resolution glitch
    degrades to 'no namespace' (shared/flat), never to a wrong owner. Used by
    data tools (e.g. secrets_store) that must namespace by who is calling."""
    try:
        from fastmcp.server.dependencies import get_access_token
        import authz  # lazy: avoids an import cycle at module load
        tok = get_access_token()
        cid = getattr(tok, "client_id", None) or "unknown"
        claims = getattr(tok, "claims", None) or {}
        person = authz._claim_value(claims, "sub") or cid
        role = authz.role_for(person, cid == "runner", claims)
        return person, role
    except Exception:
        return None, None


# ── Per-user capability areas: which SERVICES / SKILLS a caller may use ──────
# Unlike memory/vault (private data, confined by default), services and skills are
# SHARED capabilities: default "all", narrowed only by an explicit admin allow-list.
_ACCESS_ALL = "all"
_ACCESS_NONE = "none"


def _access_spec(identity: str, role: str, key: str):
    """Resolve a caller's access for a capability class (key = 'services'|'skills').
    Returns the string 'all' (unrestricted) or a frozenset of lowercased allow-list
    entries (names and/or categories; empty frozenset = 'none' = nothing allowed).

    Fail-open to 'all': isolation off, an admin, an unresolved identity, a missing
    field, or any error → full access (capabilities are shared by default and a
    glitch must never strand a caller)."""
    try:
        if not isolation_enabled():
            return _ACCESS_ALL
        if role == "admin" or not identity or identity == "unknown":
            return _ACCESS_ALL
        cfg = (_policy().get("users") or {}).get(identity, {})
        spec = cfg.get(key, _ACCESS_ALL)
        if isinstance(spec, str):
            s = spec.strip().lower()
            if s == _ACCESS_NONE:
                return frozenset()
            if s in ("", _ACCESS_ALL):
                return _ACCESS_ALL
            # a comma/space-separated string is also accepted as an allow-list
            return frozenset(x for x in re.split(r"[,\s]+", s) if x)
        if isinstance(spec, (list, tuple, set)):
            return frozenset(str(x).strip().lower() for x in spec if str(x).strip())
        return _ACCESS_ALL
    except Exception:
        return _ACCESS_ALL


def _capability_allowed(identity, role, key, name, category="") -> bool:
    spec = _access_spec(identity, role, key)
    if spec == _ACCESS_ALL:
        return True
    n = (name or "").strip().lower()
    c = (category or "").strip().lower()
    return (n in spec) or (bool(c) and c in spec)


def service_allowed(identity: str, role: str, name: str, category: str = "") -> bool:
    """Whether `identity` may see/call service `name` (matched by name OR category).
    Fail-open (see _access_spec)."""
    try:
        return _capability_allowed(identity, role, "services", name, category)
    except Exception:
        return True


def skill_allowed(identity: str, role: str, name: str, category: str = "") -> bool:
    """Whether `identity` may see/load skill `name` (matched by name OR category).
    Fail-open (see _access_spec)."""
    try:
        return _capability_allowed(identity, role, "skills", name, category)
    except Exception:
        return True


def caller_service_allowed(name: str, category: str = "") -> bool:
    """service_allowed for the CURRENT caller (resolved from the auth context).
    For use inside service tools, mirroring how secrets_store filters itself.
    Fail-open: an unresolved caller → allowed."""
    try:
        ident, role = current_identity()
        if ident is None:
            return True
        return service_allowed(ident, role, name, category)
    except Exception:
        return True


def caller_skill_allowed(name: str, category: str = "") -> bool:
    """skill_allowed for the CURRENT caller. Fail-open: unresolved → allowed."""
    try:
        ident, role = current_identity()
        if ident is None:
            return True
        return skill_allowed(ident, role, name, category)
    except Exception:
        return True


def act_as_owner(caller_identity: str, caller_role: str, requested_owner: str):
    """Validate the ``owner`` (act-as identity) for a scheduled job. Returns
    ``(ok, value_or_reason)``:

    - requested empty → ``(True, "")`` — no act-as; the job runs as the runner's
      default identity (unchanged behaviour).
    - caller is admin → ``(True, requested)`` — an admin may schedule on anyone's
      behalf.
    - requested == caller → ``(True, requested)`` — scheduling as yourself is fine.
    - otherwise → ``(False, reason)`` — a non-admin may NOT schedule a job that runs
      AS another (more-privileged) user. This is the privilege-escalation guard."""
    req = (requested_owner or "").strip()
    if not req:
        return True, ""
    if caller_role == "admin":
        return True, req
    if caller_identity and req == caller_identity:
        return True, req
    return False, ("only an admin may schedule a job to run AS another user "
                   "(act-as); you can schedule jobs only as yourself")


# ── Control plane: admin tools to manage per-user areas (policy.json) ───────
# "The assistant is the dashboard": an admin sets who-may-what from any device,
# stored as DATA in policy.json — no separate UI, no redeploy.

_AREA_VALUES = ("own", "all")
_POLICY_BAK = AUTH_DIR / "policy.json.bak"


def _write_policy(obj: dict) -> None:
    """Atomic write of policy.json with a one-generation backup (so a crash or a
    bad edit can't leave a truncated policy that would silently change everyone's
    access)."""
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    tmp = AUTH_DIR / "policy.json.tmp"
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    if POLICY_FILE.exists():
        try:
            import shutil
            shutil.copy2(POLICY_FILE, _POLICY_BAK)
        except Exception:
            pass
    os.replace(tmp, POLICY_FILE)


def _fmt_access(spec) -> str:
    """Render a services/skills access spec compactly for the describe output."""
    if isinstance(spec, (list, tuple, set)):
        items = [str(x).strip() for x in spec if str(x).strip()]
        return ",".join(items) if items else "none"
    s = str(spec if spec is not None else "all").strip()
    return s or "all"


def _describe_area(identity: str) -> str:
    """Human line describing a user's stored config + how it resolves for the
    'user' role (the common non-admin case)."""
    cfg = (_policy().get("users") or {}).get(identity, {})
    mem = str(cfg.get("memory", "own"))
    vault = str(cfg.get("vault", "own"))
    svcs = _fmt_access(cfg.get("services", "all"))
    skls = _fmt_access(cfg.get("skills", "all"))
    note = cfg.get("note", "")
    resolved = area_for(identity, "user")
    scope = resolved["memory_scope"] or "shared + all (unconfined)"
    extra = f" · note: {note}" if note else ""
    return (f"- {identity}: memory={mem}, vault={vault}, services={svcs}, "
            f"skills={skls} → memory scope: {scope}{extra}")


def register(mcp):
    """Register the tenancy_* admin tools (per-user data areas)."""

    @mcp.tool
    def tenancy_status() -> str:
        """Show whether per-user data isolation is active and how many users are
        configured. Isolation is opt-in via the TENANCY_ISOLATE env var; per-user
        areas live in data/auth/policy.json under 'users'. (Admin-only.)"""
        on = isolation_enabled()
        users = _policy().get("users") or {}
        lines = [
            f"Per-user isolation: {'ON' if on else 'OFF'} "
            f"(TENANCY_ISOLATE={'1' if on else '0'}).",
            f"Configured users: {len(users)}.",
        ]
        if not on:
            lines.append("Note: isolation is OFF — every caller shares one brain "
                         "(homelab mode). Set TENANCY_ISOLATE=1 to enforce areas.")
        if users:
            lines.append("")
            lines += [_describe_area(i) for i in sorted(users)]
        return "\n".join(lines)

    @mcp.tool
    def tenancy_set(identity: str, memory: str = "", vault: str = "",
                    services: str = "", skills: str = "", note: str = "") -> str:
        """Set a user's data area in policy.json (create or update). `identity` =
        the person's Pocket ID `sub` (or a client_id).

        - `memory`/`vault` = 'own' (confined to their own scope — the default for
          non-admins) or 'all' (full access, like an admin).
        - `services`/`skills` = 'all' (every registered one — the default), 'none'
          (locked out), or a comma-separated allow-list of names and/or categories
          (e.g. "github, Documents"). These are SHARED capabilities, so default 'all';
          set an allow-list to narrow a user.

        Leave a field empty to KEEP its current value; a brand-new user with nothing
        set defaults to memory='own' (services/skills default to 'all' implicitly).
        `note` is an optional label. (Admin-only.)"""
        identity = (identity or "").strip()
        if not identity:
            return "Refused: identity is required (the user's Pocket ID sub or client_id)."
        for label, val in (("memory", memory), ("vault", vault)):
            if val and val.strip().lower() not in _AREA_VALUES:
                return (f"Refused: {label} must be one of {_AREA_VALUES} "
                        f"(or empty to keep current). Got '{val}'.")
        pol = _policy()
        users = pol.get("users")
        if not isinstance(users, dict):
            users = {}
        entry = dict(users.get(identity, {}))
        if memory.strip():
            entry["memory"] = memory.strip().lower()
        if vault.strip():
            entry["vault"] = vault.strip().lower()
        # services/skills: store 'all'/'none' verbatim, else a normalized list so
        # the catalog + _access_spec agree on the shape.
        for label, val in (("services", services), ("skills", skills)):
            v = val.strip()
            if not v:
                continue
            low = v.lower()
            if low in (_ACCESS_ALL, _ACCESS_NONE):
                entry[label] = low
            else:
                entry[label] = [x for x in re.split(r"[,\s]+", low) if x]
        if note.strip():
            entry["note"] = note.strip()
        entry.setdefault("memory", "own")  # meaningful default for a new user
        users[identity] = entry
        pol["users"] = users
        _write_policy(pol)
        warn = "" if isolation_enabled() else (
            "\n⚠ TENANCY_ISOLATE is OFF, so this area is stored but NOT enforced "
            "yet — set TENANCY_ISOLATE=1 in .env and restart to activate.")
        return f"Set area for '{identity}'.\n{_describe_area(identity)}{warn}"

    @mcp.tool
    def tenancy_show(identity: str) -> str:
        """Show one user's stored area config and how it resolves. (Admin-only.)"""
        identity = (identity or "").strip()
        users = _policy().get("users") or {}
        if identity not in users:
            return (f"No per-user area for '{identity}'. They fall back to the "
                    f"default (non-admins are confined to their own scope when "
                    f"isolation is on).")
        return _describe_area(identity)

    @mcp.tool
    def tenancy_list() -> str:
        """List all users with a configured area + their resolved scope. (Admin-only.)"""
        users = _policy().get("users") or {}
        if not users:
            return ("No per-user areas configured. Use tenancy_set(identity, "
                    "memory='own'|'all') to add one.")
        return "\n".join(_describe_area(i) for i in sorted(users))

    @mcp.tool
    def tenancy_unset(identity: str) -> str:
        """Remove a user's per-user area override (they revert to the default).
        (Admin-only.)"""
        identity = (identity or "").strip()
        pol = _policy()
        users = pol.get("users")
        if not isinstance(users, dict) or identity not in users:
            return f"No per-user area for '{identity}' — nothing to remove."
        del users[identity]
        pol["users"] = users
        _write_policy(pol)
        return (f"Removed area for '{identity}'. They now follow the default "
                f"(confined to their own scope when isolation is on).")
