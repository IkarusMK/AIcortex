"""AICortex — MCP server.

A self-hosted MCP server you add to any MCP-capable LLM app as a custom connector.

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

import version
import cfgstore
import memory
import skills
import services
import mqtt_tools
import ftp_tools
import mcp_gateway
import coordination
import cron
import webhook_tools
import sessions
import print_tools
import scan_tools
import webdav_tools
import caldav_tools
import fs_tools
import ssh_tools
import mail_tools
import imap_tools
import secrets_store
import guide
import bootstrap
import learn
import authz
import tenancy
import apikeys
import rest_api
import webui

MEMORY_DIR = os.environ.get("MEMORY_DIR", "/data/memory")
SKILLS_DIR = os.environ.get("SKILLS_DIR", "/data/skills")
HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "8787"))


def _csv_env(name: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


def _host_guard():
    """Host/Origin allow-lists for fastmcp's DNS-rebinding guard.

    Default (None, None): fastmcp's "auto" mode, which checks Host/Origin only for
    a server bound to loopback — the case DNS rebinding actually threatens. On the
    NAS every request needs a token or an OAuth login anyway, and a fixed list
    turned away LAN addresses, Docker neighbours and VPN names with 421.

    Strict opt-in: set MCP_ALLOWED_HOSTS (and optionally MCP_ALLOWED_ORIGINS),
    comma-separated; the BASE_URL host/origin is added automatically.
    """
    from urllib.parse import urlparse

    extra_hosts = _csv_env("MCP_ALLOWED_HOSTS")
    extra_origins = _csv_env("MCP_ALLOWED_ORIGINS")
    if not (extra_hosts or extra_origins):
        return None, None
    hosts: list[str] = []
    origins: list[str] = []
    base = os.environ.get("BASE_URL", "")
    parsed = urlparse(base) if base else None
    if parsed and parsed.netloc:
        netloc = parsed.netloc.split("@")[-1]  # drop any userinfo
        host_only = netloc.split(":")[0]
        scheme = parsed.scheme or "https"
        hosts += [netloc, host_only, f"{host_only}:*"]
        origins += [f"{scheme}://{netloc}", f"{scheme}://{host_only}", f"{scheme}://{host_only}:*"]
    hosts = list(dict.fromkeys(hosts + extra_hosts))  # de-dup, preserve order
    origins = list(dict.fromkeys(origins + extra_origins))
    return (hosts or None, origins or None)


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
        print(f"[AICortex] WARNING: disk client_storage unavailable ({exc}); using default")
        return None


def _build_auth():
    """Who may connect to /mcp.

    - OIDC configured: OAuth for interactive apps (claude.ai, ChatGPT, ...) PLUS
      static bearer tokens (per-agent ``ak_`` keys, RUNNER_TOKEN) via MultiAuth.
    - No OIDC: bearer tokens only — header-only agents still work.
    - ALLOW_INSECURE=1 without OIDC: no auth at all (local development only).
    """
    import agent_auth

    config_url = os.environ.get("OIDC_CONFIG_URL")
    client_id = os.environ.get("OIDC_CLIENT_ID")
    if not (config_url and client_id):
        if os.environ.get("ALLOW_INSECURE") == "1":
            return None
        print("[AICortex] auth: bearer tokens only (agent keys / RUNNER_TOKEN), no OIDC")
        return agent_auth.build(base_url=os.environ.get("BASE_URL") or None)

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
    # fastmcp ≥3.4 rebuilt the authorize flow with two behaviours that regress
    # IdPs without RFC 8707 support: the `resource` parameter MCP clients send is
    # now FORWARDED to the upstream authorize request — Pocket ID rejects it with
    # `invalid_request — "The 'resource' or 'scope' parameter is invalid"` and the
    # connector login dies at the IdP — and a /consent interstitial appears before
    # the IdP. Restore the proven behaviour: never send `resource` upstream, and
    # let the IdP login itself be the consent step ("external"). Signature-checked
    # so a fastmcp without these kwargs can never break boot.
    import inspect
    proxy_params = inspect.signature(OIDCProxy.__init__).parameters
    if "forward_resource" in proxy_params:
        kwargs["forward_resource"] = False
    if "require_authorization_consent" in proxy_params:
        kwargs["require_authorization_consent"] = "external"
    storage = _client_storage()
    if storage is not None:
        kwargs["client_storage"] = storage
    # Use the PocketID-aware proxy so the upstream identity (sub/groups) is
    # forwarded under the token's `upstream_claims` (enables per-person roles).
    # Fail-safe: if the subclass can't be built, fall back to the stock proxy so
    # the login path is never at risk.
    try:
        import pocketid_proxy
        oidc = pocketid_proxy.build_proxy(**kwargs)
        print("[AICortex] auth: OIDC proxy with upstream-claim forwarding")
    except Exception as exc:
        print(f"[AICortex] PocketIDProxy unavailable ({exc}); using stock OIDCProxy")
        oidc = OIDCProxy(**kwargs)

    # ALSO accept static bearer tokens for headless clients (per-agent keys and
    # RUNNER_TOKEN). Interactive apps keep using OIDC unchanged; MultiAuth tries
    # the OAuth proxy first and the token verifier second.
    from fastmcp.server.auth import MultiAuth
    print("[AICortex] auth: OIDC + agent keys / runner token (MultiAuth)")
    return MultiAuth(server=oidc, verifiers=[agent_auth.build(base_url=kwargs["base_url"])])


auth = _build_auth()
# `instructions` are sent to the client on connect — a fresh LLM immediately
# learns what this connector is and how to use it.
mcp = FastMCP("AICortex", auth=auth, instructions=guide.INSTRUCTIONS)


# Authorization (Welle 3): central, FAIL-OPEN policy gate. OFF unless
# AUTH_ENFORCE=1; when on, the RUNNER_TOKEN defaults to a non-admin role so
# admin-only tools (service_add/mcp_add/cron_add/secret_set/…) are blocked for it,
# while an interactive OIDC operator stays admin. Added first so it gates before
# any other middleware. Guarded so a middleware issue can never stop boot.
try:
    _authz_mw = authz.build_middleware()
    if _authz_mw is not None:
        mcp.add_middleware(_authz_mw)
        print(f"[AICortex] authorization: policy gate active "
              f"(AUTH_ENFORCE={'on' if authz.enabled() else 'off'})")
    else:
        print("[AICortex] authorization: middleware unavailable — not enforced")
except Exception as exc:
    print(f"[AICortex] authorization: middleware skipped ({exc}) — not enforced")


# Records which arguments a client actually passed, so *_add updates can tell
# "left out" from "set to empty" (see cfgstore.write_merged).
mcp.add_middleware(cfgstore.build_middleware())


# Auto-Memory (Tier B): a single, central, FAIL-OPEN middleware that stages
# memory candidates from durable tool calls. Guarded so a middleware API mismatch
# can never stop the server from booting — on any failure we simply run without
# auto-capture (Tier A in-session learning still works). On by default
# (LEARN_AUTOCAPTURE=0 to disable); see learn.py.
try:
    _learn_mw = learn.build_middleware()
    if _learn_mw is not None:
        mcp.add_middleware(_learn_mw)
        print("[AICortex] auto-memory: candidate-capture middleware active "
              f"(LEARN_AUTOCAPTURE={'on' if learn._enabled() else 'off'})")
    else:
        print("[AICortex] auto-memory: middleware unavailable — Tier A only")
except Exception as exc:
    print(f"[AICortex] auto-memory: middleware skipped ({exc}) — Tier A only")


# 'START HERE' entrypoint — registered FIRST so it leads the tool list. Its
# description tells a fresh LLM to call it before anything else; one call returns
# the guide + a live catalog of the whole brain. This is the reliable trigger
# that makes any client (phone, desktop) load the brain instead of starting blank.
bootstrap.register(mcp)


@mcp.tool
def ping(name: str = "world") -> str:
    """Health check — confirms the connector is reachable and reports the running version."""
    return f"Hello {name}, your NAS MCP server is alive! 🎉 (AICortex v{version.__version__})"


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

# Scheduled jobs as data: cron_add / cron_list / cron_delete / cron_due / cron_mark_run
cron.register(mcp)

# Webhooks: inbound receiver (POST /hooks/<name> → inbox) + outbound webhook_send
webhook_tools.register(mcp)

# Cross-LLM session handoff: session_save / session_list / session_load / session_delete / session_prune
sessions.register(mcp)

# IPP printing: print_add / print_list / print_delete / print_document (printers as data)
print_tools.register(mcp)

# eSCL scanning: scan_add / scan_list / scan_delete / scan_document (scanners as data, → Paperless)
scan_tools.register(mcp)

# WebDAV transfer: webdav_add / list / upload / download / mkdir / delete (cloud drives as data, e.g. Nextcloud)
webdav_tools.register(mcp)

# CalDAV: caldav_add / list_calendars / list_events / add_event (calendars as data, e.g. Nextcloud)
caldav_tools.register(mcp)

# Workspace files: fs_list / fs_read / fs_write / fs_move / fs_delete / fs_info (/data/work, sandboxed)
fs_tools.register(mcp)

# SSH: ssh_add / ssh_run / ssh_upload / ssh_download / ssh_list_dir (remote commands + SFTP, hosts as data)
ssh_tools.register(mcp)

# SMTP: mail_add / mail_list / mail_send (send email/notifications, accounts as data)
mail_tools.register(mcp)

# IMAP: imap_add / imap_list / imap_search / imap_fetch (read incoming email)
imap_tools.register(mcp)

# Encrypted secret vault: secret_set / secret_list / secret_delete (dynamic secrets)
secrets_store.register(mcp)

# Per-user data areas (multi-tenant control plane): tenancy_set / show / list /
# unset / status — admin tools to configure who sees which data (memory now,
# vault next). Enforcement of memory isolation lives in the authz middleware.
tenancy.register(mcp)

# REST API key control plane: apikey_create / apikey_list / apikey_revoke (admin-only)
apikeys.register(mcp)

# Native REST layer (routes served alongside /mcp, outside OAuth): GET /api/v1/tools,
# POST /api/v1/tools/<name>, GET /api/v1/openapi.json — authenticated per-user API key.
rest_api.register(mcp)

# Admin WebUI at /ui — browser OIDC login (same IdP), admin-only management of
# vault (names only), skills, and users. Static, self-contained, no build step.
webui.register(mcp)

# Self-describing usage guide (also sent as server `instructions` on connect)
guide.register(mcp)


if __name__ == "__main__":
    print(f"[AICortex] version {version.__version__} starting")
    # Native (libuv) event loop for faster async I/O — set the policy BEFORE any loop is
    # created. uvicorn also auto-detects uvloop; installing it here makes it explicit and
    # LOGS which loop is live so it's verifiable. Guarded: if uvloop is unavailable
    # (e.g. Windows), silently fall back to the default asyncio loop.
    try:
        import uvloop
        uvloop.install()
        print("[AICortex] event loop: uvloop (libuv) — accelerated async I/O")
    except Exception as exc:
        print(f"[AICortex] event loop: default asyncio (uvloop unavailable: {type(exc).__name__})")
    # Every request needs a credential (OAuth and/or bearer token, see _build_auth)
    # unless the operator explicitly runs open with ALLOW_INSECURE=1.
    bind_host = HOST
    if auth is None:
        print(f"[AICortex] WARNING: no auth — running OPEN on {bind_host}:{PORT} "
              f"because ALLOW_INSECURE=1. Do NOT expose this publicly.")
    else:
        print(f"[AICortex] auth active — binding {bind_host}:{PORT}")
    # Streamable-HTTP transport — what MCP custom connectors speak.
    # Endpoint: http://HOST:PORT/mcp
    allowed_hosts, allowed_origins = _host_guard()
    if allowed_hosts:
        print(f"[AICortex] host guard: also allowing {allowed_hosts}")
    mcp.run(
        transport="http", host=bind_host, port=PORT,
        allowed_hosts=allowed_hosts, allowed_origins=allowed_origins,
    )
