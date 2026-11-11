"""IMAP tools (imap_tools): registration, connect/login, read-only search and fetch,
attachment saving into the work dir.

imaplib is replaced by a scripted fake server and DNS by a scripted resolver (no
network). Proves: the password comes from the vault by NAME and never leaks; the
host passes netguard before AND at connect time; the TLS context follows
security / tls_insecure; mailboxes are opened READ-ONLY and messages fetched with
BODY.PEEK (never marked seen); the search term cannot inject IMAP tokens and the
uid must be numeric; attachments are saved only inside the work dir with
sanitised names and a size cap; failures are readable and the session is closed.
"""
import asyncio
import imaplib
import json
import socket
import ssl
import sys
from email.message import EmailMessage

import pytest
from cryptography.fernet import Fernet
from fastmcp import Client

LAN = "192.0.2.0/24,198.51.100.0/24,203.0.113.0/24"
IMAPError = imaplib.IMAP4.error  # kept before the fake replaces imaplib.IMAP4


def _call(server, tool, args=None):
    async def go():
        async with Client(server.mcp) as client:
            res = await client.call_tool(tool, args or {}, raise_on_error=False)
            return res.content[0].text if res.content else ""
    return asyncio.run(go())


def _fake_dns(monkeypatch, table):
    """host -> [ips] (static) or [[ips], [ips], ...] (successive answers, last repeats)."""
    seen = {}

    def resolver(host, port=None, *args, **kwargs):
        entry = table.get(host)
        if entry is None:
            raise socket.gaierror(-2, "Name or service not known")
        n = seen.get(host, 0)
        seen[host] = n + 1
        ips = entry[min(n, len(entry) - 1)] if isinstance(entry[0], (list, tuple)) else entry
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0)) for ip in ips]
    monkeypatch.setattr(socket, "getaddrinfo", resolver)


class _World:
    def __init__(self):
        self.sessions, self.init_hook = [], None
        self.login_error = self.select_error = self.fetch_error = self.logout_error = None
        self.search = ("OK", [b""])
        self.fetches = {}  # message id (str) -> imaplib-style fetch response


class _IMAP:
    def __init__(self, world, kind, host, port, timeout, ssl_context=None):
        if world.init_hook:
            world.init_hook(host, port)
        self.world, self.kind, self.host, self.port = world, kind, host, port
        self.timeout, self.ssl_context, self.starttls_context = timeout, ssl_context, None
        self.logins, self.selected, self.searched, self.fetched = [], [], [], []
        self.logged_out = False
        world.sessions.append(self)

    def starttls(self, ssl_context=None):
        self.starttls_context = ssl_context

    def login(self, user, password):
        if self.world.login_error:
            raise self.world.login_error
        self.logins.append((user, password))

    def select(self, mailbox, readonly=False):
        if self.world.select_error:
            raise self.world.select_error
        self.selected.append((mailbox, readonly))

    def search(self, charset, *criteria):
        self.searched.append(criteria)
        return self.world.search

    def fetch(self, mid, spec):
        if self.world.fetch_error:
            raise self.world.fetch_error
        mid = mid.decode() if isinstance(mid, bytes) else mid
        self.fetched.append((mid, spec))
        return self.world.fetches.get(mid, ("NO", [None]))

    def logout(self):
        self.logged_out = True
        if self.world.logout_error:
            raise self.world.logout_error


@pytest.fixture
def world(monkeypatch):
    w = _World()
    monkeypatch.setattr(imaplib, "IMAP4", lambda host, port, timeout=None: _IMAP(w, "plain", host, port, timeout))
    monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda host, port, timeout=None, ssl_context=None:
                        _IMAP(w, "ssl", host, port, timeout, ssl_context))
    return w


@pytest.fixture
def srv(fresh_server, monkeypatch, world):
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", LAN)
    _fake_dns(monkeypatch, {"imap.example.com": ["203.0.113.30"], "mail.example.net": ["10.0.0.30"]})
    return fresh_server


