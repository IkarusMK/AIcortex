"""Who may use a person's sign-in to an OAuth MCP server, and approvals for writes.

Before: a sign-in belonged to one person, but EVERY call acting as that person used it
— their interactive apps, every agent key of theirs with mcp_call in scope, and any
cron job of theirs on any runner (act-as), a third-party model runner included. Nothing
asked the person.

Now each person decides per server, in the WebUI (Services → Access), which of their
channels may use their sign-in: apps (identified by the redirect host of the app's
registered OAuth client, never by what it says about itself), runners (by id), agent
keys (by id). Defaults are strict: nothing but the person in the WebUI. A tool call
can only take permissions away, never add them. With approve_writes (default on) a
tool the server does not mark readOnlyHint does not run on an AI's call: it waits as
an approval the person grants or rejects in the WebUI; AICortex then runs exactly that
call once with the person's sign-in, and the AI fetches the result once.

All upstream data here is invented (a fake provider from test_mcp_oauth).
"""
import asyncio
import contextlib
import json
import sys

import httpx
import pytest

from test_mcp_oauth import SERVER, call, env, prov, web_sign_in  # noqa: F401

OWNER = "local-operator"           # the person of the open local WebUI
TOOLS = [
    {"name": "read_status", "description": "Read something.",
     "annotations": {"readOnlyHint": True}},
    {"name": "set_value", "description": "Change something.",
     "annotations": {"readOnlyHint": False}},
    {"name": "unmarked", "description": "No annotations at all."},
]


# ── helpers ──────────────────────────────────────────────────────────────────
@pytest.fixture
def up(env):
    """Signed in (as the person in the WebUI) to a server with neutral tools, and the
    access settings as an upgraded server has them: none stored (the strict defaults)."""
    gateway, store = sys.modules["mcp_gateway"], sys.modules["store"]
    path = gateway._cfg_path(SERVER)
    store.write_json(path, {k: v for k, v in store.read_json(path, {}).items() if k != "access"})
    env.prov.tools = [dict(t) for t in TOOLS]
    web_sign_in(env)
    return env


def access(e, ui=None, **settings):
    r = (ui or e.ui).post("/ui/api/mcp-oauth/access", json={"server": SERVER, **settings})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    return r.json()


def stored_access(person=OWNER):
    cfg = sys.modules["mcp_gateway"]._load(SERVER)
    return ((cfg or {}).get("access") or {}).get(person)


@contextlib.contextmanager
def as_token(claims, client_id="proxy-client"):
    """Run in-process calls as if an OAuth access token had come with them."""
    from fastmcp.server.auth.auth import AccessToken
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    tok = AccessToken(token="t-" + client_id, client_id=client_id, scopes=[], claims=claims)
    reset = auth_context_var.set(AuthenticatedUser(tok))
    try:
        yield
    finally:
        auth_context_var.reset(reset)


def app(host, name="An app"):
    return {"sub": OWNER, "aicortex_app": {"host": host, "name": name}}


def audit_lines():
    path = sys.modules["authz"].AUDIT_FILE
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] \
        if path.exists() else []


def mcp_client(base, token):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    return Client(StreamableHttpTransport(f"{base}/mcp",
                                          headers={"Authorization": f"Bearer {token}"}))


def text_of(res):
    return "\n".join(getattr(c, "text", "") for c in res.content)


def approval_id(answer):
    import re
    m = re.search(r"approval_id='([A-Za-z0-9_-]+)'", answer)
    assert m, answer
    return m.group(1)


def pending(e, ui=None):
    r = (ui or e.ui).get("/ui/api/mcp-oauth/approvals")
    assert r.status_code == 200, r.text
    return r.json()["approvals"]


def decide(e, aid, approve, ui=None):
    return (ui or e.ui).post("/ui/api/mcp-oauth/approvals/decide",
                             json={"id": aid, "approve": approve})


# ── who may use the sign-in ──────────────────────────────────────────────────
def test_by_default_only_the_person_in_the_webui_uses_the_sign_in(up):
    for tool, args in (("mcp_call", {"server": SERVER, "tool": "read_status"}),
                       ("mcp_tools", {"server": SERVER})):
        out = call(tool, args)                       # an unauthenticated local connection
        assert out.startswith("Refused") and "Access" in out, out
    with as_token(app("claude.ai")):
        assert call("mcp_call", {"server": SERVER, "tool": "read_status"}).startswith("Refused")
    assert up.prov.bearer_tokens() == [] and up.prov.calls == []
    assert stored_access() is None                   # nothing stored: the strict defaults
    # the person in the WebUI always may — and a write runs directly for them
    assert call("mcp_call", {"server": SERVER, "tool": "read_status"}, who=OWNER) == "ok read_status"
    assert call("mcp_call", {"server": SERVER, "tool": "set_value"}, who=OWNER) == "ok set_value"


