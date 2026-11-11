"""WebUI sessions: only a real sign-in makes one, it follows the policy, it can be ended.

Regression for the audit finding "cookie confusion": every signed WebUI cookie was
made with one HMAC key and carried no type, so the login-state cookie that
``GET /ui/login`` hands to ANYONE (state + PKCE verifier) was accepted as a session
cookie. With it an anonymous caller read, wrote and deleted shared notes and
scheduled ownerless cron jobs (the empty CSRF header matched the missing token).

Proves, over real HTTP:
- a login-state or MCP-sign-in cookie never passes as a session (cookie type);
- a session needs a person, a valid role and a CSRF token;
- the role is recomputed from the CURRENT policy on every request (a demotion
  takes effect at once, not after the 12-hour session);
- "sign out everywhere" (admin, CSRF-checked) ends every session server-side;
- logout of a live session is CSRF/Origin-checked;
- ``/ui/api/me`` tells an anonymous caller nothing about the running version.
"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

pytestmark = pytest.mark.oidc       # keep the WebUI's sign-in mode

STATIC = Path(__file__).resolve().parent.parent / "app" / "webui_static"
NOTE = {"title": "note", "content": "written without login", "type": "reference",
        "scope": "shared"}


@pytest.fixture
def ui(monkeypatch, request, fake_idp):
    monkeypatch.setenv("OIDC_CONFIG_URL", f"{fake_idp}/.well-known/openid-configuration")
    monkeypatch.setenv("OIDC_CLIENT_ID", "aicortex")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "upstream-secret")
    monkeypatch.setenv("JWT_SIGNING_KEY", "0" * 64)
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    base = request.getfixturevalue("http_server")
    clients = []

    def client(cookie, csrf="tok"):
        c = httpx.Client(base_url=base, cookies={sys.modules["webui"].SESSION_COOKIE: cookie},
                         headers={"X-CSRF": csrf} if csrf else {}, timeout=30)
        clients.append(c)
        return c

    yield SimpleNamespace(base=base, webui=sys.modules["webui"], client=client)
    for c in clients:
        c.close()


def _session(ui, sub="alice", role="admin", **extra):
    """A session as the sign-in mints it: the IdP's claim role is recorded with it."""
    return ui.webui._sign({"sub": sub, "name": sub, "role": role, "claim_role": role,
                           "csrf": "tok", "exp": time.time() + 600, **extra})


def _signed_in(c) -> bool:
    return c.get("/ui/api/me").json()["authenticated"] is True


# ── cookie confusion ─────────────────────────────────────────────────────────
def test_the_login_state_cookie_is_not_a_session(ui):
    r = httpx.get(f"{ui.base}/ui/login", follow_redirects=False)
    assert r.status_code == 302
    forged = ui.client(r.cookies[ui.webui.OAUTH_COOKIE], csrf="")
    assert not _signed_in(forged)
    assert forged.get("/ui/api/memory").status_code == 401
    assert forged.post("/ui/api/memory", json=NOTE).status_code == 401
    assert forged.post("/ui/api/memory/delete", json={"name": "note", "scope": "shared"}).status_code == 401
    assert forged.post("/ui/api/cron", json={"name": "job", "schedule": "0 3 * * *",
                                             "prompt": "say hi"}).status_code == 401
    assert forged.get("/ui/api/cron").status_code == 401


def test_cookies_of_one_kind_never_pass_as_another(ui):
    w = ui.webui
    payload = {"sub": "alice", "name": "alice", "role": "admin", "claim_role": "admin",
               "csrf": "tok", "exp": time.time() + 600}
    for typ in ("login", "mcp_oauth"):
        tok = w._sign(payload, typ=typ)
        assert w._verify(tok) is None                         # not a session …
        assert w._verify(tok, typ=typ)["sub"] == "alice"      # … only what it was made for
        assert not _signed_in(ui.client(tok))
    session = w._sign(payload)
    assert w._verify(session, typ="login") is None and w._verify(session, typ="mcp_oauth") is None
    assert _signed_in(ui.client(session))


