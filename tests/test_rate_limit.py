"""Brute force and DCR flooding against the auth endpoints are throttled per client IP.

Found in the 2026-11 review: nothing in the app limited /authorize, /token, /register,
/consent, the IdP callback, the WebUI login or the public webhook receiver — the
deployment relied on an external CrowdSec alone. Now an in-memory token bucket per
client IP and endpoint group answers 429 with Retry-After once a group's budget is
spent; /register (each call stores a client) is much stricter than the rest.

Groups and defaults (env, ``0`` switches a group off, ``RATE_LIMIT=0`` all of them):
  auth      /authorize /token /consent /revoke /auth/callback    RATE_LIMIT_AUTH=30/min
  login     /ui/login /ui/callback /ui/mcp-oauth/callback        RATE_LIMIT_LOGIN=30/min
  register  /register                                            RATE_LIMIT_REGISTER=10/hour
  hooks     /hooks/*                                             RATE_LIMIT_HOOKS=60/min
  ui        /ui/api/*                                            RATE_LIMIT_UI=300/min

The test suite as a whole runs with RATE_LIMIT=0 (conftest) because many tests hit
these endpoints in quick succession; the tests here switch it back on.
"""
import httpx
import pytest

ACCEPT = "application/json, text/event-stream"
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
_LIMIT_ENV = ("RATE_LIMIT", "RATE_LIMIT_AUTH", "RATE_LIMIT_LOGIN", "RATE_LIMIT_REGISTER",
              "RATE_LIMIT_HOOKS", "RATE_LIMIT_UI", "RATE_LIMIT_AUTH_FAILURES",
              "RATE_LIMIT_FAILURE_COOLDOWN")


@pytest.fixture
def limits(monkeypatch):
    """Production defaults: clear every rate-limit variable (conftest turns it off)."""
    for var in _LIMIT_ENV:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def _ratelimit():
    import ratelimit
    return ratelimit


# ── unit: parsing, buckets, client keys ─────────────────────────────────────

def test_limits_parse_count_and_period(fresh_server):
    parse = _ratelimit().parse_limit
    assert parse("30/min") == (30, 60.0)
    assert parse("10/hour") == (10, 3600.0)
    assert parse("5/s") == (5, 1.0)
    assert parse("100/day") == (100, 86400.0)
    assert parse(" 7 ") == (7, 60.0)          # a bare number is per minute
    assert parse("0") is None                  # 0 switches the group off
    assert parse("0/hour") is None
    for bad in ("", "abc", "-1", "5/fortnight", "1.5/min"):
        with pytest.raises(ValueError):
            parse(bad)


def test_bucket_allows_a_burst_then_refuses_until_it_refills(fresh_server):
    buckets = _ratelimit().Buckets(limit=(3, 60.0))
    assert [buckets.take("203.0.113.7", now=100.0) for _ in range(3)] == [0, 0, 0]
    wait = buckets.take("203.0.113.7", now=100.0)
    assert 19 <= wait <= 20                    # one token every 20 s
    assert buckets.take("203.0.113.8", now=100.0) == 0      # other clients unaffected
    assert buckets.take("203.0.113.7", now=121.0) == 0      # refilled one token
    assert buckets.take("203.0.113.7", now=121.0) > 0


def test_a_refused_request_does_not_cost_a_token(fresh_server):
    buckets = _ratelimit().Buckets(limit=(1, 60.0))
    assert buckets.take("203.0.113.7", now=0.0) == 0
    for t in range(1, 50):                     # hammering while blocked …
        assert buckets.take("203.0.113.7", now=float(t)) > 0
    assert buckets.take("203.0.113.7", now=60.0) == 0       # … does not extend the block


def test_memory_stays_bounded_under_many_addresses(fresh_server):
    buckets = _ratelimit().Buckets(limit=(5, 60.0), max_clients=100)
    for i in range(1000):
        buckets.take(f"198.51.100.{i % 256}-{i}", now=0.0)
    assert len(buckets) <= 100


def test_client_key_groups_ipv6_by_64_prefix(fresh_server):
    key = _ratelimit().client_key
    assert key({"client": ("203.0.113.7", 50000)}) == "203.0.113.7"
    assert key({"client": ("2001:db8:1:2::1", 1)}) == key({"client": ("2001:db8:1:2:ffff::9", 1)})
    assert key({"client": ("2001:db8:1:2::1", 1)}) != key({"client": ("2001:db8:1:3::1", 1)})
    assert key({"client": ("::ffff:203.0.113.7", 1)}) == "203.0.113.7"
    assert key({"client": None}) == "unknown"


