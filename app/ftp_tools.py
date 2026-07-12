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

import cfgstore
import os
import re
import shutil
import socket
import ssl
import subprocess
from pathlib import Path
from urllib.parse import quote

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

    def ntransfercmd(self, cmd, rest=None):
        """Reuse the control channel's TLS session on the DATA connection.

        Servers with TLS session-resumption required (vsftpd `require_ssl_reuse`,
        Bambu Lab printers' implicit-FTPS SD store) stall the data transfer otherwise
        — Python's ftplib negotiates a fresh session for the data socket, which these
        servers reject/hang → "read operation timed out". Passing the control socket's
        session makes the data channel resume it, as required.
        """
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if getattr(self, "_prot_p", False):
            conn = self.context.wrap_socket(
                conn, server_hostname=self.host,
                session=self.sock.session,
            )
        return conn, size


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
    ctx = netguard.ssl_context(cfg)  # ca_bundle > tls_insecure > verify (shared TLS policy)

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


# ── curl-based FTPS upload ────────────────────────────────────────────────────
# Why curl and not ftplib: servers that REQUIRE TLS session reuse on the data
# channel (vsftpd `require_ssl_reuse`, Bambu Lab printers' implicit-FTPS SD store)
# stall Python's ftplib even with a session-resumption shim — OpenSSL via Python
# does not reliably resume the control channel's session on the data socket, so
# the STOR hangs until "read operation timed out". curl implements FTPS session
# reuse natively and is the community-proven upload path for these devices.
# Plain (non-TLS) FTP keeps using ftplib — no benefit in shelling out there.

_CURL_MAX_TIME = int(os.environ.get("FTP_CURL_MAX_TIME", "300"))


def _curl_quote(value: str) -> str:
    """Escape a value for a double-quoted curl-config string (\\ and \")."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _is_ip_literal(host: str) -> bool:
    try:
        import ipaddress
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _curl_config(cfg: dict, password: str, src: Path, remote_path: str,
                 pinned_ip: str = "") -> str:
    """Build the curl config (fed via stdin with `-K -`, so credentials never
    appear on argv / in the process list). Implicit FTPS → ftps:// URL; explicit
    → ftp:// URL + `ssl-reqd` (refuse to continue unencrypted). TLS policy knobs
    mirror netguard.ssl_context: ca_bundle (pin) > tls_insecure (off) > verify.
    `pinned_ip` pre-seeds curl's DNS with the address that already passed the
    egress guard, closing the check→connect gap for the external process."""
    host = str(cfg["host"]).strip().strip("[]")
    port = int(cfg.get("port") or 21)
    mode = (cfg.get("tls") or "none").lower()
    scheme = "ftps" if mode == "implicit" else "ftp"
    # FTP URLs are relative to the login directory (for these devices: the SD
    # root), so a leading '/' is stripped; percent-encode the rest for the URL.
    path = quote(remote_path.lstrip("/"), safe="/")
    lines = [
        f'url = "{scheme}://{_curl_quote(host)}:{port}/{path}"',
        f'upload-file = "{_curl_quote(str(src))}"',
        f'user = "{_curl_quote(cfg.get("username") or "anonymous")}:{_curl_quote(password)}"',
        "connect-timeout = 20",
        f"max-time = {_CURL_MAX_TIME}",
        "silent",
        "show-error",
        "ftp-create-dirs",
    ]
    if mode == "explicit":
        lines.append("ssl-reqd")
    ca = (cfg.get("ca_bundle") or "").strip()
    if ca:
        lines.append(f'cacert = "{_curl_quote(ca)}"')
    elif cfg.get("tls_insecure", False):
        lines.append("insecure")
    if pinned_ip:
        lines.append(f'resolve = "{_curl_quote(host)}:{port}:{pinned_ip}"')
    return "\n".join(lines) + "\n"


def _curl_upload(cfg: dict, src: Path, remote_path: str) -> str:
    """Upload `src` to an FTPS endpoint via curl. Returns the user-facing result
    string (success or failure) — mirrors the ftplib path's messages."""
    host = cfg["host"]
    ok, reason = netguard.check_host(host)
    if not ok:
        return f"Connect failed: Blocked by network policy — {reason}"
    pinned_ip = ""
    if not _is_ip_literal(str(host)):
        # Resolve INSIDE the guard: only policy-allowed addresses come back, and
        # the first one is pinned into curl so it connects to exactly what was
        # vetted (curl itself can't be covered by the getaddrinfo guard).
        try:
            with netguard.guard(host):
                infos = socket.getaddrinfo(str(host).strip().strip("[]"),
                                           int(cfg.get("port") or 21),
                                           type=socket.SOCK_STREAM)
            infos.sort(key=lambda i: i[0] != socket.AF_INET)  # prefer IPv4 on LAN
            pinned_ip = infos[0][4][0]
        except Exception as exc:
            return f"Connect failed: cannot resolve '{host}': {exc}"
    password = ""
    if cfg.get("password_env"):
        password = secrets_store.get_secret(cfg["password_env"]) or ""
    config = _curl_config(cfg, password, src, remote_path, pinned_ip)
    try:
        proc = subprocess.run(
            ["curl", "--config", "-"], input=config.encode("utf-8"),
            capture_output=True, timeout=_CURL_MAX_TIME + 30)
    except subprocess.TimeoutExpired:
        return f"Upload failed: curl did not finish within {_CURL_MAX_TIME + 30}s."
    except Exception as exc:
        return f"Upload failed: could not run curl: {exc}"
    if proc.returncode == 0:
        return f"Uploaded {src.name} ({src.stat().st_size} bytes) → {remote_path}"
    err = (proc.stderr or b"").decode("utf-8", "replace").strip()
    return f"Upload failed: curl exit {proc.returncode}: {err or 'no error output'}"


def register(mcp):
    @mcp.tool
    def ftp_add(name: str, host: str, port: int = 21, tls: str = "none",
                tls_insecure: bool = False, ca_bundle: str = "", username: str = "",
                password_env: str = "", description: str = "") -> str:
        """Register/update an FTP/FTPS endpoint as DATA (no redeploy).
        tls: "none" | "explicit" | "implicit". password_env = NAME of the secret
        (store it with secret_set). TLS certificates are VERIFIED by default; for a
        self-signed LAN device (e.g. a 3D printer's SD over implicit FTPS) point
        `ca_bundle` at its CA/cert (the safe way) or set tls_insecure=true. Example:
        host=<device-ip>, port=990, tls="implicit", tls_insecure=true,
        username=<user>, password_env=<secret>."""
        try:
            FTP_DIR.mkdir(parents=True, exist_ok=True)
            cfg = {
                "name": name,
                "host": host,
                "port": int(port),
                "tls": (tls or "none").lower(),
                "tls_insecure": bool(tls_insecure),
                "ca_bundle": ca_bundle.strip(),
                "username": username,
                "password_env": password_env,
                "description": description,
            }
            cfgstore.write_merged(_cfg_path(name), cfg)
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
        # FTPS (implicit/explicit) → curl: it does TLS session reuse on the data
        # channel, which ftplib can't (Bambu printers et al. hang otherwise).
        # Plain FTP, or an image without curl, keeps the ftplib path below.
        if (cfg.get("tls") or "none").lower() in ("implicit", "explicit") \
                and shutil.which("curl"):
            return _curl_upload(cfg, src, remote_path)
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