@pytest.mark.parametrize("broken", [
    {"csrf": ""}, {"csrf": None}, {"sub": ""}, {"sub": 7}, {"role": "root"}, {"role": ""},
])
def test_a_session_needs_a_person_a_role_and_a_csrf_token(ui, broken):
    c = ui.client(_session(ui, **broken), csrf="")      # "" also echoes an empty token
    assert not _signed_in(c)
    assert c.post("/ui/api/memory", json=NOTE).status_code == 401


# ── the role follows the policy ──────────────────────────────────────────────
def test_a_demotion_in_the_policy_takes_effect_at_once(ui):
    tenancy = sys.modules["tenancy"]
    tenancy._write_policy({"roles": {"alice": "admin"}})
    c = ui.client(_session(ui, "alice", "admin", claim_role=""))
    assert c.get("/ui/api/secrets").status_code == 200
    tenancy._write_policy({"roles": {"alice": "viewer"}})
    assert c.get("/ui/api/secrets").status_code == 403
    assert c.get("/ui/api/me").json()["role"] == "viewer"
    assert c.post("/ui/api/memory", json=NOTE).status_code == 403      # viewer: read-only
    tenancy._write_policy({"roles": {"alice": "admin"}})
    assert c.get("/ui/api/secrets").status_code == 200                 # and back


def test_the_idp_claim_recorded_at_sign_in_still_counts_but_policy_wins(ui):
    viewer = ui.client(_session(ui, "carol", "viewer"))                # IdP group: viewer
    assert viewer.get("/ui/api/me").json()["role"] == "viewer"
    sys.modules["tenancy"]._write_policy({"roles": {"carol": "user"}})
    assert viewer.get("/ui/api/me").json()["role"] == "user"


# ── sign out everywhere ──────────────────────────────────────────────────────
def test_sign_out_everywhere_ends_every_session(ui):
    boss = ui.client(_session(ui, "boss", "admin"))
    kim = ui.client(_session(ui, "kim", "user"))
    no_csrf = ui.client(_session(ui, "boss", "admin"), csrf="")
    assert _signed_in(boss) and _signed_in(kim)
    assert kim.post("/ui/api/sessions/revoke").status_code == 403      # admins only
    assert no_csrf.post("/ui/api/sessions/revoke").status_code == 403  # CSRF-checked
    assert _signed_in(kim)                                             # nothing happened yet
    r = boss.post("/ui/api/sessions/revoke")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert "max-age=0" in r.headers.get("set-cookie", "").lower()      # the admin, too
    for c in (boss, kim, no_csrf):
        assert not _signed_in(c)
        assert c.get("/ui/api/memory").status_code == 401
    audit = sys.modules["authz"].AUDIT_FILE.read_text(encoding="utf-8")
    assert "ui:sessions_revoke" in audit
    epoch = ui.webui._session_epoch()
    assert epoch >= 1
    assert _signed_in(ui.client(_session(ui, "boss", "admin", epoch=epoch)))  # a new sign-in works


def test_admins_get_a_sign_out_everywhere_button():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'id="btn-logout-all"' in html
    assert "/ui/api/sessions/revoke" in js
    assert "Überall abmelden" in js and "Sign out everywhere" in js
    assert 'api("/ui/logout"' in js                                     # sends the X-CSRF header


# ── logout, /me ──────────────────────────────────────────────────────────────
def test_logout_of_a_live_session_is_csrf_checked(ui):
    tok = _session(ui)
    r = ui.client(tok, csrf="").post("/ui/logout")
    assert r.status_code == 403 and "set-cookie" not in r.headers
    c = ui.client(tok)
    r = c.post("/ui/logout", headers={"Origin": "https://attacker.example.com"})
    assert r.status_code == 403
    r = c.post("/ui/logout")
    assert r.status_code == 200 and "max-age=0" in r.headers["set-cookie"].lower()
    anon = httpx.post(f"{ui.base}/ui/logout")                           # nothing to protect
    assert anon.status_code == 200


def test_me_without_a_session_does_not_reveal_the_version(ui):
    assert httpx.get(f"{ui.base}/ui/api/me").json() == {"authenticated": False, "oidc": True}
    assert "version" in ui.client(_session(ui)).get("/ui/api/me").json()
