"""Encrypted secret vault.

Lets secrets be created via the connector without hand-editing the server's
``.env`` (e.g. from mobile). Values are encrypted at rest (Fernet, using
``STORAGE_ENCRYPTION_KEY``) and are **never returned to the model**:
``secret_set`` writes, ``secret_list`` shows only names, and ``call_service``
reads them server-side by name. Plain ``.env`` variables still work and take
precedence over the vault.

For the admin WebUI the vault also keeps non-secret context in ``meta.json``
(description, who changed a secret and when), computes which registered
services/devices use each secret, and reports its state (empty / encrypted /
plaintext / unreadable). Key rotation: set the new key as STORAGE_ENCRYPTION_KEY
and the previous one as STORAGE_ENCRYPTION_KEY_OLD, re-encrypt (vault_rekey),
then drop the old one. A plaintext vault is encrypted the same way.
"""
import json
import os
import shutil
from pathlib import Path

import store

VAULT_DIR = Path(os.environ.get("VAULT_DIR", "/data/vault"))
VAULT_FILE = VAULT_DIR / "secrets.enc"
VAULT_BAK = VAULT_DIR / "secrets.enc.bak"
VAULT_META = VAULT_DIR / "meta.json"     # non-secret: description, updated, updated_by
# Config fields that hold the NAME of a secret, across all registries.
_REF_FIELDS = ("token_env", "password_env", "key_env", "secret_env", "hmac_secret_env")


class VaultUnreadable(Exception):
    """The vault file exists but could not be decrypted/parsed. Distinct from an
    empty/missing vault — we must NEVER treat this as empty and overwrite it,
    or a wrong STORAGE_ENCRYPTION_KEY / corruption would destroy all secrets."""


def _fernet():
    """The vault cipher, or None without a key. With STORAGE_ENCRYPTION_KEY_OLD set
    (rotation) it decrypts with either key and encrypts with the new one."""
    key = os.environ.get("STORAGE_ENCRYPTION_KEY")
    if not key:
        return None
    from cryptography.fernet import Fernet, MultiFernet

    keys = [Fernet(key.encode())]
    old = os.environ.get("STORAGE_ENCRYPTION_KEY_OLD")
    if old:
        keys.append(Fernet(old.encode()))
    return MultiFernet(keys) if len(keys) > 1 else keys[0]


def _decode(raw: bytes):
    """(state, dict) for the vault bytes: 'encrypted' or 'plaintext'; raises on garbage.
    A plaintext JSON vault (written with ALLOW_PLAINTEXT_VAULT) stays readable after
    a key is set, so it can be re-encrypted instead of being locked out."""
    f = _fernet()
    if f is not None:
        try:
            obj = json.loads(f.decrypt(raw))
            if isinstance(obj, dict):
                return "encrypted", obj
        except Exception:
            pass
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError("vault content is not a JSON object")
    return "plaintext", obj


def _read_all(strict: bool = False) -> dict:
    """Read the vault. Missing file → {} (genuinely empty). Existing-but-unreadable
    file → raise VaultUnreadable when strict (used before any write), else {} for
    best-effort reads (get_secret). This fail-closed split is what prevents a
    silent wipe on the next write when the key is wrong or the file is corrupt."""
    if not VAULT_FILE.exists():
        return {}
    try:
        return _decode(VAULT_FILE.read_bytes())[1]
    except Exception as exc:
        if strict:
            raise VaultUnreadable(str(exc)) from exc
        return {}


def _write_all(d: dict) -> None:
    """Atomic write with a one-generation backup: write a temp file, snapshot the
    current vault to .bak, then os.replace (atomic) so a crash mid-write can't
    leave a truncated vault."""
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(d).encode()
    f = _fernet()
    out = f.encrypt(blob) if f else blob
    if VAULT_FILE.exists():
        try:
            shutil.copy2(VAULT_FILE, VAULT_BAK)
        except Exception:
            pass
    store.write_bytes_atomic(VAULT_FILE, out)


# Every read-modify-write of the vault runs under this lock (parallel secret_set
# calls used to lose each other's entries).
_serialized = store.serialized(lambda: VAULT_FILE)


def vault_status() -> dict:
    """{"state": empty|encrypted|plaintext|unreadable, "key": bool, "rotating": bool}."""
    info = {"key": bool(os.environ.get("STORAGE_ENCRYPTION_KEY")),
            "rotating": bool(os.environ.get("STORAGE_ENCRYPTION_KEY_OLD"))}
    if not VAULT_FILE.exists():
        return {"state": "empty", **info}
    try:
        return {"state": _decode(VAULT_FILE.read_bytes())[0], **info}
    except Exception:
        return {"state": "unreadable", **info}


