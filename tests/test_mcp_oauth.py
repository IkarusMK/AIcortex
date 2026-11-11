"""Sign in to an upstream MCP server with OAuth (the MCP authorization spec).

Before: mcp_add knew only a fixed bearer token from the vault. Servers that hand out
access only through a browser login (often with 2FA) and short-lived tokens could not
be connected at all. Now ``mcp_add(auth="oauth")`` registers
such a server; a person signs in once in the WebUI (Services → Sign in), AICortex
registers itself as a client (dynamic client registration), runs the authorization
code flow with PKCE and the resource indicator, keeps the tokens encrypted in that
person's own vault area and renews them on its own.

What must hold, and is pinned here against a fake provider (authorization server and
MCP server on one origin, all data invented):
- a login belongs to the person who signed in — nobody else can use it, admins included;
- the tokens never leave the server (not in a tool result, not in any WebUI answer) and
  are only ever sent to the server they were issued for;
- a server set to read_only only runs tools its own listing marks read-only;
- every step of the flow is checked (state, person, issuer, resource, PKCE, https).
"""
import asyncio
import base64
import hashlib
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet

SERVER = "acme"
SECRET = "MCP_OAUTH_ACME"


# ── a fake provider: authorization server + MCP server on one origin ─────────
class Provider:
    def __init__(self):
        self.base = ""
        self.registrations, self.authorize_hits, self.token_hits = [], [], []
        self.mcp_auth, self.calls = [], []
        self.codes, self.access, self.refresh = {}, set(), set()
        self.n = 0
        self.expires_in = 3600
        # client registration: "open" takes anything; "native_only" answers a web client
        # with `web_refusal` (HTTP 400) and takes a native client with a loopback
        # redirect; "refuse_all" refuses both
        self.register_mode = "open"
        self.web_refusal = {"error": "invalid_redirect_uri", "error_description": WEB_ONLY}
        self.challenge = True        # 401 carries WWW-Authenticate resource_metadata
        self.prm_extra = {}          # overrides for the protected-resource metadata
        self.meta_extra = {}         # overrides for the authorization-server metadata (None = drop)
        self.refresh_fails = False
        self.deny = False            # the person clicks "deny" at the login page
        self.send_iss = ""           # value for the RFC 9207 iss parameter ("" = none)
        self.page_size = 0           # >0: tools/list answers in pages (nextCursor)
        self.tools = [
            {"name": "get_items", "description": "List the items.",
             "annotations": {"readOnlyHint": True}},
            {"name": "create_item", "description": "Create an item.",
             "annotations": {"readOnlyHint": False, "destructiveHint": True}},
            {"name": "get_item", "description": "One item, no annotations."},
        ]

    # documents ---------------------------------------------------------------
    def prm(self):
        doc = {"resource": f"{self.base}/mcp", "authorization_servers": [f"{self.base}/"]}
        doc.update(self.prm_extra)
        return doc

    def meta(self):
        doc = {"issuer": f"{self.base}/",
               "authorization_endpoint": f"{self.base}/authorize",
               "token_endpoint": f"{self.base}/token",
               "registration_endpoint": f"{self.base}/register",
               "response_types_supported": ["code"],
               "grant_types_supported": ["authorization_code", "refresh_token"],
               "code_challenge_methods_supported": ["S256"],
               "token_endpoint_auth_methods_supported": ["none"],
               "scopes_supported": ["openid", "offline_access"]}
        for k, v in self.meta_extra.items():
            if v is None:
                doc.pop(k, None)
            else:
                doc[k] = v
        return doc

    def issue(self):
        self.n += 1
        at, rt = f"at-{self.n}", f"rt-{self.n}"
        self.access.add(at)
        self.refresh.add(rt)
        return {"access_token": at, "token_type": "Bearer", "expires_in": self.expires_in,
                "refresh_token": rt, "scope": "openid offline_access"}

    def bearer_tokens(self):
        return [h for h in self.mcp_auth if h]

    def refusal(self, body):
        """The 400 answer to a registration, or None when it is taken."""
        if self.register_mode == "open":
            return None
        if body.get("application_type") != "native":
            return self.web_refusal
        loopback = all(str(u).startswith("http://127.0.0.1:") for u in body.get("redirect_uris") or [])
        if self.register_mode == "refuse_all" or not loopback:
            return {"error": "invalid_client_metadata",
                    "error_description": "Native clients are not enabled for this tenant."}
        return None


