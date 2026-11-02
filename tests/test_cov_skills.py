"""The skill library (skills.py): write, search, load, resources, delete.

Proves that skills must be categorized, that categories snap onto existing spellings,
that arbitrary descriptions survive the frontmatter round trip, that search ranks by
where a word hits, that names and filenames cannot reach outside the library, and —
under AUTH_ENFORCE=1 — that only the author or an admin may change a shared skill
and that a person sees only the skills an admin granted them.
(The bundled starter skills are covered by tests/test_skill_examples.py.)
"""
import asyncio
import json
import os
import sys

import pytest
from fastmcp import Client


def _call(server, tool, args=None):
    async def go():
        async with Client(server.mcp) as client:
            res = await client.call_tool(tool, args or {}, raise_on_error=False)
            return (res.content[0].text if res.content else ""), res.is_error
    return asyncio.run(go())


def _t(server, tool, args=None):
    return _call(server, tool, args)[0]


def _as(monkeypatch, person, agent="bot"):
    monkeypatch.setattr(sys.modules["authz"], "_identity", lambda: (person, False, {
        "aicortex_key": f"k-{person}", "key_scopes": ["all"], "agent": agent}))


def _as_admin(monkeypatch, person="root"):
    monkeypatch.setattr(sys.modules["authz"], "_identity", lambda: (person, False, {}))


@pytest.fixture
def lib(monkeypatch, request):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    return request.getfixturevalue("fresh_server")


def _write(server, name, category="ops", description="", instructions="Do the thing.", tags=""):
    return _t(server, "skill_write", {"name": name, "description": description or f"About {name}",
                                      "instructions": instructions, "tags": tags,
                                      "category": category})


def _page(name):
    return sys.modules["skills"].SKILLS_DIR / name / "SKILL.md"


def _grant(**per_user):
    path = sys.modules["tenancy"].POLICY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"users": per_user}), encoding="utf-8")


# ── writing ──────────────────────────────────────────────────────────
def test_a_skill_without_a_category_is_refused_with_a_hint(lib):
    first = _write(lib, "deploy", category="")
    assert first.startswith("Refused: a skill MUST be categorized") and "No categories yet" in first
    assert not _page("deploy").exists()
    _write(lib, "alpha", category="devops")
    _write(lib, "beta", category="home")
    assert "Reuse one of these existing categories: devops, home" in _write(lib, "x", category="  ")


def test_written_skill_round_trips_through_load_with_valid_frontmatter(lib):
    out = _write(lib, "Deploy Notes!", category="ops", description="Ship it", tags="ci cd",
                 instructions="Step one.\n\n")
    assert out == "Saved skill 'deploy-notes' [ops]."
    text = _t(lib, "skill_load", {"name": "Deploy Notes!"})
    meta, body = sys.modules["skills"]._parse(text)
    assert (meta["name"], meta["description"], meta["category"], meta["tags"]) == (
        "Deploy Notes!", "Ship it", "ops", "ci cd")
    assert body == "Step one.\n" and "author" not in meta


def test_awkward_descriptions_survive_the_round_trip_and_collapse_to_one_line(lib):
    tricky = 'Quelle: Paul "Hudson" # not a comment — Größe 日本'
    _write(lib, "tricky", description=f"{tricky}\n  second   line")
    meta, _ = sys.modules["skills"]._parse(_page("tricky").read_text(encoding="utf-8"))
    assert meta["description"] == f"{tricky} second line"


def test_categories_snap_onto_the_existing_spelling(lib):
    _write(lib, "one", category="Trading")
    assert _write(lib, "two", category="  trading ") == "Saved skill 'two' [Trading]."
    assert _write(lib, "three", category="TRADING") == "Saved skill 'three' [Trading]."
    assert _write(lib, "four", category="new   area") == "Saved skill 'four' [new area]."


