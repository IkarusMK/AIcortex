"""Per-user API keys for the native REST layer (see rest_api.py).

A key maps to a specific identity (`sub`) and, when used against the REST API, runs
through the SAME authz + per-user-areas pipeline as an OIDC session — there is no
second permission model. Least privilege by construction:

- a key NEVER defaults to admin — its role resolves from policy.json, else "user"
  (like the RUNNER_TOKEN), so a leaked key can't be a super-user by accident;
- every key carries an explicit SCOPE allow-list (DEFAULT-DENY): it reaches only the
  tools it was granted, then further narrowed by the identity's role + device areas;
- a hard DENYLIST of meta/secret tools is NEVER reachable via a key, even with scope
  "all" (secret_set/delete, apikey_*, tenancy_*) — those stay OIDC-admin-only;
- keys are hashed at rest (SHA-256 of a 256-bit random secret — high entropy, so a
  plain digest is sufficient, GitHub-style), compared in constant time; the plaintext
  is shown ONCE at creation and never stored;
- a key may carry an expiry (like the act-as capability tokens).

Storage is DATA: one JSON record per key under APIKEY_DIR, keyed by a public `keyid`
prefix so lookup is O(1) and `apikey_list` can show a stable prefix without revealing
the secret. Full CRUD: create / list / revoke(=delete).
"""
import base64
import collections
import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

APIKEY_DIR = Path(os.environ.get("APIKEY_DIR", "/data/apikeys"))
_PREFIX = "ak_"
_KEYID_LEN = 12          # hex chars identifying the record (public, not secret)
_SECRET_BYTES = 32       # 256-bit secret → a plain SHA-256 at rest is sufficient

# Tools no key may EVER reach over REST, even with scope "all": secret management,
# key management itself (a key must not mint or revoke keys), and the tenancy control
# plane. These stay OIDC-admin-only. Matched by exact name or prefix.
_HARD_DENY_EXACT = {
    "secret_set", "secret_delete",
    "apikey_create", "apikey_list", "apikey_revoke",
}
_HARD_DENY_PREFIX = ("tenancy_",)

# Friendly scope aliases → tool-name glob(s), so a key can be granted "memory"
# instead of every tool name. Callers may also pass exact names or "<prefix>_*".
_ALIASES = {
    "memory": ["memory_read", "memory_list", "memory_search", "memory_write",
               "memory_note", "memory_candidates"],
    "skills": ["skill_*"],
    "files": ["fs_*"],
    "services": ["call_service", "service_list"],
    "calendar": ["caldav_*"],
    "mail": ["mail_send", "imap_*"],
    "sessions": ["session_*"],
    "tasks": ["task_*", "inbox_*", "agent_*"],
}


def _now() -> int:
    return int(time.time())


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _keyid_path(keyid: str) -> Path:
    # keyid is hex only (we generate it); sanitise anyway so a crafted id from an
    # attacker-controlled key string can never traverse out of APIKEY_DIR.
    safe = re.sub(r"[^a-f0-9]", "", (keyid or "").lower())[:_KEYID_LEN]
    return APIKEY_DIR / f"{safe}.json"


def hard_denied(tool: str) -> bool:
    """Tools no key may ever call over REST, independent of its scope."""
    return tool in _HARD_DENY_EXACT or any(tool.startswith(p) for p in _HARD_DENY_PREFIX)


def _expand_scopes(scopes) -> list:
    """Normalize a scope spec into a list of tool-name patterns. Accepts a list or a
    comma/space string; expands friendly aliases; keeps 'all', exact names, and
    '<prefix>_*' globs. Deduped, order-stable."""
    if isinstance(scopes, str):
        items = [s for s in re.split(r"[,\s]+", scopes) if s]
    else:
        items = [str(s).strip() for s in (scopes or []) if str(s).strip()]
    out: list = []
    for it in items:
        low = it.lower()
        if low == "all":
            return ["all"]
        for pat in _ALIASES.get(low, [it]):
            if pat not in out:
                out.append(pat)
    return out


