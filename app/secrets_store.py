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
import ipaddress
import json
import os
import re
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse

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
    "RUNNER_LLM_KEY",   # the 2.2 runner's model access (runners.py reads it) — never a service's
    "RUNNER_CHATGPT_SESSION",   # the runner's ChatGPT sign-in (chatgpt_signin)
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
# RUNNER_: the runners' own keys (RUNNER_KEY_<id>, runners.py) — never a service's.
# MCP_OAUTH_: a person's OAuth sign-in to an upstream MCP server (mcp_oauth.py), read
# only through get_owned and written only by mcp_oauth — never sent by a service,
# webhook or runner, never set or edited by hand.
LOGIN_PREFIX = "MCP_OAUTH_"
_INTERNAL_PREFIXES = ("OIDC_", "FASTMCP_", "AUTH_", "PYTHON", "STORAGE_ENCRYPTION_", "RUNNER_",
                      LOGIN_PREFIX)
_RESERVED = (f"Refused: secret names starting with {LOGIN_PREFIX} are reserved for OAuth "
             f"sign-ins — sign in in the AICortex WebUI (Services → Sign in) instead.")
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
    return _resolve(name)[1]


def _resolve(name: str, own_only: bool = False):
    """(storage key, value) of the secret `name` as the current caller resolves it —
    the key is what a host binding is recorded under ("" when there is no value).
    `own_only`: only the caller's PERSONAL entry (see ``get_own_secret``)."""
    if not name or "/" in name or is_internal_env(name):
        return "", None
    ns = _caller_namespace()
    if own_only:
        key = f"{ns}/{name}" if ns else ""
        value = _read_all().get(key) if key else None
        return (key, value) if value is not None else ("", None)
    env = os.environ.get(name)
    if env:
        return name, env
    data = _read_all()
    if ns:
        personal = data.get(f"{ns}/{name}")
        if personal is not None:
            return f"{ns}/{name}", personal
    value = data.get(name)
    return (name, value) if value is not None else ("", None)


def _caller_namespace() -> str:
    try:
        import tenancy
        ident, role = tenancy.current_identity()
        return tenancy.vault_namespace(ident, role) if ident else ""
    except Exception:
        return ""


def get_owned(name: str, owner: str):
    """The secret `name` from exactly `owner`'s private area — no environment value, no
    shared fallback, no caller resolution. For logins that belong to one person
    (mcp_oauth): whoever asks, only that person's entry comes back."""
    if not name or "/" in name or not owner:
        return None
    return _read_all().get(f"{_owner_prefix(owner)}/{name}")


def get_own_secret(name: str):
    """Only the current caller's PERSONAL secret `name` — never an env value, never a
    shared one. For tools that send a secret to a destination the caller chooses
    (webhook_send): a user may send their own token, not everyone's — and never one of
    the server's own names (``is_internal_env``), an OAuth sign-in included."""
    return _resolve(name, own_only=True)[1]


