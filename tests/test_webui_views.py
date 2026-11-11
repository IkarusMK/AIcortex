"""The WebUI serves every signed-in person their own area, with their AI's rights.

Before: the WebUI was an admin-only management plane — anyone else got "access
denied", and notes, services and scheduled jobs could only be managed through
tools. Now each person manages their own notes (and later jobs and playbook
runs) in the browser; vault, users, agent keys, registries and the audit log stay
admin-only. Every view runs the same policy path as an MCP call
(authz.enforce_rest + rest_identity), so the browser never gets more than the AI.
"""
import sys
import time

import httpx
import pytest

pytestmark = pytest.mark.oidc


@pytest.fixture
def ui(monkeypatch, request, fake_idp):
    monkeypatch.setenv("OIDC_CONFIG_URL", f"{fake_idp}/.well-known/openid-configuration")
    monkeypatch.setenv("OIDC_CLIENT_ID", "aicortex")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "upstream-secret")
    monkeypatch.setenv("JWT_SIGNING_KEY", "0" * 64)
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    base = request.getfixturevalue("http_server")
    webui = sys.modules["webui"]

    def client(sub, role, csrf=True):
        cookie = webui._sign({"sub": sub, "name": sub, "role": role, "claim_role": role, "csrf": "tok",
                              "exp": time.time() + 600})
        headers = {"X-CSRF": "tok"} if csrf else {}
        return httpx.Client(base_url=base, cookies={webui.SESSION_COOKIE: cookie},
                            headers=headers)
    return client


def _note(c, title, content="body", **extra):
    return c.post("/ui/api/memory", json={"title": title, "content": content,
                                          "type": "reference", **extra})


# ── A: access model ─────────────────────────────────────────────────────────

def test_a_person_without_admin_role_reaches_their_own_area(ui):
    alice = ui("alice", "user")
    me = alice.get("/ui/api/me").json()
    assert me["authenticated"] and me["role"] == "user"
    assert alice.get("/ui/api/memory").status_code == 200


def test_admin_areas_stay_admin_only(ui):
    alice = ui("alice", "user")
    for path in ("/ui/api/secrets", "/ui/api/users", "/ui/api/agentkeys",
                 "/ui/api/audit", "/ui/api/services", "/ui/api/overview",
                 "/ui/api/memory/candidates"):
        assert alice.get(path).status_code == 403, path


def test_changes_need_the_csrf_header(ui):
    alice = ui("alice", "user", csrf=False)
    assert _note(alice, "No CSRF").status_code == 403


# ── B: notes ────────────────────────────────────────────────────────────────

def test_note_round_trip(ui):
    alice = ui("alice", "user")
    r = _note(alice, "Printer nozzle", "0.4 mm", project="Garden", tags="printer")
    assert r.status_code == 200, r.text
    name = r.json()["name"]
    rows = alice.get("/ui/api/memory", params={"project": "garden"}).json()["notes"]
    assert [n["name"] for n in rows] == [name]
    got = alice.get("/ui/api/memory/get", params={"name": name}).json()
    assert got["body"].strip() == "0.4 mm" and got["meta"]["type"] == "reference"
    version = int(got["meta"]["version"])
    r = _note(alice, "Printer nozzle", "0.6 mm", expected_version=version)
    assert r.status_code == 200 and r.json()["version"] == version + 1
    assert alice.get("/ui/api/memory", params={"q": "nozzle"}).json()["notes"]
    assert alice.post("/ui/api/memory/delete", json={"name": name}).status_code == 200
    assert alice.get("/ui/api/memory").json()["notes"] == []


def test_people_see_only_their_own_notes(ui):
    _note(ui("alice", "user"), "Alice private plan")
    bob = ui("bob", "user")
    names = [n["title"] for n in bob.get("/ui/api/memory").json()["notes"]]
    assert "Alice private plan" not in names
    assert bob.get("/ui/api/memory/get", params={"name": "alice-private-plan"}).status_code == 404


def test_concurrent_edit_is_reported_not_overwritten(ui):
    alice = ui("alice", "user")
    _note(alice, "Plan", "v1")
    _note(alice, "Plan", "v2 elsewhere", expected_version=1)
    r = _note(alice, "Plan", "v2 here", expected_version=1)
    assert r.status_code == 409 and "changed" in r.json()["message"]
    got = alice.get("/ui/api/memory/get", params={"name": "plan"}).json()
    assert "v2 elsewhere" in got["body"]


def test_viewer_reads_but_cannot_write(ui):
    _note(ui("vera", "user"), "Seen by vera")
    vera = ui("vera", "viewer")
    assert vera.get("/ui/api/memory").status_code == 200
    assert _note(vera, "Viewer note").status_code == 403


