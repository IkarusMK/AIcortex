"""WebUI playbook routes (app/webui_playbooks.py): status codes of every refusal.

Proves, over real HTTP with signed session cookies: every route needs a signed-in
person (401); changes need the CSRF header (403) and a JSON object body (400); a viewer
reads but cannot save, delete, start, submit, decide or cancel (403); the library
answers 404 for unknown names and 400/409 for refused saves, deletes and example
installs; a person's runs are invisible to others (404) while an admin may decide on
them; decisions, submissions and cancellations report their refusals instead of
failing; a library error becomes a 400, never a server error.
"""
import json
import sys
import time

import httpx
import pytest

pytestmark = pytest.mark.oidc

STAGES = [{"id": "plan", "name": "Plan", "goal": "g", "artifact": "intent.md", "approval": "webui"},
          {"id": "build", "name": "Build", "artifact": "plan.md", "approval": "webui"}]
BODY = {"name": "Flow", "title": "Flow", "description": "d", "stages": STAGES}
ROUTES = [("GET", "/ui/api/playbooks"), ("GET", "/ui/api/playbooks/get"), ("GET", "/ui/api/playbooks/runs"),
          ("GET", "/ui/api/playbooks/runs/get"), ("POST", "/ui/api/playbooks"),
          ("POST", "/ui/api/playbooks/delete"), ("POST", "/ui/api/playbooks/example"),
          ("POST", "/ui/api/playbooks/runs/start"), ("POST", "/ui/api/playbooks/runs/submit"),
          ("POST", "/ui/api/playbooks/runs/decide"), ("POST", "/ui/api/playbooks/runs/cancel")]
CHANGES = [path for method, path in ROUTES if method == "POST" and not path.endswith("example")]


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
        cookie = webui._sign({"sub": sub, "name": sub, "role": role, "claim_role": role, "csrf": "tok", "exp": time.time() + 600})
        return httpx.Client(base_url=base, cookies={webui.SESSION_COOKIE: cookie}, timeout=30,
                            headers={"X-CSRF": "tok"} if csrf else {})
    client.base = base
    return client


def _start(client, project="Garden"):
    assert client.post("/ui/api/playbooks", json=BODY).status_code == 200
    r = client.post("/ui/api/playbooks/runs/start", json={"playbook": "flow", "project": project})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _msg(response):
    return response.json().get("message", "")


# ── access ───────────────────────────────────────────────────────────────────
def test_every_route_needs_a_signed_in_person(ui):
    anonymous = httpx.Client(base_url=ui.base)
    forged = httpx.Client(base_url=ui.base, cookies={sys.modules["webui"].SESSION_COOKIE: "x.y"})
    for who in (anonymous, forged):
        for method, path in ROUTES:
            assert who.request(method, path, json={} if method == "POST" else None).status_code == 401, path


def test_changes_need_the_csrf_header_and_a_json_object(ui):
    careless, alice = ui("alice", "user", csrf=False), ui("alice", "user")
    for path in CHANGES:
        assert careless.post(path, json=BODY).status_code == 403, path
        for junk in (b"{nope", b"[1, 2]"):
            r = alice.post(path, content=junk, headers={"Content-Type": "application/json"})
            assert (r.status_code, r.json()["error"]) == (400, "invalid body"), path
    assert ui("boss", "admin", csrf=False).post("/ui/api/playbooks/example", json={}).status_code == 403


def test_a_viewer_reads_but_cannot_change_anything(ui):
    alice, viewer = ui("alice", "user"), ui("vera", "viewer")
    rid = _start(alice)
    assert viewer.get("/ui/api/playbooks").json()["playbooks"][0]["name"] == "flow"
    assert viewer.get("/ui/api/playbooks/get", params={"name": "flow"}).status_code == 200
    assert viewer.get("/ui/api/playbooks/runs").status_code == 200
    for path, body in (("", BODY), ("/delete", {"name": "flow"}),
                       ("/runs/start", {"playbook": "flow", "project": "x"}),
                       ("/runs/submit", {"id": rid, "artifact": "x"}),
                       ("/runs/decide", {"id": rid, "decision": "approve"}), ("/runs/cancel", {"id": rid})):
        r = viewer.post(f"/ui/api/playbooks{path}", json=body)
        assert r.status_code == 403 and r.json()["error"] == "forbidden", path
    assert alice.get("/ui/api/playbooks/runs/get", params={"id": rid}).json()["run"]["status"] == "active"


