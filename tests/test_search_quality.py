"""Models find what was stored, and the brain doesn't cry wolf.

Measured before:
- memory_search looked for the whole query as ONE substring, so
  "printer nozzle size" found nothing when the note said "nozzle of the printer".
- the duplicate hint fired on any shared 4-letter word (e.g. "with", "from").
- titles with umlauts were mangled and non-Latin titles all became "untitled",
  overwriting each other.
- skill_search scored every word anywhere in the body equally and re-read all
  skills on every call; any user-role agent could overwrite shared skills.
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


def test_memory_search_matches_words_not_one_phrase(fresh_server):
    _call(fresh_server, "memory_write", {"title": "Printer setup", "type": "reference",
                                         "content": "The nozzle of the printer is 0.4 mm."})
    _call(fresh_server, "memory_write", {"title": "Garden", "type": "reference",
                                         "content": "Water the tomatoes."})
    text = _call(fresh_server, "memory_search", {"query": "printer nozzle size"})
    assert "printer-setup" in text
    assert "garden" not in text


def test_memory_search_ranks_title_hits_first(fresh_server):
    _call(fresh_server, "memory_write", {"title": "Notes", "type": "reference",
                                         "content": "mentions the birdhouse once"})
    _call(fresh_server, "memory_write", {"title": "Birdhouse address", "type": "reference",
                                         "content": "port 8100"})
    text = _call(fresh_server, "memory_search", {"query": "birdhouse"})
    assert text.index("birdhouse-address") < text.index("notes")


def test_duplicate_hint_ignores_common_words(fresh_server):
    _call(fresh_server, "memory_write", {"title": "Meeting with the landlord", "type": "project",
                                         "content": "x"})
    text = _call(fresh_server, "memory_write", {"title": "Dinner with friends", "type": "project",
                                                "content": "y"})
    assert "overlap" not in text


def test_umlaut_and_non_latin_titles_get_distinct_names(fresh_server):
    a = _call(fresh_server, "memory_write", {"title": "Größe der Düse", "type": "reference", "content": "a"})
    b = _call(fresh_server, "memory_write", {"title": "打印机", "type": "reference", "content": "b"})
    c = _call(fresh_server, "memory_write", {"title": "热床温度", "type": "reference", "content": "c"})
    assert "größe-der-düse" in a
    assert "untitled" not in b and "untitled" not in c
    assert _call(fresh_server, "memory_read", {"name": "打印机"}).endswith("b\n")


def _skill(server, name, desc, body, category="Birdhouse"):
    return _call(server, "skill_write", {"name": name, "description": desc,
                                         "instructions": body, "category": category})


def test_skill_search_ranks_name_and_description_above_body(fresh_server):
    _skill(fresh_server, "printer-slicing", "Slice models for the 3D printer", "Use SliceTool.")
    _skill(fresh_server, "garden-plan", "Plan the vegetable garden", "Unrelated, though slicing is mentioned.")
    text = _call(fresh_server, "skill_search", {"query": "slicing"})
    assert text.index("printer-slicing") < text.index("garden-plan")


def test_user_role_cannot_overwrite_a_shared_skill(monkeypatch, request):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    server = request.getfixturevalue("fresh_server")
    _skill(server, "printer-slicing", "Slice models", "original")
    authz = sys.modules["authz"]
    monkeypatch.setattr(authz, "_identity", lambda: (
        "anna", False, {"aicortex_key": "k", "key_scopes": ["all"], "agent": "bot"}))
    _skill(server, "printer-slicing", "Slice models", "overwritten")
    _call(server, "skill_delete", {"name": "printer-slicing"})
    monkeypatch.undo()
    assert "original" in _call(server, "skill_load", {"name": "printer-slicing"})
