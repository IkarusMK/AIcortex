"""ClaudeNasConnector — MCP server.

A self-hosted MCP server you add to the Claude apps as a custom connector.

Authentication is optional and turns on automatically when the OIDC_* environment
variables are set: the server then acts as an OAuth 2.1 resource server via
FastMCP's OIDC proxy, using your own identity provider (e.g. Pocket ID, Authentik,
Keycloak, Auth0) as the login backend. Without those variables it runs OPEN
(fine for local testing — never expose an unauthenticated server publicly).

OAuth client registrations are persisted to an on-disk store (AUTH_STORE_DIR,
optionally encrypted with STORAGE_ENCRYPTION_KEY) so they survive container
restarts — on Linux the default store is ephemeral, which breaks reconnects.
"""
import os

from fastmcp import FastMCP

import memory
import skills
import services
import secrets_store

MEMORY_DIR = os.environ.get("MEMORY_DIR", "/data/memory")
SKILLS_DIR = os.environ.get("SKILLS_DIR", "/data/skills")
HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "8787"))


def _client_storage():
    """Persistent (optionally encrypted) disk store for OAuth client
    registrations, so they survive container restarts."""
    auth_dir = os.environ.get("AUTH_STORE_DIR", "/data/auth")
    try:
        from key_value.aio.stores.disk import DiskStore

        store = DiskStore(directory=auth_dir)
        enc_key = os.environ.get("STORAGE_ENCRYPTION_KEY")
        if enc_key:
            from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
            from cryptography.fernet import Fernet

            store = FernetEncryptionWrapper(key_value=store, fernet=Fernet(enc_key))
        return store
    except Exception as exc:  # fall back to the (ephemeral) default
        print(f"[ClaudeNasConnector] WARNING: disk client_storage unavailable ({exc}); using default")
        return None


def _build_auth():
    """Enable OAuth (OIDC proxy) when OIDC_CONFIG_URL + OIDC_CLIENT_ID are set."""
    config_url = os.environ.get("OIDC_CONFIG_URL")
    client_id = os.environ.get("OIDC_CLIENT_ID")
    if not (config_url and client_id):
        return None

    from fastmcp.server.auth.oidc_proxy import OIDCProxy

    kwargs = dict(
        config_url=config_url,
        client_id=client_id,
        client_secret=os.environ.get("OIDC_CLIENT_SECRET"),
        base_url=os.environ.get("BASE_URL", f"http://localhost:{PORT}"),
        # No required_scopes: the proxy-issued MCP token doesn't carry the
        # upstream OIDC scopes as claims, so requiring them rejects valid tokens.
        # A successful login through the provider is sufficient authorization.
        jwt_signing_key=os.environ.get("JWT_SIGNING_KEY"),
    )
    storage = _client_storage()
    if storage is not None:
        kwargs["client_storage"] = storage
    return OIDCProxy(**kwargs)


auth = _build_auth()
mcp = FastMCP("ClaudeNasConnector", auth=auth)


@mcp.tool
def ping(name: str = "world") -> str:
    """Health check — confirms the connector is reachable."""
    return f"Hello {name}, your NAS MCP server is alive! 🎉"


# Memory tools: write / read / list / search / delete (file-based under MEMORY_DIR)
memory.register(mcp)

# Skill router: search / list / load / resource / write (folder-based under SKILLS_DIR)
skills.register(mcp)

# Generic service caller: call_service / service_add / service_list (integrations as data)
services.register(mcp)

# Encrypted secret vault: secret_set / secret_list / secret_delete (dynamic secrets)
secrets_store.register(mcp)


if __name__ == "__main__":
    print(f"[ClaudeNasConnector] auth: {'OIDC proxy' if auth else 'OPEN (no auth)'}")
    # Streamable-HTTP transport — what Claude custom connectors speak.
    # Endpoint: http://HOST:PORT/mcp
    mcp.run(transport="http", host=HOST, port=PORT)
