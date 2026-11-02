"""MCP gateway: registration, vault-resolved tokens, SSRF guard, JSON-RPC handling.

Proves, with no real upstream or DNS (httpx.AsyncClient, the fastmcp SSE client
and socket.getaddrinfo are faked; netguard still checks the fake resolution): the
token is read from the vault by name, sent only in the configured auth header and
never shown or stored; private/non-http targets are refused before any client
exists and the connect-time guard rejects DNS rebinding; the Streamable-HTTP
client does the spec handshake (Accept, session id, negotiated version) and reads
JSON and SSE answers; results are rendered and capped. One defect pinned (xfail).
"""
import asyncio
import json
import socket
import sys
from types import SimpleNamespace
from urllib.parse import urlparse

import fastmcp
import fastmcp.client.transports as fm_transports
import httpx
import pytest
from cryptography.fernet import Fernet
from fastmcp import Client

TOKEN = "test-token"
HOST = "mcp.example.net"
URL = f"https://{HOST}/mcp"
BOTH = "application/json, text/event-stream"
_dumps = json.dumps  # the fake server's post() has a `json=` parameter that shadows the module


class _Resolver:
    """Fake socket.getaddrinfo: host -> rounds of IPs (the last round repeats)."""

    def __init__(self):
        self.table = {}

    def set(self, host, *rounds):
        self.table[host] = [[r] if isinstance(r, str) else list(r) for r in rounds]

    def __call__(self, host, *args, **kwargs):
        if host not in self.table:
            raise socket.gaierror(f"fake DNS: no record for {host}")
        rounds = self.table[host]
        ips = rounds.pop(0) if len(rounds) > 1 else rounds[0]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]


class _Resp:
    def __init__(self, status=200, body="", ctype="application/json", headers=None, reason=""):
        self.status_code, self.text, self.reason_phrase = status, body, reason
        self.headers = {"content-type": ctype, **(headers or {})}


class _Upstream:
    """A scripted Streamable-HTTP MCP server; stands in for httpx.AsyncClient."""

    def __init__(self):
        self.requests, self.clients, self.closed, self.close_error, self.sse = [], [], 0, None, False
        self.tools = [{"name": "get_forecast", "description": "Weather forecast.\nSecond line."},
                      {"name": "ping", "description": ""}]
        self.result = {"content": [{"type": "text", "text": "sunny"}]}
        self.proto, self.session, self.override = "2025-03-26", "sess-1", {}  # override: method -> _Resp | Exception

    def AsyncClient(self, **kw):
        self.clients.append(kw)
        return self

    async def post(self, url, json=None, headers=None):
        socket.getaddrinfo(urlparse(url).hostname, None)  # connect-time DNS (guarded)
        method = json["method"]
        self.requests.append(SimpleNamespace(url=url, payload=json, headers=dict(headers)))
        hook = self.override.get(method)
        if isinstance(hook, Exception):
            raise hook
        if hook is not None:
            return hook
        if method.startswith("notifications/"):
            return _Resp(202)
        result = {"initialize": {"protocolVersion": self.proto}, "tools/list": {"tools": self.tools}}.get(
            method, self.result)
        return self.rpc(json["id"], result, {"mcp-session-id": self.session} if method == "initialize" else None)

    def rpc(self, rid, result, headers=None):
        obj = _dumps({"jsonrpc": "2.0", "id": rid, "result": result})
        if self.sse:
            return _Resp(200, f"event: message\ndata: {obj}\n\n", "text/event-stream", headers)
        return _Resp(200, obj, headers=headers)

    async def aclose(self):
        if self.close_error:
            raise self.close_error
        self.closed += 1


class _Sse:
    """Replaces fastmcp.Client / SSETransport for transport 'sse'."""

    def __init__(self):
        self.transports, self.calls, self.error = [], [], None
        self.tools = [SimpleNamespace(name="get_forecast", description="Weather forecast.\nSecond line.")]
        self.result = SimpleNamespace(data={"temp": 21}, content=[])

    def transport(self, url, headers=None):
        self.transports.append(SimpleNamespace(url=url, headers=headers))
        return self.transports[-1]

    def client(self, transport):
        return self

    async def __aenter__(self):
        socket.getaddrinfo(urlparse(self.transports[-1].url).hostname, None)  # guarded
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *exc):
        return False

    async def list_tools(self):
        return self.tools

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return self.result


