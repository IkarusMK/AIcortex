"""SMTP — send email/notifications (mail accounts as DATA).

Register an SMTP account once (host + an app-password secret in the vault), then
send mail with mail_send — reports, "print finished", a scan summary, etc.
Optionally attach a file from /data. Sending is an OUTBOUND action: confirm with
the user before sending on their behalf.

Stdlib only (smtplib + email). The SMTP host passes the SSRF egress guard.
"""
import json

import cfgstore
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

# Max attachment size accepted (default 25 MB).
_MAX_ATTACH = int(os.environ.get("MAIL_MAX_ATTACH_BYTES", str(25_000_000)))


def _recipient_allowed(rcpt: str, allow: list) -> bool:
    """A recipient is allowed if it matches an allow entry: exact address,
    a '@domain' suffix, or a bare 'domain'."""
    r = rcpt.lower()
    for a in allow:
        a = a.lower()
        if a == r:
            return True
        if a.startswith("@") and r.endswith(a):
            return True
        if "@" not in a and r.endswith("@" + a):
            return True
    return False


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
                 tls_insecure: bool = False, ca_bundle: str = "", description: str = "") -> str:
        """Register/update an SMTP account as DATA (no redeploy). security:
        "starttls" (587) | "ssl" (465) | "none" (25). password_env = NAME of a vault
        secret with the (app) password. from_addr = the From address. TLS is VERIFIED
        by default; for a self-signed LAN server point `ca_bundle` at its CA/cert (the
        safe way) or set tls_insecure=true."""
        try:
            MAIL_DIR.mkdir(parents=True, exist_ok=True)
            cfg = {"name": name, "host": host, "port": int(port), "from_addr": from_addr,
                   "username": username, "password_env": password_env,
                   "tls_insecure": bool(tls_insecure), "ca_bundle": ca_bundle.strip(),
                   "security": (security or "starttls").lower(), "description": description}
            cfgstore.write_merged(_cfg_path(name), cfg)
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
            # Ancestry check, not a string prefix: startswith("/data") would also
            # match a sibling like /data-backup and let a path escape the sandbox.
            if ap != DATA_ROOT and DATA_ROOT not in ap.parents:
                return "Attachment must be under /data."
            if not ap.is_file():
                return f"No attachment file at '{ap}'."
            if ap.stat().st_size > _MAX_ATTACH:
                return f"Refused: attachment is {ap.stat().st_size} bytes, over the {_MAX_ATTACH}-byte limit."
            maintype, subtype = _TYPES.get(ap.suffix.lower().lstrip("."), ("application", "octet-stream"))
            msg.add_attachment(ap.read_bytes(), maintype=maintype, subtype=subtype, filename=ap.name)

        rcpts = [a.strip() for a in (to + ("," + cc if cc else "")).split(",") if a.strip()]
        # #11: optional recipient allow-list (server-side policy). Unset = unrestricted.
        raw_allow = os.environ.get("MAIL_ALLOWED_RECIPIENTS", "").strip()
        if raw_allow:
            allow = [x for x in re.split(r"[,\s]+", raw_allow) if x]
            bad = [r for r in rcpts if not _recipient_allowed(r, allow)]
            if bad:
                return ("Refused: recipient(s) not allowed by MAIL_ALLOWED_RECIPIENTS — "
                        f"{', '.join(bad)}. Ask the operator to permit them.")
        sec = cfg.get("security", "starttls")
        port = int(cfg.get("port", 587))
        try:
            # guard() wraps the actual connect so the SSRF egress policy is
            # re-applied at resolve time (anti DNS-rebinding), not just at the
            # check_host() preflight above.
            ctx = netguard.ssl_context(cfg)  # ca_bundle > tls_insecure > verify (shared TLS policy)
            with netguard.guard(host):
                if sec == "ssl":
                    server = smtplib.SMTP_SSL(host, port, timeout=30, context=ctx)
                else:
                    server = smtplib.SMTP(host, port, timeout=30)
                with server:
                    if sec == "starttls":
                        server.starttls(context=ctx)
                    if cfg.get("username") and cfg.get("password_env"):
                        pw = secrets_store.get_secret(cfg["password_env"])
                        if not pw:
                            return f"Account needs secret '{cfg['password_env']}'. Use secret_set."
                        server.login(cfg["username"], pw)
                    server.send_message(msg, from_addr=cfg.get("from_addr"), to_addrs=rcpts)
        except Exception as exc:
            return f"Send failed: {exc}"
        return f"Sent '{subject}' to {', '.join(rcpts)}" + (f" with attachment {Path(attachment).name}" if attachment else "") + "."
