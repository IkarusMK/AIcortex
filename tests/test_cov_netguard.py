"""netguard (1/2): preflight decisions and TLS policy.

Proves, with DNS fully mocked (no real network, no real lookups):

* ``check_host`` / ``check_url`` block loopback, private, link-local (cloud
  metadata), CGNAT, unspecified and documentation targets, fail closed on resolver
  errors, and let public addresses through;
* a host is only allowed when EVERY resolved address passes (mixed answers deny);
* the real destination of a URL is checked (userinfo / fragment tricks);
* TLS knobs (``tls_verify`` / ``ssl_context``) are secure by default.

The allow-list and the connect-time guard live in test_cov_netguard_guard.py.
"""
import datetime
import importlib.util
import socket
import ssl
from pathlib import Path

import pytest

NETGUARD_PY = Path(__file__).resolve().parent.parent / "app" / "netguard.py"

# Arbitrary globally routable addresses. They are never contacted: every lookup in
# this file goes through the fake resolver below.
PUBLIC_V4 = "11.22.33.44"
PUBLIC_V4_B = "11.22.33.45"
PUBLIC_V6 = "2a02:1234::1"


def _entry(ip):
    """One getaddrinfo() result tuple for ``ip`` (IPv4 or IPv6)."""
    if ":" in ip:
        return (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, 0, 0, 0))
    return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))


class FakeDNS:
    """Stand-in for socket.getaddrinfo: host -> ip list, or an exception to raise."""

    def __init__(self, table):
        self.table = dict(table)
        self.calls = []

    def __call__(self, host, *args, **kwargs):
        self.calls.append(host)
        answer = self.table[host]
        if isinstance(answer, Exception):
            raise answer
        return [_entry(ip) if isinstance(ip, str) else ip for ip in answer]


