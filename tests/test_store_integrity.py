"""File-backed stores survive concurrent agents and never wipe themselves.

Regressions:
- read-modify-write without a lock: parallel task_add calls lost tasks;
- a store that failed to parse read as [] and the next write replaced the whole
  board (tasks / inbox / agents / cron) with a single entry.
"""
import asyncio
import json
import sys

from fastmcp import Client


def _call_many(server, calls):
    async def go():
        async with Client(server.mcp) as client:
            return await asyncio.gather(*(client.call_tool(n, a, raise_on_error=False) for n, a in calls))
    return asyncio.run(go())


def test_parallel_task_adds_lose_nothing(fresh_server):
    n = 40
    _call_many(fresh_server, [("task_add", {"title": f"task {i}"}) for i in range(n)])
    coordination = sys.modules["coordination"]
    tasks = json.loads(coordination.TASKS_FILE.read_text(encoding="utf-8"))
    assert sorted(t["title"] for t in tasks) == sorted(f"task {i}" for i in range(n))


def test_unreadable_board_is_not_overwritten(fresh_server):
    coordination = sys.modules["coordination"]
    coordination.TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    broken = '[{"id": "a1", "title": "keep me"'          # torn / corrupt JSON
    coordination.TASKS_FILE.write_text(broken, encoding="utf-8")

    res = _call_many(fresh_server, [("task_add", {"title": "new"})])[0]

    assert coordination.TASKS_FILE.read_text(encoding="utf-8") == broken
    assert res.is_error


def test_unreadable_cron_store_is_not_overwritten(fresh_server):
    cron = sys.modules["cron"]
    cron.CRON_FILE.parent.mkdir(parents=True, exist_ok=True)
    broken = '[{"id": "nightly"'
    cron.CRON_FILE.write_text(broken, encoding="utf-8")

    _call_many(fresh_server, [("cron_add", {"name": "x", "schedule": "* * * * *", "prompt": "p"})])

    assert cron.CRON_FILE.read_text(encoding="utf-8") == broken


def test_unreadable_session_is_not_overwritten(fresh_server):
    sessions = sys.modules["sessions"]
    sessions.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = sessions._path("trip")
    broken = '{"id": "trip", "entries": [{"summary": "day one"'
    path.write_text(broken, encoding="utf-8")

    _call_many(fresh_server, [("session_save", {"summary": "day two", "session_id": "trip"})])

    assert path.read_text(encoding="utf-8") == broken


def test_parallel_secret_sets_lose_nothing(monkeypatch, request):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    server = request.getfixturevalue("fresh_server")
    n = 20
    _call_many(server, [("secret_set", {"name": f"TOKEN_{i}", "value": f"v{i}"}) for i in range(n)])
    secrets_store = sys.modules["secrets_store"]
    stored = secrets_store._read_all(strict=True)
    assert sorted(stored) == sorted(f"TOKEN_{i}" for i in range(n))


def test_only_one_agent_wins_a_claim(fresh_server):
    async def go():
        async with Client(fresh_server.mcp) as client:
            await client.call_tool("task_add", {"title": "deploy"})
            coordination = sys.modules["coordination"]
            tid = json.loads(coordination.TASKS_FILE.read_text(encoding="utf-8"))[0]["id"]
            first = await client.call_tool("task_claim", {"task_id": tid, "owner": "agent-a"})
            second = await client.call_tool("task_claim", {"task_id": tid, "owner": "agent-b"})
            board = json.loads(coordination.TASKS_FILE.read_text(encoding="utf-8"))
            return first.content[0].text, second.content[0].text, board[0]

    first, second, task = asyncio.run(go())
    assert "agent-a claimed" in first
    assert "agent-b claimed" not in second
    assert task["owner"] == "agent-a"


def test_expired_claim_can_be_taken_over(fresh_server):
    async def go():
        async with Client(fresh_server.mcp) as client:
            await client.call_tool("task_add", {"title": "backup"})
            coordination = sys.modules["coordination"]
            tid = json.loads(coordination.TASKS_FILE.read_text(encoding="utf-8"))[0]["id"]
            await client.call_tool("task_claim", {"task_id": tid, "owner": "agent-a", "lease_minutes": 1})
            board = json.loads(coordination.TASKS_FILE.read_text(encoding="utf-8"))
            board[0]["lease_until"] = "2000-01-01T00:00:00+00:00"       # lease ran out
            coordination.TASKS_FILE.write_text(json.dumps(board), encoding="utf-8")
            res = await client.call_tool("task_claim", {"task_id": tid, "owner": "agent-b"})
            return res.content[0].text

    assert "agent-b claimed" in asyncio.run(go())
