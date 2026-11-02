"""Memory store (app/memory.py): typing rule, versions, legacy files, search, candidates.

Proves the behaviour the broader suite leaves out: an untyped note is refused; a second
writer who read an old version is refused instead of overwriting; a title can neither
forge the server-stamped author/version nor leave the memory directory; notes saved
before v2 under the old ASCII file name stay reachable and are updated in place rather
than duplicated; search ranks title over text and honours project/type/tag filters; the
candidate queue stages, promotes (with type override) and rejects; catalog helpers group
notes by tier; attribution lookups degrade to "".
"""
import sys
from types import SimpleNamespace

import pytest


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, fn):
        self.tools[fn.__name__] = fn
        return fn


@pytest.fixture
def mem(monkeypatch, fresh_server):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    mod = sys.modules["memory"]
    fake = FakeMCP()
    mod.register(fake)
    return SimpleNamespace(mod=mod, t=fake.tools, authz=sys.modules["authz"])


def _write(mem, title, content="body", type_="reference", **extra):
    return mem.t["memory_write"](title=title, content=content, type=type_, **extra)


def _file(mem, name, scope="shared"):
    return mem.mod.MEMORY_DIR / scope / f"{name}.md"


def _boom(*_a, **_k):
    raise RuntimeError("lookup broke")


# ── file names ───────────────────────────────────────────────────────────────
def test_slugs_keep_letters_and_hash_titles_without_any(mem):
    assert mem.mod._slug("Größe & Maße") == "größe-maße"
    assert mem.mod._legacy_slug("Größe") == "gr-e"
    a, b = mem.mod._slug("!!!"), mem.mod._slug("???")
    assert a.startswith("m-") and len(a) == 12 and a == mem.mod._slug("!!!") and a != b
    assert mem.mod.project_key(" Bird house! ") == "bird-house"


def test_a_scope_cannot_leave_the_memory_directory(mem):
    d = mem.mod._scope_dir("../../evil")
    assert d.parent == mem.mod.MEMORY_DIR and d.name == "_evil"
    assert mem.mod._scope_dir("").name == "shared"


def test_a_note_saved_under_the_old_ascii_name_is_found_and_updated_in_place(mem):
    legacy = _file(mem, "gr-e")
    legacy.parent.mkdir(parents=True)
    legacy.write_text("# Größe\nold body\n", encoding="utf-8")
    assert "old body" in mem.t["memory_read"]("Größe")
    assert "(version 1)" in _write(mem, "Größe", "new body")
    assert [p.name for p in legacy.parent.glob("*.md")] == ["gr-e.md"]    # updated, not duplicated
    assert "new body" in legacy.read_text(encoding="utf-8")
    _file(mem, "größe").write_text("---\ntitle: Größe\ntype: user\n---\nnew name wins\n", encoding="utf-8")
    assert "new name wins" in mem.t["memory_read"]("Größe")
    assert "Deleted" in mem.t["memory_delete"]("Größe")
    assert legacy.exists() and not _file(mem, "größe").exists()


def test_v1_catch_all_untitled_note_is_never_taken_over(mem):
    untitled = _file(mem, "untitled")
    untitled.parent.mkdir(parents=True)
    untitled.write_text("# Untitled\nsomebody's text\n", encoding="utf-8")
    assert mem.t["memory_read"]("???").startswith("No memory named")
    _write(mem, "???", "fresh")
    assert "somebody's text" in untitled.read_text(encoding="utf-8")