def test_admin_reviews_candidates(ui):
    sys.modules["memory"].stage_candidate("Maybe useful", "a fact", "reference", "auto")
    boss = ui("boss", "admin")
    rows = boss.get("/ui/api/memory/candidates").json()["candidates"]
    assert [c["title"] for c in rows] == ["Maybe useful"]
    r = boss.post("/ui/api/memory/candidates/promote", json={"name": rows[0]["name"]})
    assert r.status_code == 200, r.text
    assert boss.get("/ui/api/memory/candidates").json()["candidates"] == []


# ── C: services and devices (admin) ─────────────────────────────────────────

def test_registry_editing_is_admin_only(ui):
    alice = ui("alice", "user")
    assert alice.get("/ui/api/registry/schema").status_code == 403
    r = alice.post("/ui/api/registry/services", json={"name": "x", "base_url": "https://x.example.com",
                                                       "category": "Dev"})
    assert r.status_code == 403


def test_schema_covers_every_registry_and_marks_secret_references(ui):
    kinds = {k["kind"]: k for k in ui("boss", "admin").get("/ui/api/registry/schema").json()["kinds"]}
    assert {"services", "mqtt", "ftp", "printers", "scanners", "webdav", "caldav", "ssh",
            "mail", "imap", "mcp", "webhooks"} <= set(kinds)
    fields = {f["name"]: f for f in kinds["services"]["fields"]}
    assert fields["name"]["required"] and fields["token_env"]["secret_ref"]
    assert fields["write_only"]["type"] == "boolean"


def test_admin_creates_edits_and_deletes_a_service(ui):
    boss = ui("boss", "admin")
    r = boss.post("/ui/api/registry/services", json={
        "name": "Docs API", "base_url": "https://docs.example.com", "category": "Dev",
        "token_env": "DOCS_TOKEN", "description": "Documentation"})
    assert r.status_code == 200, r.text
    name = r.json()["name"]
    cfg = boss.get(f"/ui/api/registry/services/{name}").json()["config"]
    assert cfg["base_url"] == "https://docs.example.com" and cfg["token_env"] == "DOCS_TOKEN"
    # a partial edit changes only what was sent — and an explicit "" clears a field
    r = boss.post("/ui/api/registry/services", json={"name": "Docs API", "auth_scheme": "",
                                                      "base_url": "https://docs.example.com"})
    assert r.status_code == 200, r.text
    cfg = boss.get(f"/ui/api/registry/services/{name}").json()["config"]
    assert cfg["auth_scheme"] == "" and cfg["description"] == "Documentation"
    assert boss.post("/ui/api/registry/services/delete", json={"name": name}).status_code == 200
    assert boss.get(f"/ui/api/registry/services/{name}").status_code == 404


def test_registry_form_returns_only_known_fields(ui):
    import json as _json
    boss = ui("boss", "admin")
    services = sys.modules["services"]
    services.SERVICES_DIR.mkdir(parents=True, exist_ok=True)
    (services.SERVICES_DIR / "legacy.json").write_text(_json.dumps({
        "name": "legacy", "base_url": "https://legacy.example.com",
        "api_key_plaintext": "LEAKME-NOT"}), encoding="utf-8")
    r = boss.get("/ui/api/registry/services/legacy")
    assert r.status_code == 200 and "LEAKME-NOT" not in r.text
    assert boss.get("/ui/api/registry/services/..%2Fvault%2Fsecrets").status_code == 404


def test_invalid_value_is_refused_with_a_reason(ui):
    r = ui("boss", "admin").post("/ui/api/registry/mqtt", json={"name": "Broker", "host": "broker.example.com",
                                                                 "port": "not-a-number"})
    assert r.status_code == 400 and "port" in r.json()["message"]


# ── D: scheduled jobs ───────────────────────────────────────────────────────

def _job(c, name, schedule="0 6 * * *", **extra):
    return c.post("/ui/api/cron", json={"name": name, "schedule": schedule,
                                        "prompt": f"{name} prompt", **extra})


def test_people_manage_their_own_jobs(ui):
    alice, bob = ui("alice", "user"), ui("bob", "user")
    assert _job(alice, "Morning summary").status_code == 200
    rows = alice.get("/ui/api/cron").json()["jobs"]
    assert [r["name"] for r in rows] == ["Morning summary"]
    assert rows[0]["next_run"] and rows[0]["owner"] == "alice"
    assert bob.get("/ui/api/cron").json()["jobs"] == []
    # switching off keeps the job, and it no longer has a next run
    assert _job(alice, "Morning summary", enabled=False).status_code == 200
    row = alice.get("/ui/api/cron").json()["jobs"][0]
    assert row["enabled"] is False and row["next_run"] == ""
    assert bob.post("/ui/api/cron/delete", json={"name": "Morning summary"}).status_code != 200
    assert alice.post("/ui/api/cron/delete", json={"name": "Morning summary"}).status_code == 200
    assert alice.get("/ui/api/cron").json()["jobs"] == []