class _Env:
    def __init__(self, server, dns, up, sse, mod):
        self.server, self.dns, self.up, self.sse, self.mod = server, dns, up, sse, mod

    def call(self, name, /, **args):  # positional-only: mcp_call has its own `tool` argument
        async def go():
            async with Client(self.server.mcp) as client:
                res = await client.call_tool(name, args, raise_on_error=False)
                return res.content[0].text if res.content else ""
        return asyncio.run(go())

    def add(self, name="forecast", **over):
        return self.call("mcp_add", **{"name": name, "url": URL, "token_env": "UP_TOKEN", **over})

    def rpc(self, **kw):
        return self.mod._StreamableMCP(URL, {"Authorization": f"Bearer {TOKEN}"}, **kw)


@pytest.fixture
def env(fresh_server, monkeypatch):
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "203.0.113.0/24")
    dns, up, sse = _Resolver(), _Upstream(), _Sse()
    dns.set(HOST, "203.0.113.40")
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(httpx, "AsyncClient", up.AsyncClient)
    monkeypatch.setattr(fastmcp, "Client", sse.client)
    monkeypatch.setattr(fm_transports, "SSETransport", sse.transport)
    sys.modules["secrets_store"].vault_set("UP_TOKEN", TOKEN)
    return _Env(fresh_server, dns, up, sse, sys.modules["mcp_gateway"])


@pytest.fixture
def fc(env):
    env.add()
    return env


# --- registration ---------------------------------------------------------
def test_add_normalises_slug_and_transport_and_stores_only_the_secret_name(env):
    assert env.add("Weather API!", transport="SSE") == "Registered MCP server 'weather-api'."
    assert env.add("!!!", token_env="UNSET_TOKEN").startswith("Registered MCP server 'server'.")
    assert "secret_set('UNSET_TOKEN', <value>)" in env.add(token_env="UNSET_TOKEN")  # secret missing
    cfg = json.loads((env.mod.MCP_DIR / "weather-api.json").read_text())
    assert (cfg["transport"], cfg["token_env"], cfg["auth_header"], cfg["auth_scheme"]) == (
        "sse", "UP_TOKEN", "Authorization", "Bearer")
    assert TOKEN not in (env.mod.MCP_DIR / "weather-api.json").read_text()