# ── reading files ────────────────────────────────────────────────────────────
def test_read_meta_handles_missing_legacy_and_headerless_files(mem, tmp_path):
    meta, body = mem.mod.read_meta(tmp_path / "ghost.md")
    assert (meta, body) == ({"title": "ghost", "type": ""}, "")
    legacy = tmp_path / "old.md"
    legacy.write_text("## Old Title\ntext\n", encoding="utf-8")
    meta, body = mem.mod.read_meta(legacy)
    assert meta == {"title": "Old Title", "type": ""} and "text" in body
    empty = tmp_path / "empty.md"
    empty.write_text("", encoding="utf-8")
    assert mem.mod.read_meta(empty)[0]["title"] == "empty"
    bare = tmp_path / "bare.md"
    bare.write_text("---\ntags: a, b  c\n---\n\ntext\n", encoding="utf-8")
    meta, body = mem.mod.read_meta(bare)
    assert meta["title"] == "bare" and meta["tags"] == ["a", "b", "c"] and body == "text\n"


def test_tags_and_types_are_normalized(mem):
    assert mem.mod._tag_list([" a ", "", "b"]) == ["a", "b"]
    assert mem.mod._tag_list(None) == [] and mem.mod._tag_list("x,y") == ["x", "y"]
    assert mem.mod.canon_type(" Preference ") == "user" and mem.mod.canon_type("URL") == "reference"
    assert mem.mod.canon_type("nonsense") == "" and mem.mod.canon_type(None) == ""
    assert mem.mod._version({"version": "abc"}) == 0 and mem.mod._version({"version": ""}) == 0


# ── writing ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("bad_type", ["", "  ", "weird", "memory"])
def test_an_untyped_note_is_refused_and_nothing_is_written(mem, bad_type):
    text = _write(mem, "Anything", type_=bad_type)
    assert text.startswith("Refused: a memory MUST be typed") and "user, feedback, project, reference" in text
    assert not list(mem.mod.MEMORY_DIR.glob("*/*.md"))


def test_a_type_synonym_is_snapped_to_the_canonical_type(mem):
    assert _write(mem, "Dark mode", type_="Preference").startswith("Saved [user] memory 'dark-mode'")
    assert "type: user" in _file(mem, "dark-mode").read_text(encoding="utf-8")


def test_header_carries_review_project_tags_and_server_stamped_author(mem):
    with mem.authz.rest_identity("alice", "user"):
        _write(mem, "Roof", "tiles", type_="project", review="2027-01-01", project="House",
               tags="build, roof", source="chat", scope="work")
    meta, _ = mem.mod.read_meta(_file(mem, "roof", "work"))
    assert (meta["review"], meta["project"], meta["author"], meta["source"]) == (
        "2027-01-01", "House", "alice", "chat")
    assert meta["tags"] == ["build", "roof"] and meta["version"] == "1"


def test_a_hostile_title_cannot_forge_author_or_version(mem):
    with mem.authz.rest_identity("alice", "user"):
        _write(mem, "x\nversion: 99\nauthor: mallory", "text")
    (path,) = mem.mod.MEMORY_DIR.glob("shared/*.md")
    meta, _ = mem.mod.read_meta(path)
    assert meta["author"] == "alice" and mem.mod._version(meta) == 1
    assert meta["title"] == "x version: 99 author: mallory"


def test_a_stale_writer_is_refused_and_a_current_one_succeeds(mem):
    with mem.authz.rest_identity("alice", "user"):
        assert "(version 1)" in _write(mem, "Plan", "alice v1")
    _file(mem, "plan").write_text(
        _file(mem, "plan").read_text().replace("created: ", "created: 2020-01-01#"), encoding="utf-8")
    with mem.authz.rest_identity("bob", "user"):
        refused = _write(mem, "Plan", "bob overwrites", expected_version=5)
        assert refused.startswith("Not saved") and "now version 1, by alice" in refused
        assert "expected_version=1" in refused
        assert "(version 2)" in _write(mem, "Plan", "bob v2", expected_version=1)
    text = _file(mem, "plan").read_text(encoding="utf-8")
    assert "bob v2" in text and "alice v1" not in text and "created: 2020-01-01#" in text