def test_an_allowed_app_uses_it_and_no_other(up):
    access(up, allow_apps=["claude.ai"], approve_writes=False)
    with as_token(app("claude.ai")):
        assert call("mcp_call", {"server": SERVER, "tool": "read_status"}) == "ok read_status"
        assert "read_status" in call("mcp_tools", {"server": SERVER})
    with as_token(app("chatgpt.com")):
        out = call("mcp_call", {"server": SERVER, "tool": "read_status"})
        assert out.startswith("Refused") and "chatgpt.com" in out
    with as_token({"sub": OWNER}):                    # an OAuth token without a known app
        assert call("mcp_call", {"server": SERVER, "tool": "read_status"}).startswith("Refused")
    assert up.prov.calls == ["read_status"]
    seen = [(a["identity"], a["decision"], a["reason"]) for a in audit_lines()
            if a["tool"] in ("mcp_call", "mcp_tools")]
    assert any(d == "allow" and "app:claude.ai" in r for _, d, r in seen)
    assert any(d == "deny" and "app:chatgpt.com" in r for _, d, r in seen)
    assert all(i == OWNER for i, _, _ in seen)


def owner_is_admin():
    """A key and an act-as job run with the person's explicit role (else "user", which
    needs an endpoint grant first) — the person here is an admin."""
    import pathlib
    pathlib.Path(sys.modules["authz"].POLICY_FILE).write_text(json.dumps({"roles": {OWNER: "admin"}}))


def test_an_agent_key_needs_to_be_allowed(up):
    owner_is_admin()
    apikeys = sys.modules["apikeys"]
    key, rec = apikeys.create(OWNER, name="helper", scopes="all")
    rest = httpx.Client(base_url=up.base, headers={"Authorization": f"Bearer {key}"})
    args = {"server": SERVER, "tool": "read_status"}
    r = rest.post("/api/v1/tools/mcp_call", json=args)
    assert r.status_code == 200 and "Refused" in json.dumps(r.json()), r.text

    async def over_mcp():
        async with mcp_client(up.base, key) as c:
            return text_of(await c.call_tool("mcp_call", args, raise_on_error=False))
    assert asyncio.run(over_mcp()).startswith("Refused")
    assert up.prov.calls == []
    access(up, allow_keys=[rec["keyid"]])
    assert "ok read_status" in json.dumps(rest.post("/api/v1/tools/mcp_call", json=args).json())
    assert asyncio.run(over_mcp()) == "ok read_status"
    assert any(f"key:{rec['keyid']}" in a["reason"] for a in audit_lines())
    rest.close()


def test_a_runner_job_uses_it_only_when_that_runner_is_allowed(up):
    owner_is_admin()
    actas, agent_auth = __import__("actas"), __import__("agent_auth")
    rid = "r_t3st01"
    args = {"server": SERVER, "tool": "read_status"}
    sys.modules["cron"]._write([{"id": "job-1", "name": "job-1", "schedule": "* * * * *",
                                 "prompt": "Read the status.", "notify": "user",
                                 "enabled": True, "owner": OWNER, "runner": "",
                                 "updated_by": OWNER, "created": "2026-11-01T08:00:00",
                                 "last_run": ""}])        # the person's own job

    async def job_call():
        async with mcp_client(up.base, agent_auth.internal_token(rid)) as c:
            cap = actas.issue("job-1", OWNER)
            began = text_of(await c.call_tool("act_as_begin", {"act_as_token": cap, "job_id": "job-1"}))
            assert began.startswith("Acting as"), began
            try:
                return text_of(await c.call_tool("mcp_call", args, raise_on_error=False))
            finally:
                await c.call_tool("act_as_end", {}, raise_on_error=False)
    out = asyncio.run(job_call())                    # the job's owner signed in — still no
    assert out.startswith("Refused") and rid in out
    access(up, allow_runners=["r_other1"])
    assert asyncio.run(job_call()).startswith("Refused")
    assert up.prov.calls == []
    access(up, allow_runners=[rid])
    assert asyncio.run(job_call()) == "ok read_status"
    assert any(f"runner:{rid}" in a["reason"] and a["decision"] == "allow" for a in audit_lines())


