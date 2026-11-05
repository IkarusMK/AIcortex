"""The eleven tools a client sees up front explain themselves.

Glama rated their definitions B (3.8/5) in November 2026: none of the 37 parameters
had a description, `bootstrap` said only "Same as cortex_start", and `memory_read`
pointed to memory_list — a tool a client with the core profile does not see. A model
had to guess what `scope`, `owner` or `expected_version` take, and what comes back for
an unknown name or no match. Now every parameter carries a short description, every
description stands on its own and says what happens, and a tool outside the core set
is only named together with the way to reach it (integration_call). The whole listing
stays small: models read it in every chat.
"""
import asyncio
import re

import pytest
from fastmcp import Client

# What each description must say about the tool's behaviour (Glama's second round:
# not-found and empty results, result counts and order, what comes back, when to use).
DOCUMENTED = {
    "memory_read": ["no memory named"],
    "skill_load": ["unknown name"],
    "skill_search": ["up to 10"],
    "task_list": ["oldest first"],
    "cortex_start": ["no notes yet"],
    "integration_call": ["unchanged", "changes or deletes data"],
    "session_save": ["memory_write"],
}
LISTING_BUDGET = 13_000   # characters of the core listing: 9,807 before, 12,640 now


def _listing(server):
    async def go():
        async with Client(server.mcp) as client:
            return await client.list_tools()
    return asyncio.run(go())


@pytest.fixture
def core_tools(monkeypatch, request):
    monkeypatch.setenv("TOOL_PROFILE_DEFAULT", "core")
    server = request.getfixturevalue("fresh_server")
    return server, {t.name: t for t in _listing(server)}


def test_every_parameter_is_described(core_tools):
    _, tools = core_tools
    missing = {name: sorted(p for p, schema in tool.inputSchema["properties"].items()
                            if not (schema.get("description") or "").strip())
               for name, tool in tools.items()}
    assert not {k: v for k, v in missing.items() if v}


def test_descriptions_say_what_happens(core_tools):
    _, tools = core_tools
    gaps = {name: [p for p in phrases if p not in tools[name].description.lower()]
            for name, phrases in DOCUMENTED.items()}
    assert not {k: v for k, v in gaps.items() if v}


def test_bootstrap_explains_itself(core_tools):
    _, tools = core_tools
    text = tools["bootstrap"].description
    assert "briefing" in text.lower() and "cortex_start" in text
    assert text.strip() != "Same as cortex_start (kept for existing client setups)."


def test_a_hidden_tool_is_only_named_with_the_way_to_reach_it(core_tools):
    server, tools = core_tools
    hidden = {t.name for t in asyncio.run(server.mcp.list_tools(run_middleware=False))} - set(tools)
    assert "memory_list" in hidden                       # the full registry, not the core listing
    for name, tool in tools.items():
        named = {h for h in hidden if re.search(rf"\b{re.escape(h)}\b", tool.description or "")}
        assert not named or "integration_call" in tool.description, (name, sorted(named))


def test_the_core_listing_stays_small(core_tools):
    _, tools = core_tools
    size = sum(len(t.model_dump_json(exclude_none=True, by_alias=True)) for t in tools.values())
    assert size <= LISTING_BUDGET, size
