"""The Runner tab's routes (webui_runner.py, AICortex 2.3.0).

Before: one admin-only runner setup under Scheduled jobs (/ui/api/runner, /config).
Now every signed-in person manages their own runners. Proves:
- each person sees their own runners and the standard one, an admin all of them —
  never a key's value, and other people's key names and triggers not even to admins;
- a typed key goes to the vault and never comes back, not in the audit log either;
- the vault names offered are the person's own (an admin's: shared ones too);
- only the owner changes a runner; an admin may switch it off, delete it, choose the
  standard; a viewer changes nothing; no CSRF token, no change;
- "Test now" is for the owner, once a minute;
- the old routes are gone (a cron job's runner in the WebUI: test_webui_views.py).
"""
import json
import sys

import pytest

from test_cov_webui import Ui, _audit_lines, ui  # noqa: F401 - ui is a fixture

pytestmark = pytest.mark.oidc
KEY = "sk-or-webui-test-0000"


def _make(ui, who="boss", role="admin", key=KEY, **fields):
    fields.setdefault("kind", "openrouter")
    fields.setdefault("model", "vendor/model-a")
    status, body = ui.call("/ui/api/runners", "POST", who=who, role=role, body=dict(fields, key=key))
    assert status == 200 and body["ok"], body
    return body["runner"]


def _ids(ui, who, role):
    return {r["id"] for r in ui.call("/ui/api/runners", who=who, role=role)[1]["runners"]}


def test_each_person_sees_their_runners_and_the_standard(ui):
    std = _make(ui)
    alices = _make(ui, who="alice", role="user", name="Alice")
    bobs = _make(ui, who="bob", role="user", name="Bob")
    assert _ids(ui, "alice", "user") == {std["id"], alices["id"]}
    status, body = ui.call("/ui/api/runners")
    assert status == 200 and {r["id"] for r in body["runners"]} == {std["id"], alices["id"], bobs["id"]}
    assert KEY not in json.dumps(body) and body["standard"] == std["id"]
    seen = {r["id"]: r for r in body["runners"]}
    assert seen[bobs["id"]]["key"] is None and seen[std["id"]]["key"]["name"].startswith("RUNNER_KEY_")
    assert seen[std["id"]]["state"]["state"] in ("waiting", "idle")
    assert "routine-fire-payload" in body["setup_sentence"]["de"]
    assert ui.call("/ui/api/runners", who=None)[0] == 401


def test_a_typed_key_never_comes_back(ui):
    rec = _make(ui, who="alice", role="user")
    assert KEY not in json.dumps(rec) and KEY not in json.dumps(_audit_lines())
    assert sys.modules["runners"].resolve_key(sys.modules["runners"].get(rec["id"])) == KEY


def test_the_vault_names_offered_are_the_persons_own(ui):
    v = sys.modules["secrets_store"]
    v.vault_set("OPENROUTER_API_KEY", "sk-shared")
    v.vault_set("MY_KEY", "sk-alice", owner="alice")
    v.vault_set("RUNNER_LLM_KEY", "sk-internal")
    names = ui.call("/ui/api/runners/secrets", who="alice", role="user")[1]["names"]
    assert names == [{"name": "MY_KEY", "shared": False}]
    admin = {n["name"] for n in ui.call("/ui/api/runners/secrets")[1]["names"]}
    assert "OPENROUTER_API_KEY" in admin and "RUNNER_LLM_KEY" not in admin and "MY_KEY" not in admin
    rec = _make(ui, who="alice", role="user", key="", key_ref={"name": "MY_KEY", "shared": False})
    assert rec["key"] == {"name": "MY_KEY", "shared": False}


def test_only_the_owner_changes_a_runner(ui):
    std = _make(ui)
    rec = _make(ui, who="alice", role="user")
    assert ui.call("/ui/api/runners", "POST", who="bob", role="user", body={"id": rec["id"], "name": "x"})[0] == 400
    assert ui.call("/ui/api/runners", "POST", body={"id": rec["id"], "model": "other/m"})[0] == 400
    assert ui.call("/ui/api/runners/enabled", "POST", who="bob", role="user",
                   body={"id": rec["id"], "enabled": False})[0] == 400
    assert ui.call("/ui/api/runners/enabled", "POST", body={"id": rec["id"], "enabled": False})[0] == 200
    assert ui.call("/ui/api/runners", "POST", who="carol", role="viewer", body={"kind": "openrouter"})[0] == 403
    assert ui.call("/ui/api/runners", "POST", who="alice", role="user", csrf=None,
                   body={"id": rec["id"], "name": "renamed"})[0] == 403
    assert sys.modules["runners"].get(rec["id"])["name"] != "renamed"
    other = _make(ui, name="Second")
    assert ui.call("/ui/api/runners/standard", "POST", who="alice", role="user", body={"id": rec["id"]})[0] == 400
    assert ui.call("/ui/api/runners/standard", "POST", body={"id": other["id"]})[0] == 200
    assert ui.call("/ui/api/runners/delete", "POST", who="bob", role="user", body={"id": rec["id"]})[0] == 400
    assert ui.call("/ui/api/runners/delete", "POST", who="alice", role="user", body={"id": rec["id"]})[0] == 200
    assert ui.call("/ui/api/runners/delete", "POST", body={"id": std["id"]})[0] == 200
    assert ui.call("/ui/api/runners/delete", "POST", body={"id": other["id"]})[0] == 400   # the standard


def test_test_now_is_for_the_owner_once_a_minute(ui, monkeypatch):
    rec = _make(ui, who="alice", role="user")
    asked = []

    async def model(url, payload, headers, host=""):
        asked.append(payload)
        return {"choices": [{"message": {"content": "OK"}}]}
    monkeypatch.setattr(sys.modules["runner_builtin"], "_post", model)
    assert ui.call("/ui/api/runners/test", "POST", body={"id": rec["id"]})[0] == 400       # not boss's
    status, body = ui.call("/ui/api/runners/test", "POST", who="alice", role="user", body={"id": rec["id"]})
    assert status == 200 and body["ok"] and "OK" in body["message"] and len(asked) == 1
    status, body = ui.call("/ui/api/runners/test", "POST", who="alice", role="user", body={"id": rec["id"]})
    assert status == 429 and len(asked) == 1


def test_the_old_runner_routes_are_gone(ui):
    assert ("/ui/api/runner", "GET") not in ui.mcp.routes
    assert ("/ui/api/runner/config", "POST") not in ui.mcp.routes
    assert ("/ui/api/runner/chatgpt/start", "POST") in ui.mcp.routes
