"""Several model runners at once (runner_builtin.py with runners.py, AICortex 2.3.0).

Before: one runner with one key ran every job. Now each runner asks its own model
with its own key, for exactly the jobs assigned to it. Proves:
- two runners: each provider gets only its runner's jobs, each with its own key;
- a switched-off runner asks nothing and claims nothing;
- an OpenAI-compatible address (e.g. Ollama in the LAN) gets the chat request without
  an Authorization header when it has no key, under the SSRF guard — and is refused at
  run time once the LAN is no longer allowed;
- the Claude setup-token path is gone for good;
- "Test now" asks the model one tiny question without tools.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

KEY_A, KEY_B = "sk-or-test-aaaa", "sk-oa-test-bbbb"


@pytest.fixture
def connector(monkeypatch, request):
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    monkeypatch.delenv("INTERNAL_ALLOW_CIDRS", raising=False)
    return request.getfixturevalue("http_server") + "/mcp"


class Echo:
    """Stands in for the model APIs: records each request, answers without tool calls."""

    def __init__(self):
        self.requests = []

    async def __call__(self, url, payload, headers, host=""):
        self.requests.append({"url": url, "payload": json.loads(json.dumps(payload)),
                              "headers": dict(headers), "host": host})
        return {"choices": [{"message": {"role": "assistant", "content": "OK"}}]}

    def text(self, i):
        return json.dumps(self.requests[i]["payload"])


def _runner(by="boss", admin=True, key="", **fields):
    ok, msg, rec = sys.modules["runners"].save(fields, by=by, admin=admin, key=key)
    assert ok, msg
    return rec


def _jobs(*specs):
    sys.modules["cron"]._write([
        {"id": jid, "name": jid, "schedule": "* * * * *", "prompt": f"task-{jid}-prompt", "notify": "user",
         "enabled": True, "owner": owner, "updated_by": owner or "boss", "runner": runner,
         "created": "2026-01-01T00:00:00", "last_run": ""} for jid, owner, runner in specs])


def _run(monkeypatch):
    builtin = sys.modules["runner_builtin"]
    echo = Echo()
    monkeypatch.setattr(builtin, "_post", echo)
    return echo, builtin


def _state(rec):
    return sys.modules["runner_state"].get(rec["id"], True)


def test_each_runner_asks_its_own_model_for_its_own_jobs(connector, monkeypatch):
    std = _runner(kind="openrouter", model="vendor/model-a", key=KEY_A)
    mine = _runner(by="alice", admin=False, kind="openai", model="gpt-x", key=KEY_B)
    _jobs(("x", "", ""), ("a", "alice", mine["id"]))
    echo, builtin = _run(monkeypatch)
    assert asyncio.run(builtin.run_once(connector)) == 2
    by_url = {r["url"]: i for i, r in enumerate(echo.requests)}
    first = by_url["https://openrouter.ai/api/v1/chat/completions"]
    second = by_url["https://api.openai.com/v1/chat/completions"]
    assert echo.requests[first]["headers"]["Authorization"] == f"Bearer {KEY_A}"
    assert echo.requests[second]["headers"]["Authorization"] == f"Bearer {KEY_B}"
    assert "task-x-prompt" in echo.text(first) and "task-a-prompt" not in echo.text(first)
    assert "task-a-prompt" in echo.text(second) and "task-x-prompt" not in echo.text(second)
    assert _state(std)["state"] == _state(mine)["state"] == "ran"


def test_a_switched_off_runner_asks_nothing(connector, monkeypatch):
    _runner(kind="openrouter", model="vendor/model-a", key=KEY_A)
    mine = _runner(by="alice", admin=False, kind="openai", model="gpt-x", key=KEY_B)
    sys.modules["runners"].set_enabled(mine["id"], False, by="alice", admin=False)
    _jobs(("a", "alice", mine["id"]))
    echo, builtin = _run(monkeypatch)
    assert asyncio.run(builtin.run_once(connector)) == 0 and echo.requests == []
    assert "claimed" not in sys.modules["cron"]._read()[0]


def test_an_ollama_address_is_asked_under_the_guard(connector, monkeypatch):
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "192.0.2.0/24")
    lan = _runner(kind="compatible", model="llama3.1", base_url="http://192.0.2.10:11434/v1")
    _jobs(("a", "", lan["id"]))
    echo, builtin = _run(monkeypatch)
    assert asyncio.run(builtin.run_once(connector)) == 1
    (req,) = echo.requests
    assert req["url"] == "http://192.0.2.10:11434/v1/chat/completions"
    assert "Authorization" not in req["headers"] and req["host"] == "192.0.2.10"
    _jobs(("b", "", lan["id"]))
    monkeypatch.delenv("INTERNAL_ALLOW_CIDRS")
    assert asyncio.run(builtin.run_once(connector)) == 0 and len(echo.requests) == 1
    assert _state(lan)["state"] == "error" and "INTERNAL_ALLOW_CIDRS" in _state(lan)["detail"]


def test_the_setup_token_path_is_gone(connector):
    assert "claude-abo" not in sys.modules["runners"].KINDS
    app = Path(sys.modules["runners"].__file__).parent
    assert not [p.name for p in app.glob("*.py") if "oauth-2025-04-20" in p.read_text()]


def test_test_now_asks_one_tiny_question_without_tools(connector, monkeypatch):
    rec = _runner(kind="openrouter", model="vendor/model-a", key=KEY_A)
    _jobs(("x", "", ""))
    echo, builtin = _run(monkeypatch)
    ok, msg = asyncio.run(builtin.probe(rec))
    (req,) = echo.requests
    assert ok and "OK" in msg and KEY_A not in msg
    assert "tools" not in req["payload"] and req["payload"]["max_tokens"] <= 16
    assert "task-x-prompt" not in echo.text(0) and "claimed" not in sys.modules["cron"]._read()[0]
