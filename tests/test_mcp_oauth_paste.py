"""Sign in to a provider that refuses web redirect addresses: a native client and a pasted address.

Before: AICortex registered itself only as a web client with the redirect
``<BASE_URL>/ui/mcp-oauth/callback``. An OAuth provider that only accepts approved web
redirect URIs (measured on an upstream MCP server) answers that registration with
HTTP 400 — "Web clients may only register exact redirect URIs from the approved SaaS
allowlist." — and the sign-in could not even start. The same provider takes a native
(desktop) client with the RFC 8252 loopback redirect ``http://127.0.0.1:8765/callback``.

Now: when the web registration is refused because of its redirect address, AICortex
registers once more as a native client with that loopback redirect, keeps the mode
with the client record and uses the loopback address for the login page and the token
exchange. The person signs in in a new tab; the browser then lands on an error page at
127.0.0.1 (nothing listens there) and the person pastes that address into the WebUI,
which hands it to POST /ui/api/mcp-oauth/finish. PKCE and state stay mandatory: the
code in the address is useless without the verifier, which never leaves AICortex.

What must hold, pinned here against the fake provider of test_mcp_oauth (all data
invented):
- only an address that starts with exactly the registered loopback redirect (scheme,
  host 127.0.0.1, port, path) is taken; it is parsed, never fetched;
- the pasted address passes the same checks as the redirect callback (CSRF, the
  signed-in person, the pending sign-in, state, provider error, issuer, exchange);
- a refusal stores nothing; a provider refusing both kinds of client says so clearly;
- a provider that takes the web client sees exactly what it saw before.
"""
import sys
import time
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet

from test_mcp_oauth import (LOOPBACK, SERVER, WEB_ONLY, add, call, env, prov,  # noqa: F401
                            vault_keys)


# ── helpers ──────────────────────────────────────────────────────────────────
def paste_start(e, ui=None):
    """Start in the WebUI and pass the provider's login page; (start answer, the
    address the browser lands on afterwards)."""
    r = (ui or e.ui).post("/ui/api/mcp-oauth/start", json={"server": SERVER})
    assert r.status_code == 200, r.text
    d = r.json()
    page = httpx.get(d["url"], follow_redirects=False)
    assert page.status_code == 302
    return d, page.headers["location"]


def paste_finish(e, address, ui=None):
    return (ui or e.ui).post("/ui/api/mcp-oauth/finish", json={"url": address})


def stored_client():
    return sys.modules["mcp_gateway"]._load(SERVER).get("oauth_client") or {}


def query_of(address):
    return {k: v[0] for k, v in parse_qs(urlparse(address).query).items()}


@pytest.fixture
def native(env):
    """The provider refuses web redirect addresses and takes a native loopback client."""
    env.prov.register_mode = "native_only"
    return env


# ── registration: web first, then the native loopback client ─────────────────
@pytest.mark.parametrize("refusal", [
    pytest.param({"error": "invalid_redirect_uri", "error_description": WEB_ONLY},
                 id="invalid_redirect_uri"),
    pytest.param({"error": "invalid_client_metadata", "error_description": WEB_ONLY},
                 id="invalid_client_metadata"),
    pytest.param({"message": WEB_ONLY}, id="message-only"),     # no RFC 7591 error code
    pytest.param(WEB_ONLY, id="plain-text"),                    # not even JSON
])
def test_a_refused_web_registration_is_retried_as_a_native_loopback_client(native, refusal):
    native.prov.web_refusal = refusal
    d, landed = paste_start(native)
    assert d["mode"] == "paste" and d["redirect_prefix"] == LOOPBACK
    web, desk = native.prov.registrations
    assert "application_type" not in web
    assert web["redirect_uris"] == [f"{native.base}/ui/mcp-oauth/callback"]
    assert desk["application_type"] == "native" and desk["redirect_uris"] == [LOOPBACK]
    assert desk["token_endpoint_auth_method"] == "none"
    assert set(desk["grant_types"]) == {"authorization_code", "refresh_token"}
    q = native.prov.authorize_hits[0]
    assert q["redirect_uri"] == LOOPBACK and q["client_id"] == "client-2"
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) >= 43
    assert landed.startswith(LOOPBACK + "?")
    rec = stored_client()
    assert rec["client_id"] == "client-2" and rec["mode"] == "native"
    assert rec["redirect_uri"] == LOOPBACK


