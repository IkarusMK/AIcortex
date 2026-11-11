"""Findings of the v2 security review (2026-10), each reproduced before the fix.

1. A REST key of a plain user scheduled cron jobs AS ANOTHER PERSON and fetched
   their act-as tokens: cron resolved the caller from the MCP token only, so a
   REST call without one (1.x always; 2.0 in open mode) looked like the
   unauthenticated default (admin).
2. Any user could send vault secrets to a URL of their choice with webhook_send:
   shared secrets, and — by naming the storage path — other people's private ones.
3. During a key rotation STORAGE_ENCRYPTION_KEY_OLD was not treated as internal,
   so webhook_send could send the old vault key out.
4. cortex_catalog and agent_list showed every person's cron prompts and agents.
5. session_prune deleted other people's sessions; cron_add with a name another
   person already used replaced their job.
6. Line breaks in a memory field (source, project, tags) forged the server-stamped
   author and version.
7. A bearer token with non-ASCII characters crashed the verifier (500, not 401).
8. vault_rekey left the previous generation in secrets.enc.bak — in plaintext
   after encrypting a plaintext vault, readable with the retired key after rotation.
(DNS rebinding is covered in test_host_guard.py.)
"""
import asyncio
import json
import sys

import httpx
import pytest
from cryptography.fernet import Fernet
from fastmcp.exceptions import ToolError


def _run(server, sub, role, tool, args=None):
    """Call a tool as (sub, role) the way the REST layer does."""
    authz = sys.modules["authz"]

    async def go():
        with authz.rest_identity(sub, role):
            try:
                res = await server.mcp.call_tool(tool, args or {})
                return "\n".join(getattr(c, "text", "") for c in res.content or [])
            except ToolError as exc:
                return f"DENIED: {exc}"
    return asyncio.run(go())


@pytest.fixture
def enforced(monkeypatch, request):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    return request.getfixturevalue("fresh_server")


# ── 1. REST key and cron ────────────────────────────────────────────────────

@pytest.fixture
def rest_server(monkeypatch, request):
    # Open mode: /mcp has no auth, but a REST key still stands for its person and
    # role. (With auth on, v2's bearer verifier already resolves the key.)
    monkeypatch.setenv("ALLOW_INSECURE", "1")
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    return request.getfixturevalue("http_server")


def test_rest_key_cannot_schedule_jobs_as_someone_else(rest_server):
    key, _ = sys.modules["apikeys"].create("bob", name="bobkey", scopes="all")

    def post(tool, body):
        r = httpx.post(f"{rest_server}/api/v1/tools/{tool}", json=body,
                       headers={"Authorization": f"Bearer {key}"})
        return r.status_code, r.text

    status, text = post("cron_add", {"name": "evil", "schedule": "* * * * *",
                                     "prompt": "read alice's memory", "owner": "alice"})
    assert "as alice" not in text, text
    assert all(j.get("owner") != "alice" for j in sys.modules["cron"]._read())
    status, text = post("cron_due", {})
    assert "act_as_token" not in text, text


# ── 2./3. Secrets leaving through webhook_send ──────────────────────────────

@pytest.fixture
def outbox(monkeypatch, enforced):
    sent = []
    monkeypatch.setattr(sys.modules["netguard"], "check_url", lambda url: (True, ""))

    class Resp:
        status_code = 200
    monkeypatch.setattr(httpx, "post", lambda url, **kw: sent.append(kw.get("headers")) or Resp())
    vault = sys.modules["secrets_store"]
    vault.vault_set("HA_TOKEN", "SHARED-VALUE")
    vault.vault_set("GH_TOKEN", "ALICE-VALUE", owner="alice")
    vault.vault_set("BOB_HOOK", "BOB-VALUE", owner="bob")
    return sent


def _send(server, sub, role, secret):
    return _run(server, sub, role, "webhook_send", {
        "url": "https://hooks.example.net/x", "secret_header": "X-Key", "secret_env": secret})


def test_user_cannot_send_a_shared_secret_out(outbox, enforced):
    _send(enforced, "bob", "user", "HA_TOKEN")
    assert "SHARED-VALUE" not in json.dumps(outbox)


def test_user_cannot_send_another_persons_secret_by_its_path(outbox, enforced):
    ns = sys.modules["tenancy"].vault_namespace("alice", "user")
    _send(enforced, "bob", "user", f"{ns}/GH_TOKEN")
    _send(enforced, "bob", "user", f"users/{ns}/GH_TOKEN")
    assert "ALICE-VALUE" not in json.dumps(outbox)
    assert sys.modules["secrets_store"].get_secret(f"{ns}/GH_TOKEN") is None


def test_user_can_still_send_their_own_secret_and_admin_a_shared_one(outbox, enforced):
    # webhook_send only sends a secret to a host it is explicitly bound to (host binding)
    vault = sys.modules["secrets_store"]
    vault.vault_set_hosts("BOB_HOOK", "bob", ["hooks.example.net"])
    vault.vault_set_hosts("HA_TOKEN", "", ["hooks.example.net"])
    _send(enforced, "bob", "user", "BOB_HOOK")
    _send(enforced, "boss", "admin", "HA_TOKEN")
    assert [h.get("X-Key") for h in outbox] == ["BOB-VALUE", "SHARED-VALUE"]


def test_old_vault_key_is_never_resolved(monkeypatch, outbox, enforced):
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY_OLD", Fernet.generate_key().decode())
    _send(enforced, "boss", "admin", "STORAGE_ENCRYPTION_KEY_OLD")
    assert not outbox
    assert sys.modules["secrets_store"].is_internal_env("STORAGE_ENCRYPTION_KEY_OLD")