@_serialized
def vault_rekey() -> str:
    """Re-encrypt the whole vault with the current STORAGE_ENCRYPTION_KEY (after a
    key rotation, or to encrypt a plaintext vault)."""
    if _fernet() is None:
        return "Refused: set STORAGE_ENCRYPTION_KEY first."
    try:
        data = _read_all(strict=True)
    except VaultUnreadable as exc:
        return (f"Refused: the vault can't be read with the configured keys ({exc}). "
                f"Set the previous key as STORAGE_ENCRYPTION_KEY_OLD and retry.")
    _write_all(data)
    # The backup still holds the previous generation — plaintext, or readable with the
    # key being retired. Replace it with the freshly encrypted vault.
    store.write_bytes_atomic(VAULT_BAK, VAULT_FILE.read_bytes())
    return f"Re-encrypted {len(data)} secret(s) with the current key."


def _meta() -> dict:
    try:
        return store.read_json(VAULT_META, {})
    except store.StoreCorrupt:
        return {}


def _registries():
    import bootstrap
    return [("service", bootstrap.SERVICES_DIR), ("mqtt", bootstrap.MQTT_DIR),
            ("ftp", bootstrap.FTP_DIR), ("webdav", bootstrap.WEBDAV_DIR),
            ("caldav", bootstrap.CALDAV_DIR), ("ssh", bootstrap.SSH_DIR),
            ("mail", bootstrap.MAIL_DIR), ("imap", bootstrap.IMAP_DIR),
            ("mcp", bootstrap.MCP_DIR), ("scan", bootstrap.SCAN_DIR),
            ("webhook", bootstrap.WEBHOOK_DIR)]


def secret_references() -> dict:
    """secret name -> ["service home-assistant", ...] from every registered config."""
    refs: dict = {}
    for kind, d in _registries():
        for p in sorted(Path(d).glob("*.json")):
            try:
                cfg = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for field in _REF_FIELDS:
                name = str(cfg.get(field) or "").strip() if isinstance(cfg, dict) else ""
                if name:
                    refs.setdefault(name, []).append(f"{kind} {p.stem}")
    return refs


def missing_references() -> list:
    """Secret names a config refers to that neither the vault nor the environment has."""
    stored = {k.rsplit("/", 1)[-1] for k in _read_all()}
    return sorted(n for n in secret_references()
                  if n not in stored and not os.environ.get(n))


def _owner_prefix(owner: str) -> str:
    """Storage prefix for a user's private secrets — mirrors
    tenancy.vault_namespace so set/get/list agree on the layout."""
    import tenancy
    return f"users/{tenancy._safe(owner)}"


# The server's OWN settings are never handed to an integration as a "secret".
# Every *_env field (service token_env, webhook secret_env, …) is resolved here, and
# some of those tools send the value to a URL the caller chooses — so without this
# a non-admin could point one at STORAGE_ENCRYPTION_KEY and receive it as a header.
_INTERNAL_ENV = frozenset({
    "STORAGE_ENCRYPTION_KEY", "JWT_SIGNING_KEY", "RUNNER_TOKEN", "RUNNER_ROLE",
    "BASE_URL", "DATA_ROOT", "INTERNAL_ALLOW_CIDRS", "ALLOW_INSECURE",
    "ALLOW_PLAINTEXT_VAULT", "LEARN_AUTOCAPTURE", "SESSION_RETENTION_DAYS",
    "MAIL_ALLOWED_RECIPIENTS", "SSH_STRICT_HOST_KEYS", "CATALOG_CACHE_FILE",
    "AGENT_IDLE_SECS", "AGENT_ONLINE_SECS",
    "API_ENABLED", "API_MAX_BODY_BYTES", "API_RATE_PER_MIN",
    "UI_ENABLED", "UI_MAX_BODY_BYTES", "UI_SESSION_TTL",
    "MCP_ALLOWED_HOSTS", "MCP_ALLOWED_ORIGINS", "MCP_HOST", "MCP_PORT",
    "FS_MAX_WRITE_BYTES", "FS_VIEW_CEILING_BYTES", "FS_WORKSPACE_QUOTA_BYTES",
    "IMAP_MAX_ATTACH_BYTES", "IMAP_MAX_BODY_BYTES", "MAIL_MAX_ATTACH_BYTES",
    "PRINT_MAX_BYTES", "WEBHOOK_MAX_BODY_BYTES", "FTP_CURL_MAX_TIME",
    # generic process environment
    "PATH", "HOME", "HOSTNAME", "PWD", "USER", "LANG", "TZ", "PUID", "PGID",
})
_INTERNAL_PREFIXES = ("OIDC_", "FASTMCP_", "AUTH_", "PYTHON", "STORAGE_ENCRYPTION_")
_INTERNAL_SUFFIXES = ("_DIR",)


