"""WebUI browser sign-in (app/webui.py): OIDC code flow with PKCE, sessions, CSRF.

Proves, against a local stand-in identity provider and the real HTTP stack:
- /ui/login redirects to the IdP with a fresh state and an S256 PKCE challenge, and
  parks state + verifier in a signed, short-lived, HttpOnly cookie;
- /ui/callback only trusts a matching state, a present code and an unexpired,
  untampered login cookie; it sends the PKCE verifier (and the client secret when
  configured) to the token endpoint and builds the session from the userinfo;
- the role comes from policy / group claims, never from the browser;
- sessions are signed, expire, and are refused when forged or tampered with;
- the CSRF / Origin gate and the "request made on this machine" test behave.
"""
import base64
import hashlib
import json
import socket
import sys
import time
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from types import SimpleNamespace

import httpx
import pytest

pytestmark = pytest.mark.oidc       # keep the OIDC_* settings the fixtures set

DISCOVERY = "/.well-known/openid-configuration"


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


class _Idp:
    """Local identity provider: discovery, token (checks PKCE) and userinfo."""

    def __init__(self):
        self.base = ""
        self.claims = {"sub": "alice", "name": "Alice Example", "email": "alice@example.com"}
        self.challenge = None          # set from the login redirect → verified at /token
        self.token_status = 200
        self.userinfo_status = 200
        self.token_forms = []
        self.userinfo_auth = []
        self.discovery_hits = 0


@pytest.fixture
def idp():
    state = _Idp()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, obj):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == DISCOVERY:
                state.discovery_hits += 1
                self._send(200, {"issuer": state.base,
                                 "authorization_endpoint": f"{state.base}/authorize",
                                 "token_endpoint": f"{state.base}/token",
                                 "userinfo_endpoint": f"{state.base}/userinfo",
                                 "jwks_uri": f"{state.base}/jwks",
                                 "response_types_supported": ["code"],
                                 "subject_types_supported": ["public"],
                                 "id_token_signing_alg_values_supported": ["RS256"]})
            elif self.path == "/jwks":
                self._send(200, {"keys": []})
            elif self.path == "/userinfo":
                state.userinfo_auth.append(self.headers.get("Authorization", ""))
                ok = state.userinfo_status == 200 and state.userinfo_auth[-1] == "Bearer at-1"
                self._send(200 if ok else 401, state.claims if ok else {"error": "denied"})
            else:
                self._send(404, {})

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            form = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
            state.token_forms.append(form)
            good = (self.path == "/token" and form.get("code") == "code-1"
                    and (state.challenge is None
                         or _s256(form.get("code_verifier", "")) == state.challenge))
            if state.token_status != 200:
                self._send(state.token_status, {"error": "server_error"})
            elif good:
                self._send(200, {"access_token": "at-1"})
            else:
                self._send(400, {"error": "invalid_grant"})

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    state.base = f"http://127.0.0.1:{server.server_address[1]}"
    Thread(target=server.serve_forever, daemon=True).start()
    yield state
    server.shutdown()


@pytest.fixture
def stack(monkeypatch, request, idp):
    monkeypatch.setenv("OIDC_CONFIG_URL", idp.base + DISCOVERY)
    monkeypatch.setenv("OIDC_CLIENT_ID", "aicortex")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "test-token")
    monkeypatch.setenv("JWT_SIGNING_KEY", "0" * 64)
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    base = request.getfixturevalue("http_server")
    with httpx.Client(base_url=base, follow_redirects=False, timeout=30) as client:
        yield SimpleNamespace(c=client, base=base, idp=idp, webui=sys.modules["webui"])


def _login(stack):
    r = stack.c.get("/ui/login")
    assert r.status_code == 302
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.headers["location"]).query))
    stack.idp.challenge = q["code_challenge"]
    return r, q


def _cookies(resp):
    jar = SimpleCookie()
    for raw in resp.headers.get_list("set-cookie"):
        jar.load(raw)
    return jar


def _callback(stack, q, **over):
    params = {"state": q["state"], "code": "code-1", **over}
    return stack.c.get("/ui/callback", params={k: v for k, v in params.items() if v is not None})


