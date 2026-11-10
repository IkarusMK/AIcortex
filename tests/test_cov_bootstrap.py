"""cortex_catalog and the catalog builder (bootstrap.py).

Proves the live catalog lists what exists, groups and counts it compactly, survives
broken data, reuses its cache until a directory changes, and — under AUTH_ENFORCE=1 —
shows each person only what they may use: no other people's memory titles, no
services or devices without a grant, only their own jobs, agents and tasks.
"""
import asyncio
import json
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
def brain(monkeypatch, request):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    return request.getfixturevalue("fresh_server")


def _json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


def _grant(**per_user):
    _json(sys.modules["tenancy"].POLICY_FILE, {"users": per_user})


def _section(catalog, title):
    """The lines of one catalog section (up to the next blank line)."""
    start = catalog.index(title)
    return catalog[start:].split("\n\n", 1)[0]


def _skill(name, category=None, key="category"):
    folder = sys.modules["bootstrap"].SKILLS_DIR / name
    folder.mkdir(parents=True, exist_ok=True)
    text = f"---\nname: {name}\n{key}: {category}\n---\n\nbody\n" if category else "# no header\n"
    (folder / "SKILL.md").write_text(text, encoding="utf-8")


# ── listing helpers ──────────────────────────────────────────────────
def test_json_names_lists_descriptions_and_survives_broken_files(brain, tmp_path):
    b = sys.modules["bootstrap"]
    d = tmp_path / "registry"
    _json(d / "alpha.json", {"host": "198.51.100.7", "description": "Test broker", "empty": ""})
    _json(d / "beta.json", {"host": ""})
    _json(d / "broken.json", "{not json")
    _json(d / "listy.json", ["not", "a", "dict"])
    assert b._json_names(d, fields=("host", "description")) == [
        "  - alpha — 198.51.100.7 · Test broker", "  - beta", "  - broken — (unreadable)", "  - listy"]
    assert b._json_names(d, allowed=lambda n: n == "alpha") == ["  - alpha — Test broker"]
    assert b._json_names(tmp_path / "missing") == []


def test_json_grouped_stays_flat_without_categories_and_groups_with_them(brain, tmp_path):
    b = sys.modules["bootstrap"]
    flat = tmp_path / "flat"
    _json(flat / "one.json", {"description": "first"})
    _json(flat / "two.json", {"description": "second"})
    assert b._json_grouped(flat) == ["  - one — first", "  - two — second"]

    grouped = tmp_path / "grouped"
    _json(grouped / "a-lamp.json", {"description": "lamp", "category": "Home"})
    _json(grouped / "b-shop.json", {"description": "shop", "category": " garden "})
    _json(grouped / "c-misc.json", {"description": "misc"})
    _json(grouped / "d-bad.json", "{oops")
    out = b._json_grouped(grouped)
    assert out == ["  [garden]", "    - b-shop — shop", "  [Home]", "    - a-lamp — lamp",
                   "  [Uncategorized]", "    - c-misc — misc", "    - d-bad — (unreadable)"]
    only_home = b._json_grouped(grouped, allowed=lambda name, cat: cat == "Home")
    assert "    - a-lamp — lamp" in only_home and not any("shop" in ln for ln in only_home)


def test_skill_overview_lists_names_while_small(brain):
    b = sys.modules["bootstrap"]
    assert b._skill_list() == []
    _skill("zeta", "ops")
    _skill("alpha")
    assert b._skill_list() == ["  - alpha", "  - zeta"]


def test_skill_overview_collapses_to_category_counts_when_large(brain):
    b = sys.modules["bootstrap"]
    for n in range(8):
        _skill(f"cook-{n}", "cooking")
    for n in range(3):
        _skill(f"home-{n}", "home", key="cluster")
    _skill("plain")
    (b.SKILLS_DIR / "odd" / "SKILL.md").mkdir(parents=True)     # unreadable as a file
    _skill("quoted", '"devops"')
    lines = b._skill_list()
    assert lines[:4] == ["  cooking: 8", "  home: 3", "  uncategorized: 2", "  devops: 1"]
    assert lines[-1].startswith("  → 14 skills total") and "skill_search(query)" in lines[-1]