def test_a_corrupt_version_counts_as_zero_and_stale_check_names_no_author_when_unknown(mem):
    _file(mem, "plan").parent.mkdir(parents=True)
    _file(mem, "plan").write_text("---\ntitle: Plan\ntype: project\nversion: abc\n---\nold\n", encoding="utf-8")
    assert "(version 1)" in _write(mem, "Plan", "fixed")
    assert "now version 1)" in _write(mem, "Plan", "again", expected_version=9)     # no ", by …"


def test_dedup_hint_flags_related_titles_but_not_a_single_shared_word(mem):
    _write(mem, "Printer nozzle size")
    assert "overlap" not in _write(mem, "Printer firmware update notes")
    hinted = _write(mem, "Printer nozzle diameter")
    assert "possible overlap with: printer-nozzle-size" in hinted
    assert mem.mod._overlap_hits("shared", "a b", "x") == []


# ── listing, reading, deleting ───────────────────────────────────────────────
def test_list_read_and_delete_report_missing_notes(mem):
    assert mem.t["memory_list"]() == "No memories in scope 'shared' yet."
    _write(mem, "Boiler", "pressure", type_="user")
    legacy = _file(mem, "old")
    legacy.write_text("# Old note\ntext\n", encoding="utf-8")
    assert mem.t["memory_list"]().splitlines() == ["- boiler — [user] Boiler", "- old — Old note"]
    assert "pressure" in mem.t["memory_read"]("boiler")
    assert mem.t["memory_read"]("nope") == "No memory named 'nope' in scope 'shared'."
    assert mem.t["memory_delete"]("nope") == "No memory named 'nope' in scope 'shared'."
    assert mem.t["memory_delete"]("boiler") == "Deleted memory 'boiler' from scope 'shared'."
    assert not _file(mem, "boiler").exists()


# ── search ───────────────────────────────────────────────────────────────────
def test_search_ranks_title_over_tags_over_text_and_filters(mem):
    _write(mem, "Heating schedule", "boiler runs at six\nsecond line", type_="project",
           project="House", tags="winter")
    _write(mem, "Boiler pressure", "check the gauge", type_="reference", tags="heating")
    _write(mem, "Weekly checks", "inspect the boiler", type_="feedback")
    names = [e["name"] for e in mem.mod.search_entries("shared", "boiler")]
    assert names == ["boiler-pressure", "heating-schedule", "weekly-checks"]
    assert [e["name"] for e in mem.mod.search_entries("shared", "heating")] == [
        "heating-schedule", "boiler-pressure"]                         # title 3 > tag 2
    assert [e["name"] for e in mem.mod.search_entries("shared", "", project="house")] == ["heating-schedule"]
    assert [e["name"] for e in mem.mod.search_entries("shared", "", type_="Feedback")] == ["weekly-checks"]
    assert [e["name"] for e in mem.mod.search_entries("shared", "", tag="WINTER")] == ["heating-schedule"]
    assert mem.mod.search_entries("shared", "", type_="user") == []
    assert mem.mod.search_entries("shared", "", tag="absent") == []
    hit = mem.mod.search_entries("shared", "six")[0]
    assert hit["snippet"] == "boiler runs at six" and hit["tags"] == ["winter"]
    assert len(mem.mod.search_entries("shared", "", limit=2)) == 2
    assert len(mem.mod.search_entries("shared", "", limit=0)) == 3        # 0 → default


def test_search_tool_reports_hits_and_misses(mem):
    _write(mem, "Boiler pressure", "check the gauge")
    assert mem.t["memory_search"]("gauge") == "- boiler-pressure — Boiler pressure: check the gauge"
    assert mem.t["memory_search"]("zzz") == "No matches for 'zzz' in scope 'shared'."
    assert mem.t["memory_search"]("gauge", limit=100000).count("\n") == 0
    assert mem.t["memory_search"]("gauge", type="user").startswith("No matches")