def _handler(p: Provider):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, obj=None, headers=None, raw=b""):
            body = json.dumps(obj).encode() if obj is not None else raw
            self.send_response(status)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            if obj is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/.well-known/oauth-protected-resource/mcp":
                return self._send(200, p.prm())
            if u.path == "/.well-known/oauth-authorization-server":
                return self._send(200, p.meta())
            if u.path == "/authorize":
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                p.authorize_hits.append(q)
                back = {"state": q.get("state", "")}
                if p.deny:
                    back["error"] = "access_denied"
                else:
                    code = f"code-{len(p.authorize_hits)}"
                    p.codes[code] = q
                    back["code"] = code
                if p.send_iss:
                    back["iss"] = p.send_iss
                return self._send(302, headers={"Location": f"{q['redirect_uri']}?{urlencode(back)}"})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if u.path == "/register":
                body = json.loads(raw)
                p.registrations.append(body)
                refused = p.refusal(body)
                if isinstance(refused, str):          # some providers answer in plain text
                    return self._send(400, raw=refused.encode())
                if refused is not None:
                    return self._send(400, refused)
                return self._send(201, {"client_id": f"client-{len(p.registrations)}",
                                        "redirect_uris": body.get("redirect_uris"),
                                        "token_endpoint_auth_method": "none"})
            if u.path == "/token":
                form = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
                p.token_hits.append(form)
                return self._token(form)
            if u.path == "/mcp":
                return self._mcp(json.loads(raw))
            return self._send(404, {"error": "not found"})

        def _token(self, form):
            if form.get("grant_type") == "authorization_code":
                q = p.codes.pop(form.get("code", ""), None)
                verifier = form.get("code_verifier", "")
                challenge = base64.urlsafe_b64encode(
                    hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
                if (not q or q.get("client_id") != form.get("client_id")
                        or q.get("redirect_uri") != form.get("redirect_uri")
                        or q.get("resource") != form.get("resource")
                        or q.get("code_challenge") != challenge):
                    return self._send(400, {"error": "invalid_grant"})
                return self._send(200, p.issue())
            if form.get("grant_type") == "refresh_token":
                rt = form.get("refresh_token", "")
                if p.refresh_fails or rt not in p.refresh:
                    return self._send(400, {"error": "invalid_grant"})
                p.refresh.discard(rt)              # rotation: the old one is used up
                return self._send(200, p.issue())
            return self._send(400, {"error": "unsupported_grant_type"})

        def _mcp(self, msg):
            auth = self.headers.get("Authorization", "")
            p.mcp_auth.append(auth)
            token = auth[7:] if auth.startswith("Bearer ") else ""
            if token not in p.access:
                hdr = {}
                if p.challenge:
                    hdr["WWW-Authenticate"] = (f'Bearer resource_metadata="{p.base}'
                                               f'/.well-known/oauth-protected-resource/mcp"')
                return self._send(401, {"error": "invalid_token"}, headers=hdr)
            method = msg.get("method", "")
            if method.startswith("notifications/"):
                return self._send(202, raw=b"")
            result = {"initialize": {"protocolVersion": "2025-06-18"},
                      "tools/list": {"tools": p.tools}}.get(method)
            if method == "tools/list" and p.page_size:
                start = int((msg.get("params") or {}).get("cursor") or 0)
                result = {"tools": p.tools[start:start + p.page_size]}
                if start + p.page_size < len(p.tools):
                    result["nextCursor"] = str(start + p.page_size)
            if method == "tools/call":
                name = msg["params"]["name"]
                p.calls.append(name)
                result = {"content": [{"type": "text", "text": f"ok {name}"}]}
            return self._send(200, {"jsonrpc": "2.0", "id": msg.get("id"), "result": result},
                              headers={"Mcp-Session-Id": "s-1"})
    return H


def _start(p: Provider):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(p))
    p.base = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# the native-client fallback: the loopback redirect, and how a provider that only takes
