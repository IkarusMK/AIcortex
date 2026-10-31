"""Findings of the second security review (2026-10-31: WebUI views, cron, playbooks).

H1  A few hundred bytes of YAML with nested aliases in playbook `stages` grew to
    gigabytes in memory (one request could take the server down).
H2  A cron schedule had no length limit; a long one froze the server for seconds,
    for every client, on save, preview and every job listing.
M1  "An AI never approves its own work" could be bypassed by the AI itself: it
    could save a playbook whose stages need no approval (or loosen one).
M2  Any user could take the bundled example's name first and change its stages for
    everyone; re-installing the example silently overwrote an edited one.
M3  A template line "---" ended the playbook header early: the playbook vanished,
    and anyone could then overwrite it.
L1  No limit on runs per person.  L2  Weekday ranges ending in 7 ("1-7") broke.
L5  Odd input (Infinity, wrong types) caused 500s.  L6  Audit actions read "ui:ui:".
"""
import asyncio
import json
import sys
import time

import httpx
import pytest
from fastmcp.exceptions import ToolError

pytestmark = pytest.mark.oidc       # keep the WebUI's sign-in mode for the ui fixture

STAGE = {"id": "plan", "name": "Plan", "goal": "g", "artifact": "intent.md",
         "template": "# Intent", "approval": "webui"}


def _as(server, sub, role, tool, args=None):
    authz = sys.modules["authz"]

    async def go():
        with authz.rest_identity(sub, role):
            try:
                res = await server.mcp.call_tool(tool, args or {})
                return "\n".join(getattr(c, "text", "") for c in res.content or [])
            except ToolError as exc:
                return f"DENIED: {exc}"
    return asyncio.run(go())


@pytest.fixture
def pb(monkeypatch, request):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    server = request.getfixturevalue("fresh_server")
    return server, sys.modules["playbooks"]


@pytest.fixture
def ui(monkeypatch, request, fake_idp):
    monkeypatch.setenv("OIDC_CONFIG_URL", f"{fake_idp}/.well-known/openid-configuration")
    monkeypatch.setenv("OIDC_CLIENT_ID", "aicortex")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "upstream-secret")
    monkeypatch.setenv("JWT_SIGNING_KEY", "0" * 64)
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    base = request.getfixturevalue("http_server")
    webui = sys.modules["webui"]

    def client(sub, role):
        cookie = webui._sign({"sub": sub, "name": sub, "role": role, "csrf": "tok",
                              "exp": time.time() + 600})
        return httpx.Client(base_url=base, cookies={webui.SESSION_COOKIE: cookie},
                            headers={"X-CSRF": "tok"}, timeout=30)
    return client


# ── H1 ──────────────────────────────────────────────────────────────────────
def test_yaml_alias_bomb_in_stages_is_refused(pb):
    server, playbooks = pb
    levels = ["a: &a [x, x, x, x, x, x, x, x, x]"]
    levels += [f"{chr(98 + i)}: &{chr(98 + i)} [{', '.join('*' + chr(97 + i) for _ in range(9))}]"
               for i in range(7)]
    bomb = "\n".join(levels) + "\n"
    text = _as(server, "alice", "user", "playbook_write", {
        "name": "Bomb", "title": "b", "description": "d", "stages": bomb})
    assert "Refused" in text and playbooks.load("bomb") is None
    huge = json.dumps([dict(STAGE, goal="x" * 300_000)])
    assert "Refused" in _as(server, "alice", "user", "playbook_write", {
        "name": "Huge", "title": "h", "description": "d", "stages": huge})


# ── H2 / L2 ─────────────────────────────────────────────────────────────────
def test_long_or_crowded_schedules_are_refused_fast(pb):
    cron = sys.modules["cron"]
    for expr in ("0 " + ",".join(str(h % 24) for h in range(4000)) + " * * *",
                 ",".join(str(m) for m in range(60)) + " * * * *"):
        started = time.perf_counter()
        assert cron.schedule_problem(expr)
        assert time.perf_counter() - started < 0.05
    started = time.perf_counter()
    assert cron.schedule_problem("0 0 29 2 *") == ""                 # leap day: valid
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize("expr", ["0 9 * * 1-7", "0 9 * * 5-7", "0 9 * * 0-7"])
def test_weekday_ranges_ending_in_seven(pb, expr):
    from datetime import datetime
    cron = sys.modules["cron"]
    assert cron.schedule_problem(expr) == ""
    # after Saturday 2026-10-31 10:00 the next run is Sunday 09:00 — 7 means Sunday
    assert cron.next_run(expr, datetime(2026, 10, 31, 10, 0)) == datetime(2026, 11, 1, 9, 0)
    assert cron._is_due(expr, datetime(2026, 11, 1, 9, 0))
    monday = datetime(2026, 11, 2, 9, 0)
    assert cron._is_due(expr, monday) == (expr != "0 9 * * 5-7")