@pytest.fixture
def ng(monkeypatch):
    """A private copy of netguard (its guard state is module-global) plus a clean env."""
    monkeypatch.delenv("INTERNAL_ALLOW_CIDRS", raising=False)
    spec = importlib.util.spec_from_file_location("netguard_under_test", NETGUARD_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def dns(monkeypatch):
    def install(table):
        fake = FakeDNS(table)
        monkeypatch.setattr(socket, "getaddrinfo", fake)
        return fake
    return install


def _blocked(ng, host):
    ok, reason = ng.check_host(host)
    assert ok is False, f"{host} must be blocked"
    return reason


# ── check_host: preflight decision ──────────────────────────────────────────

def test_empty_host_reports_a_clear_reason(ng):
    assert ng.check_host("") == (False, "empty host")
    assert ng.check_host(None) == (False, "empty host")


@pytest.mark.parametrize("host", ["   ", "[]"])
def test_blank_host_after_trimming_fails_closed(ng, dns, host):
    dns({})                                    # any lookup raises KeyError
    ok, reason = ng.check_host(host)
    assert ok is False and reason.startswith("cannot resolve host")


def test_public_v4_and_v6_are_allowed(ng, dns):
    dns({"api.example.com": [PUBLIC_V4], "v6.example.com": [PUBLIC_V6]})
    assert ng.check_host("api.example.com") == (True, "ok")
    assert ng.check_host("v6.example.com") == (True, "ok")


@pytest.mark.parametrize("ip", [
    "127.0.0.1",            # loopback
    "127.0.0.53",           # loopback stub resolver
    "::1",                  # v6 loopback
    "169.254.169.254",      # cloud metadata (link-local)
    "169.254.1.1",          # link-local
    "fe80::1",              # v6 link-local
    "10.0.0.5",             # RFC1918
    "172.16.4.9",           # RFC1918
    "192.168.1.20",         # RFC1918
    "100.64.0.1",           # CGNAT / shared address space
    "0.0.0.0",              # unspecified
    "::",                   # v6 unspecified
    "fc00::1",              # unique local
    "198.51.100.7",         # documentation range is not global
    "2001:db8::1",          # v6 documentation range
    "240.0.0.1",            # reserved
    "255.255.255.255",      # broadcast
    "::ffff:127.0.0.1",     # IPv4-mapped loopback must not slip through
    "::ffff:169.254.169.254",  # IPv4-mapped metadata address
])
def test_internal_addresses_are_blocked_with_an_actionable_reason(ng, dns, ip):
    dns({"target.example.com": [ip]})
    reason = _blocked(ng, "target.example.com")
    assert reason.startswith("blocked:")
    assert "INTERNAL_ALLOW_CIDRS" in reason


def test_a_public_name_resolving_to_an_internal_ip_is_caught(ng, dns):
    dns({"innocent.example.com": ["10.0.0.5"]})
    assert "10.0.0.5" in _blocked(ng, "innocent.example.com")


def test_one_internal_answer_among_public_ones_blocks_the_host(ng, dns):
    dns({"mixed.example.com": [PUBLIC_V4, "192.168.1.20", PUBLIC_V6]})
    assert "192.168.1.20" in _blocked(ng, "mixed.example.com")


def test_ipv6_scope_id_is_stripped_before_the_check(ng, dns):
    dns({"ll.example.com": ["fe80::1%eth0"]})
    reason = _blocked(ng, "ll.example.com")
    assert "fe80::1" in reason


def test_brackets_and_whitespace_around_the_host_are_ignored(ng, dns):
    fake = dns({"2001:db8::1": ["2001:db8::1"], "example.com": [PUBLIC_V4]})
    assert ng.check_host("[2001:db8::1]")[0] is False
    assert fake.calls == ["2001:db8::1"]
    assert ng.check_host("  example.com  ") == (True, "ok")


def test_resolver_failure_fails_closed(ng, dns):
    dns({"gone.example.com": socket.gaierror(-2, "Name or service not known")})
    reason = _blocked(ng, "gone.example.com")
    assert reason.startswith("cannot resolve host 'gone.example.com'")


def test_unexpected_resolver_exception_also_fails_closed(ng, dns):
    dns({"odd.example.com": UnicodeError("label too long")})
    assert "cannot resolve" in _blocked(ng, "odd.example.com")


def test_empty_resolver_answer_is_refused(ng, dns):
    dns({"nothing.example.com": []})
    assert _blocked(ng, "nothing.example.com") == "no addresses for host 'nothing.example.com'"


def test_ip_literals_resolve_locally_and_are_blocked_without_any_mock(ng):
    # Literals never reach DNS; this exercises the real _resolve_ips path.
    for literal in ("127.0.0.1", "169.254.169.254", "10.0.0.5", "::1"):
        assert ng.check_host(literal)[0] is False, literal


@pytest.mark.parametrize("ip", ["224.0.0.1", "239.1.1.1", "ff02::1"])
def test_multicast_addresses_are_blocked_as_documented(ng, dns, ip):
    dns({"mc.example.com": [ip]})
    assert ng.check_host("mc.example.com")[0] is False


# ── INTERNAL_ALLOW_CIDRS ────────────────────────────────────────────────────

@pytest.mark.parametrize("url,expected_host", [
    ("http://10.0.0.5/admin", "10.0.0.5"),
    ("https://169.254.169.254/latest/meta-data/", "169.254.169.254"),
    ("http://localhost:8080/x", "localhost"),
    ("https://[2001:db8::1]:8443/x", "2001:db8::1"),
    ("http://example.com@10.0.0.5/", "10.0.0.5"),          # userinfo trick
    ("http://10.0.0.5#@example.com/", "10.0.0.5"),         # fragment trick
])
def test_check_url_validates_the_real_destination_host(ng, dns, url, expected_host):
    fake = dns({"10.0.0.5": ["10.0.0.5"], "169.254.169.254": ["169.254.169.254"],
                "localhost": ["127.0.0.1", "::1"], "2001:db8::1": ["2001:db8::1"]})
    assert ng.check_url(url)[0] is False
    assert fake.calls == [expected_host]


def test_check_url_allows_a_public_url(ng, dns):
    dns({"api.example.com": [PUBLIC_V4]})
    assert ng.check_url("https://api.example.com/v1?x=1") == (True, "ok")


@pytest.mark.parametrize("url", ["", "not a url", "file:///etc/passwd", "/relative/path"])
def test_check_url_without_a_host_is_refused(ng, url):
    assert ng.check_url(url) == (False, "empty host")


# ── tls_verify / ssl_context ────────────────────────────────────────────────

def test_tls_verify_is_secure_by_default_and_ca_bundle_wins(ng):
    assert ng.tls_verify({}) is True
    assert ng.tls_verify({"tls_insecure": False}) is True
    assert ng.tls_verify({"tls_insecure": True}) is False
    assert ng.tls_verify({"tls_insecure": True, "ca_bundle": "/certs/ca.pem"}) == "/certs/ca.pem"
    assert ng.tls_verify({"ca_bundle": Path("/certs/ca.pem")}) == "/certs/ca.pem"
    assert ng.tls_verify({"ca_bundle": ""}) is True


@pytest.mark.parametrize("cfg", [None, "insecure", ["tls_insecure"], 0])
def test_tls_verify_with_a_non_dict_config_verifies(ng, cfg):
    assert ng.tls_verify(cfg) is True


def test_ssl_context_default_verifies_certificates_and_hostnames(ng):
    for cfg in ({}, None, "garbage"):
        ctx = ng.ssl_context(cfg)
        assert ctx.verify_mode == ssl.CERT_REQUIRED
        assert ctx.check_hostname is True


def test_ssl_context_tls_insecure_turns_verification_off(ng):
    ctx = ng.ssl_context({"tls_insecure": True})
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False


def _write_self_signed_ca(path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "device.example.com")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def test_ssl_context_ca_bundle_pins_a_cert_and_beats_tls_insecure(ng, tmp_path):
    pem = tmp_path / "device-ca.pem"
    _write_self_signed_ca(pem)
    baseline = ssl.create_default_context().cert_store_stats()["x509_ca"]
    ctx = ng.ssl_context({"ca_bundle": str(pem), "tls_insecure": True})
    assert ctx.verify_mode == ssl.CERT_REQUIRED          # ca_bundle takes precedence
    assert ctx.check_hostname is True
    assert ctx.cert_store_stats()["x509_ca"] == baseline + 1


def test_ssl_context_with_an_unreadable_ca_bundle_raises_instead_of_degrading(ng, tmp_path):
    with pytest.raises((OSError, ssl.SSLError)):
        ng.ssl_context({"ca_bundle": str(tmp_path / "missing.pem")})


def test_ssl_context_blank_ca_bundle_is_ignored(ng):
    assert ng.ssl_context({"ca_bundle": "   "}).verify_mode == ssl.CERT_REQUIRED


# ── guard: connect-time re-check ────────────────────────────────────────────