# approved web redirect addresses words its refusal
LOOPBACK = "http://127.0.0.1:8765/callback"
WEB_ONLY = "Web clients may only register exact redirect URIs from the approved SaaS allowlist."


@pytest.fixture
def prov():
    p = Provider()
    srv = _start(p)
    yield p
    srv.shutdown()


@pytest.fixture
def env(monkeypatch, request, prov):
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "127.0.0.0/8")
    base = request.getfixturevalue("http_server")
    ui = httpx.Client(base_url=base, headers={"X-CSRF": "local"})
    e = SimpleNamespace(base=base, prov=prov, ui=ui, mo=sys.modules.get("mcp_oauth"))
    add(prov.base)
    yield e
    ui.close()


# ── helpers ──────────────────────────────────────────────────────────────────
def call(tool, args, who=None):
    """Run a tool the way a client would, optionally as a given person."""
    srv, authz = sys.modules["server"], sys.modules["authz"]

    async def go():
        t = await srv.mcp.get_tool(tool)
        if who:
            with authz.rest_identity(who, "admin"):
                res = await t.run(args)
        else:
            res = await t.run(args)
        return "\n".join(getattr(c, "text", "") for c in res.content)
    return asyncio.run(go())


def add(base, name=SERVER, **extra):
    out = call("mcp_add", {"name": name, "url": f"{base}/mcp", "auth": "oauth",
                           "description": "A test service", **extra})
    assert out.startswith("Registered"), out
    return out


def web_sign_in(e, server=SERVER, ui=None):
    """Start in the WebUI, pass the provider's login page, come back to the callback."""
    ui = ui or e.ui
    r = ui.post("/ui/api/mcp-oauth/start", json={"server": server})
    assert r.status_code == 200, r.text
    page = httpx.get(r.json()["url"], follow_redirects=False)
    assert page.status_code == 302
    return ui.get(page.headers["location"], follow_redirects=False)


def direct_sign_in(owner, server=SERVER, redirect="http://127.0.0.1:9/cb"):
    """Sign in as `owner` through the module itself (no browser session needed)."""
    mo = sys.modules["mcp_oauth"]
    url, pending = mo.begin(server, redirect_uri=redirect, owner=owner)
    page = httpx.get(url, follow_redirects=False)
    back = {k: v[0] for k, v in parse_qs(urlparse(page.headers["location"]).query).items()}
    mo.finish(pending, back)


def vault_keys():
    return [k for k in sys.modules["secrets_store"]._read_all() if k.endswith(SECRET)]


# ── registration and the sign-in flow ────────────────────────────────────────
def test_mcp_add_takes_oauth_and_says_how_to_sign_in(env):
    out = add(env.prov.base)
    assert "Sign in" in out and "WebUI" in out
    listed = call("mcp_list", {})
    assert "acme" in listed and "OAuth" in listed
    assert "Sign in" in call("mcp_call", {"server": SERVER, "tool": "get_items"})


def test_mcp_add_refuses_an_unknown_auth_or_oauth_plus_a_token(env):
    assert "auth" in call("mcp_add", {"name": "x", "url": f"{env.prov.base}/mcp", "auth": "basic"})
    out = call("mcp_add", {"name": "x", "url": f"{env.prov.base}/mcp", "auth": "oauth",
                           "token_env": "SOME_TOKEN"})
    assert out.startswith("Refused") and "token_env" in out
    out = call("mcp_add", {"name": "x", "url": f"{env.prov.base}/mcp", "token_env": SECRET})
    assert out.startswith("Refused") and "reserved" in out


def test_sign_in_registers_a_public_client_and_uses_pkce_and_the_resource(env):
    r = web_sign_in(env)
    assert r.status_code == 302 and "mcp_oauth=ok" in r.headers["location"]
    reg = env.prov.registrations[0]
    assert reg["token_endpoint_auth_method"] == "none"
    assert reg["redirect_uris"] == [f"{env.base}/ui/mcp-oauth/callback"]
    assert set(reg["grant_types"]) == {"authorization_code", "refresh_token"}
    q = env.prov.authorize_hits[0]
    assert q["client_id"] == "client-1" and q["response_type"] == "code"
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) >= 43
    assert q["resource"] == f"{env.prov.base}/mcp"
    assert q["scope"] == "openid offline_access"      # no scopes in the PRM → the AS's list
    tok = env.prov.token_hits[0]
    assert tok["grant_type"] == "authorization_code" and tok["resource"] == f"{env.prov.base}/mcp"