# ── M1 ──────────────────────────────────────────────────────────────────────
def test_an_ai_cannot_save_stages_that_skip_the_human(pb):
    server, playbooks = pb
    for approval in ("none", "chat"):
        text = _as(server, "alice", "user", "playbook_write", {
            "name": f"Self {approval}", "title": "t", "description": "d",
            "stages": [dict(STAGE, approval=approval)]})
        assert "Refused" in text and "WebUI" in text


def test_an_ai_cannot_loosen_a_stage_a_person_set(pb):
    server, playbooks = pb
    sys.modules["authz"]  # noqa: B018 - module loaded
    with sys.modules["authz"].rest_identity("alice", "user"):
        assert playbooks.save("Team flow", "t", "d", [dict(STAGE, approval="chat")],
                              via_webui=True).startswith("Saved")
    tighter = _as(server, "alice", "user", "playbook_write", {
        "name": "Team flow", "title": "t", "description": "d", "stages": [dict(STAGE, approval="webui")]})
    assert tighter.startswith("Saved")
    looser = _as(server, "alice", "user", "playbook_write", {
        "name": "Team flow", "title": "t", "description": "d", "stages": [dict(STAGE, approval="none")]})
    assert "Refused" in looser


# ── M2 ──────────────────────────────────────────────────────────────────────
def test_example_install_is_admin_only_and_never_overwrites(ui):
    bob, boss = ui("bob", "user"), ui("boss", "admin")
    assert bob.post("/ui/api/playbooks/example", json={}).status_code == 403
    assert boss.post("/ui/api/playbooks/example", json={}).json()["ok"]
    r = boss.post("/ui/api/playbooks/example", json={})
    assert r.status_code == 409 and "exists" in r.json()["message"]


def test_relaxed_approvals_count_only_for_the_playbooks_author(pb):
    server, playbooks = pb
    with sys.modules["authz"].rest_identity("bob", "user"):
        playbooks.save("Bob flow", "t", "d", [dict(STAGE, approval="none")], via_webui=True)
    _as(server, "alice", "user", "playbook_start", {"playbook": "bob-flow", "project": "x"})
    run = playbooks.runs_for("alice")[0]
    assert run["stages"][0]["approval"] == "webui"
    _as(server, "bob", "user", "playbook_start", {"playbook": "bob-flow", "project": "x"})
    assert playbooks.runs_for("bob")[0]["stages"][0]["approval"] == "none"


# ── M3 ──────────────────────────────────────────────────────────────────────
def test_a_rule_line_in_a_template_keeps_the_playbook_intact(pb):
    server, playbooks = pb
    with sys.modules["authz"].rest_identity("boss", "admin"):
        playbooks.save("Ruled", "t", "d", [dict(STAGE, template="# A\n---\n# B")], via_webui=True)
    d = playbooks.load("ruled")
    assert d and d["stages"][0]["template"] == "# A\n---\n# B"
    text = _as(server, "alice", "user", "playbook_write", {
        "name": "Ruled", "title": "taken", "description": "d", "stages": [STAGE]})
    assert "Refused" in text


def test_an_unreadable_playbook_is_not_free_for_all(pb):
    server, playbooks = pb
    folder = playbooks.PLAYBOOKS_DIR / "broken"
    folder.mkdir(parents=True)
    (folder / "PLAYBOOK.md").write_text("---\ntitle: [unclosed\n---\n", encoding="utf-8")
    text = _as(server, "alice", "user", "playbook_write", {
        "name": "Broken", "title": "t", "description": "d", "stages": [STAGE]})
    assert "Refused" in text


# ── L1 / L5 / L6 ────────────────────────────────────────────────────────────
def test_active_runs_per_person_are_limited(pb):
    server, playbooks = pb
    with sys.modules["authz"].rest_identity("alice", "user"):
        playbooks.save("Small", "t", "d", [STAGE], via_webui=True)
    for _ in range(playbooks.MAX_ACTIVE_RUNS):
        _as(server, "alice", "user", "playbook_start", {"playbook": "small", "project": "x"})
    text = _as(server, "alice", "user", "playbook_start", {"playbook": "small", "project": "x"})
    assert "Refused" in text and len(playbooks.runs_for("alice")) == playbooks.MAX_ACTIVE_RUNS


def test_odd_input_is_refused_not_a_server_error(ui):
    boss = ui("boss", "admin")
    r = boss.post("/ui/api/registry/mqtt", content=b'{"name": "b", "host": "h.example.com", "port": Infinity}',
                  headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    r = boss.post("/ui/api/playbooks", json={"name": "x", "title": "t", "stages": [{"id": ["a"], "name": 5}]})
    assert r.status_code in (200, 400)                 # coerced or refused — never a crash
    r = boss.post("/ui/api/playbooks", json={"name": "x", "title": "t", "stages": "just text"})
    assert r.status_code == 400


def test_audit_actions_carry_one_prefix(ui, monkeypatch):
    monkeypatch.setenv("AUTH_AUDIT_ALL", "1")
    alice = ui("alice", "user")
    alice.post("/ui/api/memory", json={"title": "n", "content": "c", "type": "reference"})
    log = sys.modules["authz"].AUDIT_FILE
    text = log.read_text(encoding="utf-8")
    assert "ui:memory_write" in text and "ui:ui:" not in text
