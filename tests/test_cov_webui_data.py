"""WebUI management API (app/webui.py): vault, re-key, agent keys, skills, users.

Proves behaviour behind the gates: secret values are write-only and never reach the
audit log; a re-key keeps every secret readable (also across a key rotation and for a
plaintext vault) and refuses without a key or with an unreadable vault, leaving the
file untouched; agent keys show their secret once and can be revoked; skills keep one
spelling per category and cannot be read outside the library; user edits validate
before anything is written and deletions only remove what exists.
"""
import asyncio
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

pytestmark = pytest.mark.oidc       # sign-in mode: sessions come from the signed cookie

GET, POST = "GET", "POST"


class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def deco(fn):
            for m in methods:
                self.routes[(path, m)] = fn
            return fn
        return deco


class Ui:
    def __init__(self, webui):
        self.webui, self.mcp = webui, FakeMCP()
        webui.register(self.mcp)

    def call(self, route, method=GET, *, who="boss", body=None, query=None):
        cookie = self.webui._sign({"sub": who, "name": who, "role": "admin", "claim_role": "admin",
                                   "csrf": "tok",
                                   "exp": time.time() + 600})
        raw = body if isinstance(body, bytes) else b"" if body is None else json.dumps(body).encode()

        async def read():
            return raw
        req = SimpleNamespace(
            client=SimpleNamespace(host="192.0.2.10"), headers={"x-csrf": "tok"},
            cookies={self.webui.SESSION_COOKIE: cookie}, query_params=query or {}, path_params={},
            base_url="http://cortex.example.com/", body=read)
        resp = asyncio.run(self.mcp.routes[(route, method)](req))
        return resp.status_code, (json.loads(resp.body) if resp.body else {})


@pytest.fixture
def ui(monkeypatch, request, fake_idp):
    monkeypatch.setenv("OIDC_CONFIG_URL", f"{fake_idp}/.well-known/openid-configuration")
    monkeypatch.setenv("OIDC_CLIENT_ID", "aicortex")
    monkeypatch.setenv("JWT_SIGNING_KEY", "0" * 64)
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    monkeypatch.setenv("BASE_URL", "http://cortex.example.com")
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    request.getfixturevalue("fresh_server")
    return Ui(sys.modules["webui"])


def _audit():
    path = sys.modules["authz"].AUDIT_FILE
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _set(ui, name="DB_PASSWORD", value="test-token", **extra):
    return ui.call("/ui/api/secrets", POST, body={"name": name, "value": value, **extra})


# ── vault ────────────────────────────────────────────────────────────────────
def test_secret_is_stored_encrypted_attributed_and_never_audited_with_its_value(ui):
    status, body = _set(ui, description="Database login " + "x" * 400)
    assert status == 200 and body["ok"] and "test-token" not in json.dumps(body)
    vault = sys.modules["secrets_store"]
    assert b"test-token" not in vault.VAULT_FILE.read_bytes()
    entry = ui.call("/ui/api/secrets")[1]["secrets"][0]
    assert (entry["name"], entry["updated_by"], len(entry["description"])) == ("DB_PASSWORD", "boss", 300)
    assert "test-token" not in json.dumps(_audit())
    last = _audit()[-1]
    assert (last["tool"], last["decision"], last["reason"]) == (
        "ui:secret_set", "allow", "name=DB_PASSWORD owner=shared")


def test_a_private_secret_lives_in_the_owners_namespace_and_is_deleted_there(ui):
    assert _set(ui, owner="alice@example.com")[0] == 200
    entry = ui.call("/ui/api/secrets")[1]["secrets"][0]
    assert entry["owner"] and entry["name"] == "DB_PASSWORD"
    assert ui.call("/ui/api/secrets/delete", POST, body={"name": "DB_PASSWORD"})[0] == 404   # not shared
    status, body = ui.call("/ui/api/secrets/delete", POST,
                           body={"name": "DB_PASSWORD", "owner": entry["owner"]})
    assert status == 200 and body["ok"]
    assert ui.call("/ui/api/secrets")[1]["secrets"] == []