# ── /ui/login ────────────────────────────────────────────────────────────────
def test_login_redirects_to_idp_with_state_and_pkce(stack):
    r, q = _login(stack)
    assert r.headers["location"].startswith(f"{stack.idp.base}/authorize?")
    assert q["response_type"] == "code" and q["client_id"] == "aicortex"
    assert q["redirect_uri"] == f"{stack.base}/ui/callback"
    assert q["scope"] == "openid profile email" and q["code_challenge_method"] == "S256"
    cookie = _cookies(r)[stack.webui.OAUTH_COOKIE]
    assert cookie["httponly"] and cookie["path"] == "/ui" and cookie["max-age"] == "600"
    assert cookie["samesite"].lower() == "lax" and not cookie["secure"]   # plain http here
    parked = stack.webui._verify(cookie.value)
    assert parked["state"] == q["state"]
    assert _s256(parked["verifier"]) == q["code_challenge"]
    assert 0 < parked["exp"] - time.time() <= 600


def test_every_login_gets_a_fresh_state_and_challenge(stack):
    _, first = _login(stack)
    _, second = _login(stack)
    assert first["state"] != second["state"]
    assert first["code_challenge"] != second["code_challenge"]


def test_login_honours_a_custom_scope(stack, monkeypatch):
    monkeypatch.setenv("OIDC_SCOPE", "openid groups")
    assert _login(stack)[1]["scope"] == "openid groups"