def _add(srv, name="inbox", **extra):
    args = {"name": name, "host": "imap.example.com", "username": "alice", **extra}
    return _call(srv, "imap_add", args)


def _vault(name="IMAP_PW", value="test-password"):
    # Stored with its host, as a person does (only a person binds a secret, 2.6.0).
    sys.modules["secrets_store"].vault_set(name, value, hosts=["imap.example.com", "mail.example.net"])


def _header_response(uid, size, subject, sender="bob@example.com", date="Mon, 03 Nov 2025 10:00:00 +0000"):
    head = f"From: {sender}\r\nSubject: {subject}\r\nDate: {date}\r\n\r\n".encode()
    meta = f"{uid} (RFC822.SIZE {size} BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {{{len(head)}}}".encode()
    return "OK", [(meta, head), b")"]


def _full(msg):
    raw = msg.as_bytes()
    return "OK", [(f"7 (BODY[] {{{len(raw)}}}".encode(), raw), b")"]


def _mail(body="Hello Alice", subject="Invoice", **attachments):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "Bob <bob@example.com>", "alice@example.com", subject
    m["Date"] = "Mon, 03 Nov 2025 10:00:00 +0000"
    m.set_content(body)
    for filename, data in attachments.items():
        m.add_attachment(data, maintype="application", subtype="pdf", filename=filename)
    return m


def test_imap_add_stores_the_secret_name_only_and_normalises(srv, tmp_path):
    out = _add(srv, name="Work Mail", password_env="IMAP_PW", security="STARTTLS", port=143)
    assert "Registered IMAP account 'work-mail' (alice @ imap.example.com)" in out
    assert "secret_set('IMAP_PW'" in out
    cfg = json.loads((tmp_path / "imap_dir" / "work-mail.json").read_text())
    assert cfg["security"] == "starttls" and cfg["port"] == 143 and cfg["password_env"] == "IMAP_PW"
    _vault()
    assert "secret_set" not in _add(srv, name="Work Mail", password_env="IMAP_PW")
    _add(srv, name="Work Mail", description="updated")  # unnamed fields are kept
    cfg = json.loads((tmp_path / "imap_dir" / "work-mail.json").read_text())
    assert cfg["description"] == "updated" and cfg["port"] == 143
    assert "test-password" not in json.dumps(cfg)


def test_imap_add_reports_a_storage_failure(srv, monkeypatch):
    def boom(*a, **k):
        raise OSError("read-only")
    monkeypatch.setattr(sys.modules["imap_tools"].cfgstore, "write_merged", boom)
    assert "Could not register account: read-only" in _add(srv)


def test_imap_list_and_delete_roundtrip(srv, tmp_path):
    assert "No IMAP accounts yet" in _call(srv, "imap_list")
    _add(srv, name="a")
    (tmp_path / "imap_dir" / "broken.json").write_text("{nope")
    listing = _call(srv, "imap_list")
    assert "- a — alice@imap.example.com" in listing and "- broken — (unreadable)" in listing
    assert "Deleted IMAP account 'a'" in _call(srv, "imap_delete_account", {"name": "a"})
    assert "No IMAP account 'a'" in _call(srv, "imap_delete_account", {"name": "a"})


@pytest.mark.parametrize("query,expected", [
    ("unseen", ("UNSEEN",)), ("ALL", ("ALL",)), ("", ("UNSEEN",)), (None, ("UNSEEN",)),
    ("invoice", ("OR", "SUBJECT", '"invoice"', "FROM", '"invoice"')),
    ('x" ALL "', ("OR", "SUBJECT", '"x ALL "', "FROM", '"x ALL "')),  # quotes cannot break out
    ("a\r\nb", ("OR", "SUBJECT", '"ab"', "FROM", '"ab"')),
])
def test_search_criteria_cannot_inject_tokens(srv, query, expected):
    assert sys.modules["imap_tools"]._criteria(query) == expected


