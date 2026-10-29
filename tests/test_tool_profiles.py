"""Tool profiles: few tools up front, every function still reachable.

Before: every client got all 124 tools (~50,000 characters of descriptions and
schemas) — more than small models handle and, with an agent's own tools, past
OpenAI's 128-tool limit. Profile "core" (default) lists about ten tools; the
rest is found with integration_search and run with integration_call. Profile
"full" lists everything. Nothing is removed, and the router obeys the same role
and key-scope rules as a direct call.
"""
import asyncio
import json
import sys

import pytest
from fastmcp import Client


def _session(server, fn):
    async def go():
        async with Client(server.mcp) as client:
            return await fn(client)
    return asyncio.run(go())


@pytest.fixture
def core_server(monkeypatch, request):
    monkeypatch.setenv("TOOL_PROFILE_DEFAULT", "core")
    return request.getfixturevalue("fresh_server")


def test_core_profile_lists_about_ten_tools(core_server):
    names = _session(core_server, lambda c: c.list_tools())
    names = {t.name for t in names}
    assert len(names) <= 12, sorted(names)
    assert {"cortex_start", "memory_search", "memory_write", "skill_search",
            "integration_search", "integration_call"} <= names


def test_integration_search_finds_hidden_tools_with_their_parameters(core_server):
    async def go(c):
        res = await c.call_tool("integration_search", {"query": "print a document on the printer"})
        return res.content[0].text
    text = _session(core_server, go)
    assert "print_document" in text
    assert "printer" in text                  # parameters are shown so the call can be made


def test_integration_call_runs_a_hidden_tool(core_server):
    async def go(c):
        await c.call_tool("integration_call", {"name": "task_add", "arguments": {"title": "via router"}})
        res = await c.call_tool("integration_call", {"name": "task_list", "arguments": {}})
        return res.content[0].text
    assert "via router" in _session(core_server, go)


def test_router_obeys_roles_and_key_scopes(monkeypatch, core_server):
    authz = sys.modules["authz"]
    monkeypatch.setattr(authz, "_identity", lambda: (
        "bob", False, {"aicortex_key": "k1", "key_scopes": ["memory_search", "memory_read",
                                                             "memory_write", "integration_search",
                                                             "integration_call", "cortex_start"]}))

    async def go(c):
        found = (await c.call_tool("integration_search", {"query": "secret vault set"})).content[0].text
        admin = await c.call_tool("integration_call", {"name": "secret_set",
                                                       "arguments": {"name": "X", "value": "y"}},
                                  raise_on_error=False)
        out_of_scope = await c.call_tool("integration_call", {"name": "fs_write",
                                                              "arguments": {"path": "a.txt", "content": "b"}},
                                         raise_on_error=False)
        return found, admin, out_of_scope

    found, admin, out_of_scope = _session(core_server, go)
    assert "secret_set" not in found
    assert admin.is_error
    assert out_of_scope.is_error


def test_full_profile_lists_everything(monkeypatch, request):
    monkeypatch.setenv("TOOL_PROFILE_DEFAULT", "full")
    server = request.getfixturevalue("fresh_server")
    names = {t.name for t in _session(server, lambda c: c.list_tools())}
    assert len(names) > 100


def test_agent_key_can_carry_its_own_profile(monkeypatch, core_server):
    apikeys = sys.modules["apikeys"]
    _, rec = apikeys.create("alice", name="claude-code", scopes="all", profile="full")
    assert rec["profile"] == "full"
    stored = json.loads(apikeys._keyid_path(rec["keyid"]).read_text(encoding="utf-8"))
    assert stored["profile"] == "full"


def test_viewer_can_start_and_search_but_router_stays_read_only(monkeypatch, core_server):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    authz = sys.modules["authz"]
    monkeypatch.setattr(authz, "_identity", lambda: ("vera", False, {}))
    monkeypatch.setattr(authz, "role_for", lambda ident, is_runner, claims=None: "viewer")

    async def go(c):
        start = await c.call_tool("cortex_start", {}, raise_on_error=False)
        found = await c.call_tool("integration_search", {"query": "list tasks"}, raise_on_error=False)
        read = await c.call_tool("integration_call", {"name": "service_list", "arguments": {}},
                                 raise_on_error=False)
        write = await c.call_tool("integration_call", {"name": "task_add",
                                                       "arguments": {"title": "x"}},
                                  raise_on_error=False)
        return start, found, read, write

    start, found, read, write = _session(core_server, go)
    assert not start.is_error and not found.is_error and not read.is_error
    assert write.is_error
