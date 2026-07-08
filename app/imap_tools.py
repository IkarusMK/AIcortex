"""IMAP — read incoming email (mail accounts as DATA).

The read-side counterpart of mail_tools.py (SMTP). Register an IMAP account once
(host + an app-password secret in the vault), then list / search / fetch messages:
"any new orders?", "read the last mail from the landlord", pull an attachment into
/data/work for printing or archiving.

Stdlib only (imaplib + email). Reads are done READ-ONLY (BODY.PEEK / readonly
select) so fetching a message never marks it seen. The IMAP host passes the SSRF
egress guard and the connect is wrapped in netguard.guard() (anti DNS-rebinding),
matching the SMTP hardening.
"""
import email
import imaplib
import json
import os
import re
from email.header import decode_header, make_header
from pathlib import Path

import cfgstore
import netguard
import secrets_store

IMAP_DIR = Path(os.environ.get("IMAP_DIR", "/data/imap"))
WORK_DIR = Path(os.environ.get("WORK_DIR", "/data/work"))

# Message states IMAP understands directly; anything else is treated as a text term.
_STATES = {"all", "unseen", "seen", "recent", "flagged", "unflagged", "answered"}
# Max bytes of a text body returned inline by imap_fetch (keep the context small).
_MAX_BODY = int(os.environ.get("IMAP_MAX_BODY_BYTES", str(40_000)))
_MAX_ATTACH = int(os.environ.get("IMAP_MAX_ATTACH_BYTES", str(25_000_000)))


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:60] or "imap"


def _cfg_path(name: str) -> Path:
    return IMAP_DIR / f"{_slug(name)}.json"


def _load(name: str):
    p = _cfg_path(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _dec(raw) -> str:
    """Decode a possibly RFC 2047-encoded header to a plain string."""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw)


def _open(cfg):
    """Connect + login. Returns (imap, None) or (None, error_string). The connect is
    inside netguard.guard() so the egress policy is re-applied at resolve time."""
    host = cfg.get("host", "")
    port = int(cfg.get("port", 0))
    sec = (cfg.get("security", "ssl") or "ssl").lower()
    ok, reason = netguard.check_host(host)
    if not ok:
        return None, f"Blocked by network policy — {reason}"
    pw = ""
    if cfg.get("password_env"):
        pw = secrets_store.get_secret(cfg["password_env"])
        if not pw:
            return None, f"Account needs secret '{cfg['password_env']}'. Use secret_set."
    try:
        with netguard.guard(host):
            if sec == "starttls":
                M = imaplib.IMAP4(host, port or 143, timeout=30)
                M.starttls()
            else:
                M = imaplib.IMAP4_SSL(host, port or 993, timeout=30)
        M.login(cfg.get("username", ""), pw)
    except Exception as exc:
        return None, f"IMAP connect/login failed: {exc}"
    return M, None


def _criteria(query: str):
    """Map a friendly query to IMAP SEARCH criteria args."""
    q = (query or "unseen").strip()
    if q.lower() in _STATES:
        return (q.upper(),)
    # free text → subject OR from (quoted). Strip quotes/CR/LF so the term can't
    # break out of the quoted string and inject extra SEARCH tokens (IMAP injection).
    q = re.sub(r'["\r\n]', "", q)
    return ("OR", "SUBJECT", f'"{q}"', "FROM", f'"{q}"')


