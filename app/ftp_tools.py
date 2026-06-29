"""Generic, allow-listed FTP/FTPS transfer — integrations as DATA.

Lets any FTP/FTPS endpoint (including implicit-FTPS file stores on port 990, as
some LAN devices use) be added at RUNTIME as a config (data) plus a secret — no new code,
no redeploy. Upload sources are restricted to files under DATA_ROOT (the NAS
workspace). Passwords are referenced by NAME and resolved server-side via
``secrets_store`` — never stored in data, never returned. Only registered
endpoints can be reached.
"""
import ftplib
import json
import os
import re
import ssl
from pathlib import Path

import netguard
import secrets_store

FTP_DIR = Path(os.environ.get("FTP_DIR", "/data/ftp"))
DATA_ROOT = Path(os.environ.get("DATA_ROOT", "/data")).resolve()


class _ImplicitFTP_TLS(ftplib.FTP_TLS):
    """FTP_TLS variant that wraps the control socket in TLS immediately
    (implicit FTPS, typically on port 990), as some LAN devices require."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
        self._sock = value


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:60] or "endpoint"


def _cfg_path(name: str) -> Path:
    return FTP_DIR / f"{_slug(name)}.json"


def _load(name: str):
    p = _cfg_path(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _connect(cfg):
    host = cfg["host"]
    ok, reason = netguard.check_host(host)
    if not ok:
        raise ConnectionError(f"Blocked by network policy — {reason}")
    port = int(cfg.get("port") or 21)
    mode = (cfg.get("tls") or "none").lower()  # none | explicit | implicit
    ctx = ssl.create_default_context()
    if cfg.get("tls_insecure", False):  # default: verify (only self-signed LAN devices opt out)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    if mode == "implicit":
        ftp = _ImplicitFTP_TLS(context=ctx)
    elif mode == "explicit":
        ftp = ftplib.FTP_TLS(context=ctx)
    else:
        ftp = ftplib.FTP()

    # guard(host): enforce the egress IP policy at CONNECT time (anti DNS-rebinding)
    with netguard.guard(host):
        ftp.connect(host, port, timeout=30)
        username = cfg.get("username") or "anonymous"
        password = ""
        if cfg.get("password_env"):
            password = secrets_store.get_secret(cfg["password_env"]) or ""
        ftp.login(username, password)
        if mode in ("implicit", "explicit"):
            ftp.prot_p()  # encrypt the data channel too
    return ftp


def _safe_source(nas_path: str):
    """Resolve a source path and ensure it stays within DATA_ROOT."""
    p = Path(nas_path)
    p = p if p.is_absolute() else (DATA_ROOT / p)
    p = p.resolve()
    try:
        p.relative_to(DATA_ROOT)
    except ValueError:
        return None
    return p


def register(mcp):
    @mcp.tool
    def ftp_add(name: str, host: str, port: int = 21, tls: str = "none",
                tls_insecure: bool = False, username: str = "",
                password_env: str = "", description: str = "") -> str:
        """Register/update an FTP/FTPS endpoint as DATA (no redeploy).
        tls: "none" | "explicit" | "implicit". password_env = NAME of the secret
        (store it with secret_set). TLS certificates are VERIFIED by default; only
        set tls_insecure=true for a self-signed LAN device (e.g. a 3D printer's SD
        card over implicit FTPS). Example: host=<device-ip>, port=990,
        tls="implicit", tls_insecure=true, username=<user>, password_env=<secret>."""
        try:
            FTP_DIR.mkdir(parents=True, exist_ok=True)
            cfg = {
                "name": name,
                "host": host,
                "port": int(port),
                "tls": (tls or "none").lower(),
                "tls_insecure": bool(tls_insecure),
                "username": username,
                "password_env": password_env,
                "description": description,
            }
            _cfg_path(name).write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            note = ""
            if password_env and not secrets_store.get_secret(password_env):
                note = f" — set the password with secret_set('{password_env}', <value>)"
            return f"Registered FTP endpoint '{_slug(name)}'.{note}"
        except Exception as exc:
            return f"Could not register endpoint: {exc}"

    @mcp.tool
    def ftp_list_endpoints() -> str:
        """List configured FTP endpoints (name — host:port — description)."""
        if not FTP_DIR.exists():
            return "No FTP endpoints configured yet."
        items = sorted(FTP_DIR.glob("*.json"))
        if not items:
            return "No FTP endpoints configured yet. Use ftp_add."
        out = []
        for p in items:
            try:
                c = json.loads(p.read_text(encoding="utf-8"))
                out.append(f"- {p.stem} — {c.get('host', '')}:{c.get('port', '')} — {c.get('description', '')}")
            except Exception:
                out.append(f"- {p.stem} — (unreadable config)")
        return "\n".join(out)

    @mcp.tool
    def ftp_list(server: str, path: str = "/") -> str:
        """List files at `path` on a registered FTP endpoint."""
        cfg = _load(server)
        if not cfg:
            return f"Unknown FTP endpoint '{server}'. Use ftp_list_endpoints / ftp_add."
        try:
            ftp = _connect(cfg)
        except Exception as exc:
            return f"Connect failed: {exc}"
        try:
            names = ftp.nlst(path)
            return "\n".join(names) if names else f"(empty) {path}"
        except Exception as exc:
            return f"List failed: {exc}"
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()

    @mcp.tool
    def ftp_delete(name: str) -> str:
        """Remove a registered FTP endpoint by name."""
        p = _cfg_path(name)
        if p.exists():
            p.unlink()
            return f"Deleted FTP endpoint '{_slug(name)}'."
        return f"No FTP endpoint '{name}'."

    @mcp.tool
    def ftp_upload(server: str, nas_path: str, remote_path: str) -> str:
        """Upload a file from the NAS (path under /data) to the FTP endpoint.
        STATE-CHANGING — confirm with the user first. nas_path is relative to
        /data (e.g. "work/model.3mf") or an absolute path inside /data."""
        cfg = _load(server)
        if not cfg:
            return f"Unknown FTP endpoint '{server}'. Use ftp_list_endpoints / ftp_add."
        src = _safe_source(nas_path)
        if src is None:
            return "Source path must be under /data (the NAS workspace)."
        if not src.is_file():
            return f"No such file: {src}"
        try:
            ftp = _connect(cfg)
        except Exception as exc:
            return f"Connect failed: {exc}"
        try:
            with src.open("rb") as f:
                ftp.storbinary(f"STOR {remote_path}", f)
            return f"Uploaded {src.name} ({src.stat().st_size} bytes) → {remote_path}"
        except Exception as exc:
            return f"Upload failed: {exc}"
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()
