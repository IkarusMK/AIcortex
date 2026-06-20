"""LLMConnector — MCP server.

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
import mqtt_tools
import ftp_tools
import mcp_gateway
import coordination
import secrets_store
import guide

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
        print(f"[LLMConnector] WARNING: disk client_storage unavailable ({exc}); using default")
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
        # We MUST still send a `scope` to the IdP's /authorize endpoint, though.
        # Without it, some providers (e.g. Pocket ID) hand `scope=null` to their
        # web UI, which then crashes ("null is not an object — n.scope.includes")
        # and the login spinner hangs forever — the authorize request is never
        # sent. extra_authorize_params injects the scope ONLY into the upstream
        # authorize request, so it stays out of MCP token validation (unlike
        # required_scopes). The IdP must support these scopes.
        extra_authorize_params={
            "scope": os.environ.get("OIDC_SCOPE", "openid profile email"),
        },
    )
    storage = _client_storage()
    if storage is not None:
        kwargs["client_storage"] = storage
    return OIDCProxy(**kwargs)


auth = _build_auth()
# `instructions` are sent to the client on connect — a fresh LLM immediately
# learns what this connector is and how to use it.
mcp = FastMCP("LLMConnector", auth=auth, instructions=guide.GUIDE)


@mcp.tool
def ping(name: str = "world") -> str:
    """Health check — confirms the connector is reachable."""
    return f"Hello {name}, your NAS MCP server is alive! 🎉"


# Memory tools: write / read / list / search / delete (file-based under MEMORY_DIR)
memory.register(mcp)

# Skill router: search / list / load / resource / write (folder-based under SKILLS_DIR)
skills.register(mcp)

# Generic service caller: call_service / service_add / service_list (HTTP integrations as data)
services.register(mcp)

# Generic MQTT dispatcher: mqtt_add / mqtt_list / mqtt_publish / mqtt_get (MQTT devices as data)
mqtt_tools.register(mcp)

# Generic FTP/FTPS transfer: ftp_add / ftp_list_endpoints / ftp_list / ftp_upload (files as data)
ftp_tools.register(mcp)

# MCP gateway: mcp_add / mcp_list / mcp_tools / mcp_call (other MCP servers as data)
mcp_gateway.register(mcp)

# Multi-agent coordination: inbox_* / task_* / agent_* (shared inbox, tasks, registry)
coordination.register(mcp)

# Encrypted secret vault: secret_set / secret_list / secret_delete (dynamic secrets)
secrets_store.register(mcp)

# Self-describing usage guide (also sent as server `instructions` on connect)
guide.register(mcp)


if __name__ == "__main__":
    # Fail closed: without OIDC the server has no auth. Rather than silently
    # listen on 0.0.0.0 (an accidental port-forward would expose every tool),
    # bind to localhost only — unless the operator explicitly opts in with
    # ALLOW_INSECURE=1. With OIDC configured, bind as configured (HOST).
    bind_host = HOST
    if auth is None:
        if os.environ.get("ALLOW_INSECURE") == "1":
            print(f"[LLMConnector] WARNING: no OIDC — running OPEN (no auth) on "
                  f"{bind_host}:{PORT} because ALLOW_INSECURE=1. Do NOT expose this publicly.")
        else:
            bind_host = "127.0.0.1"
            print(f"[LLMConnector] No OIDC configured → binding to 127.0.0.1:{PORT} "
                  f"(local only). Set OIDC_* for real auth, or ALLOW_INSECURE=1 to force "
                  f"an open bind (not recommended).")
    else:
        print(f"[LLMConnector] auth: OIDC proxy — binding {bind_host}:{PORT}")
    # Streamable-HTTP transport — what Claude custom connectors speak.
    # Endpoint: http://HOST:PORT/mcp
    mcp.run(transport="http", host=bind_host, port=PORT)