def test_secret_input_and_vault_refusals_map_to_400_409_404(ui, monkeypatch):
    assert _set(ui, value="")[0] == 400 and _set(ui, name="  ")[0] == 400
    status, body = _set(ui, name="bad/name")
    assert status == 409 and not body["ok"] and _audit()[-1]["decision"] == "deny"
    assert ui.call("/ui/api/secrets/delete", POST, body={"name": "NOPE"})[0] == 404
    assert _audit()[-1]["tool"] == "ui:secret_delete" and _audit()[-1]["decision"] == "deny"
    monkeypatch.delenv("STORAGE_ENCRYPTION_KEY")
    status, body = _set(ui)
    assert status == 409 and "STORAGE_ENCRYPTION_KEY" in body["message"]


def test_an_unreadable_vault_is_never_overwritten_through_the_ui(ui):
    assert _set(ui)[0] == 200
    vault = sys.modules["secrets_store"]
    vault.VAULT_FILE.write_bytes(b"corrupt-bytes")
    status, body = _set(ui, name="OTHER")
    assert status == 409 and "Refusing to write" in body["message"]
    assert ui.call("/ui/api/secrets/delete", POST, body={"name": "DB_PASSWORD"})[0] == 404
    assert vault.VAULT_FILE.read_bytes() == b"corrupt-bytes"
    assert ui.call("/ui/api/secrets")[1]["status"]["state"] == "unreadable"


def test_missing_references_are_listed_until_the_secret_exists(ui, tmp_path):
    services = tmp_path / "services_dir"
    services.mkdir()
    (services / "ha.json").write_text(json.dumps(
        {"base_url": "http://ha.example.com", "token_env": "HA_TOKEN"}), encoding="utf-8")
    assert ui.call("/ui/api/secrets")[1]["missing"] == ["HA_TOKEN"]
    _set(ui, name="HA_TOKEN")
    body = ui.call("/ui/api/secrets")[1]
    assert body["missing"] == [] and body["secrets"][0]["used_by"] == ["service ha"]


# ── re-key ───────────────────────────────────────────────────────────────────
def test_rekey_after_rotation_keeps_secrets_readable_with_only_the_new_key(ui, monkeypatch):
    vault = sys.modules["secrets_store"]
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", old)
    _set(ui, name="DB_PASSWORD", value="test-token")
    _set(ui, name="MAIL_PASSWORD", value="test-token-2")
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", new)
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY_OLD", old)
    assert ui.call("/ui/api/secrets")[1]["status"]["rotating"] is True
    status, body = ui.call("/ui/api/secrets/rekey", POST)
    assert status == 200 and body["ok"] and body["message"].startswith("Re-encrypted 2 secret(s)")
    monkeypatch.delenv("STORAGE_ENCRYPTION_KEY_OLD")
    assert (vault.get_secret("DB_PASSWORD"), vault.get_secret("MAIL_PASSWORD")) == ("test-token", "test-token-2")
    assert ui.call("/ui/api/secrets")[1]["status"] == {"state": "encrypted", "key": True, "rotating": False}
    assert (_audit()[-1]["tool"], _audit()[-1]["decision"]) == ("ui:secret_rekey", "allow")


def test_rekey_encrypts_a_plaintext_vault(ui, monkeypatch):
    vault = sys.modules["secrets_store"]
    key = Fernet.generate_key().decode()
    monkeypatch.delenv("STORAGE_ENCRYPTION_KEY")
    monkeypatch.setenv("ALLOW_PLAINTEXT_VAULT", "1")
    _set(ui)
    assert ui.call("/ui/api/secrets")[1]["status"]["state"] == "plaintext"
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", key)
    assert ui.call("/ui/api/secrets/rekey", POST)[1]["ok"] is True
    assert ui.call("/ui/api/secrets")[1]["status"]["state"] == "encrypted"
    assert b"test-token" not in vault.VAULT_FILE.read_bytes() and vault.get_secret("DB_PASSWORD") == "test-token"


def test_rekey_refuses_without_a_key_and_with_a_wrong_key_and_changes_nothing(ui, monkeypatch):
    vault = sys.modules["secrets_store"]
    good = os.environ["STORAGE_ENCRYPTION_KEY"]
    _set(ui)
    before = vault.VAULT_FILE.read_bytes()
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())    # wrong key
    status, body = ui.call("/ui/api/secrets/rekey", POST)
    assert status == 409 and not body["ok"] and "can't be read" in body["message"]
    monkeypatch.delenv("STORAGE_ENCRYPTION_KEY")                                      # no key at all
    status, body = ui.call("/ui/api/secrets/rekey", POST)
    assert status == 409 and "set STORAGE_ENCRYPTION_KEY first" in body["message"]
    assert vault.VAULT_FILE.read_bytes() == before and _audit()[-1]["decision"] == "deny"
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", good)
    assert vault.get_secret("DB_PASSWORD") == "test-token"


