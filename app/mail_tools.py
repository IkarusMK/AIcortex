"""SMTP — send email/notifications (mail accounts as DATA).

Register an SMTP account once (host + an app-password secret in the vault), then
send mail with mail_send — reports, "print finished", a scan summary, etc.
Optionally attach a file from /data. Sending is an OUTBOUND action: confirm with
the user before sending on their behalf.

Stdlib only (smtplib + email). The SMTP host passes the SSRF egress guard.
"""
import json
import os
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path

import netguard
import secrets_store

MAIL_DIR = Path(os.environ.get("MAIL_DIR", "/data/mail"))
DATA_ROOT = Path(os.environ.get("DATA_ROOT", "/data")).resolve()

_TYPES = {"pdf": ("application", "pdf"), "png": ("image", "png"),
          "jpg": ("image", "jpeg"), "jpeg": ("image", "jpeg"),
          "txt": ("text", "plain"), "csv": ("text", "csv")}


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:60] or "mail"


def _cfg_path(name: str) -> Path:
    return MAIL_DIR / f"{_slug(name)}.json"


def _load(name: str):
    p = _cfg_path(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def register(mcp):
    @mcp.tool
    def mail_add(name: str, host: str, from_addr: str, port: int = 587,
                 username: str = "", password_env: str = "", security: str = "starttls",
                 description: str = "") -> str:
        """Register/update an SMTP account as DATA (no redeploy). security:
        "starttls" (587) | "ssl" (465) | "none" (25). password_env = NAME of a vault
        secret with the (app) password. from_addr = the From address."""
        try:
            MAIL_DIR.mkdir(parents=True, exist_ok=True)
            cfg = {"name": name, "host": host, "port": int(port), "from_addr": from_addr,
                   "username": username, "password_env": password_env,
                   "security": (security or "starttls").lower(), "description": description}
            _cfg_path(name).write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            note = ""
            if password_env and not secrets_store.get_secret(password_env):
                note = f" — set the password: secret_set('{password_env}', <value>)"
            return f"Registered SMTP account '{_slug(name)}' ({from_addr} via {host}:{port}).{note}"
        except Exception as exc:
            return f"Could not register account: {exc}"

    @mcp.tool
    def mail_list() -> str:
        """List SMTP accounts (name — from — host:port)."""
        if not MAIL_DIR.exists() or not any(MAIL_DIR.glob("*.json")):
            return "No SMTP accounts yet. Use mail_add."
        out = []
        for p in sorted(MAIL_DIR.glob("*.json")):
            try:
                c = json.loads(p.read_text(encoding="utf-8"))
                out.append(f"- {p.stem} — {c.get('from_addr')} — {c.get('host')}:{c.get('port')}")
            except Exception:
                out.append(f"- {p.stem} — (unreadable)")
        return "\n".join(out)

    @mcp.tool
    def mail_delete_account(name: str) -> str:
        """Remove a registered SMTP account by name."""
        p = _cfg_path(name)
        if p.exists():
            p.unlink()
            return f"Deleted SMTP account '{_slug(name)}'."
        return f"No SMTP account '{name}'."

    @mcp.tool
    def mail_send(account: str, to: str, subject: str, body: str,
                  attachment: str = "", cc: str = "") -> str:
        """Send an email via a registered account. to/cc may be comma-separated.
        attachment = optional path under /data. OUTBOUND — confirm with the user first."""
        cfg = _load(account)
        if not cfg:
            return f"Unknown SMTP account '{account}'. Use mail_list / mail_add."
        host = cfg["host"]
        ok, reason = netguard.check_host(host)
        if not ok:
            return f"Blocked by network policy — {reason}"

        msg = EmailMessage()
        msg["From"] = cfg.get("from_addr")
        msg["To"] = to
        if cc:
            msg["Cc"] = cc
        msg["Subject"] = subject
        msg.set_content(body or "")

        if attachment:
            ap = Path(attachment)
            if not ap.is_absolute():
                ap = DATA_ROOT / attachment
            ap = ap.resolve()
            if not str(ap).startswith(str(DATA_ROOT)):
                return "Attachment must be under /data."
            if not ap.is_file():
                return f"No attachment file at '{ap}'."
            maintype, subtype = _TYPES.get(ap.suffix.lower().lstrip("."), ("application", "octet-stream"))
            msg.add_attachment(ap.read_bytes(), maintype=maintype, subtype=subtype, filename=ap.name)

        rcpts = [a.strip() for a in (to + ("," + cc if cc else "")).split(",") if a.strip()]
        sec = cfg.get("security", "starttls")
        port = int(cfg.get("port", 587))
        try:
            if sec == "ssl":
                server = smtplib.SMTP_SSL(host, port, timeout=30)
            else:
                server = smtplib.SMTP(host, port, timeout=30)
            with server:
                if sec == "starttls":
                    server.starttls()
                if cfg.get("username") and cfg.get("password_env"):
                    pw = secrets_store.get_secret(cfg["password_env"])
                    if not pw:
                        return f"Account needs secret '{cfg['password_env']}'. Use secret_set."
                    server.login(cfg["username"], pw)
                server.send_message(msg, from_addr=cfg.get("from_addr"), to_addrs=rcpts)
        except Exception as exc:
            return f"Send failed: {exc}"
        return f"Sent '{subject}' to {', '.join(rcpts)}" + (f" with attachment {Path(attachment).name}" if attachment else "") + "."
