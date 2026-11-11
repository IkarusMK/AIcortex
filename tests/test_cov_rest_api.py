"""rest_api over real HTTP: every gate applies on every call.

Proves for /api/v1 (tools list, openapi.json, POST tools/<name>):

* 401 + ``WWW-Authenticate`` for a missing, wrong, expired, disabled, revoked or
  query-string key (the key is accepted from the Authorization header ONLY);
* 429 + ``Retry-After`` once a key exhausts its budget, per key, and failed logins
  do not eat a real key's budget;
* 403 for tools outside the key's scope, for the hard denylist (secrets, key
  management, tenancy) even with scope ``all`` and even for an admin-role key, and
  for admin-only tools called by a plain ``user`` key; a viewer key is read-only;
* the role is never admin by default, the identity comes from the key (never from
  the body) and a confined user cannot reach another person's memory;
* 400/404/413/500 error paths, SSE streaming (result, error, keepalive), the
  per-key OpenAPI document, and ``API_ENABLED=0`` switching the layer off.
"""
import asyncio
import json
import sys
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet

ALL_ROUTES = [("GET", "/api/v1/tools"), ("GET", "/api/v1/openapi.json"),
              ("POST", "/api/v1/tools/memory_list")]


@pytest.fixture
def api(monkeypatch, request):
    """Real HTTP server + helpers. Keys are minted for fictitious people."""
    monkeypatch.setenv("ALLOW_INSECURE", "1")        # open /mcp; /api is keyed regardless
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    base = request.getfixturevalue("http_server")
    server, apikeys, authz = sys.modules["server"], sys.modules["apikeys"], sys.modules["authz"]
    authz.AUTH_DIR.mkdir(parents=True, exist_ok=True)
    authz.POLICY_FILE.write_text(json.dumps({"roles": {"root": "admin", "vera": "viewer"}}))

    @server.mcp.tool
    def who_probe(claim: str = "") -> str:
        """Report the effective identity of this call."""
        ident, role = authz.effective_identity()
        return f"{ident}/{role}|{claim}"

    @server.mcp.tool
    def dict_probe() -> dict:
        """Return structured data."""
        return {"a": 1}

    @server.mcp.tool
    def boom_probe() -> str:
        """Always fails."""
        raise ValueError("kaboom")

    @server.mcp.tool
    async def slow_probe() -> str:
        """Takes a moment."""
        await asyncio.sleep(0.18)
        return "done"

    def call(method, path, key=None, **kw):
        headers = dict(kw.pop("headers", {}))
        if key:
            headers.setdefault("Authorization", f"Bearer {key}")
        return httpx.request(method, base + path, headers=headers, timeout=10, **kw)

    def mint(sub="alice", scopes="all", **kw):
        return apikeys.create(sub, scopes=scopes, **kw)[0]

    def audit():
        if not authz.AUDIT_FILE.exists():
            return []
        return [json.loads(x) for x in authz.AUDIT_FILE.read_text().splitlines() if x]

    return SimpleNamespace(base=base, call=call, mint=mint, audit=audit, apikeys=apikeys,
                           authz=authz, rest=sys.modules["rest_api"], mp=monkeypatch,
                           tenancy=sys.modules["tenancy"])


def _events(text):
    out = []
    for block in text.split("\n\n"):
        lines = [x for x in block.split("\n") if x]
        if lines and lines[0].startswith("event: "):
            out.append((lines[0][7:], json.loads(lines[1][6:])))
    return out


# ── 401: authentication ─────────────────────────────────────────────────────

@pytest.mark.parametrize("method,path", ALL_ROUTES)
def test_missing_key_is_401_on_every_route(api, method, path):
    r = api.call(method, path)
    assert r.status_code == 401 and r.json() == {"error": "unauthorized"}
    assert r.headers["www-authenticate"] == "Bearer"


def test_wrong_malformed_and_query_string_keys_are_401_with_one_uniform_body(api):
    good = api.mint()
    bodies = []
    for headers in ({"Authorization": good[:-3] + "xyz"},              # no scheme
                    {"Authorization": f"Basic {good}"},                # wrong scheme
                    {"Authorization": "Bearer"},                       # empty
                    {"Authorization": "Bearer ak_deadbeefdead_" + "A" * 43},   # unknown id
                    {"Authorization": "Bearer " + good[:-3] + "xyz"},  # wrong secret
                    {"X-Api-Key": good}):                              # wrong header
        r = api.call("GET", "/api/v1/tools", headers=headers)
        assert r.status_code == 401, headers
        bodies.append(r.text)
    assert len(set(bodies)) == 1                       # unknown id vs wrong secret: no oracle
    assert api.call("GET", f"/api/v1/tools?api_key={good}&key={good}&token={good}").status_code == 401


