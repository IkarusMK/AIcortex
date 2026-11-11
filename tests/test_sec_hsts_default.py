"""HSTS by default covers this host only — not every subdomain of it.

Regression for the re-review finding: the default
``Strict-Transport-Security: max-age=31536000; includeSubDomains`` also pinned every
sibling under the same name (``nas.example.net`` → ``*.nas.example.net``) to HTTPS for
a year, and some of those may still be plain HTTP (a printer, a router page). The
default is now ``max-age=31536000``; HSTS=<value> still sets anything else, e.g.
"max-age=63072000; includeSubDomains; preload", and HSTS=off switches it off.
"""
import pytest


@pytest.fixture
def secheaders(fresh_server, monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://agent.example.net")
    monkeypatch.delenv("HSTS", raising=False)
    import secheaders
    return secheaders


def test_the_default_leaves_subdomains_alone(secheaders, monkeypatch):
    assert secheaders.hsts_value() == "max-age=31536000"
    assert "includeSubDomains" not in secheaders.DEFAULT_HSTS
    monkeypatch.setenv("HSTS", "max-age=63072000; includeSubDomains; preload")
    assert secheaders.hsts_value() == "max-age=63072000; includeSubDomains; preload"
    monkeypatch.setenv("HSTS", "off")
    assert secheaders.hsts_value() is None