def test_section_formatting_and_failing_providers_degrade_to_empty(brain, monkeypatch):
    b = sys.modules["bootstrap"]
    assert b._section("T", ["  - a"], "none") == "T:\n  - a"
    assert b._section("T", [], "none yet") == "T:\n  (none yet)"

    def boom(*a, **k):
        raise RuntimeError("store down")
    monkeypatch.setattr(sys.modules["coordination"], "agent_rows", boom)
    monkeypatch.setattr(sys.modules["coordination"], "board_overview", boom)
    assert b._agents() == [] and b._board() == []


# ── the catalog as an admin ──────────────────────────────────────────
def test_an_empty_brain_shows_every_section_with_a_hint(brain):
    text = _t(brain, "cortex_catalog")
    assert text.startswith("===== LIVE BRAIN CATALOG (AICortex v")
    for title, hint in (("MEMORY", "capture facts with memory_write"), ("SKILLS", "skill_write"),
                        ("SERVICES", "service_add"), ("MQTT DEVICES", "mqtt_add"),
                        ("SSH HOSTS", "ssh_add"), ("SCHEDULED JOBS", "cron_add"),
                        ("INBOUND WEBHOOKS", "webhook_add"), ("TEAM", "agent_register"),
                        ("TASK BOARD", "task_add")):
        assert hint in _section(text, title), title
    assert "RESUME" not in text and text.rstrip().endswith("before you stop.")


def test_a_filled_brain_lists_everything_an_admin_may_use(brain):
    b = sys.modules["bootstrap"]
    _t(brain, "memory_write", {"title": "Shared fact", "type": "user", "content": "x"})
    _t(brain, "memory_note", {"title": "Maybe later", "content": "y"})
    _skill("deploy-notes", "devops")
    _json(b.SERVICES_DIR / "lamp.json", {"base_url": "https://lamp.example.com", "category": "Home"})
    _json(b.MQTT_DIR / "broker.json", {"host": "mqtt.example.net", "description": "Test broker"})
    _json(b.WEBHOOK_DIR / "gh.json", {"notify": "user", "description": "pushes"})
    _t(brain, "agent_register", {"name": "hermes", "role": "cad"})
    _t(brain, "task_add", {"title": "Slice bracket", "session_id": "s-1"})
    _t(brain, "session_save", {"summary": "Measured the hole", "next_steps": "enlarge it"})
    text = _t(brain, "cortex_catalog")
    assert "shared-fact — Shared fact" in _section(text, "MEMORY")
    assert "1 candidate(s) awaiting review" in _section(text, "MEMORY")
    assert "  - deploy-notes" in _section(text, "SKILLS")
    assert "[Home]" in _section(text, "SERVICES") and "lamp — https://lamp.example.com" in text
    assert "broker — mqtt.example.net · Test broker" in _section(text, "MQTT DEVICES")
    assert "gh — user · pushes" in _section(text, "INBOUND WEBHOOKS")
    assert "hermes [online] — cad" in _section(text, "TEAM")
    assert "Slice bracket · sess:s-1" in _section(text, "TASK BOARD")
    assert text.startswith("===== RESUME — recent sessions (newest first)")
    assert "last: Measured the hole" in text and "NEXT: enlarge it" in text
    assert text.index("RESUME") < text.index("LIVE BRAIN CATALOG")


def test_cron_jobs_are_listed_with_a_clipped_prompt_and_a_broken_store_is_flagged(brain, monkeypatch):
    cron = sys.modules["cron"]
    _json(cron.CRON_FILE, [{"id": "j1", "schedule": "30 6 * * *", "prompt": "x" * 200,
                            "owner": "alice"}, "stray-entry"])
    section = _section(_t(brain, "cortex_catalog"), "SCHEDULED JOBS")
    assert f"  - j1 — 30 6 * * * · {'x' * 80}" in section and "x" * 81 not in section
    assert "stray-entry" not in section
    _json(cron.CRON_FILE, "{not json")
    assert "(cron store unreadable — see cron_list)" in _t(brain, "cortex_catalog")


