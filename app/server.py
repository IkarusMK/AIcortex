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


def _host_guard():
    """Allow-list for FastMCP's Host/Origin DNS-rebinding guard.

    fastmcp 3.4.3 enforces Host/Origin validation on the HTTP transport. Behind a
    reverse proxy the public domain must be allow-listed, or every proxied request
    (incl. the OAuth discovery/registration) is rejected with 421 Misdirected
    Request. Derived from BASE_URL — the public URL already required for OIDC — so
    it can't drift; localhost/127.0.0.1 stay allowed by the guard's own defaults,
    and MCP_ALLOWED_HOSTS (comma-separated) can add more for edge setups. Returns
    (None, None) when no public URL is set, leaving the safe defaults untouched.
    """
    from urllib.parse import urlparse

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
    for extra in os.environ.get("MCP_ALLOWED_HOSTS", "").split(","):
        if extra.strip():
            hosts.append(extra.strip())
    hosts = list(dict.fromkeys(hosts))  # de-dup, preserve order
    origins = list(dict.fromkeys(origins))
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

    # Optionally ALSO accept a static runner token for headless machine clients
    # (the autonomy runner, any LLM). Interactive apps keep using OIDC unchanged;
    # MultiAuth just adds the token as a second accepted credential. Backward
    # compatible: with no RUNNER_TOKEN set, this is plain OIDC as before.
    runner_token = os.environ.get("RUNNER_TOKEN")
    if runner_token:
        try:
            from fastmcp.server.auth import MultiAuth
            try:
                from fastmcp.server.auth import StaticTokenVerifier
            except Exception:
                from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
            verifier = StaticTokenVerifier(
                tokens={runner_token: {"client_id": "runner", "scopes": []}}
            )
            print("[AICortex] auth: OIDC + static runner token (MultiAuth)")
            return MultiAuth(server=oidc, verifiers=[verifier])
        except Exception as exc:
            print(f"[AICortex] WARNING: RUNNER_TOKEN set but MultiAuth unavailable "
                  f"({exc}); falling back to OIDC only")
    return oidc


auth = _build_auth()
# `instructions` are sent to the client on connect — a fresh LLM immediately
# learns what this connector is and how to use it.
mcp = FastMCP("AICortex", auth=auth, instructions=guide.GUIDE)


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
    # Fail closed: without OIDC the server has no auth. Rather than silently
    # listen on 0.0.0.0 (an accidental port-forward would expose every tool),
    # bind to localhost only — unless the operator explicitly opts in with
    # ALLOW_INSECURE=1. With OIDC configured, bind as configured (HOST).
    bind_host = HOST
    if auth is None:
        if os.environ.get("ALLOW_INSECURE") == "1":
            print(f"[AICortex] WARNING: no OIDC — running OPEN (no auth) on "
                  f"{bind_host}:{PORT} because ALLOW_INSECURE=1. Do NOT expose this publicly.")
        else:
            bind_host = "127.0.0.1"
            print(f"[AICortex] No OIDC configured → binding to 127.0.0.1:{PORT} "
                  f"(local only). Set OIDC_* for real auth, or ALLOW_INSECURE=1 to force "
                  f"an open bind (not recommended).")
    else:
        print(f"[AICortex] auth: OIDC proxy — binding {bind_host}:{PORT}")
    # Streamable-HTTP transport — what MCP custom connectors speak.
    # Endpoint: http://HOST:PORT/mcp
    allowed_hosts, allowed_origins = _host_guard()
    if allowed_hosts:
        print(f"[AICortex] host guard: also allowing {allowed_hosts}")
    mcp.run(
        transport="http", host=bind_host, port=PORT,
        allowed_hosts=allowed_hosts, allowed_origins=allowed_origins,
    )