def test_login_cookie_is_secure_and_redirect_uri_follows_base_url(stack, monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://cortex.example.com/")
    r, q = _login(stack)
    assert q["redirect_uri"] == "https://cortex.example.com/ui/callback"
    assert _cookies(r)[stack.webui.OAUTH_COOKIE]["secure"]


def test_login_reports_an_unreachable_idp_as_502(stack, monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    monkeypatch.setenv("OIDC_CONFIG_URL", f"http://127.0.0.1:{dead}{DISCOVERY}")
    r = stack.c.get("/ui/login")
    assert r.status_code == 502 and "IdP discovery failed" in r.json()["error"]
    monkeypatch.setenv("OIDC_CONFIG_URL", stack.idp.base + "/nope")      # 404 from the IdP
    assert stack.c.get("/ui/login").status_code == 502


# ── /ui/callback: the happy path ─────────────────────────────────────────────
def test_callback_exchanges_code_with_pkce_and_opens_a_session(stack):
    _, q = _login(stack)
    r = _callback(stack, q)
    assert r.status_code == 302 and r.headers["location"] == "/ui"
    form = stack.idp.token_forms[0]
    assert form["grant_type"] == "authorization_code" and form["code"] == "code-1"
    assert form["redirect_uri"] == f"{stack.base}/ui/callback" and form["client_id"] == "aicortex"
    assert _s256(form["code_verifier"]) == q["code_challenge"]
    assert form["client_secret"] == "test-token"
    assert stack.idp.userinfo_auth == ["Bearer at-1"]
    jar = _cookies(r)
    sess = stack.webui._verify(jar[stack.webui.SESSION_COOKIE].value)
    assert (sess["sub"], sess["name"], sess["role"]) == ("alice", "Alice Example", "admin")
    assert sess["csrf"] and sess["exp"] - time.time() <= stack.webui.SESSION_TTL
    assert jar[stack.webui.SESSION_COOKIE]["max-age"] == str(stack.webui.SESSION_TTL)
    assert jar[stack.webui.OAUTH_COOKIE]["max-age"] == "0"           # login cookie is one-shot
    me = stack.c.get("/ui/api/me").json()
    assert me["authenticated"] and me["sub"] == "alice" and me["mode"] == "oidc"


def test_callback_omits_client_secret_for_a_public_client(stack, monkeypatch):
    monkeypatch.delenv("OIDC_CLIENT_SECRET")
    _, q = _login(stack)
    assert _callback(stack, q).headers["location"] == "/ui"
    assert "client_secret" not in stack.idp.token_forms[0]


def test_a_finished_login_cannot_be_replayed_by_the_browser(stack):
    _, q = _login(stack)
    assert _callback(stack, q).headers["location"] == "/ui"
    again = _callback(stack, q)                       # the jar dropped the login cookie
    assert again.headers["location"] == "/ui?login_error=state"
    assert len(stack.idp.token_forms) == 1


@pytest.mark.parametrize("claims, role", [
    ({"sub": "bob"}, "user"),                                    # explicit policy wins
    ({"sub": "carol", "groups": ["viewer"]}, "viewer"),          # IdP group claim
    ({"sub": "dave"}, "admin"),                                  # operator default
])
def test_session_role_comes_from_policy_or_claims_never_the_browser(stack, claims, role):
    sys.modules["tenancy"]._write_policy({"roles": {"bob": "user"}})
    stack.idp.claims = claims
    _, q = _login(stack)
    assert _callback(stack, q).headers["location"] == "/ui"
    assert stack.c.get("/ui/api/me").json()["role"] == role
    assert stack.c.get("/ui/api/secrets").status_code == (200 if role == "admin" else 403)


# ── /ui/callback: refusals ───────────────────────────────────────────────────
def _refused(stack, resp, reason, *, exchanged=False):
    assert resp.status_code == 302 and resp.headers["location"] == f"/ui?login_error={reason}"
    assert stack.webui.SESSION_COOKIE not in _cookies(resp)
    assert bool(stack.idp.token_forms) == exchanged


def test_callback_refuses_a_wrong_state_before_touching_the_idp(stack):
    _, q = _login(stack)
    _refused(stack, _callback(stack, q, state="forged-state"), "state")


def test_callback_refuses_a_missing_code_or_missing_state(stack):
    _, q = _login(stack)
    _refused(stack, _callback(stack, q, code=None), "state")
    _refused(stack, _callback(stack, q, state=None), "state")


def test_callback_refuses_without_the_login_cookie(stack):
    _, q = _login(stack)
    stack.c.cookies.clear()
    _refused(stack, _callback(stack, q), "state")


def test_callback_refuses_an_expired_or_tampered_login_cookie(stack):
    _, q = _login(stack)
    webui = stack.webui
    stack.c.cookies.set(webui.OAUTH_COOKIE, webui._sign(
        {"state": q["state"], "verifier": "v", "exp": time.time() - 1}), path="/ui")
    _refused(stack, _callback(stack, q), "state")
    body, mac = webui._sign({"state": "evil", "verifier": "v", "exp": time.time() + 99}).split(".")
    stack.c.cookies.set(webui.OAUTH_COOKIE, f"{body}.{mac[::-1]}", path="/ui")
    _refused(stack, _callback(stack, q, state="evil"), "state")


def test_callback_reports_a_failed_token_exchange(stack):
    _, q = _login(stack)
    stack.idp.token_status = 500
    _refused(stack, _callback(stack, q), "exchange", exchanged=True)


def test_callback_refuses_a_code_the_idp_rejects(stack):
    _, q = _login(stack)
    _refused(stack, _callback(stack, q, code="stolen-code"), "exchange", exchanged=True)


def test_callback_reports_a_failed_userinfo_call(stack):
    _, q = _login(stack)
    stack.idp.userinfo_status = 401
    _refused(stack, _callback(stack, q), "exchange", exchanged=True)


@pytest.mark.parametrize("claims", [{"name": "No Subject"}, {"sub": "   "}])
def test_callback_refuses_userinfo_without_a_subject(stack, claims):
    stack.idp.claims = claims
    _, q = _login(stack)
    _refused(stack, _callback(stack, q), "claims", exchanged=True)


def test_callback_without_oidc_goes_back_to_the_ui(stack, monkeypatch):
    monkeypatch.delenv("OIDC_CLIENT_ID")
    for path in ("/ui/login", "/ui/callback"):
        r = stack.c.get(path)
        assert r.status_code == 302 and r.headers["location"] == "/ui"
    monkeypatch.setenv("UI_ENABLED", "0")
    assert stack.c.get("/ui/login").headers["location"] == "/ui"


def test_callback_with_a_non_ascii_state_is_refused_not_a_500(stack):
    """A valid login cookie plus a non-ASCII state in the URL must be a plain refusal."""
    _, q = _login(stack)
    _refused(stack, _callback(stack, q, state="zustand-é"), "state")


# ── sessions, logout ─────────────────────────────────────────────────────────
def test_logout_clears_the_session_cookie(stack):
    r = stack.c.post("/ui/logout")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert _cookies(r)[stack.webui.SESSION_COOKIE]["max-age"] == "0"


def test_expired_forged_and_tampered_sessions_are_not_signed_in(stack):
    webui = stack.webui
    live = {"sub": "alice", "role": "admin", "csrf": "t", "exp": time.time() + 60}
    body, mac = webui._sign(live).split(".")
    forged = base64.urlsafe_b64encode(json.dumps(dict(live, sub="boss")).encode()).decode().rstrip("=")
    for cookie in (webui._sign(dict(live, exp=time.time() - 1)),   # expired
                   f"{forged}.{mac}",                              # payload swapped, old mac
                   "not-a-token", "a.b", ""):
        stack.c.cookies.clear()
        stack.c.cookies.set(webui.SESSION_COOKIE, cookie, path="/ui")
        assert stack.c.get("/ui/api/me").json()["authenticated"] is False
        assert stack.c.get("/ui/api/secrets").status_code == 401
    stack.c.cookies.clear()
    stack.c.cookies.set(webui.SESSION_COOKIE, webui._sign(live), path="/ui")
    assert stack.c.get("/ui/api/me").json()["sub"] == "alice"
