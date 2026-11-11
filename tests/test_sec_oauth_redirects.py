"""The OAuth proxy only sends sign-ins back to real MCP clients — and asks once.

Regression for the audit finding "OAuth confused deputy": ``/register`` is open
Dynamic Client Registration, ``allowed_client_redirect_uris`` was unset (any
redirect URI accepted) and ``require_authorization_consent="external"`` switched
FastMCP's consent page off. Anyone could register a client whose redirect URI they
control and send a signed-in person a link: one click at the IdP and the person's
authorization code — a token for their whole brain — went to the attacker.

Proves, over real HTTP against a local stand-in IdP:
- a client registering a foreign redirect URI (or a look-alike) is refused;
- the hosted callbacks of claude.ai / claude.com / ChatGPT and loopback callbacks of
  desktop/CLI clients (any port) are accepted;
- ``MCP_REDIRECT_URIS`` extends the list, and the single value ``*`` knowingly
  restores "allow any";
- the consent page is shown before the IdP, remembered per client, and a complete
  sign-in (consent → IdP → callback → token → /mcp) still works.
"""
import base64
import hashlib
import json
import re
import secrets
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import httpx
import pytest

pytestmark = pytest.mark.oidc

CLIENT_CB = "http://localhost:53682/callback"


@pytest.fixture
def idp():
    """An OIDC provider that really signs in: discovery, JWKS and a token endpoint
    issuing RS256 tokens for "alice" (upstream code "upstream-code" only)."""
    from cryptography.hazmat.primitives import serialization
    from fastmcp.server.auth.providers.jwt import RSAKeyPair

    keys = RSAKeyPair.generate()
    nums = serialization.load_pem_public_key(keys.public_key.encode()).public_numbers()

    def b64(i: int) -> str:
        return base64.urlsafe_b64encode(i.to_bytes((i.bit_length() + 7) // 8, "big")).decode().rstrip("=")
    jwk = {"kty": "RSA", "kid": "k1", "use": "sig", "alg": "RS256", "n": b64(nums.n), "e": b64(nums.e)}
    state = SimpleNamespace(base="", token_forms=[])

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, obj):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/.well-known/openid-configuration":
                self._send(200, {"issuer": state.base,
                                 "authorization_endpoint": f"{state.base}/authorize",
                                 "token_endpoint": f"{state.base}/token",
                                 "jwks_uri": f"{state.base}/jwks",
                                 "response_types_supported": ["code"],
                                 "subject_types_supported": ["public"],
                                 "id_token_signing_alg_values_supported": ["RS256"]})
            elif self.path == "/jwks":
                self._send(200, {"keys": [jwk]})
            else:
                self._send(404, {})

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            form = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
            state.token_forms.append(form)
            if self.path != "/token" or form.get("code") != "upstream-code":
                return self._send(400, {"error": "invalid_grant"})
            access = keys.create_token(subject="alice", issuer=state.base, kid="k1")
            id_token = keys.create_token(subject="alice", issuer=state.base, audience="aicortex",
                                         kid="k1", additional_claims={"name": "Alice Example"})
            self._send(200, {"access_token": access, "id_token": id_token,
                             "token_type": "Bearer", "expires_in": 3600})

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    state.base = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield state
    server.shutdown()


@pytest.fixture
def boot(monkeypatch, request, idp):
    """Start the server with the given MCP_REDIRECT_URIS (None = unset)."""
    def start(redirects=None):
        monkeypatch.setenv("OIDC_CONFIG_URL", f"{idp.base}/.well-known/openid-configuration")
        monkeypatch.setenv("OIDC_CLIENT_ID", "aicortex")
        monkeypatch.setenv("OIDC_CLIENT_SECRET", "upstream-secret")
        monkeypatch.setenv("JWT_SIGNING_KEY", "0" * 64)
        if redirects is None:
            monkeypatch.delenv("MCP_REDIRECT_URIS", raising=False)
        else:
            monkeypatch.setenv("MCP_REDIRECT_URIS", redirects)
        return request.getfixturevalue("http_server")
    return start


def _register(base, *uris):
    return httpx.post(f"{base}/register", json={
        "redirect_uris": list(uris), "client_name": "test client",
        "token_endpoint_auth_method": "none", "response_types": ["code"],
        "grant_types": ["authorization_code", "refresh_token"]}, timeout=30)


def _proxy():
    return sys.modules["server"].auth.server       # MultiAuth → the OIDC proxy


# ── registration: whose callbacks are allowed ────────────────────────────────
@pytest.mark.parametrize("uri", [
    "https://attacker.example.com/callback",
    "https://claude.ai.attacker.example.com/api/mcp/auth_callback",   # look-alike host
    "https://claude.ai/api/mcp/other_callback",                       # other path
    "http://claude.ai/api/mcp/auth_callback",                         # downgraded scheme
    "http://localhost@attacker.example.com/callback",                 # userinfo trick
    "http://attacker.example.com:53682/callback",
])
def test_a_foreign_redirect_uri_is_refused(boot, uri):
    r = _register(boot(), uri)
    assert r.status_code == 400, r.text
    assert r.json()["error"] == "invalid_redirect_uri"


@pytest.mark.parametrize("uri", [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "https://chatgpt.com/connector_platform_oauth_redirect",
    CLIENT_CB, "http://127.0.0.1:8976/oauth/callback", "http://[::1]:33418/",
])
def test_the_callbacks_of_real_mcp_clients_are_accepted(boot, uri):
    r = _register(boot(), uri)
    assert r.status_code == 201, r.text


def test_the_allow_list_can_be_extended(boot):
    base = boot(" https://app.example.com/oauth/* , myapp://callback ")
    assert _register(base, "https://app.example.com/oauth/done").status_code == 201
    assert _register(base, "myapp://callback").status_code == 201
    assert _register(base, "https://claude.ai/api/mcp/auth_callback").status_code == 201
    assert _register(base, "https://attacker.example.com/callback").status_code == 400


def test_a_single_star_knowingly_allows_any_redirect(boot):
    base = boot("*")
    assert _proxy()._allowed_client_redirect_uris is None
    assert _register(base, "https://attacker.example.com/callback").status_code == 201


def test_a_star_among_other_entries_does_not_open_everything(boot):
    base = boot("*, https://app.example.com/cb")
    assert _register(base, "https://attacker.example.com/callback").status_code == 400
    assert _register(base, "https://app.example.com/cb").status_code == 201


def test_the_proxy_keeps_resource_upstream_off_and_asks_for_consent(boot):
    boot()
    proxy = _proxy()
    assert proxy._require_authorization_consent == "remember"
    assert proxy._forward_resource is False


# ── the whole sign-in still works, with one consent step ─────────────────────
def _local(url: str) -> str:
    """The server's own redirects name BASE_URL; send them to the test server."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.path}?{parts.query}"


def _query(url: str) -> dict:
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}


def test_a_full_sign_in_completes_after_the_consent_step(boot, idp):
    base = boot()
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    client_id = _register(base, CLIENT_CB).json()["client_id"]
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": CLIENT_CB,
              "code_challenge": challenge, "code_challenge_method": "S256", "state": "client-state"}
    with httpx.Client(base_url=base, follow_redirects=False, timeout=30) as browser:
        r = browser.get("/authorize", params=params)
        assert r.status_code == 302 and "/consent?txn_id=" in r.headers["location"]
        page = browser.get(_local(r.headers["location"]))
        assert page.status_code == 200
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        txn = _query(r.headers["location"])["txn_id"]
        r = browser.post("/consent", data={"action": "approve", "txn_id": txn, "csrf_token": csrf,
                                           "submit": "true"})
        assert r.status_code == 302 and r.headers["location"].startswith(f"{idp.base}/authorize?")
        upstream = _query(r.headers["location"])
        assert "resource" not in upstream and upstream["scope"] == "openid profile email"
        # … alice signs in at the IdP, which sends her browser back:
        r = browser.get("/auth/callback", params={"code": "upstream-code", "state": upstream["state"]})
        assert r.status_code == 302 and r.headers["location"].startswith(CLIENT_CB), r.text
        back = _query(r.headers["location"])
        assert back["state"] == "client-state"
        # the client trades the code (with its PKCE verifier) for a token that opens /mcp
        tok = httpx.post(f"{base}/token", data={
            "grant_type": "authorization_code", "code": back["code"], "redirect_uri": CLIENT_CB,
            "client_id": client_id, "code_verifier": verifier}, timeout=30)
        assert tok.status_code == 200, tok.text
        init = httpx.post(f"{base}/mcp", timeout=30, headers={
            "Authorization": f"Bearer {tok.json()['access_token']}",
            "Accept": "application/json, text/event-stream"}, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "1"}}})
        assert init.status_code == 200, init.text
        # next time the approval is remembered: straight on to the IdP
        r = browser.get("/authorize", params={**params, "state": "again"})
        r = browser.get(_local(r.headers["location"]), headers={"Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 302 and r.headers["location"].startswith(f"{idp.base}/authorize?")


def test_a_registered_client_cannot_redirect_elsewhere_at_authorize(boot):
    base = boot()
    client_id = _register(base, CLIENT_CB).json()["client_id"]
    r = httpx.get(f"{base}/authorize", follow_redirects=False, timeout=30, params={
        "response_type": "code", "client_id": client_id,
        "redirect_uri": "https://attacker.example.com/callback",
        "code_challenge": "c" * 43, "code_challenge_method": "S256", "state": "s"})
    assert r.status_code == 400
    assert "attacker.example.com" not in r.headers.get("location", "")
