"""Egress guard for async clients and the scope of the connect-time filter.

Before: ``netguard.guard`` filtered by patching ``socket.getaddrinfo``. The server runs
on uvloop, whose ``loop.getaddrinfo`` never calls ``socket.getaddrinfo``, and an IP
literal (a redirect to ``http://[::1]/`` or ``http://169.254.169.254/``) is connected
without any lookup at all — so the async MCP gateway, which also followed redirects,
could be steered at loopback / metadata / LAN addresses after a clean preflight. And
while any guard was active, EVERY lookup in the process was filtered, other threads
and other asyncio tasks included.

Now an async request resolves its host once, checks every answer and connects to a
checked address (Host header and TLS name unchanged), on every hop; the gateway
stays on the server's own origin; and the getaddrinfo filter applies only to the
context that entered the guard.

Real local sockets, fake DNS for the names; documentation addresses only.
"""
import asyncio
import importlib
import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
import uvloop

LOOPS = {"asyncio": None, "uvloop": uvloop.new_event_loop}


def _run(coro, loop="uvloop"):
    return asyncio.run(coro, loop_factory=LOOPS[loop])


class _V6Server(HTTPServer):
    address_family = socket.AF_INET6


class Upstream:
    """A tiny HTTP server that records every request and answers JSON-RPC (or redirects)."""

    def __init__(self, host="127.0.0.1", redirect="", only=""):
        self.hits, self.redirect, self.only = [], redirect, only
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self):
                size = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(size) if size else b""
                outer.hits.append({"path": self.path, "host": self.headers.get("Host")})
                if outer.redirect and (not outer.only or self.path == outer.only):
                    self.send_response(307)
                    self.send_header("Location", outer.redirect)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                try:
                    rid = json.loads(raw or b"{}").get("id")
                except ValueError:
                    rid = None
                if rid is None and raw:
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = json.dumps({"jsonrpc": "2.0", "id": rid, "result": {
                    "protocolVersion": "2025-06-18", "tools": []}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_POST = do_GET = _answer

            def log_message(self, *args):
                pass

        cls = _V6Server if ":" in host else HTTPServer
        self.server = cls((host, 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def upstreams():
    made = []

    def make(**kw):
        made.append(Upstream(**kw))
        return made[-1]
    yield make
    for u in made:
        u.close()


@pytest.fixture
def mods(monkeypatch):
    monkeypatch.delenv("INTERNAL_ALLOW_CIDRS", raising=False)
    gw = importlib.import_module("mcp_gateway")
    return gw, sys.modules["netguard"]


def _answer(ip, port=0):
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    addr = (ip, port, 0, 0) if fam == socket.AF_INET6 else (ip, port)
    return [(fam, socket.SOCK_STREAM, 6, "", addr)]


def _rebinding_dns(monkeypatch, name, first, then):
    """`name` answers `first` once (the preflight), then `then` for good."""
    real, seen = socket.getaddrinfo, []

    def fake(host, port=None, *a, **k):
        if host != name:
            return real(host, port, *a, **k)
        seen.append(host)
        return _answer(first if len(seen) == 1 else then, port or 0)
    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return seen


async def _list(client):
    return await client.list_tools()


# ── finding: SSRF guard bypass under uvloop / via redirects ─────────────────
def test_uvloop_connect_cannot_reach_loopback_after_a_clean_preflight(mods, monkeypatch, upstreams):
    gw, ng = mods
    up = upstreams()
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "203.0.113.0/24")
    _rebinding_dns(monkeypatch, "localhost", "203.0.113.10", "127.0.0.1")
    url = f"http://localhost:{up.port}/mcp"
    assert ng.check_url(url) == (True, "ok")
    with pytest.raises(Exception, match="internal/blocked at connect"):
        _run(gw._run("probe", {"url": url}, _list))
    assert up.hits == []


@pytest.mark.parametrize("loop", ["asyncio", "uvloop"])
def test_a_redirect_to_an_internal_address_is_never_followed(mods, monkeypatch, upstreams, loop):
    gw, ng = mods
    inner = upstreams(host="::1")
    front = upstreams(redirect=f"http://[::1]:{inner.port}/mcp")
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "127.0.0.1/32")
    url = f"http://127.0.0.1:{front.port}/mcp"
    assert ng.check_url(url) == (True, "ok")
    with pytest.raises(Exception):
        _run(gw._run("probe", {"url": url}, _list), loop)
    assert front.hits and inner.hits == []


def test_the_gateway_stays_on_its_origin_even_for_an_allowed_redirect(mods, monkeypatch, upstreams):
    gw, _ = mods
    other = upstreams()
    front = upstreams(redirect=f"http://127.0.0.1:{other.port}/mcp")   # same IP, other port
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "127.0.0.1/32")
    with pytest.raises(Exception, match="another address"):
        _run(gw._run("probe", {"url": f"http://127.0.0.1:{front.port}/mcp"}, _list))
    assert other.hits == []


def test_a_same_origin_redirect_still_works(mods, monkeypatch, upstreams):
    gw, _ = mods
    up = upstreams()
    up.redirect, up.only = f"http://127.0.0.1:{up.port}/new", "/old"
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "127.0.0.1/32")
    assert _run(gw._run("probe", {"url": f"http://127.0.0.1:{up.port}/old"}, _list)) == []
    assert {h["path"] for h in up.hits} == {"/old", "/new"}