def test_bearer_scheme_is_case_insensitive(api):
    assert api.call("GET", "/api/v1/tools", headers={"Authorization": f"bEaReR {api.mint()}"}).status_code == 200


@pytest.mark.parametrize("breakage", ["expired", "disabled", "revoked"])
def test_unusable_keys_are_401(api, breakage):
    key = api.mint()
    keyid = key.split("_")[1]
    assert api.call("GET", "/api/v1/tools", key).status_code == 200
    path = api.apikeys.APIKEY_DIR / f"{keyid}.json"
    rec = json.loads(path.read_text())
    if breakage == "revoked":
        api.apikeys.revoke(keyid)
    else:
        rec.update({"expires": 1} if breakage == "expired" else {"disabled": True})
        path.write_text(json.dumps(rec))
    assert api.call("GET", "/api/v1/tools", key).status_code == 401


@pytest.mark.parametrize("method,path", ALL_ROUTES)
def test_api_enabled_zero_switches_every_route_off(api, method, path):
    api.mp.setenv("API_ENABLED", "off")
    for key in (None, api.mint()):
        r = api.call(method, path, key)
        assert r.status_code == 404 and r.json() == {"error": "REST API disabled"}


# ── 429: rate limiting ──────────────────────────────────────────────────────

def test_rate_limit_is_per_key_and_answers_429_with_retry_after(api):
    api.mp.setattr(api.apikeys, "_RATE_PER_MIN", 3)
    a, b = api.mint("alice"), api.mint("bob")
    assert [api.call("GET", "/api/v1/tools", a).status_code for _ in range(3)] == [200] * 3
    r = api.call("POST", "/api/v1/tools/memory_list", a)
    assert r.status_code == 429 and r.json()["error"] == "rate limited"
    assert 1 <= int(r.headers["retry-after"]) <= 61 and r.json()["retry_after"] == int(r.headers["retry-after"])
    assert api.call("GET", "/api/v1/openapi.json", a).status_code == 429       # shared budget
    assert api.call("GET", "/api/v1/tools", b).status_code == 200              # other key unaffected


def test_failed_logins_do_not_consume_a_real_keys_budget(api):
    api.mp.setattr(api.apikeys, "_RATE_PER_MIN", 2)
    key = api.mint()
    for _ in range(5):
        assert api.call("GET", "/api/v1/tools", key[:-2] + "zz").status_code == 401
    assert api.call("GET", "/api/v1/tools", key).status_code == 200


# ── 403: scope, denylist, role ──────────────────────────────────────────────

def test_tool_outside_the_key_scope_is_403_and_never_runs(api):
    key = api.mint("alice", scopes="memory_read")
    r = api.call("POST", "/api/v1/tools/memory_write", key,
                 json={"title": "t", "content": "c", "type": "user"})
    assert r.status_code == 403 and r.json()["detail"] == "tool not in this key's scope"
    assert api.call("POST", "/api/v1/tools/memory_read", key, json={"name": "t"}).status_code == 200
    rec = [x for x in api.audit() if x["tool"] == "api:memory_write"][-1]
    assert rec["decision"] == "deny" and rec["identity"] == "alice" and "out of key scope" in rec["reason"]
    assert key.split("_")[1] in rec["reason"] and key not in json.dumps(api.audit())


@pytest.mark.parametrize("sub", ["alice", "root"])
@pytest.mark.parametrize("tool", ["secret_set", "secret_delete", "apikey_create", "apikey_list",
                                  "apikey_revoke", "tenancy_set", "tenancy_list", "tenancy_unset"])
def test_hard_denylist_beats_scope_all_even_for_an_admin_key(api, sub, tool):
    key = api.mint(sub, scopes="all")
    for suffix in ("", "?stream=1"):
        r = api.call("POST", f"/api/v1/tools/{tool}{suffix}", key, json={})
        assert r.status_code == 403 and r.json()["error"] == "forbidden"
    assert tool not in {t["name"] for t in api.call("GET", "/api/v1/tools", key).json()["tools"]}


def test_user_key_cannot_call_admin_tools_but_admin_key_passes_the_gate(api):
    r = api.call("POST", "/api/v1/tools/service_add", api.mint("alice"), json={"name": "x"})
    assert r.status_code == 403 and "admin-only" in r.json()["detail"]
    assert api.call("POST", "/api/v1/tools/service_add", api.mint("root"), json={}).status_code != 403


