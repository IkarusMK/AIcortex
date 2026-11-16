"""Inbox forwarding (app/inbox_forward.py): a message for a person also reaches them.

A message posted to the inbox for "user" (a cron job's report, a webhook event) was
stored and nothing else — nobody sees the inbox. A forwarding rule sends each message
for a recipient through a REGISTERED service (e.g. a self-hosted ntfy) with a JSON
body template. Proves: the message is filled into the template literally (no second
substitution, long bodies cut), the title falls back to the first line, the token comes
from the vault and only goes to its bound host, the network policy still applies, a
failing delivery never breaks posting and is logged, another person's message is not
forwarded to the rule owner, and only an admin may create, change or test rules.
No real network: httpx.request is replaced.
"""
import asyncio
import contextlib
import json
import sys
import threading
from types import SimpleNamespace

import pytest
from fastmcp import Client


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, fn):
        self.tools[fn.__name__] = fn
        return fn


class FakeResponse:
    def __init__(self, status_code=200, text="{}"):
        self.status_code, self.text = status_code, text


NTFY_BODY = {"topic": "aicortex", "title": "{title}", "message": "{body}",
             "tags": ["{from}"], "markdown": True}


@pytest.fixture
def fw(monkeypatch, fresh_server):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    monkeypatch.setenv("OPERATORS", "carol")          # the rule owner is an admin
    monkeypatch.setenv("PUSH_TOKEN", "tk_test")
    sys.modules["secrets_store"].vault_set_hosts("PUSH_TOKEN", "", ["push.example.com"], by="test")
    services = sys.modules["services"]
    services.SERVICES_DIR.mkdir(parents=True, exist_ok=True)
    (services.SERVICES_DIR / "push.json").write_text(json.dumps({
        "name": "push", "base_url": "https://push.example.com", "token_env": "PUSH_TOKEN",
        "category": "Haushalt"}), encoding="utf-8")
    mod = sys.modules["inbox_forward"]
    monkeypatch.setattr(mod, "ASYNC", False)
    sent = []

    def fake_request(method, url, **kw):
        sent.append(dict(kw, method=method, url=url))
        resp = wire["response"]
        if isinstance(resp, Exception):
            raise resp
        return resp
    wire = {"response": FakeResponse()}
    monkeypatch.setattr(services.httpx, "request", fake_request)
    monkeypatch.setattr(services.netguard, "check_url", lambda url: (True, "ok"))
    monkeypatch.setattr(services.netguard, "guard", lambda host: contextlib.nullcontext())
    fake = FakeMCP()
    mod.register(fake)
    return SimpleNamespace(mod=mod, coord=sys.modules["coordination"], t=fake.tools,
                           sent=sent, wire=wire, authz=sys.modules["authz"])


def _as(monkeypatch, person, role_admin=True):
    claims = {} if role_admin else {"aicortex_key": f"k-{person}", "key_scopes": ["all"]}
    monkeypatch.setattr(sys.modules["authz"], "_identity", lambda: (person, False, claims))


def _approve(fw, recipient="user", owner="carol"):
    """What the person does in the WebUI (Inbox tab): switch the rule on."""
    with fw.authz.rest_identity(owner, "admin"):
        return fw.mod.approve(recipient)


def _rule(fw, recipient="user", body=None, **kw):
    text = fw.t["inbox_forward_set"](recipient=recipient, service="push",
                                     body=NTFY_BODY if body is None else body, **kw)
    _approve(fw, recipient)
    return text


# ── delivery ─────────────────────────────────────────────────────────────────
def test_a_message_for_a_forwarded_recipient_is_posted_through_the_service(fw, monkeypatch):
    _as(monkeypatch, "carol")
    assert "Forwarding" in _rule(fw)
    mid = fw.coord.post_inbox("user", "Disk usage 81 %", subject="Weekly report", sender="cron-runner")
    assert len(fw.sent) == 1
    req = fw.sent[0]
    assert (req["method"], req["url"]) == ("POST", "https://push.example.com/")
    assert req["headers"] == {"Authorization": "Bearer tk_test"}
    assert req["json"] == {"topic": "aicortex", "title": "Weekly report", "message": "Disk usage 81 %",
                           "tags": ["cron-runner"], "markdown": True}
    stored = json.loads(fw.coord.INBOX_FILE.read_text(encoding="utf-8"))
    assert [m["id"] for m in stored] == [mid]


