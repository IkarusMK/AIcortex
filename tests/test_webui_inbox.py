"""The WebUI shows a person their inbox (tab "Postfach" / "Inbox").

Before: cron jobs and webhooks reported "to your inbox", but the inbox was a file on
the server that no screen showed. Now a signed-in person sees the messages for "user"
and "all" that are theirs (or unowned), newest first, can mark them read, and sees
whether their messages are forwarded and how the latest deliveries went. Another
person's messages and rules stay hidden; marking read needs the CSRF header.
"""
import json
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
        return httpx.Client(base_url=base, cookies={webui.SESSION_COOKIE: cookie}, headers=headers)
    return client


def _post(monkeypatch, person, to, body, subject=""):
    coord = sys.modules["coordination"]
    monkeypatch.setattr(coord, "_person", lambda: person)
    return coord.post_inbox(to, body, subject, "cron-runner")


def test_a_person_sees_their_own_messages_newest_first(ui, monkeypatch):
    first = _post(monkeypatch, "alice", "user", "Report 03:00", "Backup")
    second = _post(monkeypatch, "", "all", "Webhook: Tür")
    _post(monkeypatch, "bob", "user", "bob's report")
    _post(monkeypatch, "alice", "planner-bot", "an agent's note")
    data = ui("alice", "user").get("/ui/api/inbox").json()
    assert [m["id"] for m in data["messages"]] == [second, first]
    assert data["messages"][1]["subject"] == "Backup"
    assert data["unread"] == 2


def test_marking_read_needs_csrf_and_only_works_on_own_messages(ui, monkeypatch):
    mine = _post(monkeypatch, "alice", "user", "report")
    bobs = _post(monkeypatch, "bob", "user", "bob's report")
    alice = ui("alice", "user")
    assert ui("alice", "user", csrf=False).post("/ui/api/inbox/ack", json={"id": mine}).status_code == 403
    assert alice.post("/ui/api/inbox/ack", json={"id": mine}).status_code == 200
    assert alice.post("/ui/api/inbox/ack", json={"id": bobs}).status_code == 404
    rows = alice.get("/ui/api/inbox").json()["messages"]
    assert [m["read"] for m in rows] == [True]
    stored = json.loads(sys.modules["coordination"].INBOX_FILE.read_text(encoding="utf-8"))
    assert [m["read"] for m in stored if m["id"] == bobs] == [False]


def test_the_forwarding_state_is_shown_to_its_owner_only(ui, monkeypatch):
    fw = sys.modules["inbox_forward"]
    fw.RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    rule = {"recipient": "user", "service": "ntfy", "path": "/", "body": {"topic": "t"},
            "enabled": True, "owner": "alice", "role": "admin", "updated": "2026-11-16T20:00:00+00:00"}
    fw.RULES_FILE.write_text(json.dumps([rule]), encoding="utf-8")
    fw._note("abc", rule, False, "HTTP 403: forbidden")
    alice = ui("alice", "user").get("/ui/api/inbox").json()
    assert [(r["recipient"], r["service"], r["enabled"]) for r in alice["forwarding"]] == [("user", "ntfy", True)]
    assert "body" not in alice["forwarding"][0]
    assert [(d["ok"], d["info"]) for d in alice["deliveries"]] == [(False, "HTTP 403: forbidden")]
    bob = ui("bob", "user").get("/ui/api/inbox").json()
    assert bob["forwarding"] == [] and bob["deliveries"] == []


def test_the_inbox_needs_a_session(ui):
    r = httpx.get(ui("x", "user").base_url.join("/ui/api/inbox"))
    assert r.status_code in (401, 403)


def _pending_rule(owner="alice"):
    services = sys.modules["services"]
    services.SERVICES_DIR.mkdir(parents=True, exist_ok=True)
    (services.SERVICES_DIR / "ntfy.json").write_text(json.dumps({
        "name": "ntfy", "base_url": "https://push.example.com", "category": "Haushalt"}), encoding="utf-8")
    fw = sys.modules["inbox_forward"]
    fw.RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    rule = {"recipient": "user", "service": "ntfy", "path": "/", "body": {"topic": "t"},
            "enabled": True, "approved": False, "owner": owner, "role": "admin",
            "updated": "2026-11-16T20:00:00+00:00"}
    fw.RULES_FILE.write_text(json.dumps([rule]), encoding="utf-8")
    return fw


def test_the_person_approves_a_pending_rule_in_the_webui(ui):
    fw = _pending_rule()
    alice = ui("alice", "admin")
    shown = alice.get("/ui/api/inbox").json()["forwarding"]
    assert [(r["approved"], r["host"]) for r in shown] == [(False, "push.example.com")]
    assert ui("alice", "admin", csrf=False).post(
        "/ui/api/inbox/forward/approve", json={"recipient": "user"}).status_code == 403
    assert ui("bob", "user").post(
        "/ui/api/inbox/forward/approve", json={"recipient": "user"}).status_code == 403
    r = alice.post("/ui/api/inbox/forward/approve", json={"recipient": "user"})
    assert r.status_code == 200, r.text
    assert [x["approved"] for x in fw.rules()] == [True]
    assert alice.post("/ui/api/inbox/forward/approve", json={"recipient": "nobody"}).status_code == 404


def test_a_corrupt_inbox_is_reported_not_a_server_error(ui):
    coord = sys.modules["coordination"]
    coord.INBOX_FILE.parent.mkdir(parents=True, exist_ok=True)
    coord.INBOX_FILE.write_text("{broken", encoding="utf-8")
    alice = ui("alice", "user")
    assert alice.get("/ui/api/inbox").status_code == 409
    assert alice.post("/ui/api/inbox/ack", json={"id": "x"}).status_code == 409
    assert coord.INBOX_FILE.read_text(encoding="utf-8") == "{broken"


def test_every_asset_the_page_loads_is_served():
    """A new view script has to be in the WebUI's asset allow-list, or the page loads
    without it (the Inbox tab first showed its raw label "nav_inbox")."""
    import re
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
    import webui
    html = (Path(webui.__file__).parent / "webui_static" / "index.html").read_text(encoding="utf-8")
    wanted = set(re.findall(r'/ui/assets/([\w.-]+)\?', html))
    assert "view-inbox.js" in wanted
    assert wanted <= set(webui._ASSETS), sorted(wanted - set(webui._ASSETS))
