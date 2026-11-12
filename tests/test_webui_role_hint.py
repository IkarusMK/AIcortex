"""What a signed-in person who is not admin is told in the WebUI.

After an update a person can find they are no longer admin; the WebUI then only said
"not enough rights". Now /ui/api/me says whether anyone is in charge (a yes/no, never
who), and the limited view shows the person their OWN identity (the IdP ``sub``), ready
to copy: as ``OPERATORS=<sub>`` for the .env when nobody is in charge yet, or to send
to an admin otherwise. Rendered with text nodes only, no inline styles (strict CSP).

Invented identities only (alice, boss, carol, kim, vera).
"""
import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.secure_default_role, pytest.mark.oidc]

STATIC = Path(__file__).resolve().parent.parent / "app" / "webui_static"


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
    """The WebUI routes of a freshly started server; ``me(sub)`` asks /ui/api/me with a
    session of ``sub``."""
    request.getfixturevalue("fresh_server")
    webui, mcp = sys.modules["webui"], _FakeMCP()
    webui.register(mcp)

    def me(sub):
        cookie = webui._sign({"sub": sub, "name": sub, "role": "user", "claim_role": "",
                              "csrf": "tok", "exp": time.time() + 600})
        req = SimpleNamespace(client=SimpleNamespace(host="192.0.2.10"), headers={},
                              cookies={webui.SESSION_COOKIE: cookie}, query_params={},
                              path_params={}, base_url="http://cortex.example.com/")
        return json.loads(asyncio.run(mcp.routes[("/ui/api/me", "GET")](req)).body)

    return SimpleNamespace(me=me, az=sys.modules["authz"])


def test_me_says_whether_anyone_is_in_charge_and_only_names_the_caller(ui, oidc_env):
    az = ui.az
    body = ui.me("kim")
    assert body["sub"] == "kim" and body["role"] == "user"
    assert body["operators_configured"] is False
    az.POLICY_FILE.write_text(json.dumps({"operators": ["boss"], "roles": {"vera": "viewer"},
                                          "users": {"carol": {}}}), encoding="utf-8")
    body = ui.me("kim")
    assert body["operators_configured"] is True
    text = json.dumps(body)
    assert "boss" not in text and "vera" not in text and "carol" not in text
    assert ui.me("boss")["role"] == "admin"
    az.POLICY_FILE.write_text(json.dumps({"roles": {"boss": "admin"}}), encoding="utf-8")
    assert ui.me("kim")["operators_configured"] is True        # an explicit admin is in charge
    az.POLICY_FILE.write_text(json.dumps({"operators": ["boss"], "roles": {"boss": "user"}}),
                              encoding="utf-8")
    assert ui.me("kim")["operators_configured"] is False       # … a demoted operator is not
    oidc_env.setenv("OPERATORS", "alice")
    assert ui.me("kim")["operators_configured"] is True


def _js_function(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    return src[start:src.index("\n}\n", start) + 2]


def test_the_limited_view_shows_only_the_persons_own_identity():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    start = html.index('id="role-hint"')
    block = html[html.rindex("<", 0, start):html.index("</aside>", start)]
    assert "hidden" in block and "style=" not in block         # strict CSP: no inline styles
    fn = _js_function(js, "renderRoleHint")
    assert "ME.sub" in fn and "ME.operators_configured" in fn
    assert "innerHTML" not in fn and "insertAdjacentHTML" not in fn and ".style" not in fn
    assert "api(" not in fn                                    # nobody else's identity is fetched
    assert "renderRoleHint()" in js
    # DE and EN, both cases
    assert "Noch kein Admin eingerichtet. Trage in die .env {0} ein und starte neu." in js
    assert "Bitte einen Admin, dir Rechte zu geben — deine Kennung: {0}" in js
    assert "No admin has been set up yet. Add {0} to the .env file and restart." in js
    assert "Ask an admin to give you access — your identity: {0}" in js
    assert "`OPERATORS=${ME.sub}`" in fn