# ── Host binding: where a secret may be sent ────────────────────────────────
# An admin session — the default for an interactive MCP client, so also a
# prompt-injected one — could otherwise send any secret anywhere: webhook_send to a
# URL of its choice, or a service / MCP / WebDAV / mail … entry re-pointed (or newly
# added) at another host that references an existing token. So every secret carries
# the set of destination hosts it may go to (in meta.json, under the vault lock), and
# EVERY outbound use goes through ``use``:
#
# - the destination host must be bound to the secret;
# - an unbound secret is bound by the first registered integration that is created or
#   updated with it (``check_registration``) or that uses it — to that integration's
#   host, or, when configs written BEFORE binding existed reference it, to their hosts
#   (trusted as found, so an existing install with one token on two hosts keeps
#   working after the upgrade);
# - another host is refused, at registration and at use, unless a person makes the
#   change in the WebUI (``authz.from_webui``) — then the host is added;
# - webhook_send never binds on its own: the host must have been bound explicitly,
#   with ``secret_set(..., hosts=...)`` (which needs the value, so an AI that doesn't
#   know it can only overwrite it, never widen where it goes) or in the WebUI vault.
#
# A NEW value keeps the binding: the binding says where the integration lives, not
# what the token is — rotating a token must not reopen trust-on-first-use, or anyone
# allowed to write a value could re-aim the secret by writing a junk one first.
_HOSTS_KEY = "_/hosts"   # meta.json: storage key -> [host, ...]; "/" never occurs in a name
_SINCE_KEY = "_/since"   # meta.json: when binding started; older configs are trusted as found
_MAX_HOSTS = 32
_HOST_RE = re.compile(r"^[a-z0-9_]([a-z0-9_.-]{0,251}[a-z0-9_])?$")
# Registry kind -> how its destination host is read. Inbound webhooks have none: a
# secret they reference is only compared, never sent.
_DEST = {"service": "base_url", "mcp": "url", "webdav": "base_url", "caldav": "base_url",
         "ftp": "host", "mqtt": "host", "ssh": "host", "mail": "host", "imap": "host"}


class Refused(Exception):
    """A secret may not be sent to this destination (host binding)."""


def host_of(dest) -> str:
    """The normalised host of a URL or a bare host name / IP ("" if there is none)."""
    d = str(dest or "").strip()
    if "://" in d:
        try:
            d = urlparse(d).hostname or ""
        except ValueError:
            d = ""
    return d.strip().strip("[]").rstrip(".").lower()


def _valid_host(h: str) -> bool:
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return bool(_HOST_RE.match(h)) and ".." not in h


def _clean_hosts(hosts):
    """(sorted unique hosts, "") or (None, refusal) for hosts a person/caller names."""
    items = re.split(r"[,\s]+", hosts) if isinstance(hosts, str) else hosts
    if not isinstance(items, (list, tuple)):
        return None, "Refused: hosts must be a list of host names or IP addresses."
    out = set()
    for raw in items:
        h = host_of(raw)
        if not h:
            continue
        if not _valid_host(h):
            return None, f"Refused: '{str(raw)[:80]}' is not a host name or IP address."
        out.add(h)
    if len(out) > _MAX_HOSTS:
        return None, f"Refused: at most {_MAX_HOSTS} hosts per secret."
    return sorted(out), ""


def _meta_strict() -> dict:
    """meta.json for a binding decision — an unreadable file is NOT empty (that would
    unbind every secret); StoreCorrupt propagates and the caller refuses."""
    meta = store.read_json(VAULT_META, {})
    return meta if isinstance(meta, dict) else {}


def _bindings(meta: dict) -> dict:
    b = meta.get(_HOSTS_KEY)
    return b if isinstance(b, dict) else {}


def _bound(meta: dict, key: str):
    """The hosts `key` is bound to, or None when it is not bound yet."""
    hosts = _bindings(meta).get(key)
    return [str(h) for h in hosts] if isinstance(hosts, list) else None


def _with_binding(meta: dict, key: str, hosts) -> dict:
    return {**meta, _HOSTS_KEY: {**_bindings(meta), key: sorted(set(hosts))}}


def _with_since(meta: dict) -> dict:
    return meta if isinstance(meta.get(_SINCE_KEY), (int, float)) else {**meta, _SINCE_KEY: time.time()}


def _config_host(kind: str, cfg: dict):
    """Destination host of a registry config; None for kinds that send nothing."""
    field = _DEST.get(kind)
    return host_of(cfg.get(field)) if field and isinstance(cfg, dict) else None


def _found_hosts(name: str, before: float):
    """(referenced, hosts) from the configs written before binding started that name
    `name` — the setup found at upgrade time, trusted as it was."""
    referenced, hosts = False, set()
    for kind, d in _registries():
        for p in sorted(Path(d).glob("*.json")):
            try:
                if p.stat().st_mtime >= before:
                    continue
                cfg = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(cfg, dict) or name not in {str(cfg.get(f) or "").strip() for f in _REF_FIELDS}:
                continue
            referenced = True
            host = _config_host(kind, cfg)
            if host:
                hosts.add(host)
    return referenced, hosts


