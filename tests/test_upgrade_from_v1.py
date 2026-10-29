"""An existing v1.12 installation keeps working after the update to v2.

Checked end to end against a real v1.12.2 server (tools/upgrade_check): OAuth
tokens, refresh tokens, registered clients, RUNNER_TOKEN, REST keys, vault,
services, tasks, sessions, agents and cron jobs all carry over. What broke, and
is pinned here:
- v1 stripped umlauts from note file names ("Größe der Düse" → gr-e-der-d-se.md),
  v2 keeps them. Without a fallback the note was not found under its title any
  more, and saving it again created a second file next to the old one.
- Old project instructions say "call bootstrap first", but the core tool profile
  did not list bootstrap, so the model could not find it.
"""
import asyncio
import sys

from fastmcp import Client

V1_NOTE = """---
title: Größe der Düse
type: reference
source: session
created: 2026-10-29
updated: 2026-10-29
---

0.4 mm
"""


def _call(server, tool, args=None):
    async def go():
        async with Client(server.mcp) as client:
            res = await client.call_tool(tool, args or {}, raise_on_error=False)
            return res.content[0].text if res.content else ""
    return asyncio.run(go())


def _shared(server):
    d = sys.modules["memory"].MEMORY_DIR / "shared"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_v1_note_with_umlauts_is_found_by_its_title(fresh_server):
    (_shared(fresh_server) / "gr-e-der-d-se.md").write_text(V1_NOTE, encoding="utf-8")
    assert "0.4 mm" in _call(fresh_server, "memory_read", {"name": "Größe der Düse"})
    assert "0.4 mm" in _call(fresh_server, "memory_read", {"name": "gr-e-der-d-se"})


def test_saving_a_v1_note_again_updates_it_instead_of_duplicating(fresh_server):
    shared = _shared(fresh_server)
    (shared / "gr-e-der-d-se.md").write_text(V1_NOTE, encoding="utf-8")
    _call(fresh_server, "memory_write", {"title": "Größe der Düse", "type": "reference",
                                         "content": "0.6 mm"})
    assert [p.name for p in shared.glob("*.md")] == ["gr-e-der-d-se.md"]
    text = (shared / "gr-e-der-d-se.md").read_text(encoding="utf-8")
    assert "0.6 mm" in text and "created: 2026-10-29" in text


def test_v1_note_can_be_deleted_by_its_title(fresh_server):
    shared = _shared(fresh_server)
    (shared / "gr-e-der-d-se.md").write_text(V1_NOTE, encoding="utf-8")
    assert "Deleted" in _call(fresh_server, "memory_delete", {"name": "Größe der Düse"})
    assert not list(shared.glob("*.md"))


def test_v1_untitled_collision_file_is_not_taken_over(fresh_server):
    # v1 saved every non-Latin title as untitled.md; a new note must not overwrite it.
    shared = _shared(fresh_server)
    (shared / "untitled.md").write_text(V1_NOTE.replace("Größe der Düse", "热床温度"),
                                        encoding="utf-8")
    _call(fresh_server, "memory_write", {"title": "打印机", "type": "reference", "content": "new"})
    assert "0.4 mm" in (shared / "untitled.md").read_text(encoding="utf-8")
    assert len(list(shared.glob("*.md"))) == 2


def test_core_profile_still_offers_bootstrap(monkeypatch, request):
    monkeypatch.setenv("TOOL_PROFILE_DEFAULT", "core")
    server = request.getfixturevalue("fresh_server")

    async def go():
        async with Client(server.mcp) as client:
            return {t.name for t in await client.list_tools()}
    assert "bootstrap" in asyncio.run(go())