def test_the_pasted_address_completes_the_sign_in(native):
    _, landed = paste_start(native)
    r = paste_finish(native, landed)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["code"] == "ok"
    tok = native.prov.token_hits[0]
    assert tok["grant_type"] == "authorization_code" and tok["redirect_uri"] == LOOPBACK
    assert tok["client_id"] == "client-2" and len(tok["code_verifier"]) >= 43
    keys = vault_keys()
    assert len(keys) == 1 and keys[0].startswith("users/local-operator-")
    assert not call("mcp_tools", {"server": SERVER}).startswith(("Refused", "Not signed in"))
    assert native.prov.bearer_tokens()[-1] == "Bearer at-1"        # calls carry the login
    st = native.ui.get("/ui/api/mcp-oauth").json()["servers"][0]
    assert st["signed_in"] is True and st["expires_at"] > time.time() + 3000
    for text in (r.text, native.ui.get("/ui/api/mcp-oauth").text):
        assert "at-1" not in text and "rt-1" not in text
    # one-shot: the same address a second time finds no sign-in in flight
    again = paste_finish(native, landed)
    assert again.status_code == 400 and again.json()["code"] == "state"
    assert len(native.prov.token_hits) == 1


def test_a_second_sign_in_reuses_the_native_client(native):
    for _ in range(2):
        _, landed = paste_start(native)
        assert paste_finish(native, landed).json()["ok"] is True
    assert len(native.prov.registrations) == 2           # web refused + native, once
    assert {q["redirect_uri"] for q in native.prov.authorize_hits} == {LOOPBACK}
    assert {q["client_id"] for q in native.prov.authorize_hits} == {"client-2"}


def test_a_provider_refusing_both_kinds_of_client_says_so(env):
    env.prov.register_mode = "refuse_all"
    r = env.ui.post("/ui/api/mcp-oauth/start", json={"server": SERVER})
    assert r.status_code == 502, r.text
    msg = r.json()["message"]
    assert "web" in msg and "desktop" in msg and LOOPBACK in msg
    assert "approved SaaS allowlist" in msg and "Native clients are not enabled" in msg
    assert len(env.prov.registrations) == 2 and env.prov.authorize_hits == []
    assert stored_client() == {}
    assert "aicortex_ui_mcp_oauth" not in r.cookies


def test_a_refusal_that_is_not_about_the_redirect_is_not_retried(env):
    env.prov.register_mode = "native_only"
    env.prov.web_refusal = {"error": "invalid_software_statement",
                            "error_description": "A software statement is required."}
    r = env.ui.post("/ui/api/mcp-oauth/start", json={"server": SERVER})
    assert r.status_code == 502 and "software statement" in r.json()["message"]
    assert len(env.prov.registrations) == 1


def test_web_mode_is_unchanged(env):
    r = env.ui.post("/ui/api/mcp-oauth/start", json={"server": SERVER})
    d = r.json()
    assert d["mode"] == "web" and "redirect_prefix" not in d
    assert len(env.prov.registrations) == 1
    reg = env.prov.registrations[0]
    assert "application_type" not in reg
    assert reg["redirect_uris"] == [f"{env.base}/ui/mcp-oauth/callback"]
    assert query_of(d["url"])["redirect_uri"] == f"{env.base}/ui/mcp-oauth/callback"
    rec = stored_client()
    assert rec["mode"] == "web" and rec["redirect_uri"] == f"{env.base}/ui/mcp-oauth/callback"
    page = httpx.get(d["url"], follow_redirects=False)
    back = env.ui.get(page.headers["location"], follow_redirects=False)
    assert "mcp_oauth=ok" in back.headers["location"]


def test_a_client_record_from_before_is_reused_as_a_web_client(env):
    """2.6.1 stored the client without a mode: it keeps working, nothing is registered."""
    mo = sys.modules["mcp_oauth"]
    mo._save_client(SERVER, {"client_id": "client-old", "issuer": f"{env.prov.base}/",
                             "resource": f"{env.prov.base}/mcp",
                             "redirect_uri": f"{env.base}/ui/mcp-oauth/callback",
                             "registered": "2026-10-01T00:00:00+00:00"})
    d = env.ui.post("/ui/api/mcp-oauth/start", json={"server": SERVER}).json()
    assert d["mode"] == "web" and env.prov.registrations == []
    assert query_of(d["url"])["client_id"] == "client-old"