# ── 4. Catalog and agent list per person ────────────────────────────────────

def test_catalog_and_agent_list_show_only_own_jobs_and_agents(enforced):
    _run(enforced, "alice", "user", "agent_register", {"name": "alice-agent",
                                                       "capabilities": "finance mail"})
    _run(enforced, "alice", "user", "cron_add", {"name": "nightly", "schedule": "0 3 * * *",
                                                 "prompt": "Summarise my salary mails"})
    for role in ("user", "viewer"):
        catalog = _run(enforced, "bob", role, "cortex_catalog")
        assert "alice-agent" not in catalog and "salary" not in catalog, catalog
    assert "alice-agent" not in _run(enforced, "bob", "user", "agent_list")
    assert "alice-agent" in _run(enforced, "alice", "user", "agent_list")
    admin = _run(enforced, "boss", "admin", "cortex_catalog")
    assert "alice-agent" in admin and "salary" in admin


# ── 5. Prune and job names per person ───────────────────────────────────────

def test_session_prune_removes_only_own_sessions(enforced):
    sessions = sys.modules["sessions"]
    _run(enforced, "alice", "user", "session_save", {"summary": "alice work",
                                                     "session_id": "alice-s"})
    path = sessions._path("alice-s")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["updated"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(data), encoding="utf-8")
    _run(enforced, "bob", "user", "session_prune", {"max_age_days": 1})
    assert path.exists()
    _run(enforced, "alice", "user", "session_prune", {"max_age_days": 1})
    assert not path.exists()


def test_cron_name_of_another_person_is_not_taken_over(enforced):
    _run(enforced, "alice", "user", "cron_add", {"name": "nightly", "schedule": "0 3 * * *",
                                                 "prompt": "alice's job"})
    _run(enforced, "bob", "user", "cron_add", {"name": "nightly", "schedule": "0 4 * * *",
                                               "prompt": "bob was here"})
    job = next(j for j in sys.modules["cron"]._read() if j["id"] == "nightly")
    assert job["owner"] == "alice" and job["prompt"] == "alice's job"


# ── 6. Memory frontmatter ───────────────────────────────────────────────────

def test_line_breaks_cannot_forge_author_or_version(enforced):
    memory = sys.modules["memory"]
    _run(enforced, "mallory", "user", "memory_write", {
        "title": "T1", "content": "body", "type": "reference",
        "source": "x\nauthor: alice/admin-agent\nversion: 999",
        "project": "p\rauthor: bob", "tags": "a version: 5"})
    path = next(memory.MEMORY_DIR.rglob("t1.md"))
    meta, _ = memory.read_meta(path)
    assert "alice" not in meta.get("author", "") and "bob" not in meta.get("author", "")
    assert str(meta.get("version")) == "1"
    fields = [ln.split(":", 1)[0] for ln in
              path.read_text(encoding="utf-8").split("\n---", 1)[0].splitlines()]
    assert fields.count("author") == 1 and fields.count("version") == 1


# ── 7. Non-ASCII bearer token ───────────────────────────────────────────────

@pytest.fixture
def runner_server(monkeypatch, request):
    monkeypatch.setenv("RUNNER_TOKEN", "runner-secret-123")
    return request.getfixturevalue("http_server")


def test_non_ascii_bearer_token_is_refused_not_crashing(runner_server):
    for token in ("café", "ak_zzz_é"):
        r = httpx.post(f"{runner_server}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                       headers=[(b"accept", b"application/json, text/event-stream"),
                                (b"authorization", b"Bearer " + token.encode("latin-1"))])
        assert r.status_code == 401, (token, r.status_code)


# ── 8. Vault backup after rekey ─────────────────────────────────────────────

def test_rekey_leaves_no_plaintext_backup(monkeypatch, request):
    monkeypatch.setenv("ALLOW_PLAINTEXT_VAULT", "1")
    request.getfixturevalue("fresh_server")
    vault = sys.modules["secrets_store"]
    vault.vault_set("HA_TOKEN", "PLAIN-SECRET")
    vault.vault_set("OTHER", "x")                       # .bak now holds a generation
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    vault.vault_rekey()
    assert b"PLAIN-SECRET" not in vault.VAULT_BAK.read_bytes()


def test_rekey_leaves_no_backup_readable_with_the_retired_key(monkeypatch, request):
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", old)
    request.getfixturevalue("fresh_server")
    vault = sys.modules["secrets_store"]
    vault.vault_set("HA_TOKEN", "abc")
    vault.vault_set("OTHER", "x")
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", new)
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY_OLD", old)
    vault.vault_rekey()
    with pytest.raises(Exception):
        Fernet(old.encode()).decrypt(vault.VAULT_BAK.read_bytes())


def test_dashes_in_a_title_or_description_keep_the_header_intact(enforced):
    # Re-review: "---" inside a value ended the header block early, so the note lost
    # its type, author and version (and every later expected_version check failed).
    memory, skills = sys.modules["memory"], sys.modules["skills"]
    _run(enforced, "mallory", "user", "memory_write", {"title": "Plan --- draft",
                                                       "content": "body", "type": "project"})
    meta, body = memory.read_meta(next(memory.MEMORY_DIR.rglob("plan-draft.md")))
    assert meta.get("type") == "project" and str(meta.get("version")) == "1"
    assert body.strip() == "body"
    _run(enforced, "boss", "admin", "skill_write", {"name": "plan-skill",
                                                    "description": "Before --- after",
                                                    "instructions": "steps", "category": "Garden"})
    meta, body = skills._parse((skills.SKILLS_DIR / "plan-skill" / "SKILL.md").read_text(encoding="utf-8"))
    assert meta.get("category") == "Garden" and body.strip() == "steps"