def _first_binding(meta: dict, name: str, host: str) -> list:
    """What an unbound secret is bound to on its first registered use: the hosts of the
    configs found at upgrade time that reference it — or else this use's host."""
    referenced, found = _found_hosts(name, meta[_SINCE_KEY])
    return sorted(found) if referenced else [host]


def _from_webui() -> bool:
    try:
        import authz
        return authz.from_webui()
    except Exception:
        return False


def _refusal(name: str, hosts, host: str, registered: bool) -> str:
    where = ", ".join(hosts) if hosts else "no host yet"
    if registered:
        return (f"Refused: secret '{name}' may only be sent to {where} — not to '{host}'. "
                f"If this integration really moved, a person can allow the new host in the "
                f"AICortex WebUI (Vault → the secret's hosts, or edit the integration under "
                f"Services), or re-enter the secret with secret_set('{name}', <value>, "
                f"hosts=...).")
    return (f"Refused: secret '{name}' may only be sent to {where} — not to '{host}'. "
            f"webhook_send only sends a secret to a host it is bound to. Bind it with "
            f"secret_set('{name}', <value>, hosts='{host}') (needs the value) or in the "
            f"AICortex WebUI (Vault → the secret's hosts).")


_UNREADABLE = ("Refused: the vault's metadata (meta.json) is unreadable, so where secrets "
               "may be sent can't be checked. Restore or fix it, then retry.")


@_serialized
def _admit(key: str, name: str, host: str, registered: bool) -> str:
    """"" when the secret stored at `key` may go to `host` (recording a first or a
    WebUI binding on the way), else the refusal."""
    try:
        meta = _with_since(_meta_strict())
    except store.StoreCorrupt:
        return _UNREADABLE
    bound, changed = _bound(meta, key), False
    if bound is None and registered:
        bound, changed = _first_binding(meta, name, host), True
    if bound is None:
        return _refusal(name, [], host, registered)
    if host not in bound:
        if not (registered and _from_webui()):
            if changed:
                store.write_json(VAULT_META, _with_binding(meta, key, bound))
            return _refusal(name, bound, host, registered)
        bound, changed = [*bound, host], True
    if changed:
        store.write_json(VAULT_META, _with_binding(meta, key, bound))
    return ""


def use(name: str, dest: str, registered: bool = True, own_only: bool = False):
    """The value of secret `name` for sending to `dest` (a URL or a host) — the ONE
    check every outbound use of a secret goes through. None/"" when there is no value
    (callers report that as before); raises ``Refused`` when `dest` is not a host the
    secret is bound to. `registered`: a registered integration (it may bind an unbound
    secret, and a person in the WebUI may add its host); webhook_send passes False.
    `own_only`: only the caller's personal entry (``get_own_secret``)."""
    key, value = _resolve(name, own_only)
    if not value:
        return value
    host = host_of(dest)
    if not host:
        raise Refused(f"Refused: no destination host to check secret '{name}' against.")
    refusal = _admit(key, name, host, registered)
    if refusal:
        raise Refused(refusal)
    return value


def _keys_of(name: str) -> list:
    """Every storage key that holds a value for `name`: shared/env and everyone's own."""
    data = _read_all()
    keys = [name] if (os.environ.get(name) or name in data) else []
    return keys + sorted(k for k in data if k.startswith("users/") and k.split("/", 2)[-1] == name)