def test_widening_needs_the_person_in_the_webui_tightening_does_not(up):
    out = call("mcp_access", {"server": SERVER, "allow_apps": ["claude.ai"]})
    assert out.startswith("Refused") and "WebUI" in out
    assert stored_access() is None
    # the calls below come through the connection without sign-in: it must be allowed to
    # use the sign-in at all (test_sec_mcp_access_review)
    local = sys.modules["mcp_access"].LOCAL
    access(up, allow_apps=[local, "claude.ai", "chatgpt.com"], allow_keys=["abc123"],
           approve_writes=False)
    for widen in ({"allow_apps": [local, "claude.ai", "chatgpt.com", "other.example"]},
                  {"allow_runners": ["r_any001"]}):
        assert call("mcp_access", {"server": SERVER, **widen}).startswith("Refused"), widen
    assert stored_access()["allow_runners"] == [] and len(stored_access()["allow_apps"]) == 3
    out = call("mcp_access", {"server": SERVER, "allow_apps": [local, "claude.ai"],
                              "approve_writes": True})
    assert not out.startswith("Refused"), out
    now = stored_access()
    assert now["allow_apps"] == [local, "claude.ai"] and now["approve_writes"] is True
    assert now["allow_keys"] == ["abc123"]
    assert call("mcp_access", {"server": SERVER, "approve_writes": False}).startswith("Refused")
    assert stored_access()["approve_writes"] is True
    shown = call("mcp_access", {"server": SERVER})            # no change: shows the settings
    assert "claude.ai" in shown and "abc123" in shown
    no_csrf = httpx.Client(base_url=up.base)
    assert no_csrf.post("/ui/api/mcp-oauth/access",
                        json={"server": SERVER, "allow_apps": ["x.example"]}).status_code == 403
    no_csrf.close()
    assert stored_access()["allow_apps"] == [local, "claude.ai"]


def test_settings_are_checked_and_belong_to_the_person(up):
    r = up.ui.post("/ui/api/mcp-oauth/access", json={"server": SERVER, "allow_apps": ["<b>"]})
    assert r.status_code == 400 and r.json()["ok"] is False
    r = up.ui.post("/ui/api/mcp-oauth/access", json={"server": SERVER, "allow_keys": "all"})
    assert r.status_code == 400
    access(up, allow_apps=["claude.ai"])
    assert stored_access("someone-else") is None
    d = up.ui.get(f"/ui/api/mcp-oauth/access?server={SERVER}").json()
    assert d["settings"]["allow_apps"] == ["claude.ai"] and d["settings"]["approve_writes"] is True
    assert {"apps", "runners", "keys"} <= set(d["choices"])
    with as_token(app("chatgpt.com", "Another app")):
        call("mcp_call", {"server": SERVER, "tool": "read_status"})       # refused, but seen
    d = up.ui.get(f"/ui/api/mcp-oauth/access?server={SERVER}").json()
    assert "chatgpt.com" in [a["id"] for a in d["choices"]["apps"]]


# ── approvals for tools that change something ────────────────────────────────
def test_a_write_waits_for_approval_and_does_not_run(up):
    access(up, allow_apps=["claude.ai"])
    with as_token(app("claude.ai", "Claude")):
        assert call("mcp_call", {"server": SERVER, "tool": "read_status"}) == "ok read_status"
        held = call("mcp_call", {"server": SERVER, "tool": "set_value", "args": {"v": 7}})
        unmarked = call("mcp_call", {"server": SERVER, "tool": "unmarked"})
    assert "approv" in held.lower() and "WebUI" in held
    approval_id(held), approval_id(unmarked)
    assert up.prov.calls == ["read_status"]
    rows = pending(up)
    assert [r["tool"] for r in rows] == ["set_value", "unmarked"]
    first = rows[0]
    assert first["server"] == SERVER and json.loads(first["args"]) == {"v": 7}
    assert "claude.ai" in first["requested_by"] and first["expires"] > first["created"]


def test_an_approved_call_runs_once_and_its_result_is_fetched_once(up):
    access(up, allow_apps=["claude.ai"])
    with as_token(app("claude.ai")):
        aid = approval_id(call("mcp_call", {"server": SERVER, "tool": "set_value",
                                            "args": {"v": 7}}))
        out = call("mcp_call", {"server": SERVER, "tool": "set_value", "approval_id": aid})
        assert "waiting" in out.lower()
    r = decide(up, aid, True)
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    assert up.prov.calls == ["set_value"] and up.prov.call_args == [{"v": 7}]
    assert decide(up, aid, True).json()["ok"] is False             # no replay
    assert up.prov.calls == ["set_value"]
    assert pending(up) == []
    with as_token(app("claude.ai")):
        fetch = {"server": SERVER, "tool": "set_value", "approval_id": aid}
        assert call("mcp_call", fetch) == "ok set_value"
        assert "already" in call("mcp_call", fetch).lower()
    with as_token(app("chatgpt.com")):                     # a channel without access: nothing
        assert call("mcp_call", fetch).startswith("Refused")
    assert up.prov.calls == ["set_value"]