def test_the_same_rule_shape_serves_gotify(fw, monkeypatch):
    """Not tied to ntfy: Gotify takes POST /message with its key in X-Gotify-Key."""
    _as(monkeypatch, "carol")
    services = sys.modules["services"]
    monkeypatch.setenv("GOTIFY_APP_TOKEN", "A1b2C3")
    sys.modules["secrets_store"].vault_set_hosts("GOTIFY_APP_TOKEN", "", ["gotify.example.com"], by="test")
    (services.SERVICES_DIR / "gotify.json").write_text(json.dumps({
        "name": "gotify", "base_url": "https://gotify.example.com", "token_env": "GOTIFY_APP_TOKEN",
        "auth_header": "X-Gotify-Key", "auth_scheme": "", "category": "Haushalt"}), encoding="utf-8")
    body = {"title": "{title}", "message": "{body}", "priority": 5,
            "extras": {"client::display": {"contentType": "text/markdown"}}}
    fw.t["inbox_forward_set"](recipient="user", service="gotify", path="/message", body=body)
    _approve(fw)
    fw.coord.post_inbox("user", "**fertig**", subject="Review")
    req = fw.sent[0]
    assert (req["url"], req["headers"]) == ("https://gotify.example.com/message", {"X-Gotify-Key": "A1b2C3"})
    assert req["json"] == {"title": "Review", "message": "**fertig**", "priority": 5,
                           "extras": {"client::display": {"contentType": "text/markdown"}}}


def test_without_a_rule_nothing_is_sent(fw, monkeypatch):
    _as(monkeypatch, "carol")
    _rule(fw, recipient="user")
    fw.coord.post_inbox("planner-bot", "a note for an agent")
    assert fw.sent == []


def test_values_are_filled_in_literally_and_long_bodies_are_cut(fw, monkeypatch):
    _as(monkeypatch, "carol")
    _rule(fw, body={"t": "{title}", "m": "{body}", "n": 3, "list": ["x {to} y", {"id": "{id}"}]})
    body = "{subject} {from} {x}" + "a" * 5000
    mid = fw.coord.post_inbox("user", body, subject="{body}", sender="{to}")
    sent = fw.sent[0]["json"]
    assert sent["t"] == "{body}"                              # the subject, not a second fill
    assert sent["m"].startswith("{subject} {from} {x}aaa")    # braces in the body stay as they are
    assert len(sent["m"]) <= fw.mod.MAX_BODY + 1 and sent["m"].endswith("…")
    assert sent["n"] == 3 and sent["list"] == ["x user y", {"id": mid}]


def test_the_title_falls_back_to_the_first_line_of_the_body(fw, monkeypatch):
    _as(monkeypatch, "carol")
    _rule(fw)
    fw.coord.post_inbox("user", "\n\nBackup check 03:00\nSummary: …")
    fw.coord.post_inbox("user", "   ")
    titles = [r["json"]["title"] for r in fw.sent]
    assert titles == ["Backup check 03:00", "AICortex"]


def test_a_failing_delivery_never_breaks_posting_and_is_logged(fw, monkeypatch):
    _as(monkeypatch, "carol")
    _rule(fw)
    fw.wire["response"] = ConnectionError("refused")
    mid = fw.coord.post_inbox("user", "report")
    assert mid
    fw.wire["response"] = FakeResponse(500, "echo: Authorization: Bearer tk_test")
    fw.coord.post_inbox("user", "report 2")
    log = fw.mod.recent()
    assert [e["ok"] for e in log[-2:]] == [False, False]
    assert log[-1]["info"] == "HTTP 500" and log[-2]["id"] == mid
    assert "tk_test" not in json.dumps(log)


