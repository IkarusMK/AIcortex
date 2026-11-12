"""Review of the upstream sign-in access control (2.7.0): the gaps it found, pinned.

1. The app id of a connection without sign-in was "local" — a name a real app could
   carry too (an OAuth client whose redirect host is ``local``, e.g. registered through
   a wildcard in MCP_REDIRECT_URIS) and so inherit that grant. The marker is now
   "(local)", which no host name can be; stored settings with the old value are read
   as the new one, and no app is ever recognised by the host ``local``.

All upstream data is invented (the fake provider of test_mcp_oauth).
"""
import sys

from test_mcp_access import OWNER, access, app, as_token, up  # noqa: F401
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