def scope_allows(scopes, tool: str) -> bool:
    """Whether a key's scope list permits `tool` (BEFORE role/tenancy gating). The
    hard denylist always wins. 'all' allows everything else; otherwise an exact-name
    or '<prefix>_*' glob match."""
    if hard_denied(tool):
        return False
    patterns = _expand_scopes(scopes)
    if "all" in patterns:
        return True
    for p in patterns:
        if p.endswith("*") and tool.startswith(p[:-1]):
            return True
        if p == tool:
            return True
    return False


def _public(rec: dict) -> dict:
    """A record without the secret hash (safe to show / return)."""
    return {k: v for k, v in rec.items() if k != "secret_sha256"}


def create(sub: str, name: str = "", scopes="", ttl_days: int = 0,
           created_by: str = "") -> tuple:
    """Mint a new API key for identity `sub`. Returns (full_key, public_record).
    The full key is shown ONCE — only its SHA-256 is persisted. Raises ValueError on
    a missing identity or empty scope (default-deny: a key must be granted something)."""
    sub = (sub or "").strip()
    if not sub:
        raise ValueError("sub (the key's identity) is required")
    patterns = _expand_scopes(scopes)
    if not patterns:
        raise ValueError("scopes is required (default-deny) — grant at least one tool "
                         "name, a '<prefix>_*' glob, an alias, or 'all'")
    keyid = os.urandom(6).hex()                      # 12 hex chars
    secret = _b64(os.urandom(_SECRET_BYTES))         # 256-bit, ~43 url-safe chars
    full = f"{_PREFIX}{keyid}_{secret}"
    expires = (_now() + int(ttl_days) * 86400) if ttl_days and int(ttl_days) > 0 else 0
    rec = {
        "keyid": keyid, "sub": sub, "name": (name or "").strip(),
        "secret_sha256": _hash(secret),
        "scopes": patterns,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "expires": expires, "disabled": False,
        "created_by": (created_by or "").strip(),
    }
    APIKEY_DIR.mkdir(parents=True, exist_ok=True)
    _keyid_path(keyid).write_text(json.dumps(rec, indent=2), encoding="utf-8")
    return full, _public(rec)


def _parse(key: str):
    """Split ``ak_<keyid>_<secret>`` → (keyid, secret) or (None, None)."""
    if not key or not key.startswith(_PREFIX):
        return None, None
    keyid, _, secret = key[len(_PREFIX):].partition("_")
    if not keyid or not secret:
        return None, None
    return keyid, secret


def verify(key: str):
    """Validate a presented key. Returns the (private) record dict on success, else
    None. Constant-time hash compare; enforces disabled + expiry. Never raises."""
    try:
        keyid, secret = _parse(key)
        if not keyid:
            return None
        p = _keyid_path(keyid)
        if not p.exists():
            return None
        rec = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(rec, dict) or rec.get("disabled"):
            return None
        exp = int(rec.get("expires", 0) or 0)
        if exp and exp < _now():
            return None
        if not hmac.compare_digest(_hash(secret), str(rec.get("secret_sha256", ""))):
            return None
        return rec
    except Exception:
        return None


def list_keys() -> list:
    """Public metadata for every stored key (never the secret)."""
    if not APIKEY_DIR.exists():
        return []
    out = []
    for p in sorted(APIKEY_DIR.glob("*.json")):
        try:
            out.append(_public(json.loads(p.read_text(encoding="utf-8"))))
        except Exception:
            continue
    return out


def revoke(keyid: str) -> bool:
    """Delete a key record (revocation = removal). Returns True if it existed."""
    keyid = re.sub(r"[^a-f0-9]", "", (keyid or "").lower())[:_KEYID_LEN]
    p = _keyid_path(keyid)
    if p.exists():
        p.unlink()
        return True
    return False


# ── Per-key rate limiting (in-memory sliding window) ────────────────────────
# The event loop is single-threaded, so there's no await between the read and the
# append below → a plain dict/deque is race-free. In-memory is fine: a restart just
# resets the window (a limiter, not an audit trail).
_RATE_PER_MIN = int(os.environ.get("API_RATE_PER_MIN", "60"))
_hits: dict = collections.defaultdict(collections.deque)


