"""Review of the upstream sign-in access control (2.7.0): the gaps it found, pinned.

1. The app id of a connection without sign-in was "local" — a name a real app could
   carry too (an OAuth client whose redirect host is ``local``, e.g. registered through
   a wildcard in MCP_REDIRECT_URIS) and so inherit that grant. The marker is now
   "(local)", which no host name can be; stored settings with the old value are read
   as the new one, and no app is ever recognised by the host ``local``.
2. A scheduled job runs as its owner, but an admin may write a job for someone else
   (cron_add owner=<person>). Such a job used the person's runner grant — and with it
   their upstream sign-in — for a prompt the person never wrote. A runner now uses a
   sign-in only for a job its owner wrote themselves; anything else is refused and
   audited.
3. Approving a call in the WebUI ran it without asking the person's CURRENT policy:
   their role, the MCP servers an admin assigned them, the server's read_only flag and
   their own access settings could all have changed since the call was held. Now the
   decision re-checks all of that first; a refused approval is not run and stays
   waiting.
4. The ``mcp_access`` tool (show or narrow the settings) had no channel check: any
   caller acting as the person — an app they never allowed, an unknown one — could read
   who may use their sign-in, or narrow it to nothing. It now needs the same permission
   as using the sign-in (an allowed channel, or the person in the WebUI).
5. One allowed app could fill all of a person's waiting approvals (20) and crowd out
   every other channel. Each channel may now ask for at most 5 approvals per 10 minutes.
6. An approvals file that couldn't be read (another STORAGE_ENCRYPTION_KEY, damage) was
   taken as empty and overwritten by the next held call — every kept answer gone. Now
   nothing is written while it can't be read; every use says so.
7. (the first-come window after an unreadable audit log: test_sec_operators_restore.py)
8. An approval shows arguments the AI (or an upstream answer it copied) chose.
   Direction overrides and invisible characters (U+202E, U+2066, U+200B, U+FEFF …)
   could make the text the person approves read differently from the call that runs.
   They are shown as visible escapes (\\u202e); the arguments stay the exact call.

All upstream data is invented (the fake provider of test_mcp_oauth).
"""
import asyncio
import json
import sys
import time

import httpx
import pytest
from cryptography.fernet import Fernet

from test_mcp_access import (OWNER, TOOLS, access, app, approval_id, as_token,  # noqa: F401
                             audit_lines, decide, mcp_client, owner_is_admin, pending,
                             stored_access, text_of, up)
from test_mcp_oauth import SERVER, add, call, direct_sign_in, env, prov  # noqa: F401

MARKER = "(local)"
READ = {"server": SERVER, "tool": "read_status"}


def _store_access(person, settings):
    gateway, store = sys.modules["mcp_gateway"], sys.modules["store"]
    path = gateway._cfg_path(SERVER)
    store.write_json(path, {**store.read_json(path, {}), "access": {person: settings}})


# ── 1. the marker of a connection without sign-in ────────────────────────────
def test_no_app_is_recognised_by_a_host_like_the_marker():
    import pocketid_proxy as pp
    for uri in ("https://local/cb", "http://LOCAL:8080/cb", "https://(local)/cb",
                "https://a b.example/cb"):
        assert pp.app_of([uri]) == "", uri
    assert pp.app_of(["https://local.example/cb"]) == "local.example"
    assert pp.app_of(["https://app.example/cb"]) == "app.example"


def test_an_app_with_the_host_local_does_not_get_the_grant_of_a_connection_without_sign_in(up):
    access(up, allow_apps=[sys.modules["mcp_access"].LOCAL], approve_writes=False)
    assert call("mcp_call", READ) == "ok read_status"         # the connection without sign-in
    with as_token(app("local", "Some app")):
        out = call("mcp_call", READ)
    assert out.startswith("Refused") and "local" in out, out
    assert up.prov.calls == ["read_status"]
    shown = up.ui.get(f"/ui/api/mcp-oauth/access?server={SERVER}").json()
    assert shown["settings"]["allow_apps"] == [MARKER]
    assert MARKER in [a["id"] for a in shown["choices"]["apps"]]