@pytest.mark.parametrize("stale", [
    {"redirect_uri": "http://127.0.0.1:9999/callback"},          # another loopback address
    {"issuer": "https://elsewhere.example/"},                    # another provider
    {"resource": "https://elsewhere.example/mcp"},               # another server
])
def test_a_stale_native_client_is_registered_again(native, stale):
    mo = sys.modules["mcp_oauth"]
    mo._save_client(SERVER, {"client_id": "client-old", "issuer": f"{native.prov.base}/",
                             "resource": f"{native.prov.base}/mcp", "redirect_uri": LOOPBACK,
                             "mode": "native", **stale})
    d, landed = paste_start(native)
    assert d["mode"] == "paste" and len(native.prov.registrations) == 2
    assert native.prov.authorize_hits[0]["client_id"] == "client-2"
    assert native.prov.authorize_hits[0]["redirect_uri"] == LOOPBACK
    assert stored_client()["redirect_uri"] == LOOPBACK
    assert paste_finish(native, landed).json()["ok"] is True


# ── the pasted address ───────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", [
    pytest.param(lambda a: a.replace("http://", "https://", 1), id="https"),
    pytest.param(lambda a: a.replace("127.0.0.1", "localhost", 1), id="localhost"),
    pytest.param(lambda a: a.replace("127.0.0.1", "127.0.0.2", 1), id="other-loopback"),
    pytest.param(lambda a: a.replace("127.0.0.1:8765", "provider.example", 1), id="other-host"),
    pytest.param(lambda a: a.replace(":8765", ":8766", 1), id="other-port"),
    pytest.param(lambda a: a.replace(":8765", "", 1), id="no-port"),
    pytest.param(lambda a: a.replace("/callback", "/callback2", 1), id="other-path"),
    pytest.param(lambda a: a.replace("/callback", "/x/../callback", 1), id="dot-path"),
    pytest.param(lambda a: a.replace("http://", "http://someone@", 1), id="userinfo"),
    pytest.param(lambda a: urlparse(a).query, id="query-only"),
    pytest.param(lambda a: f"{LOOPBACK}?{urlencode({'state': query_of(a)['state']})}",
                 id="no-code"),
    pytest.param(lambda a: f"{a}&state=again", id="state-twice"),
    pytest.param(lambda a: f"{a}&x={'a' * 5000}", id="too-long"),
    pytest.param(lambda a: "", id="empty"),
    pytest.param(lambda a: ["not", "a", "string"], id="not-a-string"),
])
def test_only_the_loopback_redirect_address_is_taken(native, bad):
    _, landed = paste_start(native)
    r = paste_finish(native, bad(landed))
    assert r.status_code == 400, r.text
    d = r.json()
    assert d["ok"] is False and d["code"] == "address" and LOOPBACK in d["message"]
    assert native.prov.token_hits == [] and vault_keys() == []
    # nothing was spent: the right address still completes this sign-in
    assert paste_finish(native, landed).json()["ok"] is True
    assert len(vault_keys()) == 1


def test_the_provider_answer_in_the_address_is_checked_like_the_callback(native):
    _, landed = paste_start(native)
    forged = landed.replace(f"state={query_of(landed)['state']}", "state=forged")
    r = paste_finish(native, forged)
    assert r.status_code == 400 and r.json()["code"] == "state"
    assert paste_finish(native, landed).json()["code"] == "state"     # one-shot, like the callback
    native.prov.deny = True
    _, landed = paste_start(native)
    assert paste_finish(native, landed).json()["code"] == "denied"
    native.prov.deny, native.prov.send_iss = False, "https://someone-else.example/"
    _, landed = paste_start(native)
    assert paste_finish(native, landed).json()["code"] == "issuer"
    assert native.prov.token_hits == [] and vault_keys() == []


def test_a_wrong_code_is_refused_by_the_exchange(native):
    _, landed = paste_start(native)
    code = query_of(landed)["code"]
    r = paste_finish(native, landed.replace(f"code={code}", "code=made-up"))
    assert r.status_code == 400 and r.json()["code"] == "exchange"
    assert vault_keys() == []


