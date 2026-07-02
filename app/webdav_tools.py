"""Generic WebDAV transfer — cloud file stores as DATA (Nextcloud, ownCloud, …).

The HTTP counterpart of ftp_tools for cloud drives: register a WebDAV endpoint
(its base URL + an app-password secret) once, then list / upload / download /
mkdir / delete. Large files move NAS-side (endpoint <-> /data) and stream, so
nothing flows through the model's context.

Nextcloud's files WebDAV base looks like:
    https://<host>/remote.php/dav/files/<username>/
Use a Nextcloud **app password** (Settings → Security), not the login password.
Only registered endpoints are reachable and the host passes the SSRF guard.
"""
import json

import cfgstore
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlparse
# defusedxml for untrusted server responses (billion-laughs / entity DoS defense);
# fall back to stdlib only if the dependency is absent so the connector still boots.
try:
    from defusedxml.ElementTree import fromstring as _xml_fromstring
except Exception:  # pragma: no cover
    _xml_fromstring = ET.fromstring

import httpx

import netguard
import secrets_store

WEBDAV_DIR = Path(os.environ.get("WEBDAV_DIR", "/data/webdav"))
DATA_ROOT = Path(os.environ.get("DATA_ROOT", "/data")).resolve()
WORK_DIR = Path(os.environ.get("WORK_DIR", "/data/work"))


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:60] or "webdav"


def _cfg_path(name: str) -> Path:
    return WEBDAV_DIR / f"{_slug(name)}.json"