def register(mcp):
    @mcp.tool
    def imap_add(name: str, host: str, username: str, password_env: str = "",
                 port: int = 0, security: str = "ssl", description: str = "") -> str:
        """Register/update an IMAP account as DATA (no redeploy). security: "ssl"
        (993, default) | "starttls" (143). password_env = NAME of a vault secret with
        the (app) password. Leave port=0 to use the default for the security mode."""
        try:
            IMAP_DIR.mkdir(parents=True, exist_ok=True)
            cfg = {"name": name, "host": host, "port": int(port), "username": username,
                   "password_env": password_env,
                   "security": (security or "ssl").lower(), "description": description}
            cfgstore.write_merged(_cfg_path(name), cfg)
            note = ""
            if password_env and not secrets_store.get_secret(password_env):
                note = f" — set the password: secret_set('{password_env}', <value>)"
            return f"Registered IMAP account '{_slug(name)}' ({username} @ {host}).{note}"
        except Exception as exc:
            return f"Could not register account: {exc}"

    @mcp.tool
    def imap_list() -> str:
        """List IMAP accounts (name — user@host)."""
        if not IMAP_DIR.exists() or not any(IMAP_DIR.glob("*.json")):
            return "No IMAP accounts yet. Use imap_add."
        out = []
        for p in sorted(IMAP_DIR.glob("*.json")):
            try:
                c = json.loads(p.read_text(encoding="utf-8"))
                out.append(f"- {p.stem} — {c.get('username')}@{c.get('host')}")
            except Exception:
                out.append(f"- {p.stem} — (unreadable)")
        return "\n".join(out)

    @mcp.tool
    def imap_delete_account(name: str) -> str:
        """Remove a registered IMAP account by name."""
        p = _cfg_path(name)
        if p.exists():
            p.unlink()
            return f"Deleted IMAP account '{_slug(name)}'."
        return f"No IMAP account '{name}'."

    @mcp.tool
    def imap_search(account: str, mailbox: str = "INBOX", query: str = "unseen",
                    limit: int = 10) -> str:
        """List messages (newest first): uid · date · from · subject · size. query =
        a state (unseen | all | recent | seen | flagged | answered) OR free text
        (matched against Subject/From). Read-only — nothing is marked seen."""
        cfg = _load(account)
        if not cfg:
            return f"Unknown IMAP account '{account}'. Use imap_list / imap_add."
        M, err = _open(cfg)
        if err:
            return err
        try:
            M.select(mailbox, readonly=True)
            typ, data = M.search(None, *_criteria(query))
            if typ != "OK":
                return f"Search failed ({typ}). Check the mailbox name / query."
            ids = (data[0] or b"").split()
            if not ids:
                return f"No messages match '{query}' in {mailbox}."
            ids = ids[-max(1, int(limit)):][::-1]  # newest first, capped
            lines = []
            for i in ids:
                typ, md = M.fetch(
                    i, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)] RFC822.SIZE)")
                if typ != "OK" or not md or not isinstance(md[0], tuple):
                    continue
                head = email.message_from_bytes(md[0][1])
                meta = md[0][0].decode("ascii", "replace") if isinstance(md[0][0], bytes) else str(md[0][0])
                uidm = re.search(r"^(\d+)", meta.strip())
                sizem = re.search(r"RFC822\.SIZE (\d+)", meta)
                uid = uidm.group(1) if uidm else i.decode()
                size = f"{int(sizem.group(1))//1024}KB" if sizem else "?"
                lines.append(f"- {uid} · {_dec(head.get('Date'))[:16]} · "
                             f"{_dec(head.get('From'))[:40]} · "
                             f"{_dec(head.get('Subject')) or '(no subject)'} · {size}")
            return "\n".join(lines) or f"No messages match '{query}' in {mailbox}."
        except Exception as exc:
            return f"Search failed: {exc}"
        finally:
            try:
                M.logout()
            except Exception:
                pass

    @mcp.tool
    def imap_fetch(account: str, uid: str, mailbox: str = "INBOX",
                   save_attachments: bool = False) -> str:
        """Read one message by its uid (from imap_search): headers + text body
        (truncated). Read-only (BODY.PEEK) — it is NOT marked seen. Set
        save_attachments=true to write attachments into /data/work — image/PDF
        attachments can then be viewed with fs_view (vision, not just text)."""
        cfg = _load(account)
        if not cfg:
            return f"Unknown IMAP account '{account}'. Use imap_list / imap_add."
        if not str(uid).strip().isdigit():
            return "uid must be the numeric id from imap_search."
        M, err = _open(cfg)
        if err:
            return err
        try:
            M.select(mailbox, readonly=True)
            typ, md = M.fetch(str(uid).strip(), "(BODY.PEEK[])")
            if typ != "OK" or not md or not isinstance(md[0], tuple):
                return f"No message with uid {uid} in {mailbox}."
            msg = email.message_from_bytes(md[0][1])
            head = (f"From: {_dec(msg.get('From'))}\nTo: {_dec(msg.get('To'))}\n"
                    f"Date: {_dec(msg.get('Date'))}\n"
                    f"Subject: {_dec(msg.get('Subject')) or '(no subject)'}\n")
            body, atts = "", []
            for part in msg.walk():
                if part.is_multipart():
                    continue
                disp = str(part.get("Content-Disposition") or "")
                ctype = part.get_content_type()
                if "attachment" in disp.lower() or part.get_filename():
                    fn = _dec(part.get_filename()) or "attachment"
                    payload = part.get_payload(decode=True) or b""
                    atts.append((re.sub(r"[^A-Za-z0-9._-]+", "_", fn), payload))
                elif ctype == "text/plain" and not body:
                    body = (part.get_payload(decode=True) or b"").decode(
                        part.get_content_charset() or "utf-8", "replace")
            saved = []
            if save_attachments and atts:
                WORK_DIR.mkdir(parents=True, exist_ok=True)
                for fn, payload in atts:
                    if len(payload) > _MAX_ATTACH:
                        saved.append(f"{fn} (skipped, >{_MAX_ATTACH}B)")
                        continue
                    (WORK_DIR / fn).write_bytes(payload)
                    saved.append(f"{fn} ({len(payload)}B)")
            more = "" if len(body) <= _MAX_BODY else f"\n…(truncated at {_MAX_BODY}B)"
            att_line = ""
            if atts:
                names = ", ".join(fn for fn, _ in atts)
                att_line = f"\n\nAttachments: {names}" + (
                    f"\nSaved to /data/work: {', '.join(saved)}" if saved else
                    " (call again with save_attachments=true to save)")
            return f"{head}\n{body[:_MAX_BODY]}{more}{att_line}"
        except Exception as exc:
            return f"Fetch failed: {exc}"
        finally:
            try:
                M.logout()
            except Exception:
                pass
