"""netguard (2/2): INTERNAL_ALLOW_CIDRS and the connect-time guard.

Proves, with DNS fully mocked (no real network, no real lookups):

* ``INTERNAL_ALLOW_CIDRS`` opens exactly the listed ranges, tolerates junk entries,
  never widens to another address family and never implies the metadata address;
* the ``guard`` context manager re-checks EVERY lookup at connect time (DNS
  rebinding between preflight and connect, cross-host redirect targets), honours
  the allow-list, skips unparseable answers, is refcounted and thread-safe, and
  always restores ``socket.getaddrinfo``.

Preflight decisions and TLS policy live in test_cov_netguard.py.
"""
import importlib.util
import socket
import threading
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


def test_allow_list_opens_exactly_the_listed_range(ng, dns, monkeypatch):
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "192.168.1.0/24")
    dns({"nas.example.com": ["192.168.1.20"], "other.example.com": ["192.168.2.5"],
         "db.example.com": ["10.0.0.5"], "meta.example.com": ["169.254.169.254"]})
    assert ng.check_host("nas.example.com") == (True, "ok")
    assert ng.check_host("other.example.com")[0] is False       # neighbouring /24
    assert ng.check_host("db.example.com")[0] is False          # other private range
    assert ng.check_host("meta.example.com")[0] is False        # metadata never implied


def test_allow_list_accepts_commas_semicolons_whitespace_and_v6(ng, dns, monkeypatch):
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", " 192.168.1.0/24 ;10.0.0.0/8,, 2001:db8::/32 ")
    dns({"a.example.com": ["192.168.1.9"], "b.example.com": ["10.9.9.9"],
         "c.example.com": ["2001:db8::5"], "d.example.com": ["172.16.0.1"]})
    assert ng.check_host("a.example.com")[0] is True
    assert ng.check_host("b.example.com")[0] is True
    assert ng.check_host("c.example.com")[0] is True
    assert ng.check_host("d.example.com")[0] is False


def test_allow_list_ignores_garbage_entries_but_keeps_valid_ones(ng, dns, monkeypatch):
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "not-a-cidr,999.1.1.1/8,10.0.0.0/8")
    dns({"ok.example.com": ["10.1.2.3"], "no.example.com": ["192.168.1.1"]})
    assert ng.check_host("ok.example.com")[0] is True
    assert ng.check_host("no.example.com")[0] is False


def test_allow_list_with_only_garbage_allows_nothing(ng, dns, monkeypatch):
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "everything, *, 0")
    dns({"lan.example.com": ["192.168.1.20"]})
    assert ng.check_host("lan.example.com")[0] is False


def test_allow_list_non_strict_host_bits_are_normalised(ng, dns, monkeypatch):
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "192.168.1.77/24")
    dns({"lan.example.com": ["192.168.1.20"]})
    assert ng.check_host("lan.example.com")[0] is True


def test_allow_listing_one_family_does_not_open_the_other(ng, dns, monkeypatch):
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "10.0.0.0/8")
    dns({"v6.example.com": ["::1"], "v4.example.com": ["10.0.0.5"]})
    assert ng.check_host("v6.example.com")[0] is False
    assert ng.check_host("v4.example.com")[0] is True


def test_every_answer_must_be_public_or_allow_listed(ng, dns, monkeypatch):
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "192.168.1.0/24")
    dns({"split.example.com": ["192.168.1.20", "10.0.0.5"]})
    assert "10.0.0.5" in _blocked(ng, "split.example.com")


# ── check_url ───────────────────────────────────────────────────────────────


def test_guard_installs_a_filter_and_restores_the_original_resolver(ng, dns):
    fake = dns({"api.example.com": [PUBLIC_V4]})
    assert socket.getaddrinfo is fake
    with ng.guard("api.example.com"):
        assert socket.getaddrinfo is not fake
        assert socket.getaddrinfo("api.example.com", 443)[0][4][0] == PUBLIC_V4
    assert socket.getaddrinfo is fake
    assert ng._guarded == {}


def test_guard_blocks_dns_rebinding_between_preflight_and_connect(ng, monkeypatch):
    answers = iter([[PUBLIC_V4], ["10.0.0.5"]])        # public at check, internal at connect
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, *a, **k: [_entry(ip) for ip in next(answers)])
    assert ng.check_host("rebind.example.com") == (True, "ok")
    with ng.guard("rebind.example.com"):
        with pytest.raises(socket.gaierror, match="netguard.*DNS rebinding"):
            socket.getaddrinfo("rebind.example.com", 443)


def test_guard_drops_internal_answers_but_keeps_public_ones(ng, dns):
    dns({"mixed.example.com": [PUBLIC_V4, "10.0.0.5", "127.0.0.1", PUBLIC_V4_B]})
    with ng.guard("mixed.example.com"):
        got = [e[4][0] for e in socket.getaddrinfo("mixed.example.com", 80)]
    assert got == [PUBLIC_V4, PUBLIC_V4_B]


