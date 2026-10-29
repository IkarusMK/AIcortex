"""The start of a conversation costs little and brings the project along.

Before: the server instructions were a 27,000-character bilingual banner, and
``bootstrap`` returned that banner again plus an unfiltered catalog (~56,000
characters measured on the NAS) — listing every user's memory titles.
"""
import asyncio
import sys

from fastmcp import Client


def _call(server, tool, args=None):
    async def go():
        async with Client(server.mcp) as client:
            res = await client.call_tool(tool, args or {}, raise_on_error=False)
            return res.content[0].text if res.content else ""
    return asyncio.run(go())


def _instructions(server):
    async def go():
        async with Client(server.mcp) as client:
            return client.instructions or ""
    return asyncio.run(go())


def test_server_instructions_are_short_and_lead_with_cortex_start(fresh_server):
    text = _instructions(fresh_server)
    assert 0 < len(text) <= 2000, len(text)
    assert "cortex_start" in text[:512]


def _seed(server):
    _call(server, "memory_write", {"title": "Name and language", "type": "user",
                                   "content": "Dana, answers in German."})
    _call(server, "memory_write", {"title": "Confirm before printing", "type": "feedback",
                                   "content": "Ask before any physical action."})
    _call(server, "memory_write", {"title": "Printer slicing profile", "type": "project",
                                   "project": "Birdhouse", "content": "Use PLA+ profile."})
    _call(server, "memory_write", {"title": "Bakery release notes", "type": "project",
                                   "project": "Bakery", "content": "v1.1 in review."})
    _call(server, "task_add", {"title": "Slice the hinge mount", "project": "Birdhouse"})
    _call(server, "session_save", {"summary": "Measured the hole, 6.2 mm",
                                   "project": "Birdhouse", "next_steps": "enlarge to 6.5"})


def test_cortex_start_brings_the_project_along(fresh_server):
    _seed(fresh_server)
    text = _call(fresh_server, "cortex_start", {"project": "birdhouse", "task": "slice a part"})

    assert len(text) <= 4000, len(text)
    assert "Name and language" in text                 # who the user is
    assert "Confirm before printing" in text           # how to work
    assert "Printer slicing profile" in text               # this project's memory
    assert "Bakery release notes" not in text          # not another project's
    assert "Slice the hinge mount" in text            # open task of the project
    assert "Measured the hole" in text                 # last session of the project


def test_cortex_start_without_project_lists_projects(fresh_server):
    _seed(fresh_server)
    text = _call(fresh_server, "cortex_start")
    assert "birdhouse" in text.lower() and "bakery" in text.lower()
    assert len(text) <= 4000


def test_bootstrap_is_the_same_short_start(fresh_server):
    _seed(fresh_server)
    text = _call(fresh_server, "bootstrap")
    assert len(text) <= 4000
    assert "Name and language" in text


def test_memory_search_filters_by_project(fresh_server):
    _seed(fresh_server)
    text = _call(fresh_server, "memory_search", {"query": "profile", "project": "Birdhouse"})
    assert "printer-slicing-profile" in text
    text = _call(fresh_server, "memory_search", {"query": "release", "project": "Birdhouse"})
    assert "bakery" not in text.lower()


def test_cortex_start_shows_a_confined_user_only_their_own(monkeypatch, request):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    server = request.getfixturevalue("fresh_server")
    authz = sys.modules["authz"]
    _call(server, "memory_write", {"title": "Alice private plan", "type": "project",
                                   "content": "secret plan"})
    # Bob connects with an agent key → role "user" → confined to his own area.
    monkeypatch.setattr(authz, "_identity",
                        lambda: ("bob", False, {"aicortex_key": "k1", "key_scopes": ["all"]}))
    assert "Alice private plan" not in _call(server, "cortex_start")
    assert "Alice private plan" not in _call(server, "bootstrap")
    assert "Alice private plan" not in _call(server, "cortex_catalog")
