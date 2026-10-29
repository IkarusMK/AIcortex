"""OAuth discovery meets what claude.ai and ChatGPT require (docs as of 2026-10).

- An unauthenticated /mcp request gets 401 + ``WWW-Authenticate: Bearer
  resource_metadata="<public>/.well-known/oauth-protected-resource/mcp"``
  (Claude only starts the sign-in on exactly this).
- Protected-resource metadata: ``resource`` equals the public MCP URL.
- Authorization-server metadata: PKCE S256, a DCR registration endpoint, and
  CIMD (``client_id_metadata_document_supported`` + ``none`` auth method) —
  ChatGPT prefers CIMD, Pocket ID itself refuses DCR.
- Per-agent bearer tokens keep working next to OAuth.
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

PUBLIC = "https://agent.example.net"

pytestmark = pytest.mark.oidc


@pytest.fixture
def fake_idp():
    """A minimal OIDC provider: just the discovery document and an empty JWKS."""
    server = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    docs = {
        "/.well-known/openid-configuration": {
            "issuer": base,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "jwks_uri": f"{base}/jwks",
            "response_types_supported": ["code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
        },
        "/jwks": {"keys": []},
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(docs.get(self.path, {})).encode()
            self.send_response(200 if self.path in docs else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server.RequestHandlerClass = Handler
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield base
    server.shutdown()


@pytest.fixture
def oauth_server(monkeypatch, request, fake_idp):
    monkeypatch.setenv("OIDC_CONFIG_URL", f"{fake_idp}/.well-known/openid-configuration")
    monkeypatch.setenv("OIDC_CLIENT_ID", "aicortex")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "upstream-secret")
    monkeypatch.setenv("BASE_URL", PUBLIC)
    monkeypatch.setenv("JWT_SIGNING_KEY", "0" * 64)
    return request.getfixturevalue("http_server")


def test_unauthenticated_request_points_to_resource_metadata(oauth_server):
    r = httpx.post(f"{oauth_server}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                   headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 401
    challenge = r.headers.get("www-authenticate", "")
    assert challenge.lower().startswith("bearer")
    assert f'resource_metadata="{PUBLIC}/.well-known/oauth-protected-resource/mcp"' in challenge


def test_protected_resource_metadata_names_the_public_url(oauth_server):
    meta = httpx.get(f"{oauth_server}/.well-known/oauth-protected-resource/mcp").json()
    assert meta["resource"] == f"{PUBLIC}/mcp"
    assert meta["authorization_servers"]


def test_authorization_server_supports_pkce_dcr_and_cimd(oauth_server):
    meta = httpx.get(f"{oauth_server}/.well-known/oauth-authorization-server").json()
    assert "S256" in meta["code_challenge_methods_supported"]
    assert meta.get("registration_endpoint")
    assert meta.get("client_id_metadata_document_supported") is True
    assert "none" in meta["token_endpoint_auth_methods_supported"]


def test_agent_key_still_works_next_to_oauth(oauth_server):
    import asyncio
    from fastmcp import Client
    apikeys = sys.modules["apikeys"]
    key, _ = apikeys.create("alice@example.com", name="hermes", scopes="all")

    async def go():
        async with Client(f"{oauth_server}/mcp", auth=key) as client:
            return {t.name for t in await client.list_tools()}

    assert "memory_search" in asyncio.run(go())