def test_finish_needs_csrf_the_pending_sign_in_and_a_small_body(native):
    _, landed = paste_start(native)
    no_csrf = httpx.Client(base_url=native.base, cookies=native.ui.cookies)
    assert no_csrf.post("/ui/api/mcp-oauth/finish", json={"url": landed}).status_code == 403
    no_csrf.close()
    fresh = httpx.Client(base_url=native.base, headers={"X-CSRF": "local"})   # no pending cookie
    r = fresh.post("/ui/api/mcp-oauth/finish", json={"url": landed})
    assert r.status_code == 400 and r.json()["code"] == "state"
    fresh.close()
    big = native.ui.post("/ui/api/mcp-oauth/finish", json={"url": landed, "pad": "x" * 20000})
    assert big.status_code == 413
    assert native.prov.token_hits == [] and vault_keys() == []
    assert paste_finish(native, landed).json()["ok"] is True       # still the person's own


def test_a_web_sign_in_takes_no_pasted_address(env):
    d = env.ui.post("/ui/api/mcp-oauth/start", json={"server": SERVER}).json()
    landed = httpx.get(d["url"], follow_redirects=False).headers["location"]
    assert landed.startswith(f"{env.base}/ui/mcp-oauth/callback?")
    pasted = f"{LOOPBACK}?{urlparse(landed).query}"
    r = paste_finish(env, pasted)
    assert r.status_code == 400 and r.json()["ok"] is False
    assert env.prov.token_hits == [] and vault_keys() == []


def test_the_finish_endpoint_is_rate_limited_like_the_callback(env):
    assert sys.modules["ratelimit"].group_of("/ui/api/mcp-oauth/finish") == "login"


@pytest.mark.oidc
def test_only_the_person_who_started_can_paste_the_address(monkeypatch, request, prov, fake_idp):
    monkeypatch.setenv("OIDC_CONFIG_URL", f"{fake_idp}/.well-known/openid-configuration")
    monkeypatch.setenv("OIDC_CLIENT_ID", "aicortex")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "upstream-secret")
    monkeypatch.setenv("JWT_SIGNING_KEY", "0" * 64)
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "127.0.0.0/8")
    base = request.getfixturevalue("http_server")
    webui = sys.modules["webui"]

    def client(sub, **cookies):
        tok = webui._sign({"sub": sub, "name": sub, "role": "admin", "csrf": "tok",
                           "exp": time.time() + 600})
        return httpx.Client(base_url=base, cookies={webui.SESSION_COOKIE: tok, **cookies},
                            headers={"X-CSRF": "tok"})
    prov.register_mode = "native_only"
    add(prov.base)
    alice = client("alice")
    r = alice.post("/ui/api/mcp-oauth/start", json={"server": SERVER})
    assert r.json()["mode"] == "paste"
    pending = r.cookies.get("aicortex_ui_mcp_oauth")
    landed = httpx.get(r.json()["url"], follow_redirects=False).headers["location"]
    bob = client("bob", aicortex_ui_mcp_oauth=pending)      # bob holds alice's pending cookie
    out = bob.post("/ui/api/mcp-oauth/finish", json={"url": landed})
    assert out.status_code == 403 and out.json()["code"] == "person"
    nobody = httpx.Client(base_url=base, cookies={"aicortex_ui_mcp_oauth": pending},
                          headers={"X-CSRF": "tok"})        # no session at all
    assert nobody.post("/ui/api/mcp-oauth/finish", json={"url": landed}).status_code == 401
    assert prov.token_hits == [] and vault_keys() == []
    for c in (alice, bob, nobody):
        c.close()


# ── the WebUI and the guide ──────────────────────────────────────────────────
def test_the_paste_step_is_wired_into_the_webui(env):
    js = env.ui.get("/ui/assets/view-registry.js").text
    for needle in ("/ui/api/mcp-oauth/finish", "oauthPaste", "window.open", "noopener",
                   "showModal", "redirect_prefix", "oa_paste_text"):
        assert needle in js, needle
    assert "innerHTML" not in js
    assert "Abschließen" in js and "Finish" in js
    assert "127.0.0.1:8765/callback" not in js       # the address comes from the server
    html = env.ui.get("/ui").text
    start = html.index('<dialog id="oa-paste"')
    dialog = html[start:html.index("</dialog>", start)]
    assert 'id="oa-paste-url"' in dialog and "style=" not in dialog
    css = env.ui.get("/ui/assets/style.css").text
    assert ".oa-dialog" in css and "::backdrop" in css


def test_the_guide_mentions_the_paste_step(env):
    text = __import__("guide").GUIDE
    de, en = text[:text.index("Other MCP servers")], text[text.index("Other MCP servers"):]
    assert "127.0.0.1" in de and "einfügen" in de
    assert "127.0.0.1" in en and "paste" in en
