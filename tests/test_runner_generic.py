"""runner/generic/agent.py: no model while idle, the model from the WebUI when due.

Before: every tick started the model, which then asked cron_due — 1,440 model runs
a day with nothing to do — and the model choice lived only in the runner's .env.
Ticks were "run, then sleep 60 s", so a slow run skipped whole minutes. Proves:
- an idle minute makes no model call and does not even fetch the model access;
- due jobs go to the model set up in the WebUI (LiteLLM: model + key; Claude:
  the Claude Code CLI with the connector as its only MCP server), with the jobs
  already in the prompt and the runner plumbing withheld from the model;
- without a WebUI setup it falls back to LLM_MODEL, and does nothing without one;
- ticks are aligned to the clock instead of drifting.
"""
import asyncio
import importlib.util
import json
import os
import stat
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

AGENT = Path(__file__).resolve().parents[1] / "runner" / "generic" / "agent.py"
JOB = {"id": "report", "prompt": "do report", "notify": "user", "owner": "", "act_as_token": ""}
KEY = "sk-test-key-0000"


class FakeClient:
    def __init__(self, due, config):
        self.due, self.config, self.calls = due, config, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def list_tools(self):
        return [SimpleNamespace(name=n, description="", inputSchema={"type": "object"})
                for n in ("cron_due", "runner_config", "cron_mark_run", "integration_call")]

    async def call_tool(self, name, args=None, raise_on_error=True):
        self.calls.append((name, args or {}))
        text = {"cron_due": json.dumps(self.due), "runner_config": json.dumps(self.config)}.get(name, "ok")
        return SimpleNamespace(content=[SimpleNamespace(text=text)], data=None)


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setenv("CONNECTOR_URL", "http://cortex.example.com/mcp")
    monkeypatch.setenv("RUNNER_TOKEN", "ak_runner_test")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    calls = []

    async def acompletion(**kw):
        calls.append(kw)
        msg = SimpleNamespace(tool_calls=None, model_dump=lambda: {"role": "assistant", "content": "done"})
        if len(calls) == 1 and kw.get("tools"):
            fn = SimpleNamespace(name="integration_call", arguments=json.dumps({"name": "runner_config"}))
            msg.tool_calls = [SimpleNamespace(id="t1", function=fn)]
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(acompletion=acompletion))
    spec = importlib.util.spec_from_file_location("runner_agent_under_test", AGENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.StreamableHttpTransport = lambda url, headers=None: None
    mod.model_calls = calls
    return mod


def _use(agent, due, config):
    client = FakeClient(due, config)
    agent.Client = lambda transport: client
    return client


def test_an_idle_minute_starts_no_model(agent):
    client = _use(agent, [], {"backend": "openrouter", "model": "x", "key": KEY, "key_env": "OPENROUTER_API_KEY"})
    assert asyncio.run(agent.run_once()) == 0
    assert agent.model_calls == [] and [c[0] for c in client.calls] == ["cron_due"]


def test_due_jobs_go_to_the_webui_model_without_the_runner_plumbing(agent):
    client = _use(agent, [JOB], {"backend": "openrouter", "model": "anthropic/claude-sonnet-4.5",
                                 "key": KEY, "key_env": "OPENROUTER_API_KEY"})
    assert asyncio.run(agent.run_once()) == 1
    first = agent.model_calls[0]
    assert first["model"] == "openrouter/anthropic/claude-sonnet-4.5" and first["api_key"] == KEY
    assert '"id": "report"' in first["messages"][0]["content"]
    assert {t["function"]["name"] for t in first["tools"]} == {"cron_mark_run", "integration_call"}
    assert [c[0] for c in client.calls] == ["cron_due", "runner_config"]     # the model's try was refused
    tool_replies = [m for m in agent.model_calls[1]["messages"] if m.get("role") == "tool"]
    assert len(tool_replies) == 1 and tool_replies[0]["content"].startswith("Refused")


def test_a_claude_setup_runs_claude_code_with_the_connector_only(agent, monkeypatch):
    _use(agent, [JOB], {"backend": "claude-abo", "model": "", "key": KEY, "key_env": "CLAUDE_CODE_OAUTH_TOKEN"})
    seen = {}

    def fake_run(cmd, env=None, **kw):
        path = cmd[cmd.index("--mcp-config") + 1]
        seen.update(cmd=cmd, env=env, mode=stat.S_IMODE(os.stat(path).st_mode),
                    config=json.loads(Path(path).read_text()), path=path)
        return SimpleNamespace(returncode=0, stdout="all done", stderr="")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-leak-into-the-run")
    assert asyncio.run(agent.run_once()) == 1
    assert seen["cmd"][:2] == ["claude", "-p"] and "--strict-mcp-config" in seen["cmd"]
    assert seen["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == KEY and "ANTHROPIC_API_KEY" not in seen["env"]
    assert KEY not in " ".join(seen["cmd"]) and '"id": "report"' in seen["cmd"][2]
    assert seen["mode"] == 0o600 and not Path(seen["path"]).exists()
    server = seen["config"]["mcpServers"]["aicortex"]
    assert server["headers"]["Authorization"] == "Bearer ak_runner_test"
    assert "mcp__aicortex__runner_config" in seen["cmd"][seen["cmd"].index("--disallowedTools") + 1]
    assert agent.model_calls == []


def test_without_a_webui_setup_it_falls_back_to_llm_model_or_waits(agent, monkeypatch):
    _use(agent, [JOB], {"backend": ""})
    assert asyncio.run(agent.run_once()) == 1 and agent.model_calls == []
    monkeypatch.setattr(agent, "MODEL", "gpt-4o-mini")
    asyncio.run(agent.run_once())
    assert agent.model_calls[0]["model"] == "gpt-4o-mini" and agent.model_calls[0]["api_key"] is None


def test_ticks_are_aligned_to_the_clock(agent):
    assert agent.seconds_to_next_tick(1_000_020.0, 60) == 60 - 1_000_020 % 60 + agent.TICK_OFFSET
    assert agent.seconds_to_next_tick(120.0, 60) == 60 + agent.TICK_OFFSET