# ── candidates ───────────────────────────────────────────────────────────────
def test_candidates_are_staged_listed_promoted_and_rejected(mem):
    assert mem.mod.candidate_count() == 0                      # no queue directory yet
    assert mem.t["memory_candidates"]() == "No memory candidates awaiting review."
    assert mem.mod.candidate_count() == 0
    assert "Staged candidate 'maybe-keep'" in mem.t["memory_note"]("Maybe keep", "long body " * 30, tags="a, b")
    mem.t["memory_note"]("Drop me", "noise", type="bogus")
    assert mem.mod.candidate_count() == 2
    listing = mem.t["memory_candidates"]()
    assert "- maybe-keep — [reference] Maybe keep (source: note) — long body" in listing
    assert not list((mem.mod.MEMORY_DIR / "shared").glob("*.md")) if (mem.mod.MEMORY_DIR / "shared").exists() else True

    assert mem.t["memory_promote"]("nope").startswith("No candidate named 'nope'")
    done = mem.t["memory_promote"]("maybe-keep", scope="work", type="preference")
    assert done == "Promoted candidate → [user] memory 'maybe-keep' in scope 'work'."
    meta, body = mem.mod.read_meta(_file(mem, "maybe-keep", "work"))
    assert meta["type"] == "user" and meta["source"] == "note · promoted" and meta["tags"] == ["a", "b"]
    assert body.startswith("long body")
    assert mem.mod.candidate_count() == 1
    assert mem.t["memory_reject"]("nope") == "No candidate named 'nope'."
    assert mem.t["memory_reject"]("drop-me") == "Rejected candidate 'drop-me'."
    assert mem.mod.candidate_count() == 0


def test_restaging_a_candidate_keeps_its_created_date(mem):
    slug = mem.mod.stage_candidate("Idea", "v1")
    path = _file(mem, slug, mem.mod.CANDIDATES_SCOPE)
    path.write_text(path.read_text(encoding="utf-8").replace("created: ", "created: 2020-01-01#"), encoding="utf-8")
    mem.mod.stage_candidate("Idea", "v2", type_="feedback", source="auto")
    meta, body = mem.mod.read_meta(path)
    assert meta["created"].startswith("2020-01-01#") and meta["type"] == "feedback"
    assert body.strip() == "v2" and meta["status"] == "candidate"


# ── catalog helpers ──────────────────────────────────────────────────────────
def test_catalog_lines_group_notes_by_tier(mem):
    _write(mem, "Roof work", type_="project")
    _write(mem, "Dark mode", type_="user")
    _write(mem, "Wiki link", type_="reference")
    _file(mem, "old").write_text("# Old note\ntext\n", encoding="utf-8")
    d = mem.mod.MEMORY_DIR / "shared"
    flat = mem.mod.scope_title_lines(d)
    assert "  - dark-mode — [user] Dark mode" in flat and "  - old — Old note" in flat
    tiers = [ln.strip() for ln in mem.mod.scope_tiered_lines(d) if not ln.startswith("      -")]
    assert [t.split()[1] for t in tiers] == ["Core", "Projects", "References", "Other"]
    assert "      - roof-work — Roof work" in mem.mod.scope_tiered_lines(d)


def test_entries_describe_every_note_of_a_scope(mem):
    assert mem.mod.entries("missing") == []
    with mem.authz.rest_identity("alice", "user"):
        _write(mem, "Roof", "first line\nsecond", type_="project", project="House", tags="a")
    (e,) = mem.mod.entries("shared")
    assert (e["name"], e["type"], e["project"], e["tags"], e["version"], e["author"], e["first"]) == (
        "roof", "project", "House", ["a"], 1, "alice", "first line")


# ── attribution ──────────────────────────────────────────────────────────────
def test_caller_label_is_empty_when_unknown_or_unresolvable(mem, monkeypatch):
    assert mem.mod._caller_label() == ""
    with mem.authz.rest_identity("alice", "user"):
        assert mem.mod._caller_label() == "alice"
    monkeypatch.setattr(mem.authz, "caller_label", _boom)
    assert mem.mod._caller_label() == ""