def test_viewer_key_is_read_only(api):
    key = api.mint("vera")
    body = {"title": "t", "content": "c", "type": "user"}
    r = api.call("POST", "/api/v1/tools/memory_write", key, json=body)
    assert r.status_code == 403 and "read-only" in r.json()["detail"]
    assert api.call("POST", "/api/v1/tools/memory_list", key).status_code == 200


def test_key_role_is_never_admin_by_default_and_identity_ignores_the_body(api):
    r = api.call("POST", "/api/v1/tools/who_probe", api.mint("alice"), json={"claim": "root/admin"})
    assert r.json() == {"ok": True, "tool": "who_probe", "result": "alice/user|root/admin"}
    assert api.call("POST", "/api/v1/tools/who_probe", api.mint("root")).json()["result"] == "root/admin|"


def test_tools_listing_shows_only_what_this_key_may_call(api):
    names = lambda key: [t["name"] for t in api.call("GET", "/api/v1/tools", key).json()["tools"]]  # noqa: E731
    scoped = names(api.mint("alice", scopes="memory"))
    assert scoped == sorted(scoped) and set(scoped) <= set(api.apikeys._ALIASES["memory"])
    assert "memory_read" in scoped and "fs_read" not in scoped
    user_all, admin_all = names(api.mint("alice")), names(api.mint("root"))
    assert "who_probe" in user_all and "service_add" not in user_all
    assert "service_add" in admin_all and "secret_set" not in admin_all
    body = api.call("GET", "/api/v1/tools", api.mint("bob", scopes="who_probe")).json()
    assert body["count"] == 1 and body["tools"][0]["name"] == "who_probe"
    assert body["tools"][0]["description"] and isinstance(body["tools"][0]["input_schema"], dict)


# ── confinement through the REST layer ──────────────────────────────────────

def test_a_user_key_is_confined_to_its_own_memory(api):
    alice, bob, root = api.mint("alice", scopes="memory"), api.mint("bob", scopes="memory"), api.mint("root")
    saved = api.call("POST", "/api/v1/tools/memory_write", alice,
                     json={"title": "Pin", "content": "alice-only-text", "type": "user", "scope": "shared"})
    assert f"users/{api.tenancy._safe('alice')}" in saved.json()["result"]
    own = api.call("POST", "/api/v1/tools/memory_read", alice, json={"name": "pin"}).json()["result"]
    assert "alice-only-text" in own
    for scope in ("shared", f"users/{api.tenancy._safe('alice')}"):       # bob cannot name her area
        got = api.call("POST", "/api/v1/tools/memory_read", bob, json={"name": "pin", "scope": scope})
        assert "alice-only-text" not in got.text
    assert "pin" in api.call("POST", "/api/v1/tools/memory_list", root,
                             json={"scope": f"users/{api.tenancy._safe('alice')}"}).json()["result"]
    assert "pin" not in api.call("POST", "/api/v1/tools/memory_list", root).json()["result"]


# ── 4xx/5xx on invoke ───────────────────────────────────────────────────────

def test_unknown_tool_is_404(api):
    r = api.call("POST", "/api/v1/tools/no_such_tool", api.mint(), json={})
    assert r.status_code == 404 and r.json() == {"error": "no such tool", "tool": "no_such_tool"}


@pytest.mark.parametrize("content,msg", [
    (b"{not json", "invalid JSON body"), (b"\xff\xfe", "invalid JSON body"),
    (b"[1, 2]", "body must be a JSON object"), (b'"text"', "body must be a JSON object"),
    (b"null", "body must be a JSON object"), (b"42", "body must be a JSON object")])
def test_bad_bodies_are_400(api, content, msg):
    r = api.call("POST", "/api/v1/tools/who_probe", api.mint(), content=content)
    assert r.status_code == 400 and msg in r.json()["error"]


def test_empty_and_whitespace_bodies_mean_no_arguments(api):
    key = api.mint()
    for content in (b"", b"  \n "):
        assert api.call("POST", "/api/v1/tools/who_probe", key, content=content).json()["result"] == "alice/user|"


def test_oversized_body_is_413_but_scope_is_checked_first(api):
    api.mp.setattr(api.rest, "_MAX_BODY", 40)
    big = json.dumps({"claim": "x" * 100}).encode()
    assert api.call("POST", "/api/v1/tools/who_probe", api.mint(), content=big).status_code == 413
    r = api.call("POST", "/api/v1/tools/fs_read", api.mint("bob", scopes="who_probe"), content=big)
    assert r.status_code == 403


