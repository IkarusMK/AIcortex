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


def test_instructions_and_briefing_ask_for_the_users_language(fresh_server):
    # Everything AICortex says is English; without this, models drift into English
    # with a German user — clients without server instructions only see the briefing.
    assert "user's language" in _instructions(fresh_server)
    assert "user's language" in _call(fresh_server, "cortex_start")
    assert "user's language" in _call(fresh_server, "cortex_start", {"project": "birdhouse"})


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


RULES = ["Answer in the user's language", "Ask before pushing", "Check generated files",
         "Collect findings before a release", "Commit messages in English",
         "Confirm physical actions", "Explain risks, let the user decide",
         "Keep secrets in the vault", "Measure, don't guess", "No private data in commits",
         "No unsolicited screenshots", "One stack folder per service", "Optimise, don't delete",
         "Prefer the NAS web interface", "Quote in context only", "Run the counter-test",
         "Store pictures in English", "Translations in all languages"]


def test_every_general_rule_reaches_the_briefing(fresh_server):
    # 2.0.0 listed the first four working-style rules (alphabetically) and hid the rest
    # behind "… 14 more" — a session started from the hook never saw most of them.
    for t in RULES:
        _call(fresh_server, "memory_write", {"title": t, "type": "feedback",
                                             "content": f"{t}: details " + "x" * 120})
    for text in (_call(fresh_server, "cortex_start"),
                 _call(fresh_server, "cortex_start", {"project": "birdhouse"})):
        assert len(text) <= 4000, len(text)
        missing = [t for t in RULES if t not in text]
        assert not missing, missing


def test_pinned_rules_come_first_with_their_text(fresh_server):
    for t in RULES:
        _call(fresh_server, "memory_write", {"title": t, "type": "feedback",
                                             "content": f"{t}: details " + "x" * 120})
    _call(fresh_server, "memory_write", {"title": "Zebra crossing rule", "type": "feedback",
                                         "tags": "pinned", "content": "Always stop at zebra crossings."})
    _call(fresh_server, "memory_write", {"title": "Yak shaving rule", "type": "feedback",
                                         "tags": "verbindlich", "content": "Never shave the yak first."})
    text = _call(fresh_server, "cortex_start")
    rules = text.split("How they want you to work:")[1]
    assert "Always stop at zebra crossings." in rules
    assert "Never shave the yak first." in rules
    assert rules.index("Yak shaving rule") < rules.index("Answer in the user's language")


def test_project_rules_stay_in_their_project(fresh_server):
    _seed(fresh_server)
    _call(fresh_server, "memory_write", {"title": "Tag bakery releases only on request",
                                         "type": "feedback", "project": "Bakery",
                                         "content": "Collect fixes, ask before tagging."})
    general = _call(fresh_server, "cortex_start")
    assert "Tag bakery releases" not in general.split("Known projects")[0]
    assert "Tag bakery releases" not in _call(fresh_server, "cortex_start", {"project": "birdhouse"})
    assert "Tag bakery releases" in _call(fresh_server, "cortex_start", {"project": "bakery"})
    assert "Confirm before printing" in _call(fresh_server, "cortex_start", {"project": "bakery"})


def test_the_titles_line_counts_exactly(fresh_server):
    # 2.0.1 counted three characters too many per title, so the closing titles line
    # dropped the last rule ("… 1 more") although its own length fit the budget.
    start = sys.modules["start"]
    ms = [{"title": t, "first": f"{t}: " + "x" * 100, "tags": []} for t in RULES]
    full = start._names(ms, 10_000)
    assert "more" not in start._names(ms, len(full))
    for budget in range(300, 2000, 7):
        block = start._named_block("Rules:", ms, lambda m: f"  - {m['title']}: {m['first']}", budget)
        if len(full) <= budget:
            missing = [t for t in RULES if t not in block]
            assert not missing, (budget, missing)


def test_the_briefing_names_the_playbook_library(fresh_server):
    # A model that has never heard of playbooks learns at the start that there are
    # some — not only when a run is already active.
    _call(fresh_server, "playbook_write", {
        "name": "Release flow", "title": "Release flow", "description": "Ship a release",
        "stages": [{"id": "plan", "name": "Plan", "goal": "g", "artifact": "intent.md",
                    "template": "# Intent", "approval": "webui"}]})
    for text in (_call(fresh_server, "cortex_start"),
                 _call(fresh_server, "cortex_start", {"project": "birdhouse"})):
        assert "release-flow" in text and "playbook_start" in text


def test_general_reference_notes_are_named(fresh_server):
    _call(fresh_server, "memory_write", {"title": "Home network map", "type": "reference",
                                         "content": "Router, NAS and printer addresses."})
    assert "Home network map" in _call(fresh_server, "cortex_start")


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