def test_the_network_policy_still_applies(fw, monkeypatch):
    _as(monkeypatch, "carol")
    _rule(fw)
    services = sys.modules["services"]
    monkeypatch.setattr(services.netguard, "check_url", lambda url: (False, "internal address"))
    fw.coord.post_inbox("user", "report")
    assert fw.sent == []
    assert "Blocked by network policy" in fw.mod.recent()[-1]["info"]


def test_the_token_only_goes_to_its_bound_host(fw, monkeypatch):
    _as(monkeypatch, "carol")
    services = sys.modules["services"]
    (services.SERVICES_DIR / "other.json").write_text(json.dumps({
        "name": "other", "base_url": "https://evil.example.net", "token_env": "PUSH_TOKEN",
        "category": "Haushalt"}), encoding="utf-8")
    fw.t["inbox_forward_set"](recipient="user", service="other", body=NTFY_BODY)
    _approve(fw)
    fw.coord.post_inbox("user", "report")
    assert fw.sent == []
    assert fw.mod.recent()[-1]["ok"] is False


def test_delivery_does_not_gain_webui_rights(fw, monkeypatch):
    _as(monkeypatch, "carol")
    _rule(fw)
    seen = []
    monkeypatch.setattr(sys.modules["services"], "post_json",
                        lambda *a, **k: (seen.append(fw.authz.from_webui()), (True, "HTTP 200"))[1])
    fw.coord.post_inbox("user", "report")
    assert seen == [False]


def test_another_persons_message_is_not_forwarded_to_the_rule_owner(fw, monkeypatch):
    _as(monkeypatch, "carol")
    _rule(fw)
    _as(monkeypatch, "bob", role_admin=False)
    fw.coord.post_inbox("user", "bob's private note")
    assert fw.sent == []
    monkeypatch.setattr(fw.coord, "_person", lambda: "")     # unowned, e.g. a webhook event
    fw.coord.post_inbox("user", "doorbell", subject="Ring", sender="webhook:door")
    assert [r["json"]["message"] for r in fw.sent] == ["doorbell"]
    assert fw.sent[0]["json"]["title"] == "[external · webhook:door] Ring"   # not the owner's own


def test_delivery_runs_in_the_background_by_default(fw, monkeypatch):
    _as(monkeypatch, "carol")
    _rule(fw)
    monkeypatch.setattr(fw.mod, "ASYNC", True)
    started = threading.Event()
    release = threading.Event()

    def slow(*a, **k):
        started.set()
        release.wait(5)
        return True, "HTTP 200"
    monkeypatch.setattr(sys.modules["services"], "post_json", slow)
    fw.coord.post_inbox("user", "report")          # returns while the delivery still waits
    assert started.wait(5)
    release.set()
    assert fw.mod.drain(5)
    assert fw.mod.recent()[-1]["ok"] is True


# ── rules ────────────────────────────────────────────────────────────────────
def test_set_list_update_and_delete_a_rule(fw, monkeypatch):
    _as(monkeypatch, "carol")
    assert "Forwarding" in _rule(fw)
    _rule(fw, path="/v1/publish")                               # same recipient: replaced
    rules = fw.mod.rules()
    assert len(rules) == 1 and rules[0]["path"] == "/v1/publish" and rules[0]["owner"] == "carol"
    listing = fw.t["inbox_forward_list"]()
    assert "user → push" in listing and "/v1/publish" in listing
    assert "Deleted" in fw.t["inbox_forward_delete"](recipient="user")
    assert fw.mod.rules() == []
    assert "No forwarding" in fw.t["inbox_forward_delete"](recipient="user")


@pytest.mark.parametrize("kw, word", [
    ({"service": "nope"}, "Unknown service"),
    ({"recipient": ""}, "recipient"),
    ({"recipient": "x" * 200}, "recipient"),
    ({"body": "just text"}, "body"),
    ({"body": {"m": "x" * 5000}}, "body"),
    ({"path": "https://evil.example.net/"}, "path"),
])
def test_invalid_rules_are_refused(fw, monkeypatch, kw, word):
    _as(monkeypatch, "carol")
    args = {"recipient": "user", "service": "push", "body": NTFY_BODY} | kw
    text = fw.t["inbox_forward_set"](**args)
    assert text.startswith("Refused") and word in text
    assert fw.mod.rules() == []