def is_internal_env(name: str) -> bool:
    """True for names that belong to the server itself, never to an integration."""
    return (name in _INTERNAL_ENV or name.startswith(_INTERNAL_PREFIXES)
            or name.endswith(_INTERNAL_SUFFIXES))


def get_secret(name: str):
    """Server-side lookup: environment first, then the encrypted vault.
    NOT exposed as a tool — secret values never go back to the model.
    The server's own settings (see ``is_internal_env``) are never resolved.

    Per-user vault (P2): when the call happens for a CONFINED caller, their OWN
    secret (``users/<sub>/<name>``) is preferred over the shared/flat one — so an
    admin can hand a single user their own token for a shared service. Fail-open:
    if the caller can't be resolved, only env + the flat secret are used (the
    original behaviour). A name with "/" (a storage path such as another person's
    ``users/<ns>/<name>``) is never resolved."""
    if not name or "/" in name or is_internal_env(name):
        return None
    env = os.environ.get(name)
    if env:
        return env
    data = _read_all()
    ns = _caller_namespace()
    if ns:
        personal = data.get(f"{ns}/{name}")
        if personal is not None:
            return personal
    return data.get(name)


def _caller_namespace() -> str:
    try:
        import tenancy
        ident, role = tenancy.current_identity()
        return tenancy.vault_namespace(ident, role) if ident else ""
    except Exception:
        return ""


def get_own_secret(name: str):
    """Only the current caller's PERSONAL secret `name` — never an env value, never a
    shared one. For tools that send a secret to a destination the caller chooses
    (webhook_send): a user may send their own token, not everyone's."""
    if not name or "/" in name:
        return None
    ns = _caller_namespace()
    return _read_all().get(f"{ns}/{name}") if ns else None


# ── Core vault operations ────────────────────────────────────────────────────
# Module-level so BOTH front doors (the MCP tools below and the admin WebUI)
# share one implementation — same refusal logic, same guards, no drift.