def test_tool_results_are_unwrapped_or_passed_through_as_structured_data(api):
    key = api.mint()
    assert api.call("POST", "/api/v1/tools/dict_probe", key).json()["result"] == {"a": 1}
    assert api.call("POST", "/api/v1/tools/who_probe", key).json()["result"] == "alice/user|"


def test_tool_failures_are_500_with_a_reference_and_an_error_audit_line(api):
    r = api.call("POST", "/api/v1/tools/boom_probe", api.mint())
    assert r.status_code == 500 and r.json()["error"] == "tool error" and "kaboom" not in r.text
    ref = r.json()["error_id"]
    rec = [x for x in api.audit() if x["tool"] == "api:boom_probe"][-1]
    assert rec["decision"] == "error" and "kaboom" in rec["reason"] and ref in rec["reason"]
    bad_args = api.call("POST", "/api/v1/tools/memory_read", api.mint(), json={})       # name missing
    assert bad_args.status_code == 400 and bad_args.json()["error"] == "invalid arguments"


def test_successful_calls_are_audited_as_allow(api):
    api.call("POST", "/api/v1/tools/who_probe", api.mint("alice"))
    rec = [x for x in api.audit() if x["tool"] == "api:who_probe"][-1]
    assert (rec["decision"], rec["identity"], rec["role"]) == ("allow", "alice", "apikey")


# ── SSE streaming ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("path,headers", [
    ("/api/v1/tools/who_probe?stream=1", {}), ("/api/v1/tools/who_probe?stream=SSE", {}),
    ("/api/v1/tools/who_probe", {"Accept": "application/json, text/event-stream"})])
def test_stream_modes_return_connected_comment_then_one_result_event(api, path, headers):
    r = api.call("POST", path, api.mint(), headers=headers, json={"claim": "x"})
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache" and r.text.startswith(": connected\n\n")
    assert _events(r.text) == [("result", {"ok": True, "tool": "who_probe", "result": "alice/user|x"})]


def test_stream_off_values_give_plain_json(api):
    r = api.call("POST", "/api/v1/tools/who_probe?stream=0", api.mint())
    assert r.headers["content-type"].startswith("application/json")


def test_stream_reports_failures_and_unknown_tools_as_error_events(api):
    key = api.mint()
    boom = _events(api.call("POST", "/api/v1/tools/boom_probe?stream=1", key).text)
    assert boom[0][0] == "error" and boom[0][1]["error_id"] in boom[0][1]["detail"]
    assert "kaboom" not in json.dumps(boom)
    ghost = _events(api.call("POST", "/api/v1/tools/ghost_probe?stream=1", key).text)
    assert ghost[0][0] == "error" and "no such tool: ghost_probe" in ghost[0][1]["detail"]


def test_stream_sends_keepalives_while_a_slow_tool_runs(api):
    api.mp.setattr(api.rest, "_SSE_HEARTBEAT", 0.04)
    r = api.call("POST", "/api/v1/tools/slow_probe?stream=1", api.mint())
    assert r.text.count(": keepalive\n\n") >= 2
    assert _events(r.text)[-1] == ("result", {"ok": True, "tool": "slow_probe", "result": "done"})


# ── openapi.json ────────────────────────────────────────────────────────────

def test_openapi_lists_only_this_keys_tools(api):
    spec = api.call("GET", "/api/v1/openapi.json", api.mint("alice", scopes="who_probe, memory_read")).json()
    assert spec["openapi"] == "3.1.0" and spec["security"] == [{"bearerAuth": []}]
    assert spec["info"]["version"] == sys.modules["version"].__version__
    assert spec["components"]["securitySchemes"]["bearerAuth"] == {"type": "http", "scheme": "bearer"}
    assert sorted(spec["paths"]) == ["/api/v1/tools/memory_read", "/api/v1/tools/who_probe"]
    op = spec["paths"]["/api/v1/tools/who_probe"]["post"]
    assert op["operationId"] == "who_probe" and op["summary"] == "Report the effective identity of this call."
    assert op["requestBody"]["content"]["application/json"]["schema"]["type"] == "object"
    assert "secret_set" not in api.call("GET", "/api/v1/openapi.json", api.mint("root")).text


def test_openapi_server_url_uses_base_url_or_the_request_host(api):
    key = api.mint()
    assert api.call("GET", "/api/v1/openapi.json", key).json()["servers"] == [{"url": f"{api.base}/api/v1"}]
    api.mp.setenv("BASE_URL", "https://agent.example.net/")
    assert api.call("GET", "/api/v1/openapi.json", key).json()["servers"] == \
        [{"url": "https://agent.example.net/api/v1"}]
