"""SMTP tools (mail_tools): account registration, mail_send, attachments, the
recipient allow-list, TLS options and the SSRF guard.

smtplib is replaced by a recording fake and DNS by a scripted resolver (no
network). Proves: the SMTP password is resolved from the vault by NAME and never
stored or returned; the server passes netguard before AND at connect time; the
TLS context follows security / tls_insecure / ca_bundle; MAIL_ALLOWED_RECIPIENTS
refuses the whole send when any To/Cc address is outside the list; attachments
cannot come from outside the data root and are size-capped; header injection is
rejected.
"""
import asyncio
import datetime
import json
import smtplib
import socket
import ssl
import sys

import pytest
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastmcp import Client

LAN = "192.0.2.0/24,198.51.100.0/24,203.0.113.0/24"


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
        self.sessions, self.init_hook, self.send_error = [], None, None


class _Session:
    def __init__(self, world, kind, host, port, timeout, context=None):
        if world.init_hook:
            world.init_hook(host, port)
        self.world, self.kind, self.host, self.port = world, kind, host, port
        self.timeout, self.context = timeout, context
        self.starttls_context, self.logins, self.sent = None, [], []
        world.sessions.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.starttls_context = context

    def login(self, user, password):
        self.logins.append((user, password))

    def send_message(self, msg, from_addr=None, to_addrs=None):
        if self.world.send_error:
            raise self.world.send_error
        self.sent.append({"msg": msg, "from": from_addr, "to": to_addrs})


@pytest.fixture
def world(monkeypatch):
    w = _World()
    monkeypatch.setattr(smtplib, "SMTP", lambda host, port, timeout=None: _Session(w, "plain", host, port, timeout))
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda host, port, timeout=None, context=None:
                        _Session(w, "ssl", host, port, timeout, context))
    return w


@pytest.fixture
def srv(fresh_server, monkeypatch, world):
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", LAN)
    monkeypatch.delenv("MAIL_ALLOWED_RECIPIENTS", raising=False)
    _fake_dns(monkeypatch, {"smtp.example.com": ["203.0.113.25"], "relay.example.net": ["10.0.0.25"]})
    return fresh_server


def _add(srv, name="main", **extra):
    args = {"name": name, "host": "smtp.example.com", "from_addr": "noreply@example.com", **extra}
    return _call(srv, "mail_add", args)


def _send(srv, to="alice@example.com", **extra):
    return _call(srv, "mail_send", {"account": "main", "to": to, "subject": "Report", "body": "Hello", **extra})


def _sent(world):
    return [m for s in world.sessions for m in s.sent]


def _vault(name="SMTP_PW", value="test-password"):
    sys.modules["secrets_store"].vault_set(name, value)


