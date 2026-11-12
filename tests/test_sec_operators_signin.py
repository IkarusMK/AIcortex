"""The first-come bootstrap fires only for a person signing in to the WebUI.

Regression for a field report: after an update to 2.6.x an install's owner was no
longer admin. The first-come bootstrap (``role_for``) fired for ANY interactive
identity within the window after a start — MCP calls of a connected app included.
When the IdP doesn't forward the person's ``sub`` (stock OIDCProxy), that identity is
the app's client_id: an app, not the person, became the operator, and an app calling
first after a restart took the slot. Now only /ui/callback (``signin_role``, the IdP
userinfo ``sub``) can become the first operator; /mcp and /api never do.

Invented identities only (dave, erin).
"""
import asyncio
import json
import sys
import time
from http.cookies import SimpleCookie
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.secure_default_role, pytest.mark.oidc]


@pytest.fixture
def oidc_env(monkeypatch, fake_idp):
    monkeypatch.setenv("OIDC_CONFIG_URL", f"{fake_idp}/.well-known/openid-configuration")
    monkeypatch.setenv("OIDC_CLIENT_ID", "aicortex")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "upstream-secret")
    monkeypatch.setenv("JWT_SIGNING_KEY", "0" * 64)
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    monkeypatch.setenv("BASE_URL", "http://cortex.example.com")
    for var in ("OPERATORS", "OPERATOR_BOOTSTRAP_MINUTES", "AUTH_ROLE_CLAIM", "AUDIT_KEEP",
                "AUDIT_MAX_BYTES"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def _policy(az):
    return json.loads(az.POLICY_FILE.read_text(encoding="utf-8"))


class _FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def deco(fn):
            for m in methods:
                self.routes[(path, m)] = fn
            return fn
        return deco


@pytest.fixture
def ui(oidc_env, request):
    """The WebUI routes of a freshly started server, plus a stand-in IdP for
    /ui/callback (token + userinfo) that signs in whoever ``idp.sub`` names."""
    request.getfixturevalue("fresh_server")
    webui, mcp = sys.modules["webui"], _FakeMCP()
    webui.register(mcp)
    import httpx
    idp = SimpleNamespace(sub="dave")

    class _Resp:
        def __init__(self, obj):
            self._obj = obj

        def raise_for_status(self):
            return None

        def json(self):
            return self._obj

    oidc_env.setattr(webui, "_discovery", lambda: {
        "token_endpoint": "https://idp.example.com/token",
        "userinfo_endpoint": "https://idp.example.com/userinfo"})
    oidc_env.setattr(httpx, "post", lambda *a, **k: _Resp({"access_token": "at-1"}))
    oidc_env.setattr(httpx, "get", lambda *a, **k: _Resp({"sub": idp.sub, "name": idp.sub}))

    def _req(cookies, query=None):
        return SimpleNamespace(client=SimpleNamespace(host="192.0.2.10"), headers={},
                               cookies=cookies, query_params=query or {}, path_params={},
                               base_url="http://cortex.example.com/")

    def sign_in(sub):
        """The browser's round trip through /ui/callback; returns the session."""
        idp.sub = sub
        login = webui._sign({"state": "st-1", "verifier": "v" * 43,
                             "exp": time.time() + 600}, typ="login")
        resp = asyncio.run(mcp.routes[("/ui/callback", "GET")](
            _req({webui.OAUTH_COOKIE: login}, {"state": "st-1", "code": "code-1"})))
        jar = SimpleCookie()
        for key, value in resp.raw_headers:
            if key == b"set-cookie":
                jar.load(value.decode())
        assert resp.headers["location"] == "/ui"
        return webui._verify(jar[webui.SESSION_COOKIE].value)

    return SimpleNamespace(sign_in=sign_in, az=sys.modules["authz"])


def _mcp_caller(monkeypatch, client_id, claims):
    """Make the next authz lookup see an OAuth token of a connected app on /mcp."""
    import fastmcp.server.dependencies as deps
    tok = SimpleNamespace(client_id=client_id, original_client_id=client_id,
                          claims=claims, token=f"tok-{client_id}")
    monkeypatch.setattr(deps, "get_access_token", lambda: tok)


def test_an_app_takes_the_free_slot_only_for_the_person_the_idp_names(ui, oidc_env):
    az = ui.az
    assert az.default_role() == "admin"                        # the window is open
    # Stock OIDCProxy: no forwarded sub, the identity is the app's client_id — never.
    _mcp_caller(oidc_env, "app-client-7", {})
    assert az.effective_identity() == ("app-client-7", "user")
    assert not az.POLICY_FILE.exists() or "operators" not in _policy(az)
    assert az.default_role() == "admin"                        # the slot is still free
    # Forwarded sub: the IdP names the person, so the person's own app may take it
    # (an install only ever used through an app would otherwise have no operator).
    _mcp_caller(oidc_env, "app-client-7", {"upstream_claims": {"sub": "dave"}})
    assert az.effective_identity() == ("dave", "admin")
    assert _policy(az)["operators"] == ["dave"]
    assert az.role_for("app-client-7", False) == "user"
    assert ui.sign_in("erin")["role"] == "user"                # only the first one
    assert _policy(az)["operators"] == ["dave"]