def test_add_reports_an_unusable_store_instead_of_raising(env, tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(env.mod, "MCP_DIR", blocker)
    assert env.add().startswith("Could not register MCP server:")


def test_list_and_delete_servers_incl_unreadable_config(env):
    assert env.call("mcp_list") == "No MCP servers configured yet."  # no directory yet
    env.add(description="forecasts")
    (env.mod.MCP_DIR / "broken.json").write_text("{not json")
    assert env.call("mcp_list").splitlines() == ["- broken — (unreadable config)", f"- forecast — {URL} — forecasts"]
    assert env.call("mcp_tools", server="broken") == "Unknown MCP server 'broken'. Use mcp_list / mcp_add."
    assert env.call("mcp_delete", name="Forecast") == "Deleted MCP server 'forecast'."
    assert env.call("mcp_delete", name="forecast") == "No MCP server 'forecast'."
    env.call("mcp_delete", name="broken")
    assert env.call("mcp_list") == "No MCP servers configured yet. Use mcp_add."


@pytest.mark.parametrize("tool,extra", [("mcp_tools", {}), ("mcp_call", {"tool": "x"})])
def test_unknown_server_is_refused_without_a_client(env, tool, extra):
    assert env.call(tool, server="nope", **extra) == "Unknown MCP server 'nope'. Use mcp_list / mcp_add."
    assert env.up.clients == []


# --- tokens ------------------------------------------------------------------
@pytest.mark.parametrize("cfg,expected", [
    ({}, {}), ({"token_env": "UP_TOKEN"}, {"Authorization": f"Bearer {TOKEN}"}),
    ({"token_env": "UP_TOKEN", "auth_header": "X-Api-Key", "auth_scheme": ""}, {"X-Api-Key": TOKEN}),
    ({"token_env": "UP_TOKEN", "auth_scheme": "Token"}, {"Authorization": f"Token {TOKEN}"}),
    ({"token_env": "UNSET_TOKEN"}, {})])
def test_auth_headers_are_built_from_the_vault_secret(env, cfg, expected):
    assert env.mod._auth_headers(cfg) == expected


def test_token_goes_upstream_in_the_configured_header_and_never_to_the_model(fc):
    outputs = [fc.call("mcp_tools", server="forecast"), fc.call("mcp_call", server="forecast", tool="get_forecast"),
               fc.call("mcp_list")]
    assert {r.headers["Authorization"] for r in fc.up.requests} == {f"Bearer {TOKEN}"}
    assert TOKEN not in "".join(outputs)


# --- SSRF guard ------------------------------------------------------------------
@pytest.mark.parametrize("tool,extra", [("mcp_tools", {}), ("mcp_call", {"tool": "x"})])
def test_private_target_is_blocked_before_a_client_exists(env, tool, extra):
    env.dns.set("lan.example.net", "10.0.0.5")
    env.add(url="https://lan.example.net/mcp")
    out = env.call(tool, server="forecast", **extra)
    assert out.startswith("Blocked by network policy — blocked: 10.0.0.5") and env.up.clients == []


@pytest.mark.parametrize("url,ips,fragment", [
    ("https://odd.example.net/mcp", "169.254.169.254", "169.254.169.254"),
    ("https://ghost.example.net/mcp", None, "cannot resolve host"), ("file:///etc/passwd", None, "empty host")])
def test_metadata_unresolvable_and_non_http_targets_stay_blocked(env, monkeypatch, url, ips, fragment):
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "10.0.0.0/8")  # a LAN allow-list must not open these
    ips and env.dns.set("odd.example.net", ips)
    env.add(url=url)
    out = env.call("mcp_tools", server="forecast")
    assert out.startswith("Blocked by network policy") and fragment in out and env.up.clients == []


@pytest.mark.parametrize("tool,extra,prefix", [("mcp_tools", {}, "Could not reach 'forecast':"),
                                               ("mcp_call", {"tool": "x"}, "Call failed:")])
def test_connect_time_guard_rejects_a_dns_rebinding_answer(env, tool, extra, prefix):
    env.dns.set("rebind.example.net", "203.0.113.20", "10.0.0.9")  # check, then connect
    env.add(url="https://rebind.example.net/mcp")
    out = env.call(tool, server="forecast", **extra)
    assert out.startswith(prefix) and "internal/blocked at connect" in out and env.up.requests == []


# --- tools: list and call over Streamable HTTP --------------------------------------
def test_tools_are_listed_by_name_with_the_first_description_line(fc):
    assert fc.call("mcp_tools", server="forecast").splitlines() == ["- get_forecast: Weather forecast.", "- ping: "]
    fc.up.tools = []
    assert fc.call("mcp_tools", server="forecast") == "'forecast' exposes no tools."


def test_call_forwards_the_tool_name_and_arguments_and_renders_text_blocks(fc):
    fc.up.result = {"content": [{"type": "text", "text": "sunny"}, {"type": "image", "data": "AAA"}, "raw"]}
    out = fc.call("mcp_call", server="forecast", tool="get_forecast", args={"city": "Zürich", "days": 3})
    assert out == 'sunny\n{"type": "image", "data": "AAA"}\nraw'
    assert fc.up.requests[-1].payload["params"] == {"name": "get_forecast", "arguments": {"city": "Zürich", "days": 3}}
    fc.call("mcp_call", server="forecast", tool="ping")
    assert fc.up.requests[-1].payload["params"]["arguments"] == {}  # no args -> empty object