# ── library ──────────────────────────────────────────────────────────────────
def test_saving_reports_refusals_and_a_library_error_is_not_a_server_error(ui, monkeypatch):
    alice = ui("alice", "user")
    for body in (dict(BODY, stages=[]), dict(BODY, stages=5), dict(BODY, name="Runs"),
                 dict(BODY, stages=[dict(STAGES[0], approval="whenever")])):
        r = alice.post("/ui/api/playbooks", json=body)
        assert r.status_code == 400 and r.json()["ok"] is False and _msg(r).startswith("Refused"), body

    def broken(*_a, **_k):
        raise TypeError("odd input")
    monkeypatch.setattr(sys.modules["playbooks"], "save", broken)
    r = alice.post("/ui/api/playbooks", json=BODY)
    assert (r.status_code, _msg(r)) == (400, "Refused: TypeError")


def test_a_person_saves_with_relaxed_approval_and_the_audit_log_notes_it(ui):
    alice = ui("alice", "user")
    body = dict(BODY, stages=[dict(STAGES[0], approval="none"), STAGES[1]])
    r = alice.post("/ui/api/playbooks", json=body)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert alice.get("/ui/api/playbooks/get", params={"name": "flow"}).json()["stages"][0]["approval"] == "none"
    assert "ui:playbook_write" in sys.modules["authz"].AUDIT_FILE.read_text(encoding="utf-8")


def test_unknown_playbooks_are_404_and_foreign_ones_are_protected(ui):
    alice, bob = ui("alice", "user"), ui("bob", "user")
    alice.post("/ui/api/playbooks", json=BODY)
    for params in ({"name": "ghost"}, {}):
        assert alice.get("/ui/api/playbooks/get", params=params).status_code == 404
    assert bob.post("/ui/api/playbooks", json=dict(BODY, title="mine now")).status_code == 400
    refused = bob.post("/ui/api/playbooks/delete", json={"name": "flow"})
    assert refused.status_code == 400 and "shared playbook" in _msg(refused)
    for name in ("ghost", ""):
        assert bob.post("/ui/api/playbooks/delete", json={"name": name}).status_code == 404
    done = alice.post("/ui/api/playbooks/delete", json={"name": "flow"})
    assert (done.status_code, done.json()["ok"]) == (200, True)
    assert alice.get("/ui/api/playbooks").json() == {"playbooks": []}


def test_example_install_reports_unknown_existing_and_forced(ui):
    boss = ui("boss", "admin")
    assert boss.post("/ui/api/playbooks/example", json={"name": "ghost"}).status_code == 400
    assert boss.post("/ui/api/playbooks/example", json={}).status_code == 200
    again = boss.post("/ui/api/playbooks/example", json={"force": "yes"})      # only a real true forces
    assert again.status_code == 409 and "exists" in _msg(again)
    forced = boss.post("/ui/api/playbooks/example", json={"force": True})
    assert (forced.status_code, forced.json()["ok"]) == (200, True)
    assert ui("alice", "user").post("/ui/api/playbooks/example", json={}).status_code == 403


# ── runs ─────────────────────────────────────────────────────────────────────
def test_starting_a_run_reports_unknown_playbooks_and_the_run_limit(ui, monkeypatch):
    alice = ui("alice", "user")
    ghost = alice.post("/ui/api/playbooks/runs/start", json={"playbook": "ghost", "project": "x"})
    assert ghost.status_code == 404 and ghost.json()["ok"] is False
    monkeypatch.setattr(sys.modules["playbooks"], "MAX_ACTIVE_RUNS", 1)
    _start(alice)
    full = alice.post("/ui/api/playbooks/runs/start", json={"playbook": "flow", "project": "x"})
    assert full.status_code == 400 and _msg(full).startswith("Refused: 1 active runs already")


