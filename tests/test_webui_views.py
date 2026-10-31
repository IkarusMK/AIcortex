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
        cookie = webui._sign({"sub": sub, "name": sub, "role": role, "csrf": "tok",
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
