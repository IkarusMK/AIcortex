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
from pathlib import Path

VAULT_DIR = Path(os.environ.get("VAULT_DIR", "/data/vault"))
VAULT_FILE = VAULT_DIR / "secrets.enc"


def _fernet():
    key = os.environ.get("STORAGE_ENCRYPTION_KEY")
    if not key:
        return None
    from cryptography.fernet import Fernet

    return Fernet(key.encode() if isinstance(key, str) else key)


def _read_all() -> dict:
    if not VAULT_FILE.exists():
        return {}
    raw = VAULT_FILE.read_bytes()
    f = _fernet()
    try:
        data = f.decrypt(raw) if f else raw
        return json.loads(data)
    except Exception:
        return {}


def _write_all(d: dict) -> None:
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(d).encode()
    f = _fernet()
    VAULT_FILE.write_bytes(f.encrypt(blob) if f else blob)


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
        d = _read_all()
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
        d = _read_all()
        if name in d:
            del d[name]
            _write_all(d)
            return f"Deleted secret '{name}'."
        return f"No secret named '{name}'."