def test_settings_stored_with_the_old_marker_are_migrated(up):
    _store_access(OWNER, {"allow_apps": ["local", "app.example"], "approve_writes": False})
    assert call("mcp_call", READ) == "ok read_status"         # still allowed, under the new id
    with as_token(app("local")):
        assert call("mcp_call", READ).startswith("Refused")
    shown = up.ui.get(f"/ui/api/mcp-oauth/access?server={SERVER}").json()["settings"]
    assert shown["allow_apps"] == [MARKER, "app.example"]
    # the tool narrows with the new marker, and the stored settings carry it from then on
    out = call("mcp_access", {"server": SERVER, "allow_apps": [MARKER]})
    assert not out.startswith("Refused"), out
    stored = sys.modules["mcp_gateway"]._load(SERVER)["access"][OWNER]
    assert stored["allow_apps"] == [MARKER]


# ── 2. a runner uses the sign-in only for a job its owner wrote ──────────────
RUNNER = "r_t3st01"


def _job(owner, by, jid="job-1"):
    """A scheduled job of `owner` whose text `by` wrote (cron_add's updated_by)."""
    sys.modules["cron"]._write([{
        "id": jid, "name": jid, "schedule": "* * * * *", "prompt": "Read the status.",
        "notify": "user", "enabled": True, "owner": owner, "runner": "", "updated_by": by,
        "once": False, "created": "2026-11-01T08:00:00", "last_run": ""}])


def _as_job(base, jid="job-1", owner=OWNER, tool="mcp_call", args=None):
    """Call `tool` the way runner RUNNER does while it carries out job `jid` of `owner`."""
    actas, agent_auth = __import__("actas"), __import__("agent_auth")

    async def run():
        async with mcp_client(base, agent_auth.internal_token(RUNNER)) as c:
            began = text_of(await c.call_tool("act_as_begin", {
                "act_as_token": actas.issue(jid, owner), "job_id": jid}))
            assert began.startswith("Acting as"), began
            try:
                return text_of(await c.call_tool(tool, args or READ, raise_on_error=False))
            finally:
                await c.call_tool("act_as_end", {}, raise_on_error=False)
    return asyncio.run(run())


def test_a_job_someone_else_wrote_does_not_use_the_owners_sign_in(up):
    owner_is_admin()
    access(up, allow_runners=[RUNNER], approve_writes=False)
    _job(OWNER, "boss")                                   # an admin wrote it for the person
    out = _as_job(up.base)
    assert out.startswith("Refused") and "boss" in out and "job-1" in out, out
    assert up.prov.calls == []
    denied = [a for a in audit_lines()
              if a["decision"] == "deny" and f"runner:{RUNNER}" in a["reason"]]
    assert denied and "boss" in denied[-1]["reason"] and denied[-1]["identity"] == OWNER
    _job(OWNER, "")                                       # a job from before 2.3: author unknown
    assert _as_job(up.base).startswith("Refused")
    assert up.prov.calls == []
    _job(OWNER, OWNER)                                    # the person's own job: it may
    assert _as_job(up.base) == "ok read_status"


def test_a_runner_without_a_scheduled_job_behind_it_gets_nothing(up):
    owner_is_admin()
    access(up, allow_runners=[RUNNER], approve_writes=False)
    _job(OWNER, OWNER, jid="another-job")
    out = _as_job(up.base, jid="no-such-job")             # a token for a job that isn't there
    assert out.startswith("Refused"), out
    assert up.prov.calls == []


# ── 3. a decision re-checks the person's current policy ──────────────────────
def _held(e, host="claude.ai"):
    access(e, allow_apps=[host])
    with as_token(app(host)):
        return approval_id(call("mcp_call", {"server": SERVER, "tool": "set_value",
                                             "args": {"v": 7}}))


def _refused_and_still_waiting(e, aid, r, ui=None):
    assert r.status_code == 403 and r.json()["ok"] is False, r.text
    assert e.prov.calls == []
    assert aid in [a["id"] for a in pending(e, ui)]