def test_a_second_sign_in_reuses_the_registered_client(env):
    web_sign_in(env)
    web_sign_in(env)
    assert len(env.prov.registrations) == 1
    assert {q["client_id"] for q in env.prov.authorize_hits} == {"client-1"}


def test_discovery_falls_back_to_the_well_known_document(env):
    env.prov.challenge = False         # a 401 without WWW-Authenticate
    assert "mcp_oauth=ok" in web_sign_in(env).headers["location"]


def test_after_sign_in_calls_carry_the_token_and_it_is_stored_privately(env):
    web_sign_in(env)
    assert call("mcp_call", {"server": SERVER, "tool": "get_items"}) == "ok get_items"
    assert env.prov.bearer_tokens()[-1] == "Bearer at-1"
    keys = vault_keys()
    assert len(keys) == 1 and keys[0].startswith("users/local-operator-")
    assert "get_items" in call("mcp_tools", {"server": SERVER})


def test_tokens_never_show_up_in_tool_results_or_webui_answers(env):
    web_sign_in(env)
    seen = [call("mcp_list", {}), call("mcp_tools", {"server": SERVER}),
            call("mcp_call", {"server": SERVER, "tool": "get_items"}),
            call("secret_list", {})]
    for path in ("/ui/api/mcp-oauth", "/ui/api/secrets", "/ui/api/services",
                 "/ui/api/registry/mcp/acme", "/ui/api/search?q=acme"):
        r = env.ui.get(path)
        assert r.status_code == 200, (path, r.text)
        seen.append(r.text)
    for text in seen:
        assert "at-1" not in text and "rt-1" not in text


def test_the_status_says_who_is_signed_in_until_when(env):
    before = env.ui.get("/ui/api/mcp-oauth").json()["servers"]
    assert before == [{"name": "acme", "signed_in": False, "expires_at": None, "renews": False,
                       "read_only": False}]
    web_sign_in(env)
    after = env.ui.get("/ui/api/mcp-oauth").json()["servers"][0]
    assert after["signed_in"] is True and after["renews"] is True
    assert after["expires_at"] > time.time() + 3000


# ── renewing the access ──────────────────────────────────────────────────────
def test_an_expired_token_is_renewed_before_the_call_and_the_new_refresh_token_kept(env):
    env.prov.expires_in = 0
    web_sign_in(env)
    assert call("mcp_call", {"server": SERVER, "tool": "get_items"}) == "ok get_items"
    assert env.prov.token_hits[-1]["grant_type"] == "refresh_token"
    assert env.prov.token_hits[-1]["refresh_token"] == "rt-1"
    assert env.prov.token_hits[-1]["resource"] == f"{env.prov.base}/mcp"
    assert env.prov.bearer_tokens() == ["Bearer at-2"] * len(env.prov.bearer_tokens())
    call("mcp_call", {"server": SERVER, "tool": "get_items"})
    assert env.prov.token_hits[-1]["refresh_token"] == "rt-2"     # the rotated one


def test_a_rejected_token_is_renewed_once_and_the_call_repeated(env):
    web_sign_in(env)
    env.prov.access.discard("at-1")      # the server revoked it early
    assert call("mcp_call", {"server": SERVER, "tool": "get_items"}) == "ok get_items"
    assert env.prov.token_hits[-1]["grant_type"] == "refresh_token"
    assert env.prov.calls == ["get_items"]


def test_a_dead_refresh_token_asks_to_sign_in_again(env):
    env.prov.expires_in = 0
    web_sign_in(env)
    env.prov.refresh_fails = True
    out = call("mcp_call", {"server": SERVER, "tool": "get_items"})
    assert "sign in again" in out.lower()
    assert env.prov.calls == []


# ── whose login it is ────────────────────────────────────────────────────────
def test_only_the_person_who_signed_in_can_use_the_login(env):
    direct_sign_in("alice")
    assert call("mcp_call", {"server": SERVER, "tool": "get_items"}, who="alice") == "ok get_items"
    out = call("mcp_call", {"server": SERVER, "tool": "get_items"}, who="bob")   # an admin too
    assert "not signed in" in out.lower() and "bob" in out
    assert env.prov.calls == ["get_items"]