def test_header_decoding_handles_rfc2047_empty_and_broken_values(srv, monkeypatch):
    tools = sys.modules["imap_tools"]
    assert tools._dec("=?UTF-8?Q?Gr=C3=BC=C3=9Fe?=") == "Grüße"
    assert tools._dec(None) == "" and tools._dec("plain") == "plain"
    monkeypatch.setattr(tools, "make_header", lambda *a: (_ for _ in ()).throw(ValueError("bad")))
    assert tools._dec("=?x?q?y?=") == "=?x?q?y?="


def test_connect_defaults_to_verified_imaps_with_the_vault_password(srv, world):
    _vault()
    _add(srv, password_env="IMAP_PW")
    world.search = ("OK", [b""])
    out = _call(srv, "imap_search", {"account": "inbox"})
    (s,) = world.sessions
    assert (s.kind, s.host, s.port, s.timeout) == ("ssl", "imap.example.com", 993, 30)
    assert s.ssl_context.verify_mode == ssl.CERT_REQUIRED and s.ssl_context.check_hostname
    assert s.logins == [("alice", "test-password")] and "test-password" not in out


def test_starttls_uses_port_143_and_upgrades_with_the_shared_tls_policy(srv, world):
    _add(srv, security="starttls", tls_insecure=True)
    _call(srv, "imap_search", {"account": "inbox"})
    (s,) = world.sessions
    assert (s.kind, s.port) == ("plain", 143)
    assert s.starttls_context.verify_mode == ssl.CERT_NONE and s.starttls_context.check_hostname is False
    _add(srv, name="custom", security="ssl", port=1993)
    _call(srv, "imap_search", {"account": "custom"})
    assert world.sessions[1].port == 1993


def test_missing_password_secret_stops_before_connecting(srv, world):
    _add(srv, password_env="IMAP_PW")
    assert _call(srv, "imap_search", {"account": "inbox"}) == "Account needs secret 'IMAP_PW'. Use secret_set."
    assert world.sessions == []


def test_login_failure_is_reported_without_the_password(srv, world):
    _vault()
    _add(srv, password_env="IMAP_PW")
    world.login_error = IMAPError("LOGIN failed")
    out = _call(srv, "imap_fetch", {"account": "inbox", "uid": "1"})
    assert out == "IMAP connect/login failed: LOGIN failed" and "test-password" not in out


def test_unreadable_ca_bundle_never_connects(srv, world, tmp_path):
    _add(srv, ca_bundle=str(tmp_path / "missing.pem"))
    out = _call(srv, "imap_search", {"account": "inbox"})
    assert world.sessions == []  # fails closed, never falls back to unverified TLS
    assert "No such file" in out or "IMAP connect/login failed" in out


def test_private_host_is_blocked_unless_allow_listed(srv, world, monkeypatch):
    _call(srv, "imap_add", {"name": "lan", "host": "mail.example.net", "username": "alice"})
    out = _call(srv, "imap_search", {"account": "lan"})
    assert out.startswith("Blocked by network policy") and "10.0.0.30" in out
    assert world.sessions == []
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "10.0.0.0/24")
    assert _call(srv, "imap_search", {"account": "lan"}).startswith("No messages match")


def test_connect_time_guard_stops_dns_rebinding(srv, world, monkeypatch):
    _fake_dns(monkeypatch, {"imap.example.com": [["203.0.113.30"], ["10.0.0.9"]]})
    world.init_hook = lambda host, port: socket.getaddrinfo(host, port)
    _add(srv)
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "203.0.113.0/24")
    out = _call(srv, "imap_search", {"account": "inbox"})
    assert out.startswith("IMAP connect/login failed: netguard:") and world.sessions == []


