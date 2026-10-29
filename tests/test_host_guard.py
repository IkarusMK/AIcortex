"""LAN clients and Docker neighbours aren't turned away with 421.

Regression: with BASE_URL set (needed for OAuth) the server passed an explicit
host allow-list of just the public name, so http://<nas-ip>:8787/mcp from the
LAN, http://aicortex:8787 from a sibling container or a VPN name all got
"421 Misdirected Request" — the likely reason self-hosted agents never got in.
"""
import asyncio

from fastmcp.server.http import HostOriginGuardMiddleware


def _status(guard_lists, host, origin=None):
    hosts, origins = guard_lists
    seen = {}

    async def app(scope, receive, send):
        seen["reached"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = HostOriginGuardMiddleware(app, allowed_hosts=hosts, allowed_origins=origins)
    headers = [(b"host", host.encode())] + ([(b"origin", origin.encode())] if origin else [])
    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": headers,
             "server": ("172.18.0.5", 8787), "scheme": "http", "query_string": b""}
    sent = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    asyncio.run(mw(scope, receive, send))
    return sent[0]["status"]


def test_lan_and_container_hosts_pass_with_base_url(monkeypatch, request):
    monkeypatch.setenv("BASE_URL", "https://agent.example.net")
    server = request.getfixturevalue("fresh_server")
    lists = server._host_guard()
    assert _status(lists, "192.168.1.20:8787") == 200
    assert _status(lists, "aicortex:8787") == 200
    assert _status(lists, "agent.example.net") == 200


def test_explicit_allow_list_is_strict_and_includes_base_url(monkeypatch, request):
    monkeypatch.setenv("BASE_URL", "https://agent.example.net")
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "nas.lan:8787")
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://chat.example.net")
    server = request.getfixturevalue("fresh_server")
    lists = server._host_guard()
    assert _status(lists, "nas.lan:8787") == 200
    assert _status(lists, "agent.example.net") == 200
    assert _status(lists, "evil.example.com") == 421
    assert _status(lists, "nas.lan:8787", origin="https://chat.example.net") == 200