def rate_ok(keyid: str, limit: int = 0) -> tuple:
    """Sliding-window limiter keyed by `keyid`. Returns (ok, retry_after_seconds).
    `limit` <= 0 uses API_RATE_PER_MIN; a configured 0 disables limiting."""
    lim = limit if limit > 0 else _RATE_PER_MIN
    if lim <= 0:
        return True, 0
    now = time.time()
    dq = _hits[keyid]
    while dq and dq[0] <= now - 60:
        dq.popleft()
    if len(dq) >= lim:
        return False, max(int(60 - (now - dq[0])) + 1, 1)
    dq.append(now)
    return True, 0


def register(mcp):
    """Register the apikey_* admin control-plane tools (create / list / revoke)."""

    @mcp.tool
    def apikey_create(identity: str, name: str = "", scopes: str = "",
                      ttl_days: int = 0) -> str:
        """Mint a REST API key for `identity` (a Pocket ID `sub` / client_id) so a
        non-MCP client (n8n, LangChain, a script) can call the native REST layer
        (POST /api/v1/tools/<name>). The key runs through the SAME authz + per-user
        areas as an OIDC session — never as admin by default.

        `scopes` = a comma-separated allow-list (DEFAULT-DENY — required): exact tool
        names, '<prefix>_*' globs, friendly aliases (memory, skills, files, calendar,
        mail, tasks, sessions), or 'all'. Secret/key/tenancy tools are NEVER reachable
        via a key. `ttl_days` > 0 sets an expiry.

        The full key is shown ONCE in the result — store it now; only its hash is
        kept. Revoke with apikey_revoke(keyid). (Admin-only.)"""
        try:
            creator = ""
            try:
                import authz
                creator, _ = authz.effective_identity()
            except Exception:
                creator = ""
            full, pub = create(identity, name=name, scopes=scopes,
                               ttl_days=ttl_days, created_by=creator or "")
        except ValueError as exc:
            return f"Refused: {exc}"
        except Exception as exc:
            return f"Could not create API key: {exc}"
        exp = ("never" if not pub["expires"]
               else datetime.fromtimestamp(pub["expires"], timezone.utc)
               .isoformat(timespec="seconds"))
        return (
            f"API key created for '{pub['sub']}' "
            f"(keyid {pub['keyid']}, scopes: {', '.join(pub['scopes'])}, expires: {exp}).\n\n"
            f"⚠ SHOWN ONCE — copy it now, it is NOT stored and cannot be shown again:\n\n"
            f"    {full}\n\n"
            f"Use it as:  Authorization: Bearer {full}\n"
            f"against POST /api/v1/tools/<name>. Revoke with apikey_revoke('{pub['keyid']}')."
        )

    @mcp.tool
    def apikey_list() -> str:
        """List API keys — keyid prefix, identity, scopes, expiry, status (NEVER the
        secret). (Admin-only.)"""
        keys = list_keys()
        if not keys:
            return "No API keys yet. Mint one with apikey_create(identity, scopes=...)."
        lines = []
        for k in sorted(keys, key=lambda r: r.get("created", "")):
            exp = ("never" if not k.get("expires")
                   else datetime.fromtimestamp(k["expires"], timezone.utc)
                   .isoformat(timespec="seconds"))
            state = "disabled" if k.get("disabled") else (
                "EXPIRED" if k.get("expires") and k["expires"] < _now() else "active")
            nm = f" '{k['name']}'" if k.get("name") else ""
            lines.append(f"- ak_{k['keyid']}…{nm} — {k.get('sub', '?')} — "
                         f"scopes: {', '.join(k.get('scopes', [])) or 'none'} — "
                         f"{state}, expires: {exp}")
        return "\n".join(lines)

    @mcp.tool
    def apikey_revoke(keyid: str) -> str:
        """Revoke (delete) an API key by its keyid (the ak_<keyid> prefix from
        apikey_list). Takes effect immediately. (Admin-only.)"""
        kid = (keyid or "").strip()
        kid = kid[len(_PREFIX):] if kid.startswith(_PREFIX) else kid
        kid = kid.split("_", 1)[0]           # tolerate a full key being pasted
        if revoke(kid):
            return f"Revoked API key '{kid}' — it can no longer authenticate."
        return f"No API key with keyid '{kid}'."
