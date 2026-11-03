"""The built-in runner: AICortex runs its own due jobs with an API model.

Before: scheduled jobs needed a separate runner container with its own folder, key
and project. Now runners are set up in the WebUI (runners.py). Proves:
- it reaches the connector as the runner with an in-memory token (no stored key);
- an idle minute calls no model; a due job goes to the provider's OpenAI-compatible
  chat API with the runner's key, and the model's tool calls run through the
  connector — never the runner plumbing (cron_due, cron_mark_run), not even through
  the router; AICortex records the run with the model's last words;
- an Anthropic key uses Anthropic's Messages API, also a 2.2 setup saved under the
  former name `anthropic`;
- it stays off unless switched on;
- a job is handed to one runner per slot, so two runners never run it twice.
"""
import asyncio
import json
import sys

import pytest

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


def _setup(kind="openrouter", model="openrouter/anthropic/claude-sonnet-4.5", enabled=True):
    ok, msg, rec = sys.modules["runners"].save({"kind": kind, "model": model, "enabled": enabled},
                                               by="boss", admin=True, key=KEY)
    assert ok, msg
    return rec


def _state() -> str:
    data = sys.modules["runners"].load()
    rec = next(r for r in data["runners"] if r["id"] == data["standard"])
    return sys.modules["runner_state"].get(rec["id"], rec["enabled"])["state"]


class Provider:
    """Stands in for the model APIs (OpenAI-compatible and Anthropic Messages): the
    first answer asks for two tool calls, the second ends the run."""

    def __init__(self):
        self.requests = []

    async def __call__(self, url, payload, headers, host=""):
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
    assert count == 0 and provider.requests == [] and _state() == "idle"


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
    assert replies[1]["content"].startswith("Refused")       # AICortex records the run itself
    job = sys.modules["cron"]._read()[0]
    assert job["last_run"] and job["last_result"] == "done"
    assert _state() == "ran"


def test_an_anthropic_key_uses_the_messages_api_with_x_api_key(connector, monkeypatch):
    sys.modules["cron"]._write([_job()])
    _setup(kind="anthropic", model="")
    _, provider, _ = _run(connector, monkeypatch)
    headers = provider.requests[0]["headers"]
    assert headers["x-api-key"] == KEY and "Authorization" not in headers
    assert provider.requests[0]["payload"]["model"] == "claude-sonnet-5-5"


def test_a_setup_saved_as_anthropic_runs_via_the_messages_api(connector, monkeypatch):
    sys.modules["cron"]._write([_job()])
    sys.modules["secrets_store"].vault_set("RUNNER_LLM_KEY", KEY)          # as 2.2 kept it
    sys.modules["store"].write_json(sys.modules["runners"].LEGACY_FILE, {
        "backend": "anthropic", "model": "anthropic/claude-sonnet-4-5", "builtin": True})
    _, provider, _ = _run(connector, monkeypatch)
    first = provider.requests[0]
    assert first["url"] == "https://api.anthropic.com/v1/messages"
    assert first["headers"]["x-api-key"] == KEY and first["payload"]["model"] == "claude-sonnet-4-5"


def test_it_stays_off_until_switched_on(connector, monkeypatch):
    sys.modules["cron"]._write([_job()])
    _setup(enabled=False)
    count, provider, builtin = _run(connector, monkeypatch)
    assert count == 0 and provider.requests == [] and _state() == "off"


def test_a_job_is_handed_to_one_runner_per_slot(connector, monkeypatch):
    sys.modules["cron"]._write([_job()])
    _setup()
    assert _run(connector, monkeypatch)[0] == 1
    sys.modules["cron"]._write([dict(_job(), last_run="")] )      # not marked, still claimed
    data = sys.modules["cron"]._read()
    data[0]["claimed"] = sys.modules["cron"]._latest_slot("* * * * *", sys.modules["cron"].datetime.now())
    sys.modules["cron"]._write(data)
    assert _run(connector, monkeypatch)[0] == 0
