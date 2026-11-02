"""The built-in runner: AICortex runs its own due jobs with an API model.

Before: scheduled jobs needed a separate runner container with its own folder, key
and project. Now an admin switches the runner on in the WebUI. Proves:
- it reaches the connector as the runner with an in-memory token (no stored key);
- an idle minute calls no model; a due job goes to the provider's OpenAI-compatible
  chat API with the stored key, and the model's tool calls run through the
  connector — never cron_due, not even through the router;
- a Claude subscription token (`claude setup-token`) goes straight to Anthropic's
  Messages API as an OAuth token — no Claude Code needed; an Anthropic key too, also
  one saved under the former name `anthropic`;
- it stays off unless switched on;
- a job is handed to one runner per slot, so two runners never run it twice;
- the WebUI saves the switch and shows the built-in runner's state.
"""
import asyncio
import json
import sys

import pytest

from test_cov_webui import Ui, ui  # noqa: F401 - ui is a fixture

KEY = "sk-or-test-0000"


@pytest.fixture
def connector(monkeypatch, request):
    monkeypatch.setenv("ALLOW_PLAINTEXT_VAULT", "1")
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    url = request.getfixturevalue("http_server")
    return f"{url}/mcp"


def _job(job_id="report"):
    return {"id": job_id, "name": job_id, "schedule": "* * * * *", "prompt": f"do {job_id}",
            "notify": "user", "enabled": True, "owner": "", "created": "2026-01-01T00:00:00",
            "last_run": ""}


def _setup(backend="openrouter", model="openrouter/anthropic/claude-sonnet-4.5", builtin=True):
    ok, msg = sys.modules["runner_setup"].save_config(backend, model, KEY, by="boss", builtin=builtin)
    assert ok, msg


