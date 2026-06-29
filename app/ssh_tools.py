"""SSH — run commands and transfer files over SSH/SFTP (servers as DATA).

One endpoint covers both: register an SSH host (with a vault-held password or
private key), then run remote commands (ssh_run) and move files (ssh_upload /
ssh_download / ssh_list_dir over SFTP). Only registered hosts are reachable and
each passes the SSRF egress guard; credentials come from the vault, never stored
in the endpoint config.

ssh_run executes whatever command is passed on the remote host — powerful and
state-changing. Confirm with the user before anything that changes remote state.
"""
import json
import os
import re
from io import StringIO
from pathlib import Path

import netguard
import secrets_store

SSH_DIR = Path(os.environ.get("SSH_DIR", "/data/ssh"))
SSH_KNOWN_HOSTS = SSH_DIR / "known_hosts"
DATA_ROOT = Path(os.environ.get("DATA_ROOT", "/data")).resolve()
WORK_DIR = Path(os.environ.get("WORK_DIR", "/data/work"))


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:60] or "ssh"


def _cfg_path(name: str) -> Path:
    return SSH_DIR / f"{_slug(name)}.json"


def _load(name: str):
    p = _cfg_path(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_key(key_str: str):
    import paramiko
    for K in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            return K.from_private_key(StringIO(key_str))
        except Exception:
            continue
    return None


def _connect(cfg: dict):
    """Return (client, error). client is a connected paramiko.SSHClient."""
    host = cfg.get("host")
    ok, reason = netguard.check_host(host)
    if not ok:
        return None, f"Blocked by network policy — {reason}"
    try:
        import paramiko
    except Exception:
        return None, "paramiko not installed in the image."
    cli = paramiko.SSHClient()
    # Persisted known_hosts → host keys are PINNED on first use: if a host's key
    # later changes (MITM / impersonation), paramiko raises BadHostKeyException
    # instead of silently trusting it (the old AutoAddPolicy-without-known_hosts
    # re-trusted blindly every time). Set SSH_STRICT_HOST_KEYS=1 to reject unknown
    # hosts entirely (no trust-on-first-use; pre-populate data/ssh/known_hosts).
    try:
        SSH_DIR.mkdir(parents=True, exist_ok=True)
        if not SSH_KNOWN_HOSTS.exists():
            SSH_KNOWN_HOSTS.touch()
        cli.load_host_keys(str(SSH_KNOWN_HOSTS))  # also sets the save path for TOFU
    except Exception:
        pass
    if os.environ.get("SSH_STRICT_HOST_KEYS") == "1":
        cli.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = dict(hostname=host, port=int(cfg.get("port", 22)),
                  username=cfg.get("username"), timeout=30,
                  allow_agent=False, look_for_keys=False)
    if cfg.get("key_env"):
        key = secrets_store.get_secret(cfg["key_env"])
        if not key:
            return None, f"Needs secret '{cfg['key_env']}' (private key). Use secret_set."
        pkey = _load_key(key)
        if pkey is None:
            return None, "Could not parse the private key (Ed25519/RSA/ECDSA, unencrypted)."
        kwargs["pkey"] = pkey
    elif cfg.get("password_env"):
        pw = secrets_store.get_secret(cfg["password_env"])
        if not pw:
            return None, f"Needs secret '{cfg['password_env']}'. Use secret_set."
        kwargs["password"] = pw
    try:
        # guard(host): enforce the egress IP policy at CONNECT time (anti rebinding)
        with netguard.guard(host):
            cli.connect(**kwargs)
    except Exception as exc:
        return None, f"SSH connect failed: {exc}"
    return cli, None


def _safe_under_data(p: str) -> Path:
    path = Path(p)
    if not path.is_absolute():
        path = DATA_ROOT / p
    return path.resolve()


def register(mcp):
    @mcp.tool
    def ssh_add(name: str, host: str, username: str, port: int = 22,
                password_env: str = "", key_env: str = "", description: str = "") -> str:
        """Register/update an SSH host as DATA (no redeploy). Auth via EITHER
        password_env (name of a vault secret with the password) OR key_env (name of
        a vault secret holding an unencrypted private key). The host must be inside
        INTERNAL_ALLOW_CIDRS if it's a private/LAN IP."""
        try:
            SSH_DIR.mkdir(parents=True, exist_ok=True)
            cfg = {"name": name, "host": host, "port": int(port), "username": username,
                   "password_env": password_env, "key_env": key_env, "description": description}
            _cfg_path(name).write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            need = key_env or password_env
            note = f" — set the credential: secret_set('{need}', <value>)" if need and not secrets_store.get_secret(need) else ""
            return f"Registered SSH host '{_slug(name)}' ({username}@{host}:{port}).{note}"
        except Exception as exc:
            return f"Could not register host: {exc}"

    @mcp.tool
    def ssh_list() -> str:
        """List registered SSH hosts (name — user@host:port — description)."""
        if not SSH_DIR.exists() or not any(SSH_DIR.glob("*.json")):
            return "No SSH hosts yet. Use ssh_add."
        out = []
        for p in sorted(SSH_DIR.glob("*.json")):
            try:
                c = json.loads(p.read_text(encoding="utf-8"))
                out.append(f"- {p.stem} — {c.get('username')}@{c.get('host')}:{c.get('port')} — {c.get('description','')}")
            except Exception:
                out.append(f"- {p.stem} — (unreadable)")
        return "\n".join(out)

    @mcp.tool
    def ssh_delete_endpoint(name: str) -> str:
        """Remove a registered SSH host by name."""
        p = _cfg_path(name)
        if p.exists():
            p.unlink()
            return f"Deleted SSH host '{_slug(name)}'."
        return f"No SSH host '{name}'."

    @mcp.tool
    def ssh_run(host: str, command: str, timeout: int = 60) -> str:
        """Run a command on a registered SSH host; returns exit code + stdout/stderr.
        STATE-CHANGING for anything that modifies the remote — confirm with the user
        before running such commands."""
        cfg = _load(host)
        if not cfg:
            return f"Unknown SSH host '{host}'. Use ssh_list / ssh_add."
        cli, err = _connect(cfg)
        if err:
            return err
        try:
            _in, out, errs = cli.exec_command(command, timeout=int(timeout))
            rc = out.channel.recv_exit_status()
            so = out.read().decode("utf-8", "replace")
            se = errs.read().decode("utf-8", "replace")
        except Exception as exc:
            cli.close()
            return f"Command failed: {exc}"
        cli.close()
        body = so + (("\n[stderr]\n" + se) if se.strip() else "")
        if len(body) > 6000:
            body = body[:6000] + "\n…(truncated)"
        return f"exit {rc}\n{body}".rstrip()

    @mcp.tool
    def ssh_upload(host: str, source: str, dest: str) -> str:
        """Upload a local file (under /data) to the SSH host at remote path `dest` (SFTP)."""
        cfg = _load(host)
        if not cfg:
            return f"Unknown SSH host '{host}'."
        src = _safe_under_data(source)
        if not str(src).startswith(str(DATA_ROOT)):
            return "Source must be under /data."
        if not src.is_file():
            return f"No file at '{src}'."
        cli, err = _connect(cfg)
        if err:
            return err
        try:
            sftp = cli.open_sftp()
            sftp.put(str(src), dest)
            sftp.close()
        except Exception as exc:
            cli.close()
            return f"Upload failed: {exc}"
        cli.close()
        return f"Uploaded {src.name} → {dest} ({src.stat().st_size} B)."

    @mcp.tool
    def ssh_download(host: str, path: str, dest: str = "") -> str:
        """Download a remote file (SFTP) from the SSH host into /data/work."""
        cfg = _load(host)
        if not cfg:
            return f"Unknown SSH host '{host}'."
        cli, err = _connect(cfg)
        if err:
            return err
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", dest or path.rstrip("/").split("/")[-1] or "download")
        out_path = WORK_DIR / name
        try:
            sftp = cli.open_sftp()
            sftp.get(path, str(out_path))
            sftp.close()
        except Exception as exc:
            cli.close()
            return f"Download failed: {exc}"
        cli.close()
        return f"Downloaded {path} → {out_path} ({out_path.stat().st_size} B)."

    @mcp.tool
    def ssh_list_dir(host: str, path: str = ".") -> str:
        """List a remote directory on the SSH host (SFTP)."""
        cfg = _load(host)
        if not cfg:
            return f"Unknown SSH host '{host}'."
        cli, err = _connect(cfg)
        if err:
            return err
        try:
            sftp = cli.open_sftp()
            entries = sftp.listdir_attr(path)
            sftp.close()
        except Exception as exc:
            cli.close()
            return f"List failed: {exc}"
        cli.close()
        if not entries:
            return "(empty)"
        import stat as _stat
        out = []
        for e in sorted(entries, key=lambda a: a.filename):
            is_dir = _stat.S_ISDIR(e.st_mode) if e.st_mode else False
            out.append(f"- {e.filename}{'/' if is_dir else ''} · {e.st_size} B")
        return "\n".join(out)
