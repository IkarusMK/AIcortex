"""Transport security headers on every response; no stack banner.

Found in the 2026-11 review:
- No HSTS. A deployment behind HTTPS (BASE_URL https://…) never told browsers to
  stay on HTTPS, so the WebUI session cookie and the OAuth pages could be reached
  over a downgraded first request. Now every response carries
  ``Strict-Transport-Security: max-age=31536000`` when BASE_URL is https
  (HSTS=<value> changes it, HSTS=off switches it off; test_sec_hsts_default.py).
- ``X-Content-Type-Options: nosniff`` was only set on the WebUI page and assets;
  now on every response, never twice.
- Every response said ``server: uvicorn``. The banner is switched off in the
  uvicorn config server.py runs with.
"""
import sys
import types

import httpx
import pytest

ACCEPT = "application/json, text/event-stream"
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
HSTS = "max-age=31536000"


@pytest.fixture
def https_server(monkeypatch, request):
    monkeypatch.setenv("BASE_URL", "https://agent.example.net")
    monkeypatch.delenv("HSTS", raising=False)
    return request.getfixturevalue("http_server")


def _responses(base):
    """A spread of responses from different layers of the app."""
    return {
        "webui": httpx.get(f"{base}/ui"),
        "mcp 401": httpx.post(f"{base}/mcp", json=PING, headers={"Accept": ACCEPT}),
        "404": httpx.get(f"{base}/no-such-page"),
        "host guard 421": httpx.get(f"{base}/ui/api/me", headers={"Host": "evil.example.com"}),
        "webhook 401": httpx.post(f"{base}/hooks/doorbell", json={}),
    }


def test_hsts_on_every_response_when_base_url_is_https(https_server):
    for name, r in _responses(https_server).items():
        assert r.headers.get_list("strict-transport-security") == [HSTS], name


def test_hsts_also_on_rate_limited_responses(monkeypatch, request):
    monkeypatch.setenv("BASE_URL", "https://agent.example.net")
    monkeypatch.delenv("RATE_LIMIT", raising=False)
    monkeypatch.setenv("RATE_LIMIT_AUTH", "1/min")
    base = request.getfixturevalue("http_server")
    httpx.post(f"{base}/token", data={})
    r = httpx.post(f"{base}/token", data={})
    assert r.status_code == 429
    assert r.headers.get("strict-transport-security") == HSTS
    assert r.headers.get("x-content-type-options") == "nosniff"


@pytest.mark.parametrize("base_url", ["http://nas.example.net:8787", None])
def test_no_hsts_without_an_https_base_url(monkeypatch, request, base_url):
    if base_url:
        monkeypatch.setenv("BASE_URL", base_url)
    else:
        monkeypatch.delenv("BASE_URL", raising=False)
    base = request.getfixturevalue("http_server")
    for name, r in _responses(base).items():
        assert "strict-transport-security" not in r.headers, name


@pytest.mark.parametrize("setting, expected", [
    ("max-age=600", "max-age=600"),
    ("max-age=63072000; includeSubDomains; preload",
     "max-age=63072000; includeSubDomains; preload"),
    ("off", None),
    ("0", None),
    ("nonsense", HSTS),                       # invalid → the safe default, with a warning
    ("max-age=1\r\nSet-Cookie: x=1", HSTS),   # never a header injection
])
def test_hsts_can_be_changed_or_switched_off(monkeypatch, request, setting, expected):
    monkeypatch.setenv("BASE_URL", "https://agent.example.net")
    monkeypatch.setenv("HSTS", setting)
    base = request.getfixturevalue("http_server")
    r = httpx.get(f"{base}/ui")
    assert r.headers.get("strict-transport-security") == expected
    assert "set-cookie" not in r.headers


def test_nosniff_on_every_response_never_twice(https_server):
    for name, r in _responses(https_server).items():
        assert r.headers.get_list("x-content-type-options") == ["nosniff"], name


def test_no_server_banner(https_server):
    for name, r in _responses(https_server).items():
        assert "server" not in r.headers, name


def test_main_runs_uvicorn_without_server_banner(fresh_server, monkeypatch):
    """server.py's own startup passes the uvicorn config (not just the test fixture)."""
    calls = []
    monkeypatch.setattr(fresh_server.mcp, "run", lambda **kw: calls.append(kw))
    monkeypatch.setattr(fresh_server.runner_builtin, "start", lambda port: None)
    monkeypatch.setitem(sys.modules, "uvloop", types.SimpleNamespace(install=lambda: None))
    fresh_server.main()
    assert len(calls) == 1
    kw = calls[0]
    assert kw["transport"] == "http"
    assert kw["uvicorn_config"]["server_header"] is False
    names = [m.cls.__name__ for m in kw["middleware"]]
    assert names == ["SecurityHeaders", "HostGuard", "RateLimit"]