class Provider:
    """Stands in for the model APIs (OpenAI-compatible and Anthropic Messages): the
    first answer asks for two tool calls, the second ends the run."""

    def __init__(self):
        self.requests = []

    async def __call__(self, url, payload, headers):
        self.requests.append({"url": url, "payload": json.loads(json.dumps(payload)),
                              "headers": dict(headers)})
        first = len(self.requests) == 1
        router = [("c1", {"name": "cron_due"}),
                  ("c2", {"name": "cron_mark_run", "arguments": {"name": "report", "result": "ok"}})]
        if url.endswith("/v1/messages"):
            if first:
                return {"content": [{"type": "tool_use", "id": cid, "name": "integration_call", "input": args}
                                    for cid, args in router], "stop_reason": "tool_use"}
            return {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn"}
        if first:
            calls = [{"id": cid, "type": "function",
                      "function": {"name": "integration_call", "arguments": json.dumps(args)}}
                     for cid, args in router]
            return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": calls}}]}
        return {"choices": [{"message": {"role": "assistant", "content": "done"}}]}


def _run(connector, monkeypatch):
    builtin = sys.modules["runner_builtin"]
    provider = Provider()
    monkeypatch.setattr(builtin, "_post", provider)
    count = asyncio.run(builtin.run_once(connector))
    return count, provider, builtin


def test_the_internal_token_reaches_the_connector_as_the_runner(connector, monkeypatch):
    _setup()
    sys.modules["cron"]._write([_job()])
    count, _, _ = _run(connector, monkeypatch)
    assert count == 1
    assert sys.modules["cron"].runner_status()["last_seen"]


def test_an_idle_minute_calls_no_model(connector, monkeypatch):
    _setup()
    count, provider, builtin = _run(connector, monkeypatch)
    assert count == 0 and provider.requests == [] and builtin.status()["state"] == "idle"


def test_a_due_job_goes_to_the_provider_and_runs_through_the_connector(connector, monkeypatch):
    _setup()
    sys.modules["cron"]._write([_job()])
    count, provider, builtin = _run(connector, monkeypatch)
    first = provider.requests[0]
    assert first["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert first["headers"]["Authorization"] == f"Bearer {KEY}"
    assert first["payload"]["model"] == "anthropic/claude-sonnet-4.5"
    assert '"id": "report"' in first["payload"]["messages"][0]["content"]
    offered = {t["function"]["name"] for t in first["payload"]["tools"]}
    assert offered and "cron_due" not in offered
    replies = [m for m in provider.requests[1]["payload"]["messages"] if m["role"] == "tool"]
    assert replies[0]["content"].startswith("Refused") and KEY not in json.dumps(provider.requests[1]["payload"])
    assert "Marked 'report' run" in replies[1]["content"]
    assert sys.modules["cron"]._read()[0]["last_run"]
    assert builtin.status()["state"] == "ran"


def test_a_claude_subscription_token_goes_straight_to_the_messages_api(connector, monkeypatch):
    sys.modules["cron"]._write([_job()])
    _setup(backend="claude-abo", model="sonnet")
    count, provider, builtin = _run(connector, monkeypatch)
    first = provider.requests[0]
    assert count == 1 and first["url"] == "https://api.anthropic.com/v1/messages"
    assert first["headers"]["Authorization"] == f"Bearer {KEY}"
    assert first["headers"]["anthropic-beta"] == "oauth-2025-04-20"
    assert "x-api-key" not in first["headers"] and "user-agent" not in {k.lower() for k in first["headers"]}
    assert first["payload"]["model"] == "claude-sonnet-5-5" and first["payload"]["max_tokens"] > 0
    assert "cron_due" not in {t["name"] for t in first["payload"]["tools"]}
    results = provider.requests[1]["payload"]["messages"][-1]["content"]
    assert results[0]["content"].startswith("Refused") and "Marked 'report' run" in results[1]["content"]
    assert builtin.status()["state"] == "ran"


def test_an_anthropic_key_uses_the_messages_api_with_x_api_key(connector, monkeypatch):
    sys.modules["cron"]._write([_job()])
    _setup(backend="claude-api", model="")
    _, provider, _ = _run(connector, monkeypatch)
    headers = provider.requests[0]["headers"]
    assert headers["x-api-key"] == KEY and "Authorization" not in headers
    assert provider.requests[0]["payload"]["model"] == "claude-sonnet-5-5"


def test_a_setup_saved_as_anthropic_runs_via_the_messages_api(connector, monkeypatch):
    sys.modules["cron"]._write([_job()])
    _setup(builtin=True)                                 # stores the key
    rs = sys.modules["runner_setup"]
    sys.modules["store"].write_json(rs.CONFIG_FILE, {
        "backend": "anthropic", "model": "anthropic/claude-sonnet-4-5", "builtin": True})
    _, provider, _ = _run(connector, monkeypatch)
    first = provider.requests[0]
    assert first["url"] == "https://api.anthropic.com/v1/messages"
    assert first["headers"]["x-api-key"] == KEY and first["payload"]["model"] == "claude-sonnet-4-5"


def test_it_stays_off_until_switched_on(connector, monkeypatch):
    sys.modules["cron"]._write([_job()])
    _setup(builtin=False)
    count, provider, builtin = _run(connector, monkeypatch)
    assert count == 0 and provider.requests == [] and builtin.status()["state"] == "off"


def test_a_job_is_handed_to_one_runner_per_slot(connector, monkeypatch):
    sys.modules["cron"]._write([_job()])
    _setup()
    assert _run(connector, monkeypatch)[0] == 1
    sys.modules["cron"]._write([dict(_job(), last_run="")] )      # not marked, still claimed
    data = sys.modules["cron"]._read()
    data[0]["claimed"] = sys.modules["cron"]._latest_slot("* * * * *", sys.modules["cron"].datetime.now())
    sys.modules["cron"]._write(data)
    assert _run(connector, monkeypatch)[0] == 0


@pytest.mark.oidc
def test_the_webui_switches_it_on_and_shows_its_state(ui):
    status, body = ui.call("/ui/api/runner/config", "POST",
                           body={"backend": "openai", "model": "gpt-x", "key": "sk-x", "builtin": True})
    assert status == 200 and body["ok"]
    got = ui.call("/ui/api/runner")[1]
    assert got["builtin"] is True and got["builtin_state"]["state"] in ("off", "idle", "waiting")
    ui.call("/ui/api/runner/config", "POST", body={"backend": "openai", "model": "gpt-x"})
    assert ui.call("/ui/api/runner")[1]["builtin"] is True            # left out = unchanged
    ui.call("/ui/api/runner/config", "POST", body={"backend": "openai", "model": "gpt-x", "builtin": False})
    assert ui.call("/ui/api/runner")[1]["builtin"] is False
