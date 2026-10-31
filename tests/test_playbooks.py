"""Playbooks: multi-stage work where a human approves each hand-off.

Modelled on Anthropic's "The AI-native SDLC playbook" (2026-08-21): every stage
produces an artifact the next stage reads (intent.md → spec.md → plan.md …) and a
person accepts it before work moves on. Pinned here: a run walks through all
stages; a stage needs the approval it declares — the WebUI by default, a chat
approval only where the stage allows it and never from an agent key, the runner
or a scheduled job; runs are per person; limits; and the briefing shows the next
step.
"""
import asyncio
import sys

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

TWO_STAGES = [
    {"id": "plan", "name": "Plan", "goal": "Capture the intent", "artifact": "intent.md",
     "template": "# Intent: <title>\n## Problem\n", "approval": "webui", "approver": "Product owner"},
    {"id": "build", "name": "Build", "goal": "Plan the change", "artifact": "plan.md",
     "template": "# Plan\n", "approval": "webui", "approver": "Engineer"},
]


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


def _person_chat(monkeypatch, server, sub, claims=None, runner=False):
    """Call tools the way an interactive app (OAuth) or an agent key would."""
    authz = sys.modules["authz"]
    monkeypatch.setattr(authz, "_identity", lambda: (sub, runner, claims or {}))

    def call(tool, args=None):
        async def go():
            async with Client(server.mcp) as client:
                res = await client.call_tool(tool, args or {}, raise_on_error=False)
                return res.content[0].text if res.content else ""
        return asyncio.run(go())
    return call


@pytest.fixture
def pb(monkeypatch, request):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    server = request.getfixturevalue("fresh_server")
    return server, sys.modules["playbooks"]


def _write(server, who="alice", stages=TWO_STAGES, name="Small change", role="user"):
    return _as(server, who, role, "playbook_write", {
        "name": name, "title": name, "description": "Two stages", "category": "Dev",
        "stages": stages})


def _write_in_webui(stages, who="alice", name="Small change"):
    """A person saving in the WebUI may relax approvals; an AI through the tools may not."""
    with sys.modules["authz"].rest_identity(who, "user"):
        return sys.modules["playbooks"].save(name, name, "Two stages", stages, "Dev", via_webui=True)


def _start(server, who="alice"):
    text = _as(server, who, "user", "playbook_start", {"playbook": "small-change",
                                                       "project": "Garden", "title": "Watering"})
    return sys.modules["playbooks"].runs_for(who)[0]["id"], text


def test_example_template_installs_with_six_stages_and_plays(pb):
    _, playbooks = pb
    playbooks.install_example()
    d = playbooks.load("ai-native-sdlc")
    assert [s["artifact"] for s in d["stages"]][:3] == ["intent.md", "spec.md", "plan.md"]
    assert len(d["stages"]) == 6 and all(s["approval"] == "webui" for s in d["stages"])
    ids = {p["id"] for p in d["plays"]}
    assert len(ids) == 14 and all(set(p["requires"]) <= ids for p in d["plays"])
    assert "claude.com" in d["source"]


def test_a_run_walks_through_every_stage(pb):
    server, playbooks = pb
    assert "Saved" in _write(server)
    run_id, text = _start(server)
    assert "intent.md" in text and "# Intent" in text            # what to produce, with template
    assert "Submitted" in _as(server, "alice", "user", "playbook_submit",
                              {"run_id": run_id, "artifact": "# Intent: water\n## Problem\nDry."})
    status = _as(server, "alice", "user", "playbook_status", {"run_id": run_id})
    assert "WebUI" in status                                     # waits for a person
    playbooks.decide(run_id, "approve", by="alice", via="webui")
    _as(server, "alice", "user", "playbook_submit", {"run_id": run_id, "artifact": "# Plan\n1. hose",
                                                     "ref_path": "plan.md", "ref_commit": "abc1234"})
    playbooks.decide(run_id, "approve", by="alice", via="webui")
    run = playbooks.get_run(run_id)
    assert run["status"] == "done"
    assert run["stages"][1]["versions"][-1]["ref"] == {"path": "plan.md", "commit": "abc1234", "url": ""}


def test_a_rejected_stage_comes_back_for_rework(pb):
    server, playbooks = pb
    _write(server)
    run_id, _ = _start(server)
    _as(server, "alice", "user", "playbook_submit", {"run_id": run_id, "artifact": "draft"})
    playbooks.decide(run_id, "reject", by="alice", via="webui", note="Name the affected users")
    status = _as(server, "alice", "user", "playbook_status", {"run_id": run_id})
    assert "Name the affected users" in status
    _as(server, "alice", "user", "playbook_submit", {"run_id": run_id, "artifact": "second draft"})
    stage = playbooks.get_run(run_id)["stages"][0]
    assert [v["text"] for v in stage["versions"]] == ["draft", "second draft"]
    assert stage["status"] == "submitted"


