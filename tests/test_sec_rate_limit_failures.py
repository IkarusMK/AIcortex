"""Credential guessing on /mcp and /api/v1 is throttled; the WebUI API is limited.

Regression for the re-review findings on rate limits:
- /mcp and /api/v1 answered an unlimited number of wrong bearer tokens per client
  IP. Now the 401/403 answers to requests that PRESENTED credentials are counted per
  client IP; past RATE_LIMIT_AUTH_FAILURES (default 20/min) the IP gets 429 on those
  paths for RATE_LIMIT_FAILURE_COOLDOWN seconds (default 300). Successful traffic and
  the credential-less 401 that starts every OAuth sign-in are not counted — many
  users of a hosted app can share one egress IP.
- /ui/api/* had no limit; now RATE_LIMIT_UI (default 300/min) per client IP.
- With BASE_URL on https (so behind a proxy) and FORWARDED_ALLOW_IPS unset, every
  client may share the proxy's address — one client's failures would lock everyone
  out. The server now warns at startup.
"""
import sys

import httpx
import pytest

ACCEPT = "application/json, text/event-stream"
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
_ENV = ("RATE_LIMIT", "RATE_LIMIT_AUTH", "RATE_LIMIT_LOGIN", "RATE_LIMIT_REGISTER",
        "RATE_LIMIT_HOOKS", "RATE_LIMIT_UI", "RATE_LIMIT_AUTH_FAILURES",
        "RATE_LIMIT_FAILURE_COOLDOWN")


@pytest.fixture
def limits(monkeypatch):
    for var in _ENV:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def _mcp(base, token=None):
    headers = {"Accept": ACCEPT, **({"Authorization": f"Bearer {token}"} if token else {})}
    return httpx.post(f"{base}/mcp", json=PING, headers=headers).status_code


def _rest(base, token):
    return httpx.get(f"{base}/api/v1/tools", headers={"Authorization": f"Bearer {token}"})


def test_wrong_tokens_lock_the_ip_out_for_a_cooldown(limits, request):
    limits.setenv("RATE_LIMIT_AUTH_FAILURES", "3/min")
    limits.setenv("RATE_LIMIT_FAILURE_COOLDOWN", "120")
    base = request.getfixturevalue("http_server")
    good, _ = sys.modules["apikeys"].create("alice", scopes="all")
    assert [_mcp(base) for _ in range(10)] == [401] * 10          # sign-in starts: not counted
    assert [_mcp(base, "ak_wrong_guess") for _ in range(3)] == [401] * 3
    assert [_rest(base, good).status_code for _ in range(5)] == [200] * 5   # successes: not counted
    assert _rest(base, "ak_wrong_guess").status_code == 401      # one too many …
    r = _rest(base, "ak_wrong_guess")                             # … locked out
    assert r.status_code == 429 and 100 <= int(r.headers["retry-after"]) <= 120
    assert _mcp(base, "ak_wrong_guess") == 429
    assert _rest(base, good).status_code == 429                   # the whole IP, for the cooldown
    assert httpx.get(f"{base}/ui/api/me").status_code != 429      # other paths are unaffected


def test_the_webui_api_is_limited_per_ip(limits, request):
    limits.setenv("RATE_LIMIT_UI", "3/min")
    base = request.getfixturevalue("http_server")
    codes = [httpx.get(f"{base}/ui/api/me").status_code for _ in range(4)]
    assert 429 not in codes[:3] and codes[3] == 429
    assert httpx.get(f"{base}/ui").status_code != 429             # the page itself is not


def test_defaults_and_off_switches(limits, fresh_server):
    import ratelimit
    assert ratelimit.limits_from_env()["ui"] == (300, 60.0)
    assert ratelimit.failure_limit_from_env() == ((20, 60.0), 300.0)
    limits.setenv("RATE_LIMIT_AUTH_FAILURES", "0")
    assert ratelimit.failure_limit_from_env() is None
    limits.setenv("RATE_LIMIT_AUTH_FAILURES", "5/min")
    limits.setenv("RATE_LIMIT_FAILURE_COOLDOWN", "0")
    assert ratelimit.failure_limit_from_env() is None
    limits.setenv("RATE_LIMIT_FAILURE_COOLDOWN", "60")
    limits.setenv("RATE_LIMIT", "0")
    assert ratelimit.failure_limit_from_env() is None and ratelimit.limits_from_env() == {}


def test_https_without_a_trusted_proxy_list_warns(fresh_server, monkeypatch, capsys):
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    monkeypatch.setenv("BASE_URL", "http://nas.example.net:8787")
    fresh_server.uvicorn_config()
    assert "FORWARDED_ALLOW_IPS" not in capsys.readouterr().out
    monkeypatch.setenv("BASE_URL", "https://agent.example.net")
    fresh_server.uvicorn_config()
    out = capsys.readouterr().out
    assert "FORWARDED_ALLOW_IPS" in out and "WARNING" in out
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "172.16.0.0/12")
    fresh_server.uvicorn_config()
    assert "FORWARDED_ALLOW_IPS" not in capsys.readouterr().out