def test_a_rejected_or_expired_approval_never_runs(up, monkeypatch):
    access(up, allow_apps=["claude.ai"])
    with as_token(app("claude.ai")):
        rejected = approval_id(call("mcp_call", {"server": SERVER, "tool": "set_value"}))
        late = approval_id(call("mcp_call", {"server": SERVER, "tool": "set_value"}))
    assert decide(up, rejected, False).json()["ok"] is True
    assert decide(up, rejected, True).json()["ok"] is False
    approvals = sys.modules["mcp_approvals"]
    real = approvals._clock
    monkeypatch.setattr(approvals, "_clock", lambda: real() + approvals.TTL + 5)
    assert decide(up, late, True).json()["ok"] is False
    assert pending(up) == []
    with as_token(app("claude.ai")):
        assert "rejected" in call("mcp_call", {"server": SERVER, "tool": "set_value",
                                               "approval_id": rejected}).lower()
        assert "expired" in call("mcp_call", {"server": SERVER, "tool": "set_value",
                                              "approval_id": late}).lower()
    assert up.prov.calls == []


def test_only_the_person_in_the_webui_can_approve(up):
    access(up, allow_apps=["claude.ai"])
    with as_token(app("claude.ai")):
        aid = approval_id(call("mcp_call", {"server": SERVER, "tool": "set_value"}))
    approvals = sys.modules["mcp_approvals"]
    other = approvals.create("someone-else", SERVER, "set_value", {}, "app:claude.ai")
    assert decide(up, other, True).json()["ok"] is False           # not this person's
    no_csrf = httpx.Client(base_url=up.base)
    assert no_csrf.post("/ui/api/mcp-oauth/approvals/decide",
                        json={"id": aid, "approve": True}).status_code == 403
    no_csrf.close()
    assert "approve" not in " ".join(t for t in asyncio.run(_tool_names()) if t.startswith("mcp_"))
    assert up.prov.calls == []
    assert [r["id"] for r in pending(up)] == [aid]


async def _tool_names():
    return [t.name for t in await sys.modules["server"].mcp.list_tools()]


def test_waiting_approvals_are_bounded(up, monkeypatch):
    approvals = sys.modules["mcp_approvals"]
    monkeypatch.setattr(approvals, "MAX_PENDING", 2)
    access(up, allow_apps=["claude.ai"])
    with as_token(app("claude.ai")):
        for _ in range(2):
            approval_id(call("mcp_call", {"server": SERVER, "tool": "set_value"}))
        out = call("mcp_call", {"server": SERVER, "tool": "set_value"})
    assert out.startswith("Refused") and "waiting" in out
    assert len(pending(up)) == 2


def test_without_approvals_an_allowed_channel_writes_directly(up):
    access(up, allow_apps=["claude.ai"], approve_writes=False)
    with as_token(app("claude.ai")):
        assert call("mcp_call", {"server": SERVER, "tool": "set_value"}) == "ok set_value"
    assert pending(up) == []


def test_read_only_still_wins_over_an_approval(up):
    from test_mcp_oauth import add
    add(up.prov.base, read_only=True)
    access(up, allow_apps=["claude.ai"])
    with as_token(app("claude.ai")):
        out = call("mcp_call", {"server": SERVER, "tool": "set_value"})
    assert out.startswith("Refused") and "read-only" in out
    assert pending(up) == []


# ── how an app is recognised ─────────────────────────────────────────────────
def test_the_app_is_the_redirect_host_of_its_registered_client():
    import pocketid_proxy as pp
    assert pp.app_of(["https://claude.ai/api/mcp/auth_callback"]) == "claude.ai"
    assert pp.app_of(["https://chatgpt.com/connector_platform_oauth_redirect"]) == "chatgpt.com"
    for loop in ("http://127.0.0.1:33418/callback", "http://localhost:6274/cb", "http://[::1]:9/x"):
        assert pp.app_of([loop]) == "localhost"
    assert pp.app_of([]) == "" and pp.app_of(["not a url"]) == ""
    assert pp.app_of(["https://a.example/cb", "https://b.example/cb"]) == ""   # ambiguous