def _ca_pem(path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mail.example.com")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(path)


def test_mail_add_stores_the_secret_name_only_and_normalises_options(srv, tmp_path):
    out = _add(srv, name="Team Mail", port=465, username="alice", password_env="SMTP_PW",
               security="SSL", ca_bundle="  /data/ca.pem  ")
    assert "Registered SMTP account 'team-mail' (noreply@example.com via smtp.example.com:465)" in out
    assert "secret_set('SMTP_PW'" in out
    cfg = json.loads((tmp_path / "mail_dir" / "team-mail.json").read_text())
    assert cfg["security"] == "ssl" and cfg["ca_bundle"] == "/data/ca.pem"
    assert cfg["password_env"] == "SMTP_PW" and "test-password" not in json.dumps(cfg)
    _vault()
    again = _add(srv, name="Team Mail", password_env="SMTP_PW")
    assert "secret_set" not in again and "test-password" not in again
    _add(srv, name="Team Mail", description="updated")  # unnamed fields are kept
    cfg = json.loads((tmp_path / "mail_dir" / "team-mail.json").read_text())
    assert cfg["description"] == "updated" and cfg["port"] == 465


def test_mail_add_reports_a_storage_failure(srv, monkeypatch):
    def boom(*a, **k):
        raise OSError("read-only")
    monkeypatch.setattr(sys.modules["mail_tools"].cfgstore, "write_merged", boom)
    assert "Could not register account: read-only" in _add(srv)


def test_mail_list_and_delete_roundtrip(srv, tmp_path):
    assert "No SMTP accounts yet" in _call(srv, "mail_list")
    _add(srv, name="a", port=25)
    (tmp_path / "mail_dir" / "broken.json").write_text("{nope")
    listing = _call(srv, "mail_list")
    assert "- a — noreply@example.com — smtp.example.com:25" in listing
    assert "- broken — (unreadable)" in listing
    assert "Deleted SMTP account 'a'" in _call(srv, "mail_delete_account", {"name": "a"})
    assert "No SMTP account 'a'" in _call(srv, "mail_delete_account", {"name": "a"})


@pytest.mark.parametrize("rcpt,allow,expected", [
    ("alice@example.com", ["alice@example.com"], True),
    ("ALICE@Example.com", ["alice@EXAMPLE.com"], True),
    ("bob@example.com", ["alice@example.com"], False),
    ("bob@example.com", ["@example.com"], True),
    ("bob@example.com", ["example.com"], True),
    ("bob@sub.example.com", ["example.com"], False),
    ("bob@evil-example.com", ["example.com"], False),
    ("bob@evil-example.com", ["@example.com"], False),
    ("bob@example.com", [], False),
])
def test_recipient_allow_list_matching(srv, rcpt, allow, expected):
    assert sys.modules["mail_tools"]._recipient_allowed(rcpt, allow) is expected


def test_unknown_account_and_corrupt_config_are_refused(srv, world, tmp_path):
    assert "Unknown SMTP account 'main'" in _send(srv)
    (tmp_path / "mail_dir").mkdir(exist_ok=True)
    (tmp_path / "mail_dir" / "main.json").write_text("{nope")
    assert "Unknown SMTP account 'main'" in _send(srv)
    assert world.sessions == []


def test_send_with_starttls_builds_the_message_and_envelope(srv, world):
    _add(srv)
    out = _send(srv, to="alice@example.com, bob@example.com", cc="carol@example.com")
    assert out == "Sent 'Report' to alice@example.com, bob@example.com, carol@example.com."
    (s,) = world.sessions
    assert (s.kind, s.host, s.port, s.timeout) == ("plain", "smtp.example.com", 587, 30)
    assert s.starttls_context.verify_mode == ssl.CERT_REQUIRED and s.starttls_context.check_hostname
    assert s.logins == []
    (m,) = s.sent
    assert m["from"] == "noreply@example.com"
    assert m["to"] == ["alice@example.com", "bob@example.com", "carol@example.com"]
    assert m["msg"]["Cc"] == "carol@example.com" and m["msg"]["Subject"] == "Report"
    assert m["msg"].get_content().strip() == "Hello"


@pytest.mark.parametrize("security,kind,starttls", [("ssl", "ssl", False), ("none", "plain", False)])
def test_security_mode_selects_the_transport(srv, world, security, kind, starttls):
    _add(srv, security=security, port=465 if security == "ssl" else 25)
    _send(srv)
    (s,) = world.sessions
    assert s.kind == kind and (s.starttls_context is not None) is starttls
    assert (s.context is not None) is (kind == "ssl")


def test_tls_insecure_turns_verification_off(srv, world):
    _add(srv, tls_insecure=True)
    _send(srv)
    ctx = world.sessions[0].starttls_context
    assert ctx.verify_mode == ssl.CERT_NONE and ctx.check_hostname is False


def test_ca_bundle_pins_the_trusted_certificate_and_a_bad_path_fails_closed(srv, world, tmp_path):
    _add(srv, ca_bundle=_ca_pem(tmp_path / "ca.pem"))
    assert _send(srv).startswith("Sent ")
    ctx = world.sessions[0].starttls_context
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert any(("commonName", "mail.example.com") in rdn for c in ctx.get_ca_certs() for rdn in c["subject"])
    _add(srv, ca_bundle=str(tmp_path / "missing.pem"))
    assert _send(srv).startswith("Send failed:") and len(_sent(world)) == 1


def test_login_uses_the_vault_password_and_never_leaks_it(srv, world, tmp_path):
    _vault()
    _add(srv, username="alice", password_env="SMTP_PW")
    out = _send(srv)
    assert world.sessions[0].logins == [("alice", "test-password")]
    assert "test-password" not in out
    assert "test-password" not in (tmp_path / "mail_dir" / "main.json").read_text()


def test_missing_password_secret_aborts_before_sending(srv, world):
    _add(srv, username="alice", password_env="SMTP_PW")
    assert _send(srv) == "Account needs secret 'SMTP_PW'. Use secret_set."
    assert _sent(world) == []


def test_username_without_password_env_skips_login(srv, world):
    _add(srv, username="alice")
    _send(srv)
    assert world.sessions[0].logins == []


def test_private_smtp_host_is_blocked_unless_allow_listed(srv, world, monkeypatch):
    _call(srv, "mail_add", {"name": "main", "host": "relay.example.net", "from_addr": "noreply@example.com"})
    out = _send(srv)
    assert out.startswith("Blocked by network policy") and "10.0.0.25" in out
    assert world.sessions == []
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "10.0.0.0/24")
    assert _send(srv).startswith("Sent ")


