"""The runner's model access stays inside AICortex; an own runner keeps working.

Before (2.1.0): a separate runner container fetched the model key with the
`runner_config` tool and connected with a runner key made in the WebUI. Now runners
are built in (runners.py), so neither exists any more. Proves:
- `runner_config` is gone — no tool, no router entry, no WebUI route for runner keys;
- runner keys made in 2.1.0 and RUNNER_TOKEN still connect as the runner (an own
  runner keeps working), with admin tools refused;
- a runner's key — the one 2.2 kept and the ones typed in since — can't be used by a
  service.
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


def _client_run(url, token, fn):
    async def go():
        transport = StreamableHttpTransport(f"{url}/mcp", headers={"Authorization": f"Bearer {token}"})
        async with Client(transport) as client:
            return await fn(client)
    return asyncio.run(go())


def _call(url, token, tool, args=None):
    async def call(client):
        res = await client.call_tool(tool, args or {}, raise_on_error=False)
        return "\n".join(getattr(c, "text", "") for c in res.content or [])
    return _client_run(url, token, call)


def _setup(kind="openrouter", model="anthropic/claude-sonnet-4.5", key=KEY):
    ok, msg, rec = sys.modules["runners"].save({"kind": kind, "model": model}, by="boss",
                                               admin=True, key=key)
    assert ok, msg
    return rec


def _runner_key_from_2_1():
    """A runner key as 2.1.0 made it: identity "runner", scope all, flagged runner."""
    apikeys = sys.modules["apikeys"]
    key, rec = apikeys.create("runner", name="nas-runner", scopes="all")
    path = apikeys._keyid_path(rec["keyid"])
    path.write_text(json.dumps(dict(json.loads(path.read_text()), runner=True)))
    return key, rec


# ── runner_config and runner keys are gone ───────────────────────────────────
def test_runner_config_is_gone(connector):
    _setup()
    tools = _client_run(connector, "runner-secret-123", lambda c: c.list_tools())
    assert "runner_config" not in {t.name for t in tools}
    out = _call(connector, "runner-secret-123", "runner_config")
    assert KEY not in out
    via_router = _call(connector, "runner-secret-123", "integration_call", {"name": "runner_config"})
    assert KEY not in via_router and "No tool 'runner_config'" in via_router
    found = _call(connector, "runner-secret-123", "integration_search", {"query": "runner model access config"})
    assert "runner_config" not in found


def test_runner_keys_are_no_longer_made(connector):
    import inspect
    assert "runner" not in inspect.signature(sys.modules["apikeys"].create).parameters


@pytest.mark.oidc
def test_the_webui_has_no_runner_key_route(ui):
    assert not [r for r in ui.mcp.routes if r[0].startswith("/ui/api/runner/key")]
    assert "keys" not in ui.call("/ui/api/runners")[1]


# ── an own runner keeps working ──────────────────────────────────────────────
def test_a_runner_key_from_2_1_still_connects_as_the_runner(connector):
    key, _ = _runner_key_from_2_1()
    assert _call(connector, key, "cron_due") == "[]"
    assert "admin" in _call(connector, key, "service_add",
                            {"name": "x", "base_url": "https://example.com", "category": "Dev"}).lower()


def test_runner_token_still_connects_as_the_runner(connector):
    assert _call(connector, "runner-secret-123", "cron_due") == "[]"


def test_a_revoked_runner_key_is_rejected(connector):
    key, rec = _runner_key_from_2_1()
    sys.modules["apikeys"].revoke(rec["keyid"])
    r = httpx.post(f"{connector}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                   headers={"Accept": "application/json, text/event-stream",
                            "Authorization": f"Bearer {key}"})
    assert r.status_code == 401


# ── the key stays inside ─────────────────────────────────────────────────────
def test_a_service_cannot_borrow_the_runner_key(connector):
    rec = _setup()
    vault = sys.modules["secrets_store"]
    vault.vault_set("RUNNER_LLM_KEY", KEY)                         # as 2.2 kept it
    assert vault.get_secret("RUNNER_LLM_KEY") is None
    assert vault.get_secret(f"RUNNER_KEY_{rec['id']}") is None
