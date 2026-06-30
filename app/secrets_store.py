"""Encrypted secret vault.

Lets secrets be created via the connector without hand-editing the server's
``.env`` (e.g. from mobile). Values are encrypted at rest (Fernet, using
``STORAGE_ENCRYPTION_KEY``) and are **never returned to the model**:
``secret_set`` writes, ``secret_list`` shows only names, and ``call_service``
reads them server-side by name. Plain ``.env`` variables still work and take
precedence over the vault.
"""
import json
import os
import shutil
from pathlib import Path

VAULT_DIR = Path(os.environ.get("VAULT_DIR", "/data/vault"))
VAULT_FILE = VAULT_DIR / "secrets.enc"
VAULT_BAK = VAULT_DIR / "secrets.enc.bak"


class VaultUnreadable(Exception):
    """The vault file exists but could not be decrypted/parsed. Distinct from an
    empty/missing vault — we must NEVER treat this as empty and overwrite it,
    or a wrong STORAGE_ENCRYPTION_KEY / corruption would destroy all secrets."""


def _fernet():
    key = os.environ.get("STORAGE_ENCRYPTION_KEY")
    if not key:
        return None
    from cryptography.fernet import Fernet

    return Fernet(key.encode() if isinstance(key, str) else key)


def _read_all(strict: bool = False) -> dict:
    """Read the vault. Missing file → {} (genuinely empty). Existing-but-unreadable
    file → raise VaultUnreadable when strict (used before any write), else {} for
    best-effort reads (get_secret). This fail-closed split is what prevents a
    silent wipe on the next write when the key is wrong or the file is corrupt."""
    if not VAULT_FILE.exists():
        return {}
    try:
        raw = VAULT_FILE.read_bytes()
        f = _fernet()
        data = f.decrypt(raw) if f else raw
        obj = json.loads(data)
        if not isinstance(obj, dict):
            raise ValueError("vault content is not a JSON object")
        return obj
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
    tmp = VAULT_DIR / "secrets.enc.tmp"
    tmp.write_bytes(out)
    if VAULT_FILE.exists():
        try:
            shutil.copy2(VAULT_FILE, VAULT_BAK)
        except Exception:
            pass
    os.replace(tmp, VAULT_FILE)


def _owner_prefix(owner: str) -> str:
    """Storage prefix for a user's private secrets — mirrors
    tenancy.vault_namespace so set/get/list agree on the layout."""
    import tenancy
    return f"users/{tenancy._safe(owner)}"


def get_secret(name: str):
    """Server-side lookup: environment first, then the encrypted vault.
    NOT exposed as a tool — secret values never go back to the model.

    Per-user vault (P2): when the call happens for a CONFINED caller, their OWN
    secret (``users/<sub>/<name>``) is preferred over the shared/flat one — so an
    admin can hand a single user their own token for a shared service. Fail-open:
    if the caller can't be resolved, only env + the flat secret are used (the
    original behaviour)."""
    if not name:
        return None
    env = os.environ.get(name)
    if env:
        return env
    data = _read_all()
    try:
        import tenancy
        ident, role = tenancy.current_identity()
        ns = tenancy.vault_namespace(ident, role) if ident else ""
    except Exception:
        ns = ""
    if ns:
        personal = data.get(f"{ns}/{name}")
        if personal is not None:
            return personal
    return data.get(name)


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
    def secret_set(name: str, value: str, owner: str = "") -> str:
        """Store a secret on the NAS, encrypted at rest. Reference it by `name`
        as a service's token_env. The value is never shown back. ADMIN-ONLY.
        `owner` (optional) = a user's Pocket ID `sub`: the secret is then stored in
        THAT user's private vault namespace (only their own service calls resolve
        it). Leave empty for a shared secret. Users can't set secrets themselves —
        an admin grants vault access this way."""
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
        where = f"in {owner}'s private vault" if owner else "as a shared secret"
        return f"Stored '{name}' {where} (encrypted on the NAS). It will not be shown again."

    @mcp.tool
    def secret_list() -> str:
        """List the NAMES of stored secrets (never the values). A confined user
        sees only the shared secrets plus the ones in their OWN namespace; an admin
        sees all (tagged by owner)."""
        keys = sorted(_read_all().keys())
        try:
            import tenancy
            ident, role = tenancy.current_identity()
            own_ns = tenancy.vault_namespace(ident, role) if ident else ""
        except Exception:
            own_ns = ""
        if own_ns:  # confined caller: shared + own only
            keys = [k for k in keys
                    if not k.startswith("users/") or k.startswith(own_ns + "/")]
        if not keys:
            return "No secrets stored yet."
        return "\n".join(_fmt_key(k, own_ns) for k in keys)

    @mcp.tool
    def secret_delete(name: str, owner: str = "") -> str:
        """Delete a stored secret by name. ADMIN-ONLY. `owner` = a user's Pocket ID
        `sub` to delete from that user's private vault; empty for a shared secret."""
        name = (name or "").strip()
        try:
            d = _read_all(strict=True)
        except VaultUnreadable as exc:
            return ("Refusing to modify: the vault could not be decrypted/parsed "
                    f"({exc}) — changing it now would destroy the other secrets. Fix "
                    "STORAGE_ENCRYPTION_KEY (or restore secrets.enc.bak) and retry.")
        owner = (owner or "").strip()
        key = f"{_owner_prefix(owner)}/{name}" if owner else name
        if key in d:
            del d[key]
            _write_all(d)
            where = f" from {owner}'s vault" if owner else ""
            return f"Deleted secret '{name}'{where}."
        return f"No secret named '{name}'{(' for ' + owner) if owner else ''}."