@_serialized
def _admit_registration(name: str, host: str) -> str:
    try:
        meta = _with_since(_meta_strict())
    except store.StoreCorrupt:
        return _UNREADABLE
    webui, refusal, changed = _from_webui(), "", False
    for key in _keys_of(name):
        bound = _bound(meta, key)
        if bound is None:
            bound = _first_binding(meta, name, host)
            meta, changed = _with_binding(meta, key, bound), True
        if host in bound:
            continue
        if webui:
            meta, changed = _with_binding(meta, key, [*bound, host]), True
        else:
            refusal = refusal or _refusal(name, bound, host, True)
    if changed:
        store.write_json(VAULT_META, meta)
    return refusal


def check_registration(kind: str, path, cfg: dict) -> str:
    """"" or why a ``*_add`` must be refused: the config about to be stored — merged
    with the stored one exactly as cfgstore will merge it, so a partial update that
    only changes the address is seen — would send a secret to a host it is not bound
    to. Binds a stored but still unbound secret on the way (first registration)."""
    import cfgstore
    try:
        existing = store.read_json(path, {})
    except store.StoreCorrupt:
        existing = {}
    merged = cfgstore.merge(existing if isinstance(existing, dict) else {}, cfg,
                            cfgstore._GIVEN.get())
    host = _config_host(kind, merged)
    if not host:
        return ""
    for field in _REF_FIELDS:
        name = str(merged.get(field) or "").strip()
        if name and "/" not in name and not is_internal_env(name):
            refusal = _admit_registration(name, host)
            if refusal:
                return refusal
    return ""


def _binding_key(name: str, owner: str) -> str:
    if not owner:
        return name
    seg = f"users/{owner}/{name}"
    return seg if seg in _read_all() else f"{_owner_prefix(owner)}/{name}"


@_serialized
def vault_set_hosts(name: str, owner: str = "", hosts=(), by: str = "") -> str:
    """Set exactly the hosts a secret may be sent to — for a person in the WebUI (the
    route checks the admin session). `owner` as the listings show it (or an identity);
    empty for a shared/environment secret. An empty list binds it to no host at all."""
    name = (name or "").strip()
    if not name or "/" in name:
        return "Refused: secret name is required and may not contain '/'."
    if name.upper().startswith(LOGIN_PREFIX) or is_internal_env(name):
        return f"Refused: '{name}' belongs to AICortex itself and is never sent anywhere."
    clean, why = _clean_hosts(hosts)
    if why:
        return why
    try:
        meta = _with_since(_meta_strict())
    except store.StoreCorrupt:
        return _UNREADABLE
    key = _binding_key(name, (owner or "").strip())
    store.write_json(VAULT_META, _with_binding(meta, key, clean))
    return f"Updated hosts for '{name}': {', '.join(clean) or 'none (sent nowhere)'}."


# ── Core vault operations ────────────────────────────────────────────────────
# Module-level so BOTH front doors (the MCP tools below and the admin WebUI)
# share one implementation — same refusal logic, same guards, no drift.

@_serialized
def vault_set(name: str, value: str, owner: str = "", description: str = "",
              by: str = "", reserved_ok: bool = False, hosts=None) -> str:
    """Store a secret (shared, or in `owner`'s private namespace). Returns a
    user-facing message; the value is never echoed back. `reserved_ok` is for
    mcp_oauth alone, which writes the sign-ins under the reserved names. `hosts`
    (optional) replaces where the secret may be sent; without it a new value keeps
    the binding the secret already has."""
    name = (name or "").strip()
    if not name or "/" in name:
        return "Refused: secret name is required and may not contain '/'."
    if name.upper().startswith(LOGIN_PREFIX) and not reserved_ok:
        return _RESERVED
    clean = None
    if hosts is not None:
        clean, why = _clean_hosts(hosts)
        if why:
            return why
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
    if clean is not None:
        meta = _with_binding(_with_since(meta), key, clean)
    store.write_json(VAULT_META, meta)
    where = f"in {owner}'s private vault" if owner else "as a shared secret"
    bound = f" It may be sent to: {', '.join(clean) or 'no host'}." if clean is not None else ""
    return f"Stored '{name}' {where} (encrypted on the NAS). It will not be shown again.{bound}"