def test_webui_stage_cannot_be_approved_from_the_chat(pb, monkeypatch):
    server, playbooks = pb
    _write(server)
    run_id, _ = _start(server)
    _as(server, "alice", "user", "playbook_submit", {"run_id": run_id, "artifact": "x"})
    call = _person_chat(monkeypatch, server, "alice")
    text = call("playbook_record_approval", {"run_id": run_id, "decision": "approve"})
    assert "WebUI" in text and playbooks.get_run(run_id)["stages"][0]["status"] == "submitted"


def test_chat_approval_only_from_the_person_themselves(pb, monkeypatch):
    server, playbooks = pb
    stages = [dict(TWO_STAGES[0], approval="chat"), TWO_STAGES[1]]
    assert "Saved" in _write_in_webui(stages)
    run_id, _ = _start(server)
    _as(server, "alice", "user", "playbook_submit", {"run_id": run_id, "artifact": "x"})
    for claims, runner in (({"aicortex_key": "k1", "key_scopes": ["all"], "agent": "bot"}, False),
                           ({}, True)):
        call = _person_chat(monkeypatch, server, "alice", claims, runner)
        call("playbook_record_approval", {"run_id": run_id, "decision": "approve"})
        assert playbooks.get_run(run_id)["stages"][0]["status"] == "submitted"
    call = _person_chat(monkeypatch, server, "alice")
    call("playbook_record_approval", {"run_id": run_id, "decision": "approve", "note": "ok in chat"})
    stage = playbooks.get_run(run_id)["stages"][0]
    assert stage["status"] == "approved" and stage["decision"]["via"] == "chat"


def test_a_stage_without_approval_moves_on_when_submitted(pb):
    server, playbooks = pb
    assert "Saved" in _write_in_webui([dict(TWO_STAGES[0], approval="none"), TWO_STAGES[1]])
    run_id, _ = _start(server)
    _as(server, "alice", "user", "playbook_submit", {"run_id": run_id, "artifact": "x"})
    assert playbooks.get_run(run_id)["current"] == 1


def test_runs_belong_to_their_person(pb):
    server, playbooks = pb
    _write(server)
    run_id, _ = _start(server)
    assert "Watering" not in _as(server, "bob", "user", "playbook_runs", {"status": "all"})
    text = _as(server, "bob", "user", "playbook_submit", {"run_id": run_id, "artifact": "hijack"})
    assert "No run" in text and playbooks.get_run(run_id)["stages"][0]["versions"] == []
    assert "Watering" in _as(server, "boss", "admin", "playbook_runs", {"status": "all"})


def test_limits_and_definition_checks(pb):
    server, playbooks = pb
    _write(server)
    run_id, _ = _start(server)
    big = "x" * (playbooks.MAX_ARTIFACT + 1)
    assert "too long" in _as(server, "alice", "user", "playbook_submit", {"run_id": run_id, "artifact": big})
    for i in range(playbooks.MAX_VERSIONS + 3):
        _as(server, "alice", "user", "playbook_submit", {"run_id": run_id, "artifact": f"v{i}"})
    assert len(playbooks.get_run(run_id)["stages"][0]["versions"]) == playbooks.MAX_VERSIONS
    bad = [dict(TWO_STAGES[0], approval="whenever")]
    assert "Refused" in _write(server, stages=bad, name="Bad approval")
    assert "Refused" in _write(server, stages=[TWO_STAGES[0], TWO_STAGES[0]], name="Twice")
    assert "Refused" in _write(server, name="runs")
    _write(server, name="Odd\ntitle: x --- y")
    assert playbooks.load("odd-title-x-y")["stages"][0]["artifact"] == "intent.md"


def test_shared_playbook_changed_only_by_admin_or_author(pb):
    server, playbooks = pb
    _write(server, who="alice")
    assert "Refused" in _write(server, who="bob", stages=[TWO_STAGES[1]])
    assert len(playbooks.load("small-change")["stages"]) == 2
    assert "Saved" in _write(server, who="boss", role="admin", stages=[TWO_STAGES[1]])


def test_briefing_shows_the_next_step_of_the_projects_runs(pb):
    server, _ = pb
    _write(server)
    run_id, _ = _start(server)
    brief = _as(server, "alice", "user", "cortex_start", {"project": "garden"})
    assert "Watering" in brief and "intent.md" in brief
    _as(server, "alice", "user", "playbook_submit", {"run_id": run_id, "artifact": "x"})
    brief = _as(server, "alice", "user", "cortex_start", {"project": "garden"})
    assert "WebUI" in brief