# ── agent keys ───────────────────────────────────────────────────────────────
def test_an_agent_key_is_shown_once_listed_without_secret_and_revocable(ui):
    status, body = ui.call("/ui/api/agentkeys", POST, body={
        "identity": " carol ", "name": "laptop", "ttl_days": 7, "profile": "full"})
    assert status == 200 and body["ok"] and body["key"].startswith("ak_")
    rec = body["record"]
    assert (rec["sub"], rec["name"], rec["created_by"], rec["profile"]) == ("carol", "laptop", "boss", "full")
    assert abs(rec["expires"] - (time.time() + 7 * 86400)) < 60
    assert body["mcp_url"] == "http://cortex.example.com/mcp"
    listing = ui.call("/ui/api/agentkeys")[1]
    assert [k["keyid"] for k in listing["keys"]] == [rec["keyid"]]
    assert body["key"] not in json.dumps(listing) and "secret_sha256" not in json.dumps(listing)
    assert body["key"] not in json.dumps(_audit()) and rec["keyid"] in _audit()[-1]["reason"]
    status, gone = ui.call("/ui/api/agentkeys/revoke", POST, body={"keyid": rec["keyid"]})
    assert status == 200 and gone["ok"] and ui.call("/ui/api/agentkeys")[1]["keys"] == []


@pytest.mark.parametrize("fields", [
    {"identity": ""}, {}, {"identity": "carol", "ttl_days": "soon"},
    {"identity": "carol", "scopes": "   "},
])
def test_agent_key_requests_that_cannot_be_honoured_are_400_and_create_nothing(ui, fields):
    status, body = ui.call("/ui/api/agentkeys", POST, body=fields)
    assert status == 400 and body["ok"] is False and body["message"]
    assert ui.call("/ui/api/agentkeys")[1]["keys"] == []


def test_revoking_an_unknown_or_empty_key_id_is_404(ui):
    for keyid in ("0123456789ab", ""):
        status, body = ui.call("/ui/api/agentkeys/revoke", POST, body={"keyid": keyid})
        assert status == 404 and body == {"ok": False, "message": "No such key."}
    assert _audit()[-1]["decision"] == "deny"


# ── skills ───────────────────────────────────────────────────────────────────
def _save_skill(ui, name="Printer Setup", category="Devices", **extra):
    return ui.call("/ui/api/skills", POST, body={
        "name": name, "category": category, "description": "d", "instructions": "# Steps", **extra})


def test_skill_save_needs_a_name_and_a_category_and_lists_the_known_ones(ui):
    assert _save_skill(ui, name="  ")[0] == 400
    _save_skill(ui)
    status, body = _save_skill(ui, name="Other", category="  ")
    assert status == 400 and body["categories"] == ["Devices"]


def test_skill_category_snaps_to_the_spelling_already_in_the_library(ui):
    _save_skill(ui, category="Home Automation")
    _, body = _save_skill(ui, name="Lights", category="  home   automation ")
    assert body["message"] == "Saved skill 'lights' [Home Automation]."
    cats = {s["category"] for s in ui.call("/ui/api/skills")[1]["skills"]}
    assert cats == {"Home Automation"}


def test_skill_get_returns_the_page_and_never_reads_outside_the_library(ui):
    _save_skill(ui, tags="print")
    status, body = ui.call("/ui/api/skills/get", query={"name": "printer-setup"})
    assert status == 200 and body["category"] == "Devices" and body["tags"] == "print"
    assert body["instructions"].startswith("# Steps")
    for name in ("nope", "../../etc/passwd", ""):
        assert ui.call("/ui/api/skills/get", query={"name": name})[0] == 404


def test_skill_delete_removes_only_an_existing_skill_and_is_audited(ui):
    _save_skill(ui)
    assert ui.call("/ui/api/skills/delete", POST, body={"name": "ghost"})[0] == 404
    assert ui.call("/ui/api/skills/delete", POST, body={"name": ""})[0] == 404
    status, body = ui.call("/ui/api/skills/delete", POST, body={"name": "Printer Setup"})
    assert status == 200 and body["ok"] and ui.call("/ui/api/skills")[1]["skills"] == []
    assert _audit()[-1]["tool"] == "ui:skill_delete"