def test_names_are_slugged_and_cannot_leave_the_library(lib, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("precious", encoding="utf-8")
    assert _write(lib, "../../outside/evil") == "Saved skill 'outside-evil' [ops]."
    assert _t(lib, "skill_delete", {"name": "../outside"}) == "No skill named '../outside'."
    assert _write(lib, "!!!").startswith("Saved skill 'skill'")
    assert _write(lib, "n" * 90).startswith("Saved skill '" + "n" * 60 + "'")
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "precious"


def test_delete_removes_the_whole_skill_folder(lib):
    _write(lib, "temp")
    (_page("temp").parent / "notes.txt").write_text("r", encoding="utf-8")
    assert _t(lib, "skill_delete", {"name": "temp"}) == "Deleted skill 'temp'."
    assert not _page("temp").parent.exists()
    assert _t(lib, "skill_delete", {"name": "temp"}) == "No skill named 'temp'."


# ── parsing ──────────────────────────────────────────────────────────
def test_unquoted_colons_fall_back_to_line_parsing_instead_of_losing_metadata(lib):
    s = sys.modules["skills"]
    meta, body = s._parse("---\nname: x\ndescription: Quelle: Paul Hudson\ncluster: ops\n---\nbody")
    assert meta["description"] == "Quelle: Paul Hudson" and s._category(meta) == "ops"
    assert body == "body"
    assert s._parse("no header at all") == ({}, "no header at all")
    assert s._parse("---\n- a\n- b\n---\ntext")[0] == {}
    assert s._category({}) == "uncategorized" and s._category({"category": " Ops "}) == "Ops"


# ── search and listing ───────────────────────────────────────────────
def test_search_ranks_name_over_description_over_tags_over_body(lib):
    _write(lib, "body-only", description="unrelated", instructions="mentions printer once")
    _write(lib, "tagged", description="unrelated", tags="printer")
    _write(lib, "cad-notes", description="tune the printer")
    _write(lib, "printer-setup", description="unrelated")
    ranked = sys.modules["skills"].search("printer")
    assert [(s, n) for s, n, _ in ranked] == [
        (4, "printer-setup"), (3, "cad-notes"), (2, "tagged"), (1, "body-only")]
    text = _t(lib, "skill_search", {"query": "printer"})
    assert text.splitlines()[0] == "- printer-setup — unrelated"
    assert len(sys.modules["skills"].search("printer", limit=2)) == 2


def test_search_filters_by_category_and_ignores_short_words(lib):
    _write(lib, "alpha-tool", category="devops", description="build things")
    _write(lib, "beta-tool", category="home", description="build things")
    assert _t(lib, "skill_search", {"query": "build", "category": "HOME"}) == "- beta-tool — build things"
    assert _t(lib, "skill_search", {"query": "a to of"}) == "No skills matched 'a to of'. (Use skill_write to add one.)"


def test_list_shows_category_counts_then_one_category(lib):
    assert _t(lib, "skill_list") == "No skills yet. Use skill_write to add one."
    for name, cat in (("a1", "ops"), ("a2", "ops"), ("h1", "home")):
        _write(lib, name, category=cat, description=f"desc {name}")
    _page("legacy").parent.mkdir()
    _page("legacy").write_text("plain markdown, no header\n", encoding="utf-8")
    head, *lines = _t(lib, "skill_list").splitlines()
    assert head.startswith("4 skills in 3 categories")
    assert lines == ["- ops — 2 skill(s)", "- home — 1 skill(s)", "- uncategorized — 1 skill(s)"]
    assert _t(lib, "skill_list", {"category": "OPS"}) == "- a1 — desc a1\n- a2 — desc a2"
    assert _t(lib, "skill_list", {"category": "nope"}) == "No skills in category 'nope'."


def test_search_notices_edits_and_deletions_through_the_mtime_cache(lib):
    _write(lib, "evolving", description="alpha topic")
    assert sys.modules["skills"].search("alpha")
    page = _page("evolving")
    page.write_text(page.read_text(encoding="utf-8").replace("alpha", "gamma"), encoding="utf-8")
    os.utime(page, ns=(1, page.stat().st_mtime_ns + 5_000_000_000))
    assert not sys.modules["skills"].search("alpha") and sys.modules["skills"].search("gamma")
    _t(lib, "skill_delete", {"name": "evolving"})
    assert sys.modules["skills"].search("gamma") == [] and not sys.modules["skills"]._INDEX


# ── resources ────────────────────────────────────────────────────────
def test_resources_are_read_by_flat_filename_and_cannot_escape(lib):
    _write(lib, "docs")
    _write(lib, "other", description="private other skill")
    (_page("docs").parent / "checklist.txt").write_text("1. plug in", encoding="utf-8")
    assert _t(lib, "skill_resource", {"name": "docs", "filename": "checklist.txt"}) == "1. plug in"
    for evil in ("../other/SKILL.md", "..", "", "missing.txt", "/etc/passwd"):
        assert _t(lib, "skill_resource", {"name": "docs", "filename": evil}) == \
            f"No resource '{evil}' in skill 'docs'."
    assert _t(lib, "skill_resource", {"name": "../other", "filename": "SKILL.md"}).startswith("---")
    assert _t(lib, "skill_load", {"name": "ghost"}) == "No skill named 'ghost'."


# ── per-person rules under enforcement ───────────────────────────────
def test_only_the_author_or_an_admin_may_change_a_shared_skill(monkeypatch, lib):
    _as(monkeypatch, "alice", agent="a1")
    assert _write(lib, "shared", instructions="v1") == "Saved skill 'shared' [ops]."
    assert "author: alice/a1" in _page("shared").read_text(encoding="utf-8")
    _as(monkeypatch, "alice", agent="a2")
    assert _write(lib, "shared", instructions="v2") == "Saved skill 'shared' [ops]."
    _as(monkeypatch, "bob")
    refused = "Refused: 'shared' is a shared skill. Ask an admin to change it, or save your variant under another name."
    assert _write(lib, "shared", instructions="hijack") == refused
    assert _t(lib, "skill_delete", {"name": "shared"}) == refused
    assert "v2" in _page("shared").read_text(encoding="utf-8")
    assert _write(lib, "shared-bob", instructions="mine") == "Saved skill 'shared-bob' [ops]."
    _as_admin(monkeypatch)
    assert _write(lib, "shared", instructions="v3") == "Saved skill 'shared' [ops]."
    assert _t(lib, "skill_delete", {"name": "shared"}) == "Deleted skill 'shared'."


def test_an_authorless_legacy_skill_is_admin_only_and_open_without_enforcement(monkeypatch, request):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    server = request.getfixturevalue("fresh_server")
    _write(server, "legacy")                                  # no identity -> no author recorded
    assert "author" not in _page("legacy").read_text(encoding="utf-8")
    _as(monkeypatch, "bob")
    assert _t(server, "skill_delete", {"name": "legacy"}).startswith("Refused: 'legacy' is a shared skill")
    monkeypatch.setenv("AUTH_ENFORCE", "0")
    assert _t(server, "skill_delete", {"name": "legacy"}) == "Deleted skill 'legacy'."


def test_a_person_sees_only_the_skills_an_admin_granted(monkeypatch, lib):
    _write(lib, "grill-tips", category="home", description="barbecue basics")
    _write(lib, "tax-help", category="finance", description="barbecue taxes")
    _as(monkeypatch, "alice")
    assert _t(lib, "skill_list").startswith("No skills available to you.")
    assert _t(lib, "skill_search", {"query": "barbecue"}).startswith("No skills matched")
    assert _t(lib, "skill_load", {"name": "tax-help"}).startswith("Denied: skill 'tax-help'")
    assert _t(lib, "skill_resource", {"name": "tax-help", "filename": "x"}).startswith("Denied:")
    _grant(alice={"skills": ["home"]})
    assert _t(lib, "skill_list").count("skill(s)") == 1 and "home — 1" in _t(lib, "skill_list")
    assert _t(lib, "skill_search", {"query": "barbecue"}) == "- grill-tips — barbecue basics"
    assert _t(lib, "skill_load", {"name": "grill-tips"}).startswith("---")
    assert _t(lib, "skill_load", {"name": "tax-help"}).startswith("Denied:")
    _as_admin(monkeypatch)
    assert _t(lib, "skill_search", {"query": "barbecue"}).count("\n") == 1


def test_a_failing_identity_lookup_falls_back_to_allowing_the_change(monkeypatch, lib):
    _write(lib, "fallback")
    monkeypatch.setattr(sys.modules["authz"], "effective_identity",
                        lambda: (_ for _ in ()).throw(RuntimeError("no identity")))
    assert sys.modules["skills"]._may_change(_page("fallback").parent) == (True, "")


def test_one_unreadable_skill_file_does_not_break_search_for_everyone(lib):
    _write(lib, "healthy", description="reliable topic")
    broken = _page("broken")
    broken.parent.mkdir()
    broken.write_bytes(b"---\nname: broken\n---\n\xff\xfe\x00 bad bytes")
    text, err = _call(lib, "skill_search", {"query": "reliable"})
    assert not err and "healthy" in text
