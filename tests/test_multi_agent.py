"""Several people and their agents work on one AICortex at the same time.

Reported by LLMs: AICortex "isn't multi-agent capable". Measured causes:
agent_register was admin-only (agents with a key couldn't register), nothing
recorded which agent wrote a memory, every person saw everyone's tasks, and two
agents editing the same memory silently overwrote each other.
"""
import asyncio
import json
import sys

import pytest
from fastmcp import Client


def _as(monkeypatch, person, agent):
    authz = sys.modules["authz"]
    monkeypatch.setattr(authz, "_identity", lambda: (person, False, {
        "aicortex_key": f"k-{agent}", "key_scopes": ["all"], "agent": agent}))


def _call(server, tool, args=None):
    async def go():
        async with Client(server.mcp) as client:
            res = await client.call_tool(tool, args or {}, raise_on_error=False)
            return res.content[0].text if res.content else "", res.is_error
    return asyncio.run(go())


@pytest.fixture
def enforced(monkeypatch, request):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    return request.getfixturevalue("fresh_server")


def test_agent_with_a_key_can_register_itself(monkeypatch, enforced):
    _as(monkeypatch, "dana", "hermes")
    text, err = _call(enforced, "agent_register", {"name": "hermes", "capabilities": "cad"})
    assert not err, text
    assert "hermes" in _call(enforced, "agent_list")[0]


def test_agents_cannot_take_over_or_remove_another_persons_agent(monkeypatch, enforced):
    _as(monkeypatch, "dana", "hermes")
    _call(enforced, "agent_register", {"name": "hermes"})
    _as(monkeypatch, "anna", "her-bot")
    text, err = _call(enforced, "agent_register", {"name": "hermes", "role": "evil"})
    assert err or "belongs to" in text
    text, err = _call(enforced, "agent_remove", {"name": "hermes"})
    assert err or "belongs to" in text
    agents = json.loads(sys.modules["coordination"].AGENTS_FILE.read_text(encoding="utf-8"))
    assert [a for a in agents if a["name"] == "hermes"][0].get("role", "") != "evil"


def test_memory_records_who_wrote_it(monkeypatch, enforced):
    _as(monkeypatch, "dana", "hermes")
    _call(enforced, "memory_write", {"title": "Printer nozzle", "type": "reference",
                                     "content": "0.4 mm"})
    text, _ = _call(enforced, "memory_read", {"name": "Printer nozzle"})
    assert "author: dana/hermes" in text


def test_people_see_only_their_own_tasks(monkeypatch, enforced):
    _as(monkeypatch, "dana", "hermes")
    _call(enforced, "task_add", {"title": "Dana's task"})
    _as(monkeypatch, "anna", "her-bot")
    _call(enforced, "task_add", {"title": "Anna's task"})
    text, _ = _call(enforced, "task_list")
    assert "Anna's task" in text and "Dana's task" not in text


def test_concurrent_edit_of_a_memory_is_detected(monkeypatch, enforced):
    _as(monkeypatch, "dana", "hermes")
    first, _ = _call(enforced, "memory_write", {"title": "Plan", "type": "project", "content": "v1"})
    assert "version 1" in first
    _as(monkeypatch, "dana", "claude-code")
    _call(enforced, "memory_write", {"title": "Plan", "type": "project", "content": "v2 by claude",
                                     "expected_version": 1})
    _as(monkeypatch, "dana", "hermes")
    text, _ = _call(enforced, "memory_write", {"title": "Plan", "type": "project",
                                               "content": "v2 by hermes", "expected_version": 1})
    assert "changed" in text.lower()
    assert "v2 by claude" in _call(enforced, "memory_read", {"name": "Plan"})[0]


def test_people_see_only_their_own_sessions(monkeypatch, enforced):
    _as(monkeypatch, "dana", "hermes")
    _call(enforced, "session_save", {"summary": "Dana's private work", "session_id": "s-work"})
    _as(monkeypatch, "anna", "her-bot")
    assert "Dana's private work" not in _call(enforced, "session_load")[0]
    assert "Dana's private work" not in _call(enforced, "session_load", {"session_id": "s-work"})[0]
    assert "s-work" not in _call(enforced, "session_list")[0]