def test_admin_sees_every_job_with_its_owner(ui):
    _job(ui("alice", "user"), "Alice job")
    rows = ui("boss", "admin").get("/ui/api/cron").json()["jobs"]
    assert [(r["name"], r["owner"]) for r in rows] == [("Alice job", "alice")]


def test_a_job_saved_in_the_webui_keeps_its_runner(ui, monkeypatch):
    monkeypatch.setenv("ALLOW_PLAINTEXT_VAULT", "1")
    ok, msg, rec = sys.modules["runners"].save({"kind": "openrouter", "model": "vendor/m"},
                                               by="alice", admin=False, key="sk-test")
    assert ok, msg
    alice, bob = ui("alice", "user"), ui("bob", "user")
    assert _job(alice, "Daily", runner=rec["id"]).status_code == 200
    assert alice.get("/ui/api/cron").json()["jobs"][0]["runner"] == rec["id"]
    assert _job(alice, "Daily", enabled=False).status_code == 200          # left out: kept
    assert alice.get("/ui/api/cron").json()["jobs"][0]["runner"] == rec["id"]
    r = _job(bob, "Bobs", runner=rec["id"])
    assert r.status_code == 400 and "Refused" in r.json()["message"]


def test_schedule_preview_and_refusal(ui):
    alice = ui("alice", "user")
    d = alice.get("/ui/api/cron/preview", params={"schedule": "0 8 * * 1-5"}).json()
    assert d["ok"] and len(d["next"]) == 3
    d = alice.get("/ui/api/cron/preview", params={"schedule": "61 * * * *"}).json()
    assert not d["ok"] and d["message"]
    r = _job(alice, "Broken", schedule="0 0 30 2 *")
    assert r.status_code == 400


# ── E: playbooks ────────────────────────────────────────────────────────────

def test_playbook_library_and_a_run_in_the_webui(ui):
    assert ui("boss", "admin").post("/ui/api/playbooks/example", json={}).json()["ok"]
    alice = ui("alice", "user")
    names = [p["name"] for p in alice.get("/ui/api/playbooks").json()["playbooks"]]
    assert names == ["ai-native-sdlc"]
    d = alice.get("/ui/api/playbooks/get", params={"name": "ai-native-sdlc"}).json()
    assert len(d["stages"]) == 6 and len(d["plays"]) == 14
    r = alice.post("/ui/api/playbooks/runs/start", json={"playbook": "ai-native-sdlc",
                                                         "project": "Garden", "title": "Watering"})
    run_id = r.json()["id"]
    assert alice.post("/ui/api/playbooks/runs/submit", json={
        "id": run_id, "artifact": "# Intent: watering", "ref_path": "intent/watering.md"}).json()["ok"]
    assert alice.post("/ui/api/playbooks/runs/decide", json={
        "id": run_id, "decision": "approve", "note": "fine"}).json()["ok"]
    run = alice.get("/ui/api/playbooks/runs/get", params={"id": run_id}).json()["run"]
    assert run["current"] == 1 and run["stages"][0]["decision"]["via"] == "webui"
    assert run["stages"][0]["decision"]["by"] == "alice"
    rows = alice.get("/ui/api/playbooks/runs", params={"project": "garden"}).json()["runs"]
    assert [r["id"] for r in rows] == [run_id] and "spec.md" in rows[0]["next_step"]


def test_others_cannot_see_or_decide_a_persons_run(ui):
    ui("boss", "admin").post("/ui/api/playbooks/example", json={})
    alice = ui("alice", "user")
    run_id = alice.post("/ui/api/playbooks/runs/start", json={
        "playbook": "ai-native-sdlc", "project": "Garden"}).json()["id"]
    alice.post("/ui/api/playbooks/runs/submit", json={"id": run_id, "artifact": "x"})
    bob = ui("bob", "user")
    assert bob.get("/ui/api/playbooks/runs/get", params={"id": run_id}).status_code == 404
    assert not bob.post("/ui/api/playbooks/runs/decide", json={"id": run_id, "decision": "approve"}).json().get("ok")
    vera = ui("alice", "viewer")
    assert vera.post("/ui/api/playbooks/runs/decide", json={"id": run_id, "decision": "approve"}).status_code == 403
    assert ui("boss", "admin").get("/ui/api/playbooks/runs/get", params={"id": run_id}).status_code == 200
