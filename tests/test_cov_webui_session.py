"""WebUI session and request helpers (app/webui.py), tested one by one.

Proves: the signed session token expires and rejects garbage or a key change; the
signing key falls back from JWT_SIGNING_KEY to STORAGE_ENCRYPTION_KEY to a per-boot
key; the IdP discovery document is cached for an hour; the CSRF / Origin gate refuses
a missing or wrong token and a foreign or unparsable Origin; "made on this machine"
means a loopback peer, a loopback Host name and no reverse proxy; without OIDC only
that local operator gets the open-mode admin session.
"""
import sys
import time
from types import SimpleNamespace

import httpx
import pytest

pytestmark = pytest.mark.oidc       # keep the OIDC_* settings the fixture sets


@pytest.fixture
def webui(monkeypatch, request, fake_idp):
    monkeypatch.setenv("OIDC_CONFIG_URL", f"{fake_idp}/.well-known/openid-configuration")
    monkeypatch.setenv("OIDC_CLIENT_ID", "aicortex")
    monkeypatch.setenv("JWT_SIGNING_KEY", "0" * 64)
    request.getfixturevalue("fresh_server")
    return sys.modules["webui"]


def _req(headers=None, host="127.0.0.1", base="http://cortex.example.com/"):
    return SimpleNamespace(headers={k.lower(): v for k, v in (headers or {}).items()},
                           client=SimpleNamespace(host=host) if host else None, base_url=base)


# ── signed session token ─────────────────────────────────────────────────────
def test_verify_expiry_boundary_and_garbage(webui, monkeypatch):
    now = time.time()
    tok = webui._sign({"sub": "a", "exp": now + 1})
    assert webui._verify(tok)["sub"] == "a"
    with monkeypatch.context() as later:
        later.setattr(webui.time, "time", lambda: now + 2)
        assert webui._verify(tok) is None                             # one second past exp
    assert webui._verify(webui._sign({"sub": "a", "exp": "soon"})) is None
    assert webui._verify(webui._sign({"sub": "a"})) is None           # no exp = expired
    assert webui._verify("%%%.%%%") is None and webui._verify("") is None


def test_session_key_follows_the_configured_secret(webui, monkeypatch):
    tok = webui._sign({"sub": "a", "exp": time.time() + 60})
    monkeypatch.setenv("JWT_SIGNING_KEY", "1" * 64)
    assert webui._verify(tok) is None                                 # another key, not valid
    monkeypatch.delenv("JWT_SIGNING_KEY")
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", "test-key-material")
    storage = webui._sign({"sub": "a", "exp": time.time() + 60})
    assert webui._verify(storage)["sub"] == "a" and webui._verify(tok) is None
    monkeypatch.delenv("STORAGE_ENCRYPTION_KEY")
    assert webui._ui_key() == webui._BOOT_KEY and webui._verify(storage) is None


# ── discovery cache, display name, audit safety ─────────────────────────────
def test_discovery_is_cached_and_refetched_after_expiry(webui, monkeypatch):
    calls = []
    real_get = httpx.get
    monkeypatch.setattr(httpx, "get", lambda *a, **k: (calls.append(a[0]), real_get(*a, **k))[1])
    first = webui._discovery()
    assert webui._discovery() is first and len(calls) == 1
    webui._OIDC_CACHE["exp"] = 0
    webui._discovery()
    assert len(calls) == 2


@pytest.mark.parametrize("claims, shown", [
    ({"name": "Alice", "email": "a@example.com"}, "Alice"),
    ({"preferred_username": "alice", "email": "a@example.com"}, "alice"),
    ({"email": "a@example.com", "sub": "s1"}, "a@example.com"),
    ({"sub": "s1"}, "s1"), ({}, "?"),
])
def test_display_name_falls_back_in_order(webui, claims, shown):
    assert webui._display_name(claims) == shown


def test_audit_never_lets_a_logging_failure_break_a_request(webui, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(sys.modules["authz"], "audit", boom)
    webui._audit({"sub": "alice", "role": "admin"}, "x", "allow", "d")   # must not raise
    webui._audit(None, "x", "deny", "d")


# ── CSRF / Origin gate and the "made on this machine" test ──────────────────
@pytest.mark.parametrize("headers, ok", [
    ({"X-CSRF": "tok"}, True),
    ({}, False), ({"X-CSRF": "other"}, False), ({"X-CSRF": ""}, False),
    ({"X-CSRF": "tok", "Origin": "http://cortex.example.com"}, True),
    ({"X-CSRF": "tok", "Origin": "https://evil.example.net"}, False),
    ({"X-CSRF": "tok", "Origin": "http://cortex.example.com.evil.example.net"}, False),
    ({"X-CSRF": "tok", "Origin": "http://[::1"}, False),                     # unparsable
    ({"X-CSRF": "tok", "Origin": "http://nas.example.net:8787", "Host": "nas.example.net:8787"}, True),
])
def test_mutation_gate_needs_the_session_token_and_a_matching_origin(webui, headers, ok):
    assert webui._mutation_ok(_req(headers), {"csrf": "tok"}) is ok


def test_a_session_without_csrf_token_only_accepts_an_empty_header(webui):
    assert webui._mutation_ok(_req({"X-CSRF": "x"}), {}) is False


@pytest.mark.parametrize("kw, local", [
    ({}, True), ({"host": "::1"}, True), ({"host": "192.0.2.15"}, False), ({"host": None}, False),
    ({"headers": {"Host": "localhost:8787"}}, True),
    ({"headers": {"Host": "evil.example.net"}}, False),                       # DNS rebinding
    ({"headers": {"X-Forwarded-For": "192.0.2.9"}}, False),
    ({"headers": {"Forwarded": "for=192.0.2.9"}}, False),
    ({"headers": {"X-Real-IP": "192.0.2.9"}}, False),
])
def test_local_request_means_loopback_peer_loopback_name_and_no_proxy(webui, kw, local):
    assert webui._is_local_request(_req(**kw)) is local


def test_open_mode_session_is_the_local_operator_only(webui, monkeypatch):
    monkeypatch.delenv("OIDC_CONFIG_URL")
    assert webui._session(_req())["sub"] == "local-operator"
    lan = _req(host="192.0.2.15")
    assert webui._session(lan) is None
    monkeypatch.setenv("ALLOW_INSECURE", "1")
    assert webui._session(lan)["role"] == "admin"


def test_cookies_are_secure_only_behind_an_https_base_url(webui, monkeypatch):
    assert webui._secure_cookies(_req(base="http://cortex.example.com/")) is False
    assert webui._secure_cookies(_req(base="https://cortex.example.com/")) is True
    monkeypatch.setenv("BASE_URL", "HTTPS://cortex.example.com/")
    assert webui._secure_cookies(_req(base="http://127.0.0.1:8787/")) is True