def test_a_failing_session_or_candidate_store_does_not_break_the_catalog(brain, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(sys.modules["sessions"], "recent", boom)
    monkeypatch.setattr(sys.modules["memory"], "candidate_count", boom)
    text = _t(brain, "cortex_catalog")
    assert "LIVE BRAIN CATALOG" in text and "RESUME" not in text and "candidate(s)" not in text


def test_the_memory_section_is_cached_until_its_directory_changes(brain, monkeypatch):
    mem = sys.modules["memory"]
    calls, real = [], mem.scope_tiered_lines
    monkeypatch.setattr(mem, "scope_tiered_lines", lambda d: calls.append(d) or real(d))
    _t(brain, "memory_write", {"title": "First fact", "type": "user", "content": "a"})
    first, second = _t(brain, "cortex_catalog"), _t(brain, "cortex_catalog")
    assert first == second and len(calls) == 1
    assert sys.modules["catalog_cache"]._CACHE_FILE.is_file()
    _t(brain, "memory_write", {"title": "Second fact", "type": "user", "content": "b"})
    assert "second-fact" in _t(brain, "cortex_catalog") and len(calls) == 2


# ── per-person visibility ────────────────────────────────────────────
def test_a_person_never_sees_another_persons_memory_titles(monkeypatch, brain):
    _as_admin(monkeypatch)
    _t(brain, "memory_write", {"title": "Admin only secret plan", "type": "user", "content": "x"})
    assert "admin-only-secret-plan" in _t(brain, "cortex_catalog")
    _as(monkeypatch, "bob")
    assert "secret-plan" not in _t(brain, "cortex_catalog")


def test_a_confined_person_sees_their_own_memory_in_the_catalog(monkeypatch, brain):
    _as(monkeypatch, "alice")
    _t(brain, "memory_write", {"title": "Alice own note", "type": "user", "content": "x"})
    assert "alice-own-note" in _t(brain, "cortex_catalog")


def test_services_and_devices_need_a_grant_for_a_non_admin(monkeypatch, brain):
    b = sys.modules["bootstrap"]
    _json(b.SERVICES_DIR / "lamp.json", {"base_url": "https://lamp.example.com", "category": "Home"})
    _json(b.SERVICES_DIR / "shop.json", {"base_url": "https://shop.example.org", "category": "Office"})
    _json(b.MQTT_DIR / "broker1.json", {"host": "mqtt.example.net"})
    _json(b.MQTT_DIR / "broker2.json", {"host": "192.0.2.9"})
    _as(monkeypatch, "alice")
    nothing = _t(brain, "cortex_catalog")
    assert "lamp" not in nothing and "shop" not in nothing and "broker" not in nothing
    _grant(alice={"services": ["home"], "mqtt": ["broker1"]})
    granted = _t(brain, "cortex_catalog")
    assert "lamp — https://lamp.example.com" in granted and "shop" not in granted
    assert "broker1" in granted and "broker2" not in granted and "192.0.2.9" not in granted
    _as_admin(monkeypatch)
    everything = _t(brain, "cortex_catalog")
    assert "shop" in everything and "broker2" in everything


def test_team_board_and_jobs_are_limited_to_the_persons_own(monkeypatch, brain):
    _as(monkeypatch, "alice")
    _t(brain, "agent_register", {"name": "alice-agent"})
    _t(brain, "task_add", {"title": "Alice job"})
    _as(monkeypatch, "bob")
    _t(brain, "agent_register", {"name": "bob-agent"})
    _t(brain, "task_add", {"title": "Bob job"})
    _json(sys.modules["cron"].CRON_FILE, [
        {"id": "ja", "schedule": "0 1 * * *", "prompt": "alice cron", "owner": "alice"},
        {"id": "jb", "schedule": "0 2 * * *", "prompt": "bob cron", "owner": "bob"}])
    mine = _t(brain, "cortex_catalog")
    assert "bob-agent" in mine and "Bob job" in mine and "bob cron" in mine
    assert "alice-agent" not in mine and "Alice job" not in mine and "alice cron" not in mine
    _as_admin(monkeypatch)
    allof = _t(brain, "cortex_catalog")
    assert all(s in allof for s in ("alice-agent", "bob-agent", "Alice job", "Bob job",
                                    "alice cron", "bob cron"))


def test_cortex_start_and_bootstrap_return_the_same_briefing(brain):
    _t(brain, "memory_write", {"title": "Name and language", "type": "user", "content": "Alice."})
    assert _t(brain, "bootstrap") == _t(brain, "cortex_start")
    assert "Name and language" in _t(brain, "bootstrap", {"project": "birdhouse", "task": "slice"})