def test_guard_filters_lookups_of_other_hosts_too_redirect_bypass(ng, dns):
    dns({"registered.example.com": [PUBLIC_V4], "metadata.example.net": ["169.254.169.254"]})
    with ng.guard("registered.example.com"):
        with pytest.raises(socket.gaierror):
            socket.getaddrinfo("metadata.example.net", 80)       # 30x target


def test_guard_honours_the_allow_list_at_connect_time(ng, dns, monkeypatch):
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "192.168.1.0/24")
    dns({"nas.example.com": ["192.168.1.20"], "evil.example.com": ["192.168.2.20"]})
    with ng.guard("nas.example.com"):
        assert socket.getaddrinfo("nas.example.com", 80)[0][4][0] == "192.168.1.20"
        with pytest.raises(socket.gaierror):
            socket.getaddrinfo("evil.example.com", 80)


def test_guard_treats_scoped_link_local_v6_as_blocked(ng, dns):
    dns({"ll.example.com": ["fe80::1%eth0"]})
    with ng.guard("ll.example.com"), pytest.raises(socket.gaierror):
        socket.getaddrinfo("ll.example.com", 80)


def test_guard_skips_unparseable_entries(ng, dns):
    bad_value = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("not-an-ip", 0))
    bad_shape = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ())
    dns({"x.example.com": [bad_value, bad_shape, _entry(PUBLIC_V4)],
         "y.example.com": [bad_value, bad_shape]})
    with ng.guard("x.example.com"):
        assert [e[4][0] for e in socket.getaddrinfo("x.example.com", 80)] == [PUBLIC_V4]
        with pytest.raises(socket.gaierror):
            socket.getaddrinfo("y.example.com", 80)


def test_guard_passes_resolver_errors_through_unchanged(ng, dns):
    dns({"gone.example.com": socket.gaierror(-2, "Name or service not known")})
    with ng.guard("gone.example.com"), pytest.raises(socket.gaierror, match="Name or service"):
        socket.getaddrinfo("gone.example.com", 80)


def test_guard_restores_the_resolver_when_the_body_raises(ng, dns):
    fake = dns({"a.example.com": [PUBLIC_V4]})
    with pytest.raises(RuntimeError):
        with ng.guard("a.example.com"):
            raise RuntimeError("boom")
    assert socket.getaddrinfo is fake and ng._guarded == {}


def test_guard_is_refcounted_for_nested_and_concurrent_use(ng, dns):
    fake = dns({"a.example.com": [PUBLIC_V4], "b.example.com": ["10.0.0.5"]})
    with ng.guard("A.example.com"):
        with ng.guard("a.EXAMPLE.com"):                  # same key, case-insensitive
            assert ng._guarded == {"a.example.com": 2}
        assert ng._guarded == {"a.example.com": 1}
        assert socket.getaddrinfo is not fake            # still active after inner exit
        with ng.guard("[::1]"), ng.guard(""), ng.guard(None):
            assert set(ng._guarded) == {"a.example.com", "::1", "-"}
        with pytest.raises(socket.gaierror):
            socket.getaddrinfo("b.example.com", 80)
    assert socket.getaddrinfo is fake and ng._guarded == {}


def test_guard_without_any_active_guard_does_not_filter(ng, dns):
    # Filtering is strictly scoped to the guarded call: internal answers pass outside.
    dns({"lan.example.com": ["10.0.0.5"]})
    assert socket.getaddrinfo("lan.example.com", 80)[0][4][0] == "10.0.0.5"


def test_filtered_resolver_is_inert_when_no_guard_is_left(ng, dns):
    dns({"lan.example.com": ["10.0.0.5"]})
    with ng.guard("lan.example.com"):
        pass
    # A late caller that still holds the filtered function must not be blocked either.
    ng._orig_getaddrinfo = socket.getaddrinfo
    assert ng._filtered_getaddrinfo("lan.example.com", 80)[0][4][0] == "10.0.0.5"
    ng._orig_getaddrinfo = None


def test_guard_is_thread_safe_and_always_restores(ng, dns):
    fake = dns({"a.example.com": [PUBLIC_V4]})
    errors = []

    def worker():
        try:
            for _ in range(40):
                with ng.guard("a.example.com"):
                    assert socket.getaddrinfo("a.example.com", 80)[0][4][0] == PUBLIC_V4
        except Exception as exc:  # pragma: no cover - only on failure
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert errors == []
    assert socket.getaddrinfo is fake
    assert ng._guarded == {} and ng._orig_getaddrinfo is None


@pytest.mark.parametrize("ip", ["224.0.0.1", "ff02::1"])
def test_guard_blocks_multicast_at_connect_time_like_the_preflight(ng, dns, ip):
    dns({"mc.example.com": [ip]})
    with ng.guard("mc.example.com"), pytest.raises(socket.gaierror):
        socket.getaddrinfo("mc.example.com", 80)