def test_a_disabled_rule_sends_nothing(fw, monkeypatch):
    _as(monkeypatch, "carol")
    _rule(fw, enabled=False)
    fw.coord.post_inbox("user", "report")
    assert fw.sent == []


def test_the_test_tool_sends_a_sample_and_reports_the_answer(fw, monkeypatch):
    _as(monkeypatch, "carol")
    assert fw.t["inbox_forward_test"](recipient="user").startswith("No forwarding")
    _rule(fw)
    text = fw.t["inbox_forward_test"](recipient="user")
    assert text.startswith("Sent") and "HTTP 200" in text
    assert fw.sent[-1]["json"]["title"] == "AICortex – test"
    stored = fw.coord.INBOX_FILE
    assert not stored.exists() or json.loads(stored.read_text(encoding="utf-8")) == []


def test_a_corrupt_rules_file_sends_nothing_and_is_not_overwritten(fw, monkeypatch):
    _as(monkeypatch, "carol")
    fw.mod.RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    fw.mod.RULES_FILE.write_text("{broken", encoding="utf-8")
    assert fw.coord.post_inbox("user", "report")
    assert fw.sent == []
    assert _rule(fw).startswith("Refused")
    assert fw.mod.RULES_FILE.read_text(encoding="utf-8") == "{broken"


# ── who may do what (through the real MCP gate) ──────────────────────────────
def _call(server, tool, args=None):
    async def go():
        async with Client(server.mcp) as client:
            res = await client.call_tool(tool, args or {}, raise_on_error=False)
            return (res.content[0].text if res.content else ""), res.is_error
    return asyncio.run(go())


def test_only_an_admin_may_change_or_test_rules(fw, monkeypatch, fresh_server):
    authz = fw.authz
    assert {"inbox_forward_set", "inbox_forward_delete", "inbox_forward_test"} <= authz.ADMIN_TOOLS
    assert "inbox_forward_list" in authz.READ_ONLY_TOOLS
    _as(monkeypatch, "bob", role_admin=False)
    text, _ = _call(fresh_server, "inbox_forward_set",
                    {"recipient": "user", "service": "push", "body": NTFY_BODY})
    assert fw.mod.rules() == [] and "admin" in text.lower()


# ── after the security review ────────────────────────────────────────────────
def test_a_rule_set_by_an_ai_waits_for_the_persons_approval(fw, monkeypatch):
    """A prompt-injected admin session must not open a standing channel out: a rule
    written over MCP is pending until the person switches it on in the WebUI."""
    _as(monkeypatch, "carol")
    text = fw.t["inbox_forward_set"](recipient="user", service="push", body=NTFY_BODY)
    assert "approve" in text.lower() and "push.example.com" in text
    fw.coord.post_inbox("user", "report")
    assert fw.sent == []
    assert "waiting for approval" in fw.t["inbox_forward_list"]()
    assert fw.mod.approve("user").startswith("Refused")          # not from the WebUI
    assert "on" in _approve(fw).lower()
    fw.coord.post_inbox("user", "report 2")
    assert [r["json"]["message"] for r in fw.sent] == ["report 2"]
    # a change of where or what is sent needs the approval again …
    fw.t["inbox_forward_set"](recipient="user", service="push", body=NTFY_BODY, path="/other")
    fw.coord.post_inbox("user", "report 3")
    assert len(fw.sent) == 1
    # … switching it off and on again does not
    _approve(fw)
    fw.t["inbox_forward_set"](recipient="user", service="push", body=NTFY_BODY, path="/other", enabled=False)
    fw.t["inbox_forward_set"](recipient="user", service="push", body=NTFY_BODY, path="/other", enabled=True)
    fw.coord.post_inbox("user", "report 4")
    assert [r["json"]["message"] for r in fw.sent][-1] == "report 4"


def test_a_flood_is_rate_limited_per_rule(fw, monkeypatch):
    _as(monkeypatch, "carol")
    _rule(fw)
    monkeypatch.setattr(fw.mod, "RATE_MAX", 5)
    for i in range(8):
        fw.coord.post_inbox("user", f"event {i}")
    assert len(fw.sent) == 5
    suppressed = [e for e in fw.mod.recent() if not e["ok"]]
    assert len(suppressed) == 1 and "rate limit" in suppressed[0]["info"]   # one note per window