def test_an_approval_does_not_run_once_the_server_is_read_only(up):
    aid = _held(up)
    add(up.prov.base, read_only=True)                     # switched on meanwhile
    r = decide(up, aid, True)
    _refused_and_still_waiting(up, aid, r)
    assert "read-only" in r.json()["message"]
    assert decide(up, aid, False).json()["ok"] is True    # rejecting it needs nothing
    assert pending(up) == [] and up.prov.calls == []


def test_an_approval_does_not_run_once_its_channel_is_no_longer_allowed(up):
    aid = _held(up)
    access(up, allow_apps=["other.example"])              # the app that asked was removed
    r = decide(up, aid, True)
    _refused_and_still_waiting(up, aid, r)
    assert "claude.ai" in r.json()["message"]
    access(up, allow_apps=["claude.ai"])                  # allowed again: now it runs
    assert decide(up, aid, True).json()["ok"] is True
    assert up.prov.calls == ["set_value"] and up.prov.call_args == [{"v": 7}]


@pytest.mark.oidc
def test_an_approval_runs_only_while_the_person_may_still_use_the_server(monkeypatch, request,
                                                                         prov, fake_idp):
    for var, value in (("OIDC_CONFIG_URL", f"{fake_idp}/.well-known/openid-configuration"),
                       ("OIDC_CLIENT_ID", "aicortex"), ("OIDC_CLIENT_SECRET", "upstream-secret"),
                       ("JWT_SIGNING_KEY", "0" * 64), ("AUTH_ENFORCE", "1"),
                       ("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode()),
                       ("INTERNAL_ALLOW_CIDRS", "127.0.0.0/8")):
        monkeypatch.setenv(var, value)
    base = request.getfixturevalue("http_server")
    webui, authz = sys.modules["webui"], sys.modules["authz"]
    add(prov.base)
    prov.tools = [dict(t) for t in TOOLS]

    def policy(**alice):
        authz.POLICY_FILE.parent.mkdir(parents=True, exist_ok=True)
        authz.POLICY_FILE.write_text(json.dumps({
            "operators": ["boss"], "roles": {"alice": alice.pop("role", "user")},
            "users": {"alice": alice}}), encoding="utf-8")
    policy(mcp=[SERVER])
    direct_sign_in("alice")
    _store_access("alice", {"allow_apps": ["app.example"]})
    aid = sys.modules["mcp_approvals"].create("alice", SERVER, "set_value", {"v": 1},
                                              "app:app.example")
    tok = webui._sign({"sub": "alice", "name": "alice", "role": "user", "csrf": "tok",
                       "claim_role": "", "exp": time.time() + 600})
    ui = httpx.Client(base_url=base, cookies={webui.SESSION_COOKIE: tok},
                      headers={"X-CSRF": "tok"})
    e = type("E", (), {"prov": prov, "ui": ui})
    for change in ({"mcp": []}, {"mcp": [SERVER], "role": "viewer"}):
        policy(**change)                                  # no longer hers / a viewer now
        r = ui.post("/ui/api/mcp-oauth/approvals/decide", json={"id": aid, "approve": True})
        _refused_and_still_waiting(e, aid, r)
    assert any(a["identity"] == "alice" and a["decision"] == "deny" and a["tool"] == "mcp_call"
               for a in audit_lines())
    policy(mcp=[SERVER])                                  # assigned again: it runs
    r = ui.post("/ui/api/mcp-oauth/approvals/decide", json={"id": aid, "approve": True})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    assert prov.calls == ["set_value"]
    ui.close()


# ── 4. the settings tool needs the same permission as the sign-in ────────────
def test_the_settings_tool_needs_an_allowed_channel(up):
    access(up, allow_apps=["claude.ai"], allow_keys=["abc123"])
    for claims in (app("chatgpt.com"), {"sub": OWNER}):  # an app not allowed / an unknown one
        with as_token(claims):
            shown = call("mcp_access", {"server": SERVER})
            narrowed = call("mcp_access", {"server": SERVER, "allow_apps": []})
        assert shown.startswith("Refused") and "abc123" not in shown, shown
        assert narrowed.startswith("Refused"), narrowed
    out = call("mcp_access", {"server": SERVER})          # a connection without sign-in: no
    assert out.startswith("Refused") and "abc123" not in out
    assert stored_access()["allow_apps"] == ["claude.ai"]
    assert any(a["tool"] == "mcp_access" and a["decision"] == "deny" for a in audit_lines())
    with as_token(app("claude.ai")):                      # the allowed app may
        assert "abc123" in call("mcp_access", {"server": SERVER})
    out = call("mcp_access", {"server": SERVER, "allow_keys": []}, who=OWNER)   # and the WebUI
    assert not out.startswith("Refused"), out
    assert stored_access()["allow_keys"] == []


# ── 5. one channel can't fill every approval slot ────────────────────────────
def test_one_channel_asks_for_at_most_five_approvals_in_ten_minutes(up, monkeypatch):
    approvals = sys.modules["mcp_approvals"]
    access(up, allow_apps=["claude.ai", "app.example"])
    write = {"server": SERVER, "tool": "set_value"}
    with as_token(app("claude.ai")):
        first = [approval_id(call("mcp_call", write)) for _ in range(5)]
        out = call("mcp_call", write)
    assert out.startswith("Refused") and "app:claude.ai" in out and "10 minutes" in out, out
    assert decide(up, first[0], False).json()["ok"] is True   # deciding frees no new request
    with as_token(app("claude.ai")):
        assert call("mcp_call", write).startswith("Refused")
    with as_token(app("app.example")):                    # another channel still can
        approval_id(call("mcp_call", write))
    assert len(pending(up)) == 5
    real = approvals._clock
    monkeypatch.setattr(approvals, "_clock", lambda: real() + 601)
    with as_token(app("claude.ai")):                      # ten minutes later: again
        approval_id(call("mcp_call", write))
    assert up.prov.calls == []


# ── 6. an unreadable approvals file is never overwritten ─────────────────────
def test_an_unreadable_approvals_file_is_left_alone(up):
    approvals = sys.modules["mcp_approvals"]
    aid = _held(up)
    path = approvals._file(OWNER)
    other_key = Fernet(Fernet.generate_key()).encrypt(b"[]")       # as after a key change
    path.write_bytes(other_key)
    with as_token(app("claude.ai")):
        held = call("mcp_call", {"server": SERVER, "tool": "set_value"})
        fetched = call("mcp_call", {"server": SERVER, "tool": "set_value", "approval_id": aid})
    for out in (held, fetched):
        assert out.startswith("Refused") and "can't be read" in out, out
    r = decide(up, aid, True)
    assert r.status_code == 409 and "can't be read" in r.json()["message"], r.text
    listed = up.ui.get("/ui/api/mcp-oauth/approvals").json()
    assert listed["approvals"] == [] and "can't be read" in listed["message"]
    assert path.read_bytes() == other_key                          # never rewritten
    assert up.prov.calls == []


# ── 8. what an approval shows can't be visually reordered ───────────────────
HIDDEN = ("\u202a", "\u202e", "\u2066", "\u2067", "\u2069", "\u200b", "\u200e", "\u200f",
          "\ufeff", "\u2028", "\U000e0041")


def test_hidden_and_direction_characters_are_shown_as_escapes(up):
    access(up, allow_apps=["claude.ai"])
    text = "report\u202etxt.exe " + "".join(HIDDEN)
    with as_token(app("claude.ai")):
        approval_id(call("mcp_call", {"server": SERVER, "tool": "set_value\u2066",
                                      "args": {"note\u202e": text, "n": 1}}))
    row = pending(up)[0]
    shown = "".join(str(row[k]) for k in ("tool", "args", "server", "requested_by"))
    assert not [c for c in HIDDEN if c in shown]
    assert row["tool"] == "set_value\\u2066" and "report\\u202etxt.exe" in row["args"]
    assert "\\udb40\\udc41" in row["args"]                     # beyond U+FFFF: a JSON pair
    assert json.loads(row["args"]) == {"note\u202e": text, "n": 1}   # still exactly the call
    assert "\n" in row["args"]                                   # the layout stays readable