def _load(name: str):
    p = _cfg_path(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _client(cfg: dict):
    """An httpx client for the endpoint (basic auth from the vault), after the
    SSRF guard. Returns (client, base_url) or (None, error_string)."""
    base = cfg.get("base_url", "").rstrip("/") + "/"
    ok, reason = netguard.check_url(base)
    if not ok:
        return None, f"Blocked by network policy — {reason}"
    auth = None
    user = cfg.get("username")
    if user:
        pw_env = cfg.get("password_env")
        pw = secrets_store.get_secret(pw_env) if pw_env else ""
        if pw_env and not pw:
            return None, f"Endpoint needs secret '{pw_env}'. Store it with secret_set."
        auth = (user, pw or "")
    verify = netguard.tls_verify(cfg)
    return httpx.Client(auth=auth, verify=verify, timeout=300, follow_redirects=True), base


def _safe_under_data(p: str) -> Path:
    path = Path(p)
    if not path.is_absolute():
        path = DATA_ROOT / p
    path = path.resolve()
    return path


def register(mcp):
    @mcp.tool
    def webdav_add(name: str, base_url: str, username: str = "",
                   password_env: str = "", tls_insecure: bool = False,
                   ca_bundle: str = "", description: str = "") -> str:
        """Register/update a WebDAV endpoint as DATA (no redeploy). base_url = the
        WebDAV root, e.g. Nextcloud https://<host>/remote.php/dav/files/<user>/ .
        password_env = NAME of the secret holding the app password (store it with
        secret_set). Paths in the other tools are relative to base_url.

        TLS is VERIFIED by default. For a self-signed endpoint, point `ca_bundle`
        at its CA/cert file (preferred), or set `tls_insecure=true` to skip
        verification. This tool is admin-only, so only an operator can relax TLS."""
        try:
            WEBDAV_DIR.mkdir(parents=True, exist_ok=True)
            cfg = {"name": name, "base_url": base_url.rstrip("/") + "/",
                   "username": username, "password_env": password_env,
                   "tls_insecure": bool(tls_insecure), "ca_bundle": ca_bundle,
                   "description": description}
            cfgstore.write_merged(_cfg_path(name), cfg)
            note = ""
            if password_env and not secrets_store.get_secret(password_env):
                note = f" — set the app password: secret_set('{password_env}', <value>)"
            return f"Registered WebDAV endpoint '{_slug(name)}'.{note}"
        except Exception as exc:
            return f"Could not register endpoint: {exc}"

    @mcp.tool
    def webdav_list_endpoints() -> str:
        """List registered WebDAV endpoints (name — base_url — description)."""
        if not WEBDAV_DIR.exists() or not any(WEBDAV_DIR.glob("*.json")):
            return "No WebDAV endpoints yet. Use webdav_add."
        out = []
        for p in sorted(WEBDAV_DIR.glob("*.json")):
            try:
                c = json.loads(p.read_text(encoding="utf-8"))
                out.append(f"- {p.stem} — {c.get('base_url')} — {c.get('description','')}")
            except Exception:
                out.append(f"- {p.stem} — (unreadable)")
        return "\n".join(out)

    @mcp.tool
    def webdav_delete_endpoint(name: str) -> str:
        """Remove a registered WebDAV endpoint by name (does NOT touch remote files)."""
        p = _cfg_path(name)
        if p.exists():
            p.unlink()
            return f"Deleted WebDAV endpoint '{_slug(name)}'."
        return f"No WebDAV endpoint '{name}'."

    @mcp.tool
    def webdav_list(endpoint: str, path: str = "") -> str:
        """List a folder on the endpoint (name · size · type). path is relative to
        base_url, e.g. "Agenten_Projekte"."""
        cfg = _load(endpoint)
        if not cfg:
            return f"Unknown endpoint '{endpoint}'. Use webdav_list_endpoints / webdav_add."
        client, base = _client(cfg)
        if client is None:
            return base
        url = base + (path or "").lstrip("/")
        try:
            with netguard.guard(urlparse(base).hostname or ""), client:
                r = client.request("PROPFIND", url, headers={"Depth": "1"})
        except Exception as exc:
            return f"List failed: {exc}"
        if r.status_code not in (207, 200):
            return f"List returned HTTP {r.status_code}. Check path / credentials."
        base_path = urlparse(base).path
        out = []
        try:
            root = _xml_fromstring(r.content)
            ns = {"d": "DAV:"}
            for resp in root.findall("d:response", ns):
                href = resp.findtext("d:href", default="", namespaces=ns)
                rel = unquote(urlparse(href).path)
                if rel.rstrip("/") == base_path.rstrip("/") + ("/" + path.strip("/") if path.strip("/") else ""):
                    continue  # the folder itself
                name = rel.rstrip("/").split("/")[-1]
                is_dir = resp.find(".//d:collection", ns) is not None
                size = resp.findtext(".//d:getcontentlength", default="", namespaces=ns)
                out.append(f"- {name}{'/' if is_dir else ''}" + (f" · {size} B" if size else ""))
        except Exception as exc:
            return f"Could not parse listing: {exc}"
        return "\n".join(out) if out else "(empty)"

    @mcp.tool
    def webdav_upload(endpoint: str, source: str, dest: str) -> str:
        """Upload a local file (under /data, e.g. /data/work/scan.pdf) to the
        endpoint at `dest` (relative to base_url, e.g. "Agenten_Projekte/scan.pdf")."""
        cfg = _load(endpoint)
        if not cfg:
            return f"Unknown endpoint '{endpoint}'."
        src = _safe_under_data(source)
        # Ancestry check, not a string prefix: startswith("/data") would also match
        # a sibling like /data-backup and let a path escape the sandbox.
        if src != DATA_ROOT and DATA_ROOT not in src.parents:
            return "Source must be under /data."
        if not src.is_file():
            return f"No file at '{src}'."
        client, base = _client(cfg)
        if client is None:
            return base
        url = base + dest.lstrip("/")
        try:
            with netguard.guard(urlparse(base).hostname or ""), client, open(src, "rb") as f:
                r = client.put(url, content=f)
        except Exception as exc:
            return f"Upload failed: {exc}"
        if r.status_code in (200, 201, 204):
            return f"Uploaded {src.name} → {dest} ({src.stat().st_size} B)."
        return f"Upload returned HTTP {r.status_code} (check the dest folder exists — webdav_mkdir)."

    @mcp.tool
    def webdav_download(endpoint: str, path: str, dest: str = "") -> str:
        """Download a file from the endpoint (`path` relative to base_url) into
        /data/work. dest = optional filename; defaults to the remote file name."""
        cfg = _load(endpoint)
        if not cfg:
            return f"Unknown endpoint '{endpoint}'."
        client, base = _client(cfg)
        if client is None:
            return base
        url = base + path.lstrip("/")
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", dest or path.rstrip("/").split("/")[-1] or "download")
        out_path = WORK_DIR / name
        try:
            with netguard.guard(urlparse(base).hostname or ""), client:
                with client.stream("GET", url) as r:
                    if r.status_code != 200:
                        return f"Download returned HTTP {r.status_code}. Check the path."
                    with open(out_path, "wb") as f:
                        for chunk in r.iter_bytes(chunk_size=1 << 16):
                            f.write(chunk)
        except Exception as exc:
            return f"Download failed: {exc}"
        return f"Downloaded {path} → {out_path} ({out_path.stat().st_size} B)."

    @mcp.tool
    def webdav_mkdir(endpoint: str, path: str) -> str:
        """Create a folder on the endpoint (MKCOL). path relative to base_url."""
        cfg = _load(endpoint)
        if not cfg:
            return f"Unknown endpoint '{endpoint}'."
        client, base = _client(cfg)
        if client is None:
            return base
        url = base + path.strip("/") + "/"
        try:
            with netguard.guard(urlparse(base).hostname or ""), client:
                r = client.request("MKCOL", url)
        except Exception as exc:
            return f"mkdir failed: {exc}"
        if r.status_code in (201, 405):  # 405 = already exists
            return f"Folder '{path}' ready."
        return f"mkdir returned HTTP {r.status_code}."

    @mcp.tool
    def webdav_delete(endpoint: str, path: str) -> str:
        """Delete a file/folder on the endpoint. STATE-CHANGING — confirm first."""
        cfg = _load(endpoint)
        if not cfg:
            return f"Unknown endpoint '{endpoint}'."
        client, base = _client(cfg)
        if client is None:
            return base
        url = base + path.lstrip("/")
        try:
            with netguard.guard(urlparse(base).hostname or ""), client:
                r = client.request("DELETE", url)
        except Exception as exc:
            return f"Delete failed: {exc}"
        if r.status_code in (200, 204, 404):
            return f"Deleted '{path}'."
        return f"Delete returned HTTP {r.status_code}."
