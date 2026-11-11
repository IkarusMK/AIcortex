"""IPv6 forms that carry or hide an internal address are not "public".

Regression for the re-review finding on ``netguard._public``: Python's ``is_global``
calls 64:ff9b::/96 (NAT64), ::/96 (IPv4-compatible, e.g. ::127.0.0.1) and fec0::/10
(site-local) global, and only newer Pythons treat 2002::/16 (6to4) as private. A name
resolving to 64:ff9b::7f00:1 reaches 127.0.0.1 through a NAT64 gateway, so the SSRF
guard let it through. Now:

- NAT64 addresses count as the IPv4 address they embed (a public one is fine — an
  IPv6-only host behind DNS64 must still reach the internet; INTERNAL_ALLOW_CIDRS
  applies to the embedded address too);
- ::/96, fec0::/10 and 2002::/16 are never public.

Addresses from the documentation ranges only (192.0.2.x, 203.0.113.x, 2001:db8::).
"""
import ipaddress
import socket

import pytest


@pytest.fixture
def ng(fresh_server, monkeypatch):
    import netguard
    monkeypatch.delenv("INTERNAL_ALLOW_CIDRS", raising=False)
    return netguard


def _ip(text):
    return ipaddress.ip_address(text)


def test_embedded_and_legacy_ipv6_forms_are_not_public(ng):
    for internal in ("64:ff9b::7f00:1",       # NAT64 → 127.0.0.1
                     "64:ff9b::a9fe:a9fe",    # NAT64 → 169.254.169.254 (cloud metadata)
                     "64:ff9b::c0a8:1",       # NAT64 → 192.168.0.1
                     "::127.0.0.1",           # IPv4-compatible
                     "::b16:212c",            # IPv4-compatible, even of a routed IPv4 (11.22.33.44)
                     "fec0::1",               # site-local
                     "2002:7f00:1::1",        # 6to4 → 127.0.0.1
                     "2002:b16:212c::1"):     # 6to4, even with a routed IPv4 inside
        assert not ng._public(_ip(internal)), internal
    # NAT64 of a globally routed IPv4 (an invented one, 11.22.33.44) is fine — an
    # IPv6-only host behind DNS64 must still reach the internet.
    nat64 = ipaddress.IPv6Address(int(ipaddress.IPv6Address("64:ff9b::"))
                                  + int(ipaddress.IPv4Address("11.22.33.44")))
    assert ng._public(nat64)


def test_a_name_resolving_to_a_nat64_loopback_is_blocked(ng, monkeypatch):
    monkeypatch.setattr(ng, "_resolve_ips", lambda host: [_ip("64:ff9b::7f00:1")])
    ok, reason = ng.check_host("sneaky.example.net")
    assert not ok and "64:ff9b::7f00:1" in reason
    with pytest.raises(ng.Blocked):
        ng.allowed_addresses("::127.0.0.1")
    with pytest.raises(ng.Blocked):
        ng.allowed_addresses("fec0::1")


def test_the_allow_list_applies_to_the_embedded_ipv4(ng, monkeypatch):
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "192.168.0.0/16")
    assert ng.allowed_addresses("64:ff9b::c0a8:1") == ["64:ff9b::c0a8:1"]   # LAN host via NAT64
    with pytest.raises(ng.Blocked):
        ng.allowed_addresses("64:ff9b::7f00:1")                          # loopback still isn't


def test_the_connect_time_filter_drops_them_too(ng, monkeypatch):
    answers = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("64:ff9b::7f00:1", 443, 0, 0))]
    monkeypatch.setattr(ng, "_raw_getaddrinfo", lambda: (lambda *a, **k: list(answers)))
    with ng.guard("sneaky.example.net"):
        with pytest.raises(socket.gaierror):
            socket.getaddrinfo("sneaky.example.net", 443)