def test_a_changed_server_url_never_receives_the_old_token(env):
    web_sign_in(env)
    other = Provider()
    srv = _start(other)
    try:
        add(other.base)                  # same name, new address
        out = call("mcp_call", {"server": SERVER, "tool": "get_items"})
        assert "sign in again" in out.lower()
        assert other.bearer_tokens() == []
    finally:
        srv.shutdown()


def test_the_login_secret_name_is_reserved(env):
    web_sign_in(env)
    store = sys.modules["secrets_store"]
    assert store.get_secret(SECRET) is None
    # nobody plants or overwrites a sign-in by hand — not the module call, not the tool,
    # not the WebUI vault
    assert store.vault_set(SECRET, "shared-copy").startswith("Refused")
    assert call("secret_set", {"name": SECRET, "value": "x"}).startswith("Refused")
    r = env.ui.post("/ui/api/secrets", json={"name": SECRET, "value": "x"})
    assert r.status_code == 409 and "reserved" in r.json()["message"]
    r = env.ui.post("/ui/api/secrets/update", json={"name": SECRET, "owner": "local-operator",
                                                    "value": "x"})
    assert r.json()["ok"] is False
    assert store.get_secret(SECRET) is None
    assert call("mcp_call", {"server": SERVER, "tool": "get_items"}) == "ok get_items"


def test_a_login_can_not_be_sent_out_by_a_webhook_either(env):
    """webhook_send lets a non-admin send their OWN secrets to a URL of their choice
    (get_own_secret) — that path must not hand out a sign-in either, or a prompt-
    injected cron job or agent key could ship the upstream login anywhere."""
    direct_sign_in("alice")
    authz, store = sys.modules["authz"], sys.modules["secrets_store"]
    hooks = sys.modules["webhook_tools"]
    url = "https://hooks.example.net/x"
    with authz.rest_identity("alice", "user"):
        assert store.get_own_secret(SECRET) is None
        assert hooks._outbound_secret(SECRET, url) is None
    with authz.rest_identity("alice", "admin"):
        assert hooks._outbound_secret(SECRET, url) is None
    assert call("mcp_call", {"server": SERVER, "tool": "get_items"}, who="alice") == "ok get_items"


def test_sign_out_and_deleting_the_server_remove_the_login(env):
    web_sign_in(env)
    r = env.ui.post("/ui/api/mcp-oauth/logout", json={"server": SERVER})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert vault_keys() == []
    assert "Sign in" in call("mcp_call", {"server": SERVER, "tool": "get_items"})
    web_sign_in(env)
    direct_sign_in("alice")
    assert len(vault_keys()) == 2
    assert call("mcp_delete", {"name": SERVER}).startswith("Deleted")
    assert vault_keys() == []


# ── read-only servers ────────────────────────────────────────────────────────
def test_a_read_only_server_runs_only_tools_marked_read_only(env):
    add(env.prov.base, read_only=True)
    web_sign_in(env)
    assert call("mcp_call", {"server": SERVER, "tool": "get_items"}) == "ok get_items"
    for tool in ("create_item", "get_item"):       # marked not read-only / not marked at all
        out = call("mcp_call", {"server": SERVER, "tool": tool})
        assert out.startswith("Refused") and "read-only" in out
    assert env.prov.calls == ["get_items"]
    tools = call("mcp_tools", {"server": SERVER})
    assert "get_items" in tools and "create_item" in tools and "blocked" in tools
    assert "read-only" in call("mcp_list", {})


def test_read_only_can_only_be_switched_off_by_a_person_in_the_webui(env):
    add(env.prov.base, read_only=True)
    from fastmcp import Client

    async def as_client(args):            # a real MCP call: middleware sees what was passed
        async with Client(sys.modules["server"].mcp) as c:
            res = await c.call_tool("mcp_add", args)
            return "\n".join(getattr(b, "text", "") for b in res.content)
    base = {"name": SERVER, "url": f"{env.prov.base}/mcp", "auth": "oauth"}
    out = asyncio.run(as_client({**base, "read_only": False}))          # an AI's tool call
    assert out.startswith("Refused") and "WebUI" in out
    assert asyncio.run(as_client({**base, "description": "x"})).startswith("Registered")
    assert "read-only" in call("mcp_list", {})       # leaving it out keeps it on
    r = env.ui.post("/ui/api/registry/mcp", json={"name": SERVER, "url": f"{env.prov.base}/mcp",
                                                  "auth": "oauth", "read_only": False})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    assert "read-only" not in call("mcp_list", {})


