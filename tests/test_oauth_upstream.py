"""Guard the OAuth proxy's upstream behaviour (the Pocket ID login regression).

fastmcp ≥3.4 forwards the RFC 8707 `resource` parameter to the upstream IdP by
default; IdPs without Resource-Indicator support (Pocket ID) reject the authorize
request with `invalid_request` and the connector login dies. server.py disables
the forwarding via `forward_resource=False`. Against the OAuth confused deputy it
keeps FastMCP's consent page on (`require_authorization_consent="remember"`: asked
once per client, then remembered) and restricts client redirect URIs
(`allowed_client_redirect_uris`) — tests/test_sec_oauth_redirects.py covers those
end to end.

These tests pin that contract against the INSTALLED fastmcp, so a future version
bump that renames or breaks a knob fails CI instead of breaking logins in
production. Skipped when fastmcp isn't installed (the slim local test env).
"""
import pytest

fastmcp_auth = pytest.importorskip(
    "fastmcp.server.auth.oauth_proxy",
    reason="fastmcp not installed in this environment (CI installs it)")


def _proxy(**over):
    from fastmcp.server.auth.oauth_proxy import OAuthProxy
    try:
        from fastmcp.server.auth import StaticTokenVerifier
    except ImportError:
        from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
    kwargs = dict(
        upstream_authorization_endpoint="https://idp.example/authorize",
        upstream_token_endpoint="https://idp.example/token",
        upstream_client_id="upstream-client",
        upstream_client_secret="upstream-secret",
        base_url="https://connector.example",
        jwt_signing_key="0" * 64,
        token_verifier=StaticTokenVerifier(tokens={}),
    )
    kwargs.update(over)
    return OAuthProxy(**kwargs)


def _txn():
    return {
        "txn_id": "txn-test", "client_id": "mcp-client",
        "client_redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        "client_state": "s", "code_challenge": None,
        "code_challenge_method": "S256", "scopes": ["openid", "profile"],
        "created_at": 0.0, "resource": "https://connector.example/mcp",
        "proxy_code_verifier": None,
    }


def test_constructor_still_offers_both_knobs():
    """The exact kwargs server.py relies on must exist on the installed version."""
    import inspect
    from fastmcp.server.auth.oauth_proxy import OAuthProxy
    from fastmcp.server.auth.oidc_proxy import OIDCProxy
    for cls in (OAuthProxy, OIDCProxy):
        params = inspect.signature(cls.__init__).parameters
        assert "forward_resource" in params, cls.__name__
        assert "require_authorization_consent" in params, cls.__name__
        assert "allowed_client_redirect_uris" in params, cls.__name__


def test_forward_resource_false_strips_resource_from_upstream_url():
    proxy = _proxy(forward_resource=False)
    url = proxy._build_upstream_authorize_url("txn-test", _txn())
    assert url.startswith("https://idp.example/authorize?")
    assert "resource" not in url          # the Pocket ID killer stays out
    assert "scope=openid+profile" in url  # scopes still forwarded


def test_default_would_forward_resource():
    """Documents WHY the override exists: the default leaks `resource` upstream.
    If a future fastmcp flips the default to safe, this reminds us the override
    (and this guard) can be retired."""
    proxy = _proxy()
    url = proxy._build_upstream_authorize_url("txn-test", _txn())
    assert "resource=" in url


def _client_and_params():
    from mcp.server.auth.provider import AuthorizationParams
    from mcp.shared.auth import OAuthClientInformationFull
    from pydantic import AnyUrl

    client = OAuthClientInformationFull(
        client_id="mcp-client",
        redirect_uris=[AnyUrl("https://claude.ai/api/mcp/auth_callback")],
    )
    params = AuthorizationParams(
        state="s", scopes=["openid"],
        code_challenge="c" * 43, code_challenge_method="S256",
        redirect_uri=AnyUrl("https://claude.ai/api/mcp/auth_callback"),
        redirect_uri_provided_explicitly=True,
        resource="https://connector.example/mcp",
    )
    return client, params


@pytest.mark.asyncio
async def test_remember_consent_routes_through_the_consent_page():
    """With require_authorization_consent="remember" (server.py), authorize() sends the
    browser to the local /consent page first — the step that stops a client an
    attacker registered from receiving a person's code without them noticing."""
    proxy = _proxy(forward_resource=False, require_authorization_consent="remember")
    client, params = _client_and_params()
    url = await proxy.authorize(client, params)
    assert url.startswith("https://connector.example/consent?txn_id=")
    assert "idp.example" not in url


@pytest.mark.asyncio
async def test_external_consent_would_skip_the_consent_page():
    """Documents WHY "external" is no longer used: it goes straight to the IdP, so
    nothing in AICortex asks the person before their code leaves for the client."""
    proxy = _proxy(forward_resource=False, require_authorization_consent="external")
    client, params = _client_and_params()
    url = await proxy.authorize(client, params)
    assert url.startswith("https://idp.example/authorize?")
    assert "/consent" not in url
    assert "resource" not in url