def test_paths_map_to_groups(fresh_server):
    group = _ratelimit().group_of
    assert group("/token") == group("/authorize") == group("/consent") == "auth"
    assert group("/auth/callback") == group("/revoke") == group("/token/") == "auth"
    assert group("/ui/login") == group("/ui/callback") == "login"
    assert group("/ui/mcp-oauth/callback") == "login"
    assert group("/register") == "register"
    assert group("/hooks/github") == "hooks"
    assert group("/ui/api/me") == group("/ui/api/secrets") == "ui"
    # /mcp and /api/v1 count credential failures instead (test_sec_rate_limit_failures.py)
    for free in ("/mcp", "/ui", "/api/v1/tools", "/hooks",
                 "/.well-known/oauth-authorization-server", "/tokens"):
        assert group(free) is None, free


def test_defaults_and_env_overrides(fresh_server, limits):
    rl = _ratelimit()
    assert rl.limits_from_env() == {"auth": (30, 60.0), "login": (30, 60.0),
                                    "register": (10, 3600.0), "hooks": (60, 60.0),
                                    "ui": (300, 60.0)}
    limits.setenv("RATE_LIMIT_HOOKS", "0")
    limits.setenv("RATE_LIMIT_AUTH", "100/min")
    limits.setenv("RATE_LIMIT_REGISTER", "nonsense")         # invalid → default, not off
    assert rl.limits_from_env() == {"auth": (100, 60.0), "login": (30, 60.0),
                                    "register": (10, 3600.0), "ui": (300, 60.0)}
    limits.setenv("RATE_LIMIT", "0")
    assert rl.limits_from_env() == {}


# ── over real HTTP, wired the way server.py runs ────────────────────────────

def test_token_endpoint_answers_429_with_retry_after(limits, request):
    limits.setenv("RATE_LIMIT_AUTH", "3/min")
    base = request.getfixturevalue("http_server")
    codes = [httpx.post(f"{base}/token", data={"grant_type": "x"}).status_code
             for _ in range(3)]
    assert 429 not in codes
    r = httpx.post(f"{base}/token", data={"grant_type": "x"})
    assert r.status_code == 429
    assert 1 <= int(r.headers["retry-after"]) <= 20
    # same group, same budget
    assert httpx.get(f"{base}/authorize").status_code == 429
    # other groups and ordinary endpoints are untouched
    assert httpx.get(f"{base}/ui/login", follow_redirects=False).status_code != 429
    assert httpx.post(f"{base}/mcp", json=PING, headers={"Accept": ACCEPT}).status_code != 429


def test_register_is_much_stricter_by_default(limits, request):
    base = request.getfixturevalue("http_server")
    codes = [httpx.post(f"{base}/register", json={}).status_code for _ in range(10)]
    assert 429 not in codes
    r = httpx.post(f"{base}/register", json={})
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 60     # 10/hour: next slot in ~6 minutes


def test_webui_login_and_webhooks_are_limited(limits, request):
    limits.setenv("RATE_LIMIT_LOGIN", "2/min")
    limits.setenv("RATE_LIMIT_HOOKS", "2/min")
    base = request.getfixturevalue("http_server")
    login = [httpx.get(f"{base}/ui/login", follow_redirects=False).status_code
             for _ in range(3)]
    assert login[-1] == 429 and 429 not in login[:2]
    hooks = [httpx.post(f"{base}/hooks/doorbell", json={}).status_code for _ in range(2)]
    hooks.append(httpx.post(f"{base}/hooks/other", json={}).status_code)
    assert hooks[-1] == 429 and 429 not in hooks[:2]     # per group, not per hook name


def test_preflight_requests_are_not_counted(limits, request):
    limits.setenv("RATE_LIMIT_REGISTER", "1/hour")
    base = request.getfixturevalue("http_server")
    for _ in range(5):
        assert httpx.options(f"{base}/register").status_code != 429
    assert httpx.post(f"{base}/register", json={}).status_code != 429
    assert httpx.post(f"{base}/register", json={}).status_code == 429


def test_zero_switches_the_limiter_off(limits, request):
    limits.setenv("RATE_LIMIT_AUTH", "0")
    base = request.getfixturevalue("http_server")
    assert all(httpx.post(f"{base}/token", data={}).status_code != 429 for _ in range(40))