def test_search_lists_newest_first_capped_by_limit_and_decodes_headers(srv, world):
    _add(srv)
    world.search = ("OK", [b"1 2 3"])
    world.fetches = {"3": _header_response(103, 5000, "=?UTF-8?Q?Gr=C3=BC=C3=9Fe?="),
                     "2": _header_response(102, 2048, ""), "1": _header_response(101, 10, "old")}
    out = _call(srv, "imap_search", {"account": "inbox", "mailbox": "Archive", "query": "all", "limit": 2})
    lines = out.splitlines()
    assert lines[0].startswith("- 103 · Mon, 03 Nov 2025 · bob@example.com · Grüße · 4KB")
    assert lines[1].endswith("(no subject) · 2KB") and len(lines) == 2
    (s,) = world.sessions
    assert s.selected == [("Archive", True)] and s.searched == [("ALL",)]
    assert all("BODY.PEEK[HEADER.FIELDS" in spec for _, spec in s.fetched) and s.logged_out


def test_search_limit_below_one_still_returns_the_newest_message(srv, world):
    _add(srv)
    world.search = ("OK", [b"1 2"])
    world.fetches = {"2": _header_response(102, 1, "newest"), "1": _header_response(101, 1, "older")}
    out = _call(srv, "imap_search", {"account": "inbox", "limit": 0, "query": "invoice"})
    assert out.count("\n") == 0 and "newest" in out
    assert world.sessions[0].searched == [("OR", "SUBJECT", '"invoice"', "FROM", '"invoice"')]


def test_search_without_hits_or_with_unusable_fetches(srv, world):
    _add(srv)
    assert _call(srv, "imap_search", {"account": "inbox", "query": "unseen"}) == "No messages match 'unseen' in INBOX."
    world.search = ("OK", [None])
    assert "No messages match" in _call(srv, "imap_search", {"account": "inbox"})
    world.search = ("OK", [b"5"])  # fetch answers NO -> skipped, nothing to show
    assert "No messages match" in _call(srv, "imap_search", {"account": "inbox"})
    world.search = ("NO", [b"bad mailbox"])
    assert _call(srv, "imap_search", {"account": "inbox"}).startswith("Search failed (NO)")


def test_search_without_size_metadata_shows_a_question_mark(srv, world):
    _add(srv)
    world.search = ("OK", [b"4"])
    head = b"From: bob@example.com\r\nSubject: s\r\n\r\n"
    world.fetches = {"4": ("OK", [(b"4 (BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {5}", head), b")"])}
    assert _call(srv, "imap_search", {"account": "inbox"}).startswith("- 4 ·") and "· ?" in _call(
        srv, "imap_search", {"account": "inbox"})


def test_search_errors_are_reported_and_the_session_still_closes(srv, world):
    _add(srv)
    world.select_error = IMAPError("no such mailbox")
    world.logout_error = OSError("connection reset")
    assert _call(srv, "imap_search", {"account": "inbox"}) == "Search failed: no such mailbox"
    assert world.sessions[0].logged_out  # logout was attempted; its own failure is swallowed


@pytest.mark.parametrize("uid", ["abc", "1 ALL", "1:*", "", " "])
def test_fetch_requires_a_numeric_uid(srv, world, uid):
    _add(srv)
    assert _call(srv, "imap_fetch", {"account": "inbox", "uid": uid}) == "uid must be the numeric id from imap_search."
    assert world.sessions == []


def test_unknown_or_corrupt_account_is_refused(srv, world, tmp_path):
    (tmp_path / "imap_dir").mkdir()
    (tmp_path / "imap_dir" / "bad.json").write_text("{nope")
    for name in ("x", "bad"):
        assert f"Unknown IMAP account '{name}'" in _call(srv, "imap_search", {"account": name})
        assert f"Unknown IMAP account '{name}'" in _call(srv, "imap_fetch", {"account": name, "uid": "1"})
    assert world.sessions == []