def _existing_key(d: dict, name: str, owner: str) -> str:
    """The stored key of an existing entry: shared, in `owner`'s area by identity, or by
    the namespace segment the listings show."""
    if not owner:
        return name if name in d else ""
    return next((k for k in (f"{_owner_prefix(owner)}/{name}", f"users/{owner}/{name}") if k in d), "")


@_serialized
def vault_update(name: str, owner: str = "", value: str = "", description=None, by: str = "") -> str:
    """Change an existing secret in place: a new value (never echoed) and/or its
    description; an empty value keeps the stored one."""
    name = (name or "").strip()
    if name.upper().startswith(LOGIN_PREFIX):
        return _RESERVED
    if value and _fernet() is None and os.environ.get("ALLOW_PLAINTEXT_VAULT") != "1":
        return "Refusing to store: STORAGE_ENCRYPTION_KEY is not set."
    try:
        d = _read_all(strict=True)
    except VaultUnreadable as exc:
        return ("Refusing to modify: the vault could not be decrypted/parsed "
                f"({exc}) — changing it now would destroy the other secrets.")
    key = _existing_key(d, name, (owner or "").strip())
    if not key:
        return f"No secret named '{name}'{(' for ' + owner) if owner else ''}."
    if value:
        d[key] = value
        _write_all(d)
    from datetime import datetime, timezone
    meta = _meta()
    old = meta.get(key, {})
    meta[key] = {"description": str(description).strip()[:300] if description is not None
                 else old.get("description", ""),
                 "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "updated_by": (by or "").strip()}
    store.write_json(VAULT_META, meta)
    return (f"Updated '{name}': " + ("new value stored, encrypted. It will not be shown." if value
                                      else "the value stays as it is."))


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


@_serialized
def vault_purge(name: str) -> int:
    """Delete `name` from every area — shared and every person's. Returns how many
    entries went. An unreadable vault is left alone (0)."""
    name = (name or "").strip()
    try:
        d = _read_all(strict=True)
    except VaultUnreadable:
        return 0
    gone = [k for k in d if k == name or (k.startswith("users/") and k.split("/", 2)[-1] == name)]
    if not gone:
        return 0
    _write_all({k: v for k, v in d.items() if k not in gone})
    meta = _meta()
    dropped = [meta.pop(k, None) for k in gone]      # every one, not just the first hit
    if any(m is not None for m in dropped):
        store.write_json(VAULT_META, meta)
    return len(gone)


def vault_entries() -> list:
    """NAMES ONLY, structured, unconfined — for the admin WebUI (which enforces
    the admin role itself). Each entry: {"name", "owner"} with owner="" = shared.
    Values never leave this module."""
    meta, refs = _meta(), secret_references()
    out = []
    for key in sorted(_read_all().keys()):
        hosts = _bound(meta, key)
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
                    "hosts": hosts,   # None = not bound yet (the first integration binds it)
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
    def secret_set(name: str, value: str, owner: str = "", description: str = "",
                   hosts: str = "") -> str:
        """Store a secret on the NAS, encrypted at rest. Reference it by `name`
        as a service's token_env. The value is never shown back. ADMIN-ONLY.
        `owner` (optional) = a user's Pocket ID `sub`: the secret is then stored in
        THAT user's private vault namespace (only their own service calls resolve
        it). Leave empty for a shared secret. Users can't set secrets themselves —
        an admin grants vault access this way. `description` = what it is for
        (shown in the WebUI, never the value). `hosts` (optional, comma-separated) =
        exactly the hosts the secret may be sent to — needed before webhook_send may
        send it; a registered integration binds an unbound secret to its own host on
        first use. Without `hosts` a new value keeps the existing binding."""
        try:
            import authz
            by = authz.caller_label()
        except Exception:
            by = ""
        return vault_set(name, value, owner, description=description, by=by,
                         hosts=hosts if hosts.strip() else None)

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