def test_tools_on_later_pages_are_listed_and_gated_too(env):
    env.prov.page_size = 1
    add(env.prov.base, read_only=True)
    web_sign_in(env)
    tools = call("mcp_tools", {"server": SERVER})
    assert all(n in tools for n in ("get_items", "create_item", "get_item"))
    assert call("mcp_call", {"server": SERVER, "tool": "get_items"}) == "ok get_items"


# ── every step of the flow is checked ────────────────────────────────────────
def test_the_callback_checks_state_cookie_and_provider_answer(env):
    r = env.ui.post("/ui/api/mcp-oauth/start", json={"server": SERVER})
    page = httpx.get(r.json()["url"], follow_redirects=False)
    cb = urlparse(page.headers["location"])
    q = {k: v[0] for k, v in parse_qs(cb.query).items()}
    bad = env.ui.get(f"/ui/mcp-oauth/callback?{urlencode({**q, 'state': 'forged'})}",
                     follow_redirects=False)
    assert "mcp_oauth=state" in bad.headers["location"]
    fresh = httpx.Client(base_url=env.base)        # no pending cookie
    assert "mcp_oauth=state" in fresh.get(f"/ui/mcp-oauth/callback?{cb.query}",
                                          follow_redirects=False).headers["location"]
    fresh.close()
    env.prov.deny = True
    assert "mcp_oauth=denied" in web_sign_in(env).headers["location"]
    env.prov.deny, env.prov.send_iss = False, "https://someone-else.example/"
    assert "mcp_oauth=issuer" in web_sign_in(env).headers["location"]
    assert vault_keys() == []


def test_start_needs_the_csrf_header_and_a_known_oauth_server(env):
    no_csrf = httpx.Client(base_url=env.base)
    assert no_csrf.post("/ui/api/mcp-oauth/start", json={"server": SERVER}).status_code == 403
    no_csrf.close()
    assert env.ui.post("/ui/api/mcp-oauth/start", json={"server": "nope"}).status_code == 404
    call("mcp_add", {"name": "plain", "url": f"{env.prov.base}/mcp"})
    assert env.ui.post("/ui/api/mcp-oauth/start", json={"server": "plain"}).status_code == 400


@pytest.mark.parametrize("where, change, fragment", [
    ("prm", {"resource": "https://elsewhere.example/mcp"}, "resource"),
    ("meta", {"issuer": "https://elsewhere.example/"}, "issuer"),
    ("meta", {"code_challenge_methods_supported": ["plain"]}, "S256"),
    ("meta", {"registration_endpoint": None}, "registration"),
    ("meta", {"authorization_endpoint": "javascript:alert(1)"}, "https"),
    ("meta", {"authorization_endpoint": "http://8.8.8.8/authorize"}, "https"),
    ("meta", {"token_endpoint": "http://8.8.8.8/token"}, "https"),
    ("meta", {"token_endpoint": "https://[::1"}, "https"),           # not even a valid URL
])
def test_unsafe_or_foreign_provider_answers_are_refused(env, where, change, fragment):
    setattr(env.prov, "prm_extra" if where == "prm" else "meta_extra", change)
    r = env.ui.post("/ui/api/mcp-oauth/start", json={"server": SERVER})
    assert r.status_code == 502, r.text
    assert fragment in r.json()["message"]
    assert env.prov.authorize_hits == []


def test_only_identity_scopes_are_asked_for_unless_the_server_names_its_own(env):
    env.prov.meta_extra = {"scopes_supported": ["openid", "offline_access", "trade:write"]}
    web_sign_in(env)
    assert env.prov.authorize_hits[-1]["scope"] == "openid offline_access"
    env.prov.prm_extra = {"scopes_supported": ["items:read"]}
    web_sign_in(env)
    assert env.prov.authorize_hits[-1]["scope"] == "items:read offline_access"