@_serialized
def vault_set(name: str, value: str, owner: str = "", description: str = "",
              by: str = "") -> str:
    """Store a secret (shared, or in `owner`'s private namespace). Returns a
    user-facing message; the value is never echoed back."""
    name = (name or "").strip()
    if not name or "/" in name:
        return "Refused: secret name is required and may not contain '/'."
    if _fernet() is None and os.environ.get("ALLOW_PLAINTEXT_VAULT") != "1":
        return ("Refusing to store: STORAGE_ENCRYPTION_KEY is not set, so the vault "
                "would be PLAINTEXT despite the .enc name. Generate a key with "
                "`python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\"`, set it as "
                "STORAGE_ENCRYPTION_KEY and restart — or set ALLOW_PLAINTEXT_VAULT=1 "
                "to override (NOT recommended).")
    try:
        d = _read_all(strict=True)
    except VaultUnreadable as exc:
        return ("Refusing to write: the existing vault exists but could NOT be "
                f"decrypted/parsed ({exc}). This usually means STORAGE_ENCRYPTION_KEY "
                "changed or the file is corrupt. Writing now would DESTROY the stored "
                "secrets. Fix the key (or restore data/vault/secrets.enc from "
                "secrets.enc.bak), then retry — nothing was changed.")
    owner = (owner or "").strip()
    key = f"{_owner_prefix(owner)}/{name}" if owner else name
    d[key] = value
    _write_all(d)
    from datetime import datetime, timezone
    meta = _meta()
    old = meta.get(key, {})
    meta[key] = {"description": (description or "").strip() or old.get("description", ""),
                 "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "updated_by": (by or "").strip()}
    store.write_json(VAULT_META, meta)
    where = f"in {owner}'s private vault" if owner else "as a shared secret"
    return f"Stored '{name}' {where} (encrypted on the NAS). It will not be shown again."


@_serialized
def vault_delete(name: str, owner: str = "") -> str:
    """Delete a secret by name (shared, or from `owner`'s private namespace)."""
    name = (name or "").strip()
    try:
        d = _read_all(strict=True)
    except VaultUnreadable as exc:
        return ("Refusing to modify: the vault could not be decrypted/parsed "
                f"({exc}) — changing it now would destroy the other secrets. Fix "
                "STORAGE_ENCRYPTION_KEY (or restore secrets.enc.bak) and retry.")
    owner = (owner or "").strip()
    key = f"{_owner_prefix(owner)}/{name}" if owner else name
    if owner and key not in d and f"users/{owner}/{name}" in d:
        # `owner` is the stored namespace segment as the listings show it
        # (WebUI, secret_list) rather than the raw identity — use it as is.
        key = f"users/{owner}/{name}"
    if key in d:
        del d[key]
        _write_all(d)
        meta = _meta()
        if meta.pop(key, None) is not None:
            store.write_json(VAULT_META, meta)
        where = f" from {owner}'s vault" if owner else ""
        return f"Deleted secret '{name}'{where}."
    return f"No secret named '{name}'{(' for ' + owner) if owner else ''}."


def vault_entries() -> list:
    """NAMES ONLY, structured, unconfined — for the admin WebUI (which enforces
    the admin role itself). Each entry: {"name", "owner"} with owner="" = shared.
    Values never leave this module."""
    meta, refs = _meta(), secret_references()
    out = []
    for key in sorted(_read_all().keys()):
        name, owner = key, ""
        if key.startswith("users/"):
            parts = key.split("/", 2)
            if len(parts) == 3:
                owner, name = parts[1], parts[2]
        m = meta.get(key, {})
        out.append({"name": name, "owner": owner,
                    "owner_label": owner.rsplit("-", 1)[0] if owner else "",
                    "description": m.get("description", ""), "updated": m.get("updated", ""),
                    "updated_by": m.get("updated_by", ""), "used_by": refs.get(name, []),
                    "env_override": bool(os.environ.get(name)) and not is_internal_env(name)})
    return out


def _fmt_key(key: str, own_ns: str) -> str:
    """Render a stored key as a name line (never a value), tagging ownership."""
    if key.startswith("users/"):
        parts = key.split("/", 2)
        if len(parts) == 3:
            owner, bare = parts[1], parts[2]
            if own_ns and key.startswith(own_ns + "/"):
                return f"- {bare}  (yours)"
            return f"- {bare}  (user: {owner})"
    return f"- {key}  (shared)"


def register(mcp):
    @mcp.tool
    def secret_set(name: str, value: str, owner: str = "", description: str = "") -> str:
        """Store a secret on the NAS, encrypted at rest. Reference it by `name`
        as a service's token_env. The value is never shown back. ADMIN-ONLY.
        `owner` (optional) = a user's Pocket ID `sub`: the secret is then stored in
        THAT user's private vault namespace (only their own service calls resolve
        it). Leave empty for a shared secret. Users can't set secrets themselves —
        an admin grants vault access this way. `description` = what it is for
        (shown in the WebUI, never the value)."""
        try:
            import authz
            by = authz.caller_label()
        except Exception:
            by = ""
        return vault_set(name, value, owner, description=description, by=by)

    @mcp.tool
    def secret_list() -> str:
        """List the NAMES of stored secrets (never the values). A confined user
        sees only the shared secrets plus the ones in their OWN namespace; an admin
        sees all (tagged by owner)."""
        keys = sorted(_read_all().keys())
        own_ns = ""
        confine = False
        try:
            import tenancy
            ident, role = tenancy.current_identity()
            own_ns = tenancy.vault_namespace(ident, role) if ident else ""
            # Confine every non-admin while isolation is on. Fail-safe: an
            # UNRESOLVED caller (role None) is treated as non-admin, so a glitch
            # can't leak the full set of other users' secret names — it just shows
            # the shared ones. When isolation is OFF (homelab), one shared brain →
            # no confinement, unchanged behaviour.
            confine = tenancy.isolation_enabled() and role != "admin"
        except Exception:
            own_ns, confine = "", True    # fail safe: shared names only
        if confine:  # shared + own only (own unknown → shared only)
            keys = [k for k in keys
                    if not k.startswith("users/")
                    or (own_ns and k.startswith(own_ns + "/"))]
        if not keys:
            return "No secrets stored yet."
        return "\n".join(_fmt_key(k, own_ns) for k in keys)

    @mcp.tool
    def secret_delete(name: str, owner: str = "") -> str:
        """Delete a stored secret by name. ADMIN-ONLY. `owner` = a user's Pocket ID
        `sub` to delete from that user's private vault; empty for a shared secret."""
        return vault_delete(name, owner)