def test_the_pinned_connection_keeps_the_host_name_in_the_host_header(mods, monkeypatch, upstreams):
    gw, ng = mods
    up = upstreams()
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "127.0.0.1/32")
    _rebinding_dns(monkeypatch, "svc.example.com", "127.0.0.1", "127.0.0.1")

    async def go():
        async with httpx.AsyncClient(transport=ng.pinned_async_transport()) as client:
            return await client.get(f"http://svc.example.com:{up.port}/x")
    assert _run(go()).status_code == 200
    assert up.hits == [{"path": "/x", "host": f"svc.example.com:{up.port}"}]


def test_the_pinned_connection_sends_the_original_name_as_tls_sni(mods, monkeypatch):
    _, ng = mods
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "203.0.113.0/24")
    _rebinding_dns(monkeypatch, "api.example.com", "203.0.113.7", "203.0.113.7")
    seen = []

    class Inner(httpx.AsyncBaseTransport):
        def __init__(self, **kw):
            pass

        async def handle_async_request(self, request):
            seen.append((str(request.url), request.headers["host"], request.extensions.get("sni_hostname")))
            return httpx.Response(204)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", Inner)

    async def go():
        async with httpx.AsyncClient(transport=ng.pinned_async_transport()) as client:
            return await client.get("https://api.example.com/v1?q=1")
    assert _run(go()).status_code == 204
    assert seen == [("https://203.0.113.7/v1?q=1", "api.example.com", "api.example.com")]


def test_the_sse_transport_gets_a_pinned_http_client(mods, monkeypatch, upstreams):
    gw, _ = mods
    import fastmcp
    import fastmcp.client.transports as fm_transports
    captured = {}

    def fake_transport(url, headers=None, **kw):
        captured.update(kw)
        return url
    monkeypatch.setattr(fm_transports, "SSETransport", fake_transport)
    monkeypatch.setattr(fastmcp, "Client", lambda transport: transport)
    gw._make_client({"url": "https://mcp.example.com/sse", "transport": "sse"}, {})
    factory = captured.get("httpx_client_factory")
    assert factory is not None
    up = upstreams()
    _rebinding_dns(monkeypatch, "sse.example.com", "127.0.0.1", "127.0.0.1")   # not allow-listed

    async def go():
        async with factory() as client:
            return await client.get(f"http://sse.example.com:{up.port}/sse")
    with pytest.raises(Exception, match="internal/blocked at connect"):
        _run(go())
    assert up.hits == []


# ── finding: the getaddrinfo filter leaked into unrelated lookups ───────────
def _fake_lan(monkeypatch):
    def fake(host, port=None, *a, **k):
        return _answer({"lan.example.com": "10.0.0.5", "api.example.com": "203.0.113.9"}[host], port or 0)
    monkeypatch.setattr(socket, "getaddrinfo", fake)


def test_a_guard_does_not_filter_lookups_made_by_other_threads(mods, monkeypatch):
    _, ng = mods
    _fake_lan(monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def holder():
        with ng.guard("api.example.com"):
            entered.set()
            release.wait(5)
    t = threading.Thread(target=holder)
    t.start()
    try:
        assert entered.wait(5)
        assert socket.getaddrinfo("lan.example.com", 80)[0][4][0] == "10.0.0.5"
    finally:
        release.set()
        t.join(5)


def test_a_guard_does_not_filter_lookups_of_other_asyncio_tasks(mods, monkeypatch):
    _, ng = mods
    _fake_lan(monkeypatch)

    async def go():
        entered, release = asyncio.Event(), asyncio.Event()

        async def guarded():
            with ng.guard("api.example.com"):
                entered.set()
                await release.wait()

        task = asyncio.create_task(guarded())
        await entered.wait()
        try:
            return socket.getaddrinfo("lan.example.com", 80)[0][4][0]
        finally:
            release.set()
            await task
    assert _run(go(), "asyncio") == "10.0.0.5"


def test_the_guarded_call_itself_stays_filtered_in_its_worker_thread(mods, monkeypatch):
    _, ng = mods
    _fake_lan(monkeypatch)

    async def go():
        with ng.guard("api.example.com"):
            return await asyncio.to_thread(socket.getaddrinfo, "lan.example.com", 80)
    with pytest.raises(socket.gaierror, match="internal/blocked at connect"):
        _run(go(), "asyncio")


def test_a_sync_redirect_to_an_internal_address_stays_blocked(mods, monkeypatch, upstreams):
    _, ng = mods
    inner = upstreams(host="::1")
    front = upstreams(redirect=f"http://[::1]:{inner.port}/x")
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "127.0.0.1/32")
    with ng.guard("127.0.0.1"), pytest.raises(httpx.ConnectError):
        httpx.get(f"http://127.0.0.1:{front.port}/x", follow_redirects=True, timeout=5)
    assert front.hits and inner.hits == []


def test_a_runner_model_endpoint_is_pinned_too(mods, monkeypatch, upstreams):
    """The built-in runner asks a model at an address a person chose — same bypass."""
    _, ng = mods
    builtin = importlib.import_module("runner_builtin")
    up = upstreams()
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "203.0.113.0/24")
    _rebinding_dns(monkeypatch, "localhost", "203.0.113.10", "127.0.0.1")
    url = f"http://localhost:{up.port}/v1/chat/completions"
    assert ng.check_url(url) == (True, "ok")
    with pytest.raises(Exception, match="internal/blocked at connect"):
        _run(builtin._post(url, {"model": "m"}, {}, "localhost"))
    assert up.hits == []