def test_start_tells_where_the_sign_in_happens_before_leaving(env):
    r = env.ui.post("/ui/api/mcp-oauth/start", json={"server": SERVER}).json()
    assert r["server_url"] == f"{env.prov.base}/mcp"
    assert r["login_host"] == urlparse(env.prov.base).netloc
    assert "oa_confirm" in env.ui.get("/ui/assets/view-registry.js").text


def test_an_issuer_that_promises_iss_must_send_it(env):
    env.prov.meta_extra = {"authorization_response_iss_parameter_supported": True}
    assert "mcp_oauth=issuer" in web_sign_in(env).headers["location"]
    env.prov.send_iss = f"{env.prov.base}/"
    assert "mcp_oauth=ok" in web_sign_in(env).headers["location"]


def test_an_oauth_server_must_use_https_outside_the_lan(env):
    out = call("mcp_add", {"name": "far", "url": "http://8.8.8.8/mcp", "auth": "oauth"})
    assert out.startswith("Refused") and "https" in out
    mo = sys.modules["mcp_oauth"]
    with pytest.raises(mo.OAuthError):          # an https server's provider may not fall back to http
        mo._safe_url(f"{env.prov.base}/token", "token endpoint", plain_ok=False)
    assert mo._safe_url(f"{env.prov.base}/token", "token endpoint", plain_ok=True)


def test_a_renewed_login_still_serves_the_call_when_it_cannot_be_stored(env, monkeypatch):
    env.prov.expires_in = 0
    web_sign_in(env)
    store = sys.modules["secrets_store"]
    monkeypatch.setattr(store, "vault_set", lambda *a, **k: "Refusing to write: vault unreadable")
    assert call("mcp_call", {"server": SERVER, "tool": "get_items"}) == "ok get_items"


def test_odd_provider_answers_do_not_break_the_sign_in(env):
    env.prov.expires_in = float("inf")          # JSON Infinity
    assert "mcp_oauth=ok" in web_sign_in(env).headers["location"]
    assert call("mcp_call", {"server": SERVER, "tool": "get_items"}) == "ok get_items"


def test_a_deleted_server_is_not_brought_back_by_a_late_registration(env):
    mo = sys.modules["mcp_oauth"]
    call("mcp_delete", {"name": SERVER})
    mo._save_client(SERVER, {"client_id": "late"})
    assert not sys.modules["mcp_gateway"]._cfg_path(SERVER).exists()


def test_without_an_encryption_key_no_login_is_stored(env, monkeypatch):
    monkeypatch.delenv("STORAGE_ENCRYPTION_KEY")
    assert "mcp_oauth=vault" in web_sign_in(env).headers["location"]
    assert vault_keys() == []


# ── a different person finishing someone else's sign-in ──────────────────────
@pytest.mark.oidc
def test_the_person_who_started_must_be_the_one_who_finishes(monkeypatch, request, prov, fake_idp):
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
    add(prov.base)
    alice = client("alice")
    r = alice.post("/ui/api/mcp-oauth/start", json={"server": SERVER})
    pending = r.cookies.get("aicortex_ui_mcp_oauth")
    assert pending
    page = httpx.get(r.json()["url"], follow_redirects=False)
    bob = client("bob", aicortex_ui_mcp_oauth=pending)      # bob holds alice's pending cookie
    out = bob.get(page.headers["location"], follow_redirects=False)
    assert "mcp_oauth=person" in out.headers["location"]
    assert vault_keys() == []
    alice.close()
    bob.close()


# ── the button in the WebUI ──────────────────────────────────────────────────
def test_the_sign_in_button_is_wired_into_the_services_view(env):
    js = env.ui.get("/ui/assets/view-registry.js")
    assert js.status_code == 200
    for needle in ("/ui/api/mcp-oauth/start", "/ui/api/mcp-oauth/logout", "/ui/api/mcp-oauth",
                   "oauthSignIn", "oauthSignOut", "mcp_oauth"):
        assert needle in js.text, needle
    assert "innerHTML" not in js.text
    app = env.ui.get("/ui/assets/app.js").text
    assert "oauthCell" in app
