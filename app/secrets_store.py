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


def get_secret(name: str):
    """Server-side lookup: environment first, then the encrypted vault.
    NOT exposed as a tool — secret values never go back to the model."""
    if not name:
        return None
    return os.environ.get(name) or _read_all().get(name)


def register(mcp):
    @mcp.tool
    def secret_set(name: str, value: str) -> str:
        """Store a secret on the NAS, encrypted at rest. Reference it by `name`
        as a service's token_env. The value is never shown back."""
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
        d[name] = value
        _write_all(d)
        return f"Stored secret '{name}' (encrypted on the NAS). It will not be shown again."

    @mcp.tool
    def secret_list() -> str:
        """List the NAMES of stored secrets (never the values)."""
        names = sorted(_read_all().keys())
        return "\n".join(f"- {n}" for n in names) if names else "No secrets stored yet."

    @mcp.tool
    def secret_delete(name: str) -> str:
        """Delete a stored secret by name."""
        try:
            d = _read_all(strict=True)
        except VaultUnreadable as exc:
            return ("Refusing to modify: the vault could not be decrypted/parsed "
                    f"({exc}) — changing it now would destroy the other secrets. Fix "
                    "STORAGE_ENCRYPTION_KEY (or restore secrets.enc.bak) and retry.")
        if name in d:
            del d[name]
            _write_all(d)
            return f"Deleted secret '{name}'."
        return f"No secret named '{name}'."
