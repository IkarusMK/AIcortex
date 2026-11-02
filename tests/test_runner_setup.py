"""Runner setup from the WebUI: runner keys and the runner's model access.

Before: the runner needed RUNNER_TOKEN in the connector's .env (plus a restart) and
its model key in its own .env. Now an admin creates a runner key and enters the
model access (Claude subscription token, Anthropic/OpenRouter/OpenAI/Gemini key) in
the WebUI. Proves:
- a runner key connects as the runner: cron_due works, admin tools stay refused;
- runner_config hands the model access to the runner only - never to an admin, a
  person's agent key, the REST API or an act-as run - and only once per run;
- the stored key is never shown in the WebUI and cannot be used by a service;
- the WebUI routes are admin-only, CSRF-gated, validate the backend and keep an
  existing key when the field is left empty.
"""
import asyncio
import json
import sys

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from test_cov_webui import Ui, _audit_lines, ui  # noqa: F401 - ui is a fixture

KEY = "sk-or-test-0000"


@pytest.fixture
def connector(monkeypatch, request):
    monkeypatch.setenv("ALLOW_PLAINTEXT_VAULT", "1")
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    monkeypatch.setenv("RUNNER_TOKEN", "runner-secret-123")
    return request.getfixturevalue("http_server")


def _call(url, token, tool, args=None):
    async def go():
        transport = StreamableHttpTransport(f"{url}/mcp", headers={"Authorization": f"Bearer {token}"})
        async with Client(transport) as client:
            res = await client.call_tool(tool, args or {}, raise_on_error=False)
            return "\n".join(getattr(c, "text", "") for c in res.content or [])
    return asyncio.run(go())


def _setup(backend="openrouter", model="anthropic/claude-sonnet-4.5", key=KEY):
    return sys.modules["runner_setup"].save_config(backend, model, key, by="boss")


def _runner_key():
    key, rec = sys.modules["apikeys"].create("runner", name="nas-runner", runner=True)
    return key, rec


# ── runner keys ──────────────────────────────────────────────────────────────
def test_a_runner_key_connects_as_the_runner(connector):
    key, rec = _runner_key()
    assert rec["runner"] is True
    assert _call(connector, key, "cron_due") == "[]"
    assert "admin" in _call(connector, key, "service_add",
                            {"name": "x", "base_url": "https://example.com", "category": "Dev"}).lower()


def test_a_revoked_runner_key_is_rejected(connector):
    key, rec = _runner_key()
    sys.modules["apikeys"].revoke(rec["keyid"])
    r = httpx.post(f"{connector}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                   headers={"Accept": "application/json, text/event-stream",
                            "Authorization": f"Bearer {key}"})
    assert r.status_code == 401


# ── runner_config: the model access goes to the runner only ─────────────────
def test_the_runner_gets_backend_model_and_key(connector):
    _setup()
    cfg = json.loads(_call(connector, "runner-secret-123", "runner_config"))
    assert cfg == {"backend": "openrouter", "model": "anthropic/claude-sonnet-4.5",
                   "key_env": "OPENROUTER_API_KEY", "key": KEY}


def test_a_runner_key_gets_it_too_and_an_unset_setup_says_so(connector):
    key, _ = _runner_key()
    assert json.loads(_call(connector, key, "runner_config")) == {"backend": ""}


def test_only_once_per_run(connector):
    _setup()
    assert KEY in _call(connector, "runner-secret-123", "runner_config")
    again = _call(connector, "runner-secret-123", "runner_config")
    assert KEY not in again and again.startswith("Refused")


def test_nobody_but_the_runner_gets_it(connector):
    _setup()
    person_key, _ = sys.modules["apikeys"].create("alice", name="laptop", scopes="all")
    assert _call(connector, person_key, "runner_config").startswith("Denied")
    r = httpx.post(f"{connector}/api/v1/tools/runner_config", json={},
                   headers={"Authorization": f"Bearer {person_key}"})
    assert r.status_code == 403 and KEY not in r.text


def test_not_during_an_act_as_run(connector, monkeypatch):
    _setup()
    monkeypatch.setattr(__import__("actas"), "current", lambda caller="": "alice")
    out = _call(connector, "runner-secret-123", "runner_config")
    assert out.startswith("Denied") and KEY not in out


def test_a_service_cannot_borrow_the_runner_key(connector):
    _setup()
    assert sys.modules["secrets_store"].get_secret("RUNNER_LLM_KEY") is None


# ── WebUI ────────────────────────────────────────────────────────────────────
@pytest.mark.oidc
def test_the_webui_shows_the_setup_but_never_the_key(ui):
    _setup()
    status, body = ui.call("/ui/api/runner")
    assert status == 200 and KEY not in json.dumps(body)
    assert (body["backend"], body["model"], body["key_set"]) == (
        "openrouter", "anthropic/claude-sonnet-4.5", True)
    assert ui.call("/ui/api/runner", role="user")[0] == 403


@pytest.mark.oidc
def test_saving_validates_and_keeps_the_key_when_left_empty(ui):
    status, body = ui.call("/ui/api/runner/config", "POST",
                           body={"backend": "claude-abo", "model": "", "key": "sk-ant-oat01-x"})
    assert status == 200 and body["ok"]
    status, body = ui.call("/ui/api/runner/config", "POST", body={"backend": "claude-abo", "model": "opus"})
    assert status == 200
    assert sys.modules["runner_setup"]._stored_key() == "sk-ant-oat01-x"
    assert ui.call("/ui/api/runner/config", "POST", body={"backend": "nope"})[0] == 400
    assert ui.call("/ui/api/runner/config", "POST", body={"backend": "openai", "model": ""})[0] == 400
    assert ui.call("/ui/api/runner/config", "POST", csrf=None, body={"backend": "openai", "model": "x"})[0] == 403
    assert "sk-ant-oat01-x" not in json.dumps(_audit_lines())


@pytest.mark.oidc
def test_a_runner_key_is_made_in_the_webui_and_shown_once(ui):
    status, body = ui.call("/ui/api/runner/key", "POST", body={"name": "nas"})
    assert status == 200 and body["key"].startswith("ak_")
    keys = ui.call("/ui/api/runner")[1]["keys"]
    assert [k["name"] for k in keys] == ["nas"] and all("key" not in k for k in keys)
    assert ui.call("/ui/api/runner/key", "POST", role="user", body={})[0] == 403


def test_the_router_never_reaches_runner_config(connector):
    _setup()
    out = _call(connector, "runner-secret-123", "integration_call", {"name": "runner_config"})
    assert KEY not in out and "integration_call" in out
    found = _call(connector, "runner-secret-123", "integration_search", {"query": "runner model access config"})
    assert "runner_config" not in found