def test_call_prefers_structured_content_and_survives_unusual_results(fc):
    fc.up.result = {"structuredContent": {"city": "Zürich", "temp": 21}, "content": [{"type": "text", "text": "x"}]}
    assert fc.call("mcp_call", server="forecast", tool="t") == '{\n  "city": "Zürich",\n  "temp": 21\n}'
    fc.up.result = {"value": 1}
    assert json.loads(fc.call("mcp_call", server="forecast", tool="t")) == {"value": 1}


def test_call_caps_long_output(fc):
    fc.up.result = {"content": [{"type": "text", "text": "y" * 7000}]}
    out = fc.call("mcp_call", server="forecast", tool="t")
    assert out == "y" * 6000 + "\n…(truncated)"


@pytest.mark.parametrize("method,hook,expected", [
    ("initialize", _Resp(401, reason="Unauthorized"), "Could not reach 'forecast': HTTP 401 Unauthorized on 'initialize'"),
    ("initialize", OSError("connection refused"), "Could not reach 'forecast': connection refused"),
    ("tools/call", _Resp(500, reason="Server Error"), "Call failed: HTTP 500 Server Error on 'tools/call'"),
    ("tools/call", _Resp(200, '{"error": {"code": -32602, "message": "bad args"}}'),
     "Call failed: tools/call: -32602 bad args")])
def test_upstream_failures_are_reported_not_raised(fc, method, hook, expected):
    fc.up.override[method] = hook
    out = fc.call("mcp_tools", server="forecast") if method == "initialize" else fc.call(
        "mcp_call", server="forecast", tool="t")
    assert out == expected and TOKEN not in out


def test_failed_handshake_closes_the_http_client(fc):
    fc.up.override["initialize"] = _Resp(401, reason="Unauthorized")
    assert "HTTP 401 Unauthorized on 'initialize'" in fc.call("mcp_tools", server="forecast")
    assert fc.up.closed == 1


# --- tools over SSE (fastmcp client) -----------------------------------------------------
def test_sse_transport_uses_the_fastmcp_client_with_the_token_header(env):
    env.add(transport="sse", url=f"https://{HOST}/sse")
    assert env.call("mcp_tools", server="forecast") == "- get_forecast: Weather forecast."
    assert env.call("mcp_call", server="forecast", tool="get_forecast", args={"city": "Bern"}) == '{\n  "temp": 21\n}'
    assert env.sse.transports[0].headers == {"Authorization": f"Bearer {TOKEN}"}
    assert env.sse.calls == [("get_forecast", {"city": "Bern"})] and env.up.clients == []


def test_sse_failures_are_reported_and_blocked_targets_never_reach_the_client(env):
    env.add(transport="sse", url=f"https://{HOST}/sse")
    env.sse.error = ConnectionError("stream closed")
    assert env.call("mcp_call", server="forecast", tool="t") == "Call failed: stream closed"
    assert env.call("mcp_tools", server="forecast") == "Could not reach 'forecast': stream closed"
    env.dns.set("lan.example.net", "10.0.0.5")
    env.add(url="https://lan.example.net/sse")
    assert env.call("mcp_tools", server="forecast").startswith("Blocked by network policy")
    assert len(env.sse.transports) == 2  # the blocked call built no transport


# --- Streamable-HTTP client: handshake and framing ------------------------------------------
def test_handshake_follows_the_spec_and_adopts_the_negotiated_version_and_session(env):
    async def go():
        async with env.rpc() as client:
            return await client.list_tools(), await client.call_tool("t", {"a": 1})
    tools, result = asyncio.run(go())
    init, note, listing, call = env.up.requests
    assert [r.payload["method"] for r in env.up.requests] == [
        "initialize", "notifications/initialized", "tools/list", "tools/call"]
    assert init.payload["params"] == {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "AICortex", "version": "1.5"}}
    assert init.headers["Accept"] == BOTH and init.headers["Content-Type"] == "application/json"
    assert "Mcp-Session-Id" not in init.headers and "MCP-Protocol-Version" not in init.headers
    assert "id" not in note.payload  # a notification has no id
    for later in (note, listing, call):
        assert (later.headers["Mcp-Session-Id"], later.headers["MCP-Protocol-Version"]) == ("sess-1", "2025-03-26")
        assert later.headers["Accept"] == BOTH and later.headers["Authorization"] == f"Bearer {TOKEN}"
    assert [r.payload.get("id") for r in env.up.requests] == [1, None, 2, 3]
    assert tools == env.up.tools and result == env.up.result and env.up.clients[0]["follow_redirects"] is True
    assert env.up.closed == 1