def test_fetch_returns_headers_and_body_read_only(srv, world):
    _add(srv)
    world.fetches = {"7": _full(_mail())}
    out = _call(srv, "imap_fetch", {"account": "inbox", "uid": " 7 ", "mailbox": "Sent"})
    assert out.startswith("From: Bob <bob@example.com>\nTo: alice@example.com\n")
    assert "Subject: Invoice" in out and "Hello Alice" in out and "Attachments" not in out
    (s,) = world.sessions
    assert s.selected == [("Sent", True)] and s.fetched == [("7", "(BODY.PEEK[])")] and s.logged_out


def test_fetch_of_a_missing_message_and_fetch_errors(srv, world):
    _add(srv)
    assert _call(srv, "imap_fetch", {"account": "inbox", "uid": "9"}) == "No message with uid 9 in INBOX."
    world.fetch_error = OSError("socket closed")
    assert _call(srv, "imap_fetch", {"account": "inbox", "uid": "9"}) == "Fetch failed: socket closed"
    assert all(s.logged_out for s in world.sessions)
    world.logout_error = OSError("connection reset")  # a failing logout must not mask the result
    assert _call(srv, "imap_fetch", {"account": "inbox", "uid": "9"}) == "Fetch failed: socket closed"


def test_fetch_truncates_long_bodies_and_decodes_the_charset(srv, world, monkeypatch):
    _add(srv)
    monkeypatch.setattr(sys.modules["imap_tools"], "_MAX_BODY", 10)
    m = EmailMessage()
    m["Subject"] = "Long"
    m.set_content("Grüße " + "x" * 50, charset="iso-8859-1")
    world.fetches = {"7": _full(m)}
    out = _call(srv, "imap_fetch", {"account": "inbox", "uid": "7"})
    assert "Grüße xxxx" in out and out.endswith("…(truncated at 10B)")


def test_attachments_are_listed_but_not_saved_unless_requested(srv, world, tmp_path):
    _add(srv)
    world.fetches = {"7": _full(_mail(**{"invoice.pdf": b"%PDF-data"}))}
    out = _call(srv, "imap_fetch", {"account": "inbox", "uid": "7"})
    assert "Attachments: invoice.pdf (call again with save_attachments=true to save)" in out
    assert not (tmp_path / "work_dir").exists()


def test_saved_attachments_land_in_the_work_dir_with_sanitised_names(srv, world, tmp_path):
    _add(srv)
    world.fetches = {"7": _full(_mail(**{"../../etc/evil name.pdf": b"EVIL", "ok.pdf": b"fine"}))}
    out = _call(srv, "imap_fetch", {"account": "inbox", "uid": "7", "save_attachments": True})
    work = tmp_path / "work_dir"
    assert sorted(p.name for p in work.iterdir()) == ["_.._etc_evil_name.pdf", "ok.pdf"]  # no hidden name
    assert (work / "ok.pdf").read_bytes() == b"fine"
    assert "Saved to /data/work: _.._etc_evil_name.pdf (4B), ok.pdf (4B)" in out
    assert not (tmp_path / "etc").exists()


def test_oversized_attachment_is_skipped(srv, world, tmp_path, monkeypatch):
    _add(srv)
    monkeypatch.setattr(sys.modules["imap_tools"], "_MAX_ATTACH", 5)
    world.fetches = {"7": _full(_mail(**{"big.pdf": b"0123456789"}))}
    out = _call(srv, "imap_fetch", {"account": "inbox", "uid": "7", "save_attachments": True})
    assert "big.pdf (skipped, >5B)" in out and not (tmp_path / "work_dir" / "big.pdf").exists()


def test_attachment_named_like_a_directory_does_not_escape_the_work_dir(srv, world, tmp_path):
    _add(srv)
    world.fetches = {"7": _full(_mail(**{"..": b"x"}))}
    out = _call(srv, "imap_fetch", {"account": "inbox", "uid": "7", "save_attachments": True})
    assert not out.startswith("Fetch failed:") and (tmp_path / "work_dir" / "attachment").read_bytes() == b"x"
