"""Header-only clients (Hermes, OpenClaw, Agents SDK, Codex, LM Studio, ...) connect.

Before: the only static credential on /mcp was RUNNER_TOKEN, and it was only
accepted when OIDC was configured; per-agent API keys worked on REST only.
Now /mcp accepts, with or without OIDC:
- per-agent keys (ak_...) — identity = the key's person, agent name stamped,
  least-privilege role, the key's tool scopes enforced;
- RUNNER_TOKEN — the runner.
A request without a valid token gets a 401 with a bearer challenge.
"""
import asyncio
import sys

import httpx
import pytest
from fastmcp import Client


@pytest.fixture
def token_server(monkeypatch, request):
    monkeypatch.setenv("RUNNER_TOKEN", "runner-secret-123")
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    return request.getfixturevalue("http_server")


def _tools(url, token):
    async def go():
        async with Client(f"{url}/mcp", auth=token) as client:
            return {t.name for t in await client.list_tools()}
    return asyncio.run(go())


def _call(url, token, tool, args):
    async def go():
        async with Client(f"{url}/mcp", auth=token) as client:
            return await client.call_tool(tool, args, raise_on_error=False)
    return asyncio.run(go())


def test_request_without_token_is_challenged(token_server):
    r = httpx.post(f"{token_server}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                   headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 401
    assert r.headers.get("www-authenticate", "").lower().startswith("bearer")


def test_wrong_token_is_rejected(token_server):
    r = httpx.post(f"{token_server}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                   headers={"Accept": "application/json, text/event-stream",
                            "Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_runner_token_connects_without_oidc(token_server):
    assert "memory_write" in _tools(token_server, "runner-secret-123")


def test_agent_key_connects_as_its_person(token_server):
    apikeys = sys.modules["apikeys"]
    key, _ = apikeys.create("alice@example.com", name="hermes", scopes="all")

    res = _call(token_server, key, "memory_write",
                {"title": "Hermes note", "content": "written by hermes", "type": "reference"})
    assert not res.is_error, res.content[0].text

    tools = _tools(token_server, key)
    assert "memory_search" in tools
    assert "secret_set" not in tools            # a key is never an accidental admin


def test_agent_key_scopes_are_enforced(token_server):
    apikeys = sys.modules["apikeys"]
    key, _ = apikeys.create("bob@example.com", name="reader", scopes="memory_search,memory_read")

    assert _tools(token_server, key) <= {"memory_search", "memory_read"}
    res = _call(token_server, key, "fs_write", {"path": "x.txt", "content": "nope"})
    assert res.is_error


def test_revoked_key_is_rejected(token_server):
    apikeys = sys.modules["apikeys"]
    key, rec = apikeys.create("carol@example.com", name="tmp", scopes="all")
    assert apikeys.revoke(rec["keyid"])
    r = httpx.post(f"{token_server}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                   headers={"Accept": "application/json, text/event-stream",
                            "Authorization": f"Bearer {key}"})
    assert r.status_code == 401