def test_runs_are_listed_per_person_with_filters_and_404_for_strangers(ui):
    alice, bob, boss = ui("alice", "user"), ui("bob", "user"), ui("boss", "admin")
    rid = _start(alice, project="Garden")
    (row,) = alice.get("/ui/api/playbooks/runs").json()["runs"]
    assert row["id"] == rid and row["next_step"].startswith("stage 1/2 Plan") and row["stages"][0]["approval"] == "webui"
    assert alice.get("/ui/api/playbooks/runs", params={"project": "roof"}).json()["runs"] == []
    assert bob.get("/ui/api/playbooks/runs", params={"status": "all"}).json()["runs"] == []
    assert len(boss.get("/ui/api/playbooks/runs").json()["runs"]) == 1
    got = alice.get("/ui/api/playbooks/runs/get", params={"id": rid}).json()
    assert got["run"]["id"] == rid and got["next_step"].startswith("stage 1/2")
    for who, params in ((alice, {"id": "deadbeef"}), (alice, {}), (bob, {"id": rid})):
        assert who.get("/ui/api/playbooks/runs/get", params=params).status_code == 404


def test_submit_and_decide_walk_a_run_and_report_every_refusal(ui):
    alice, bob, boss = ui("alice", "user"), ui("bob", "user"), ui("boss", "admin")
    rid = _start(alice)
    submit, decide = "/ui/api/playbooks/runs/submit", "/ui/api/playbooks/runs/decide"
    assert alice.post(submit, json={"id": rid}).status_code == 400
    assert alice.post(submit, json={"id": "deadbeef", "artifact": "x"}).status_code == 404
    assert bob.post(submit, json={"id": rid, "artifact": "hijack"}).status_code == 404
    assert alice.post(decide, json={"id": rid, "decision": "approve"}).status_code == 400    # nothing waits
    assert alice.post(submit, json={"id": rid, "artifact": "# Intent", "note": "n"}).json()["ok"] is True
    assert alice.post(decide, json={"id": rid, "decision": "maybe"}).status_code == 400
    assert alice.post(decide, json={"id": "deadbeef", "decision": "approve"}).status_code == 404
    assert bob.post(decide, json={"id": rid, "decision": "approve"}).status_code == 404
    rejected = alice.post(decide, json={"id": rid, "decision": "reject", "note": "more"})
    assert rejected.json()["message"].endswith("rejected — more; rework and playbook_submit again")
    alice.post(submit, json={"id": rid, "artifact": "better"})
    approved = boss.post(decide, json={"id": rid, "decision": "approve"})                     # an admin may
    assert approved.status_code == 200 and approved.json()["message"].startswith("stage 2/2 Build")
    log = sys.modules["authz"].AUDIT_FILE.read_text(encoding="utf-8")
    assert f"run={rid} decision=approve" in log
    stage = json.loads(sys.modules["playbooks"]._run_path(rid).read_text())["stages"][0]
    assert stage["decision"]["by"] == "boss" and stage["decision"]["via"] == "webui"


def test_cancelling_reports_unknown_foreign_and_repeated_requests(ui):
    alice, bob = ui("alice", "user"), ui("bob", "user")
    rid = _start(alice)
    cancel = "/ui/api/playbooks/runs/cancel"
    assert bob.post(cancel, json={"id": rid}).status_code == 404
    assert alice.post(cancel, json={"id": "deadbeef"}).status_code == 404
    assert alice.post(cancel, json={"id": rid}).json() == {"ok": True, "message": f"Cancelled run {rid}."}
    again = alice.post(cancel, json={"id": rid})
    assert again.status_code == 400 and _msg(again) == "Refused: the run is cancelled."
    assert alice.get("/ui/api/playbooks/runs", params={"status": "cancelled"}).json()["runs"][0]["id"] == rid
