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
import sys

import httpx
import pytest

PUBLIC = "https://agent.example.net"

pytestmark = pytest.mark.oidc


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