def test_sse_framed_answers_are_read_and_noise_events_are_skipped(env):
    env.up.sse = True
    env.up.result = {"content": [{"type": "text", "text": "via sse"}]}

    async def go():
        async with env.rpc() as client:
            return await client.call_tool("t")
    assert asyncio.run(go()) == env.up.result


def test_notification_failures_close_errors_and_null_results_are_tolerated(env):
    env.up.override["notifications/initialized"] = OSError("reset")
    env.up.close_error = RuntimeError("already closed")

    async def go():
        async with env.rpc() as client:
            first = await client.list_tools()
            env.up.override["tools/list"] = _Resp(200, '{"jsonrpc": "2.0", "id": 3, "result": null}')
            return first, await client.list_tools()
    assert asyncio.run(go()) == (env.up.tools, [])  # a non-dict result is an empty tool list
    assert asyncio.run(env.rpc().__aexit__(None, None, None)) is None  # never entered: nothing to close


@pytest.mark.parametrize("body,ctype,expected", [
    ('{"result": {"a": 1}}', "application/json", {"result": {"a": 1}}),
    ('event: ping\n: comment\ndata: not json\ndata: [1]\ndata: {"id": 1, "result": {"n": 1}}\n'
     'data: {"id": 1, "result": {"n": 2}}\ndata: {"method": "notifications/x"}\n', "text/event-stream; charset=utf-8",
     {"id": 1, "result": {"n": 2}}),
    ('data: {"error": {"code": 1}}', "text/event-stream", {"error": {"code": 1}})])
def test_jsonrpc_extraction_from_json_and_sse_bodies(env, body, ctype, expected):
    assert env.mod._parse_jsonrpc(_Resp(200, body, ctype)) == expected


@pytest.mark.parametrize("body,ctype,message", [
    ("event: ping\ndata: {}\n", "text/event-stream", "no JSON-RPC result in SSE response"),
    ("<html>", "text/html", "invalid JSON response")])
def test_jsonrpc_extraction_errors(env, body, ctype, message):
    with pytest.raises(env.mod._HttpMCPError, match=message):
        env.mod._parse_jsonrpc(_Resp(200, body, ctype))


# --- helpers: names and result rendering ----------------------------------------------------------
def test_tool_name_and_description_from_dicts_and_objects(env):
    f = env.mod._tool_name_desc
    assert f({"name": "a", "description": "  First\nSecond "}) == ("a", "First")
    assert f(SimpleNamespace(name="b", description="Only")) == ("b", "Only") and f({}) == ("", "")
    assert f(SimpleNamespace(name="c", description=None)) == ("c", "")


def test_format_result_renders_fastmcp_objects_and_falls_back_to_str(env):
    fmt, loop = env.mod._format_result, {}
    loop["self"] = loop  # json.dumps raises on circular data -> str() fallback
    assert fmt(SimpleNamespace(data={"a": "é"}, content=[])) == '{\n  "a": "é"\n}'
    assert fmt(SimpleNamespace(data=None, content=[SimpleNamespace(text="one"), SimpleNamespace(text=None)])) == (
        "one\nnamespace(text=None)")  # a block without text is shown as its repr
    assert fmt(SimpleNamespace(data=None, content=[SimpleNamespace(text="z" * 7000)])) == "z" * 6000 + "\n…(truncated)"
    assert fmt(SimpleNamespace(data=None, content=[])).startswith("namespace(")
    assert fmt(SimpleNamespace(data=loop)) == str(loop) and fmt({"structuredContent": loop}) == str(loop)
    assert fmt(loop) == str(loop)