def test_the_proxy_marks_the_app_on_every_token_and_never_trusts_a_forged_mark():
    import pocketid_proxy as pp
    from fastmcp.server.auth.auth import AccessToken

    class Client:
        redirect_uris = ["https://claude.ai/api/mcp/auth_callback"]
        client_name = "Claude"

    class Proxy:
        class jwt_issuer:
            @staticmethod
            def verify_token(token):
                return {"client_id": "c-1" if token == "known" else "c-2"}

        async def get_client(self, client_id):
            return Client() if client_id == "c-1" else None

    def tok(claims):
        return AccessToken(token="x", client_id="upstream", scopes=[], claims=claims)
    found = asyncio.run(pp.annotate(Proxy(), "known", tok({"sub": "p"})))
    assert found.claims[pp.APP_CLAIM] == {"host": "claude.ai", "name": "Claude"}
    forged = tok({"sub": "p", pp.APP_CLAIM: {"host": "claude.ai"}})
    assert pp.APP_CLAIM not in asyncio.run(pp.annotate(Proxy(), "other", forged)).claims


def _metadata_client(doc: dict):
    """The client FastMCP builds for an app that signs in with a client metadata
    document (its client_id is the document's URL) — made by FastMCP's own code, so
    the test follows FastMCP: it keeps the redirect addresses on the document and
    leaves the client's own redirect_uris empty."""
    from fastmcp.server.auth.cimd import CIMDClientManager, CIMDDocument

    class Fetcher:
        async def fetch(self, url):
            return CIMDDocument.model_validate(doc)
    manager = CIMDClientManager(enable_cimd=True)
    manager._fetcher = Fetcher()
    return asyncio.run(manager.get_client(doc["client_id"]))


def test_an_app_signing_in_with_a_metadata_document_is_recognised():
    """Up to 2.7.1 an app that signs in with a client metadata document — claude.ai
    does — got no mark: AICortex read only the client's own redirect_uris, which
    FastMCP leaves empty for such a client. claude.ai counted as "a caller AICortex
    cannot identify", never showed up under Access and could not be allowed."""
    import pocketid_proxy as pp
    from fastmcp.server.auth.auth import AccessToken

    url = "https://claude.ai/oauth/mcp-oauth-client-metadata"
    client = _metadata_client({"client_id": url, "client_name": "Claude",
                               "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                               "token_endpoint_auth_method": "none"})
    assert client is not None and not client.redirect_uris     # how FastMCP builds it

    class Proxy:
        class jwt_issuer:
            @staticmethod
            def verify_token(token):
                return {"client_id": url}

        async def get_client(self, client_id):
            return client if client_id == url else None

    pp._apps.clear()
    tok = AccessToken(token="x", client_id="upstream", scopes=[], claims={"sub": "p"})
    found = asyncio.run(pp.annotate(Proxy(), "t", tok))
    assert found.claims[pp.APP_CLAIM] == {"host": "claude.ai", "name": "Claude"}


def test_a_metadata_document_with_two_hosts_is_no_app():
    """The same rule as for a registered client: one host or none."""
    import pocketid_proxy as pp
    client = _metadata_client({"client_id": "https://app.example/meta", "client_name": "X",
                               "redirect_uris": ["https://a.example/cb", "https://b.example/cb"],
                               "token_endpoint_auth_method": "none"})
    assert pp.redirect_uris_of(client) == ["https://a.example/cb", "https://b.example/cb"]
    assert pp.app_of(pp.redirect_uris_of(client)) == ""
    assert pp.redirect_uris_of(None) == [] and pp.redirect_uris_of(object()) == []


# ── the WebUI ────────────────────────────────────────────────────────────────
def test_access_and_approvals_are_wired_into_the_webui(up):
    js = up.ui.get("/ui/assets/view-mcp-access.js")
    assert js.status_code == 200
    for needle in ("/ui/api/mcp-oauth/access", "/ui/api/mcp-oauth/approvals",
                   "/ui/api/mcp-oauth/approvals/decide", "openDialog", "Freigaben", "Approvals",
                   "Zugriff", "Access"):
        assert needle in js.text, needle
    assert "innerHTML" not in js.text and ".style." not in js.text
    html = up.ui.get("/ui").text
    assert "/ui/assets/view-mcp-access.js" in html
    start = html.index('<dialog id="oa-access"')
    assert "style=" not in html[start:html.index("</dialog>", start)]
    assert "mcpAccessButton" in up.ui.get("/ui/assets/view-registry.js").text
