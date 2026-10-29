"""End-to-end over a real MCP session (in-memory transport, no network).

Drives the fully assembled server the way a client does — initialize, tools/list,
tools/call — so wiring problems (middleware, registration, auth) show up here
rather than only on the NAS.
"""
import asyncio

from fastmcp import Client


def _run(coro):
    return asyncio.run(coro)


def test_server_lists_and_calls_tools(fresh_server):
    async def go():
        async with Client(fresh_server.mcp) as client:
            tools = {t.name for t in await client.list_tools()}
            pong = await client.call_tool("ping", {"name": "e2e"})
            return tools, pong

    tools, pong = _run(go())
    for name in ("bootstrap", "memory_write", "memory_read", "skill_search", "call_service"):
        assert name in tools
    assert "alive" in pong.content[0].text


def test_memory_roundtrip_over_mcp(fresh_server):
    async def go():
        async with Client(fresh_server.mcp) as client:
            await client.call_tool("memory_write", {
                "title": "E2E note", "content": "remember the e2e marker", "type": "reference",
            })
            hits = await client.call_tool("memory_search", {"query": "e2e marker"})
            return hits.content[0].text

    assert "e2e-note" in _run(go())