def test_the_background_queue_is_bounded(fw, monkeypatch):
    _as(monkeypatch, "carol")
    _rule(fw)
    monkeypatch.setattr(fw.mod, "ASYNC", True)
    monkeypatch.setattr(fw.mod, "MAX_PENDING", 2)
    release = threading.Event()
    monkeypatch.setattr(sys.modules["services"], "post_json",
                        lambda *a, **k: (release.wait(5), (True, "HTTP 200"))[1])
    for i in range(5):
        fw.coord.post_inbox("user", f"event {i}")
    release.set()
    assert fw.mod.drain(5)
    infos = [e["info"] for e in fw.mod.recent()]
    assert infos.count("HTTP 200") == 2 and sum("queue full" in i for i in infos) == 1


def test_a_rule_of_an_owner_who_is_no_longer_admin_sends_nothing(fw, monkeypatch):
    _as(monkeypatch, "carol")
    _rule(fw)
    monkeypatch.setattr(fw.authz, "session_role", lambda *a, **k: "user")   # demoted
    fw.coord.post_inbox("user", "report")
    assert fw.sent == []
    assert "no longer an admin" in fw.mod.recent()[-1]["info"]


def test_a_rule_needs_a_known_owner_when_access_is_enforced(fw, monkeypatch):
    monkeypatch.setattr(fw.authz, "effective_identity", lambda: ("unknown", "admin"))
    text = fw.t["inbox_forward_set"](recipient="user", service="push", body=NTFY_BODY)
    assert text.startswith("Refused") and "who" in text
    assert fw.mod.rules() == []


def test_the_list_shows_others_only_their_own_rules_and_no_templates(fw, monkeypatch):
    _as(monkeypatch, "carol")
    _rule(fw)
    assert '"topic"' in fw.t["inbox_forward_list"]()                 # the owner (admin) sees it all
    _as(monkeypatch, "bob", role_admin=False)
    assert fw.t["inbox_forward_list"]().startswith("No forwarding")


# ── second review round ──────────────────────────────────────────────────────
def test_re_pointing_the_service_after_approval_needs_a_new_approval(fw, monkeypatch):
    """The approval covers WHERE the messages go, not just the service's name."""
    _as(monkeypatch, "carol")
    _rule(fw)
    services = sys.modules["services"]
    cfg = json.loads((services.SERVICES_DIR / "push.json").read_text(encoding="utf-8"))
    for change in ({"base_url": "https://evil.example.net"}, {"tls_insecure": True}):
        (services.SERVICES_DIR / "push.json").write_text(json.dumps(cfg | change), encoding="utf-8")
        fw.coord.post_inbox("user", "secret report")
        assert fw.sent == []
        assert "changed" in fw.mod.recent()[-1]["info"]
        assert fw.mod.rules()[0]["approved"] is False
        (services.SERVICES_DIR / "push.json").write_text(json.dumps(cfg), encoding="utf-8")
        _approve(fw)
    fw.coord.post_inbox("user", "report")
    assert [r["json"]["message"] for r in fw.sent] == ["report"]


def test_a_rule_needs_an_owner_who_is_admin_in_aicortex_itself(fw, monkeypatch):
    _as(monkeypatch, "carol")
    monkeypatch.setattr(fw.authz, "session_role", lambda *a, **k: "user")   # admin only by IdP claim
    text = fw.t["inbox_forward_set"](recipient="user", service="push", body=NTFY_BODY)
    assert text.startswith("Refused") and "admin" in text
    assert fw.mod.rules() == []


def test_forwarding_stays_off_when_the_rules_are_reachable_through_the_file_tools(fw, monkeypatch):
    _as(monkeypatch, "carol")
    _rule(fw)
    monkeypatch.setattr(fw.mod, "WORK_DIR", fw.coord.COORD_DIR.parent)
    fw.coord.post_inbox("user", "report")
    assert fw.sent == []
