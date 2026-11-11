"""Outbound tools answer with the exception's type and a generic reason, not its text.

Regression for the re-review finding "error text": tools returned the exception text
of a failed outbound request as a normal result — "Request failed: {exc}"
(call_service), "Send failed: {exc}" (webhook_send) and the same in the gateway,
WebDAV, CalDAV, FTP, SMTP, IMAP, MQTT, SSH, scanner and printer tools. A library's
exception text can carry internal addresses and ports, file paths, URLs with
credentials or parts of a server's reply — all of it handed to the model, and to
whoever steers it. Now the tools say which exception it was and give a short generic
reason; the full text goes to the server log (logger "aicortex.outbound"). Messages
AICortex writes itself for the caller (the network policy's refusals, an upstream
MCP server's HTTP status …) are passed on unchanged.
"""
import logging
import socket
import sys
from types import SimpleNamespace

import httpx
import pytest
from fastmcp import Client

LEAK = "token=s3cr3t-value@10.0.0.7:8443 /data/vault/secrets.enc"


def _call(server, tool, args):
    import asyncio

    async def go():
        async with Client(server.mcp) as client:
            res = await client.call_tool(tool, args, raise_on_error=False)
            return res.content[0].text if res.content else ""
    return asyncio.run(go())


@pytest.fixture
def srv(monkeypatch, fresh_server):
    ng = sys.modules["netguard"]
    monkeypatch.setattr(ng, "check_url", lambda url: (True, "ok"))
    monkeypatch.setattr(ng, "check_host", lambda host: (True, "ok"))
    return fresh_server


def _leaky(exc_type):
    def boom(*_a, **_k):
        raise exc_type(LEAK)
    return boom


@pytest.mark.parametrize("exc_type,reason", [
    (httpx.ConnectError, "could not connect"),
    (httpx.ReadTimeout, "timed out"),
    (RuntimeError, "details in the server log"),
])
def test_call_service_reports_the_type_not_the_text(srv, monkeypatch, caplog, exc_type, reason):
    folder = sys.modules["services"].SERVICES_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "api.json").write_text('{"name": "api", "base_url": "https://api.example.com"}')
    monkeypatch.setattr(httpx, "request", _leaky(exc_type))
    with caplog.at_level(logging.WARNING, logger="aicortex.outbound"):
        out = _call(srv, "call_service", {"service": "api"})
    assert out.startswith("Request failed:") and exc_type.__name__ in out and reason in out
    assert "s3cr3t" not in out and "10.0.0.7" not in out and "/data/vault" not in out
    assert LEAK in caplog.text                                      # the operator still sees it


def test_webhook_send_reports_the_type_not_the_text(srv, monkeypatch, caplog):
    monkeypatch.setattr(httpx, "post", _leaky(httpx.ConnectError))
    with caplog.at_level(logging.WARNING, logger="aicortex.outbound"):
        out = _call(srv, "webhook_send", {"url": "https://hooks.example.com/n"})
    assert out.startswith("Send failed:") and "ConnectError" in out and "s3cr3t" not in out
    assert LEAK in caplog.text


def test_own_messages_and_policy_refusals_pass_unchanged(srv):
    errtext = sys.modules["errtext"]
    assert errtext.safe(errtext.Reportable("HTTP 500 Server Error on 'tools/call'"), "x") == (
        "HTTP 500 Server Error on 'tools/call'")
    blocked = sys.modules["netguard"].Blocked("netguard: every address for 'x' is internal")
    assert errtext.safe(blocked, "x").startswith("netguard: every address")
    rebind = socket.gaierror("netguard: every address for 'x' is internal/blocked at connect time")
    assert "internal/blocked at connect" in errtext.safe(rebind, "x")
    assert errtext.safe(socket.gaierror(LEAK), "x") == (
        "gaierror (the host name did not resolve; details in the server log)")


@pytest.mark.parametrize("module,tool,args", [
    ("webdav_tools", "webdav_list", {"endpoint": "home"}),
    ("caldav_tools", "caldav_list_calendars", {"endpoint": "home"}),
])
def test_dav_tools_report_the_type_not_the_text(srv, monkeypatch, module, tool, args):
    mod = sys.modules[module]
    add = "webdav_add" if module == "webdav_tools" else "caldav_add"
    assert _call(srv, add, {"name": "home", "base_url": "https://dav.example.com/",
                            "username": "alice"}).startswith("Registered")

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, *a, **k):
            raise httpx.ConnectError(LEAK)
    monkeypatch.setattr(mod, "httpx", SimpleNamespace(Client=_Client, ConnectError=httpx.ConnectError))
    out = _call(srv, tool, args)
    assert "ConnectError" in out and "s3cr3t" not in out and "10.0.0.7" not in out
