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

All upstream data is invented (the fake provider of test_mcp_oauth).
"""
import asyncio
import sys

from test_mcp_access import (OWNER, access, app, as_token, audit_lines, mcp_client,  # noqa: F401
                             owner_is_admin, text_of, up)
from test_mcp_oauth import SERVER, call, env, prov  # noqa: F401

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