def test_connect_time_guard_stops_dns_rebinding(srv, world, monkeypatch):
    _fake_dns(monkeypatch, {"smtp.example.com": [["203.0.113.25"], ["10.0.0.9"]]})
    world.init_hook = lambda host, port: socket.getaddrinfo(host, port)
    _add(srv)
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "203.0.113.0/24")
    out = _send(srv)
    assert out.startswith("Send failed: netguard:") and world.sessions == []


def test_server_errors_are_returned_as_send_failed(srv, world):
    _add(srv)
    world.send_error = smtplib.SMTPRecipientsRefused({"alice@example.com": (550, b"no such user")})
    out = _send(srv)
    assert out.startswith("Send failed:") and "550" in out


def test_header_injection_in_recipients_is_rejected(srv, world):
    _add(srv)
    out = _send(srv, to="alice@example.com\nBcc: bob@example.net")
    assert world.sessions == [] and "Sent " not in out


def test_allow_list_refuses_the_whole_send_when_any_recipient_is_outside(srv, world, monkeypatch):
    monkeypatch.setenv("MAIL_ALLOWED_RECIPIENTS", "@example.com, carol@example.net")
    _add(srv)
    out = _send(srv, to="alice@example.com", cc="bob@example.org, Dave@Example.Net")
    assert out.startswith("Refused: recipient(s) not allowed by MAIL_ALLOWED_RECIPIENTS")
    assert "bob@example.org, Dave@Example.Net" in out
    assert world.sessions == []


def test_allow_list_permits_listed_domains_exact_addresses_and_unset_means_open(srv, world, monkeypatch):
    _add(srv)
    assert _send(srv, to="anyone@example.org").startswith("Sent ")  # unset = unrestricted
    monkeypatch.setenv("MAIL_ALLOWED_RECIPIENTS", "example.com carol@example.net")
    assert _send(srv, to="alice@example.com, carol@example.net").startswith("Sent ")
    assert len(_sent(world)) == 2


def test_attachment_is_read_from_the_data_root_with_a_matching_type(srv, world, tmp_path):
    _add(srv)
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "scan.pdf").write_bytes(b"%PDF-1.4 test")
    (tmp_path / "work" / "blob.xyz").write_bytes(b"raw")
    out = _send(srv, attachment="work/scan.pdf")
    assert out == "Sent 'Report' to alice@example.com with attachment scan.pdf."
    (att,) = list(_sent(world)[0]["msg"].iter_attachments())
    assert (att.get_content_type(), att.get_filename()) == ("application/pdf", "scan.pdf")
    assert att.get_content() == b"%PDF-1.4 test"
    _send(srv, attachment=str(tmp_path / "work" / "blob.xyz"))  # absolute path inside the root
    (att2,) = list(_sent(world)[1]["msg"].iter_attachments())
    assert att2.get_content_type() == "application/octet-stream"


@pytest.mark.parametrize("attachment", ["../outside.txt", "/etc/hosts", "SIBLING"])
def test_attachment_outside_the_data_root_is_refused(srv, world, tmp_path, attachment):
    _add(srv)
    if attachment == "SIBLING":  # shares the string prefix of the data root
        attachment = str(tmp_path) + "-backup/secret.txt"
    assert _send(srv, attachment=attachment) == "Attachment must be under /data."
    assert world.sessions == []


def test_missing_and_oversized_attachments_are_refused(srv, world, tmp_path, monkeypatch):
    _add(srv)
    assert "No attachment file at" in _send(srv, attachment="nope.pdf")
    (tmp_path / "big.txt").write_bytes(b"x" * 20)
    monkeypatch.setattr(sys.modules["mail_tools"], "_MAX_ATTACH", 10)
    out = _send(srv, attachment="big.txt")
    assert out == "Refused: attachment is 20 bytes, over the 10-byte limit."
    assert world.sessions == []
