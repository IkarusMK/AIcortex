"""Every tool tells the client what it does to the world.

Claude and ChatGPT use the MCP tool annotations to decide when to ask the user
before a call (and Anthropic's connector directory requires them): read-only,
destructive (may delete or overwrite), open-world (talks to an outside system).
Before, no tool carried any — so clients had to treat everything as risky.
"""
import asyncio

from fastmcp import Client


def _tools(server):
    async def go():
        async with Client(server.mcp) as client:
            return {t.name: t for t in await client.list_tools()}
    return asyncio.run(go())


def test_every_tool_has_a_title_and_hints(fresh_server):
    for name, tool in _tools(fresh_server).items():
        a = tool.annotations
        assert a is not None, name
        assert a.title, name
        assert a.read_only_hint is not None, name
        if not a.read_only_hint:
            assert a.destructive_hint is not None, name
        assert a.open_world_hint is not None, name


def test_hints_match_what_the_tools_do(fresh_server):
    tools = _tools(fresh_server)
    ro = lambda n: tools[n].annotations.read_only_hint
    destructive = lambda n: tools[n].annotations.destructive_hint
    outside = lambda n: tools[n].annotations.open_world_hint

    for n in ("memory_search", "skill_load", "cortex_start", "service_list", "fs_read"):
        assert ro(n), n
    for n in ("fs_delete", "memory_delete", "secret_delete", "apikey_revoke", "fs_write",
              "call_service", "ftp_upload", "memory_promote", "tenancy_set"):
        assert not ro(n) and destructive(n), n
    for n in ("task_add", "session_save", "inbox_post"):
        assert not ro(n) and not destructive(n), n
    for n in ("mail_send", "call_service", "webhook_send", "print_document", "mqtt_publish"):
        assert outside(n), n
    for n in ("memory_write", "task_add"):
        assert not outside(n), n