def test_starter_skills_install_once_and_never_overwrite_an_edit(ui):
    listed = ui.call("/ui/api/skills/examples")[1]["examples"]
    assert listed and set(listed[0]) == {"name", "description", "category", "installed"}
    status, first = ui.call("/ui/api/skills/examples", POST, body={})
    assert status == 200 and first["added"] and not first["skipped"]
    page = sys.modules["skills"].SKILLS_DIR / first["added"][0] / "SKILL.md"
    page.write_text(page.read_text(encoding="utf-8") + "\nMY EDIT\n", encoding="utf-8")
    again = ui.call("/ui/api/skills/examples", POST, body={})[1]
    assert again["added"] == [] and sorted(again["skipped"]) == sorted(first["added"])
    assert "MY EDIT" in page.read_text(encoding="utf-8")
    assert _audit()[-1]["tool"] == "ui:skill_examples"


# ── users ────────────────────────────────────────────────────────────────────
def _policy():
    path = sys.modules["tenancy"].POLICY_FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


@pytest.mark.parametrize("fields", [
    {"identity": ""}, {"identity": "a", "role": "god"}, {"identity": "a", "memory": "everyone"},
    {"identity": "a", "vault": "shared"}, {"identity": "a", "devices": {"warp": "all"}},
])
def test_invalid_user_edits_are_400_and_write_no_policy(ui, fields):
    status, body = ui.call("/ui/api/users", POST, body=fields)
    assert status == 400 and body["error"] and _policy() == {}


def test_user_edit_merges_into_the_existing_entry(ui):
    ui.call("/ui/api/users", POST, body={"identity": "alice", "role": "viewer", "vault": "all",
                                         "services": "ha, nas", "devices": {"ssh": "box", "ftp": "all"},
                                         "note": "guest"})
    ui.call("/ui/api/users", POST, body={"identity": "alice", "role": "DEFAULT",
                                         "devices": {"ssh": "unset", "ftp": ""}, "memory": "all"})
    pol = _policy()
    assert "alice" not in pol["roles"]                        # "default" drops the explicit role
    entry = pol["users"]["alice"]
    assert entry == {"vault": "all", "services": ["ha", "nas"], "ftp": "all", "note": "guest",
                     "memory": "all"}
    row = ui.call("/ui/api/users")[1]["users"][0]
    assert (row["identity"], row["role"], row["services"], row["devices"]) == (
        "alice", "", "ha,nas", {"ftp": "all"})


def test_user_list_tolerates_a_hand_edited_policy_and_reports_defaults(ui, monkeypatch):
    monkeypatch.setenv("OIDC_DEFAULT_ROLE", "user")
    sys.modules["tenancy"]._write_policy({"roles": ["broken"], "users": {"bob": "oops"}})
    body = ui.call("/ui/api/users")[1]
    assert body["default_role"] == "user" and body["enforce"] is True and "ssh" in body["device_kinds"]
    assert [(u["identity"], u["role"], u["memory"]) for u in body["users"]] == [("bob", "", "own")]


def test_editing_a_user_whose_policy_entry_was_hand_edited_repairs_it(ui):
    sys.modules["tenancy"]._write_policy({"roles": ["broken"], "users": {"bob": "oops"}})
    status, _ = ui.call("/ui/api/users", POST, body={"identity": "bob", "role": "user"})
    assert status == 200
    assert _policy()["roles"] == {"bob": "user"} and _policy()["users"]["bob"] == {"memory": "own"}


def test_user_delete_removes_role_and_area_and_404s_for_unknown(ui):
    ui.call("/ui/api/users", POST, body={"identity": "alice", "role": "user"})
    ui.call("/ui/api/users", POST, body={"identity": "bob", "role": "viewer"})
    status, body = ui.call("/ui/api/users/delete", POST, body={"identity": "ghost"})
    assert status == 404 and not body["ok"] and set(_policy()["roles"]) == {"alice", "bob"}
    status, body = ui.call("/ui/api/users/delete", POST, body={"identity": "alice"})
    assert status == 200 and body["ok"]
    assert set(_policy()["roles"]) == {"bob"} and "alice" not in _policy()["users"]
    assert _audit()[-1]["tool"] == "ui:user_delete"
