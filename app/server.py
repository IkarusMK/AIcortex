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
import profiles
import tool_hints
import memory
import playbooks
import skills
import services
import mqtt_tools
import ftp_tools
import mcp_gateway
import coordination
import cron
import runner_builtin
import runners
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


def http_middleware():
    """ASGI middleware for the HTTP app, outermost first:

    - HSTS (when BASE_URL is https) and nosniff on every response, including the
      refusals below (see secheaders.py).
    - the DNS-rebinding guard (see hostguard.py). fastmcp's own host/origin guard is
      off by default and only checks loopback-bound servers, so it never protected
      the NAS — this one does, without turning away native clients on LAN IPs,
      Docker names or VPN names.
    - per-client-IP rate limits on the auth endpoints and /hooks (see ratelimit.py),
      so the app is not left to an external CrowdSec alone.
    """
    from starlette.middleware import Middleware

    import hostguard
    import ratelimit
    import secheaders
    return [Middleware(secheaders.SecurityHeaders), Middleware(hostguard.HostGuard),
            Middleware(ratelimit.RateLimit)]


def uvicorn_config():
    """Extra uvicorn settings for the HTTP transport: no "server: uvicorn" banner
    telling every client which stack (and version range) answers.

    uvicorn reads FORWARDED_ALLOW_IPS itself. With "*" it takes the FIRST
    X-Forwarded-For entry as the client IP — the one a client can write — so the
    per-IP rate limits and the audit trail can be fooled. Warn, don't refuse: an
    existing install keeps starting."""
    if os.environ.get("FORWARDED_ALLOW_IPS", "").strip() == "*":
        print("[AICortex] WARNING: FORWARDED_ALLOW_IPS=\"*\" lets clients forge their IP "
              "(rate limits, audit). Set it to your proxy's source, e.g. "
              "127.0.0.1,172.16.0.0/12.")
    return {"server_header": False}


def _client_storage():
    """Persistent (optionally encrypted) file store for OAuth client registrations
    and tokens, so they survive container restarts. A pre-2.6.0 DiskStore (diskcache,
    unsafe pickle) is migrated into it once — see oauth_store.py."""
    auth_dir = os.environ.get("AUTH_STORE_DIR", "/data/auth")
    try:
        import oauth_store
        return oauth_store.build(auth_dir, os.environ.get("STORAGE_ENCRYPTION_KEY"))
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
    # fastmcp ≥3.4 FORWARDS the `resource` parameter MCP clients send to the upstream
    # authorize request — Pocket ID rejects it with `invalid_request — "The
    # 'resource' or 'scope' parameter is invalid"` and the connector login dies at
    # the IdP. Never send `resource` upstream. Signature-checked so a fastmcp
    # without the kwarg can never break boot.
    import inspect
    proxy_params = inspect.signature(OIDCProxy.__init__).parameters
    if "forward_resource" in proxy_params:
        kwargs["forward_resource"] = False
    # Confused deputy: /register is open Dynamic Client Registration, so anyone can
    # register a client. Two walls keep a person's sign-in from being sent to a
    # client an attacker controls:
    # 1. Redirect allow-list — only the callbacks of real MCP clients: the hosted
    #    apps, plus loopback callbacks of desktop/CLI apps. A loopback pattern without
    #    a port matches every port (RFC 8252 §7.3; fastmcp redirect_validation).
    #    MCP_REDIRECT_URIS adds patterns (comma-separated, e.g.
    #    "https://app.example.com/oauth/*"); the single value "*" restores "allow any"
    #    for an operator who knowingly wants it. A "*" among other entries is ignored.
    # 2. The consent page, shown once per client + redirect URI and then remembered
    #    ("remember"; cross-site navigations are always asked again). Until 2.5.0 it
    #    was off ("external"), so one click on a crafted link handed over the code.
    extra = [p.strip() for p in os.environ.get("MCP_REDIRECT_URIS", "").split(",") if p.strip()]
    if extra == ["*"]:
        print("[AICortex] WARNING: MCP_REDIRECT_URIS=* — OAuth clients may register ANY "
              "redirect URI")
        kwargs["allowed_client_redirect_uris"] = None
    else:
        kwargs["allowed_client_redirect_uris"] = [
            "https://claude.ai/api/mcp/auth_callback",
            "https://claude.com/api/mcp/auth_callback",
            "https://chatgpt.com/connector_platform_oauth_redirect",
            "http://localhost", "http://127.0.0.1", "http://[::1]",
            *(p for p in extra if p != "*"),
        ]
    if "require_authorization_consent" in proxy_params:
        kwargs["require_authorization_consent"] = "remember"
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

    # Interactive sign-in is on, and only operators are admin by default. An
    # upgrade keeps today's admins (everyone the policy names without a non-admin
    # role) as operators — once, before anyone signs in. See authz.migrate_operators.
    migrated = authz.migrate_operators()
    if migrated:
        print(f"[AICortex] auth: kept {len(migrated)} existing admin(s) as operators")

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


# Authorization: the central policy gate, on unless AUTH_ENFORCE=0. The
# RUNNER_TOKEN and agent keys get a non-admin role, so admin-only tools
# (service_add/mcp_add/secret_set/…) are blocked for them; an interactive login is
# admin only for an operator (policy.json "operators"), everyone else "user". With
# sign-in configured, an identity that can't be resolved gets no role at all.
# Added first so it gates before any other middleware. Guarded so a middleware
# issue can never stop boot.
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

# Tool profiles: "core" lists eleven tools (the router reaches the rest), "full" all.
mcp.add_middleware(profiles.build_middleware())

# Title + read-only / destructive / open-world hints for every tool (one rule set).
mcp.add_transform(tool_hints.ToolHints())


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
playbooks.register(mcp)

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
# Who carries the jobs out: runner_list (runners are set up in the WebUI)
runners.register(mcp)

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

# Router for the core profile: integration_search / integration_call reach every tool
profiles.register(mcp)


def main():
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
    if os.environ.get("MCP_ALLOWED_HOSTS"):
        print(f"[AICortex] host guard: strict — only {os.environ['MCP_ALLOWED_HOSTS']} "
              f"(+ BASE_URL host, localhost)")
    import ratelimit
    limits = ratelimit.describe(ratelimit.limits_from_env())
    print(f"[AICortex] rate limits per client IP: {limits}")
    # The runners tick once a minute and stay idle until one is switched on in the
    # WebUI (tab "Runners").
    runner_builtin.start(PORT)
    mcp.run(transport="http", host=bind_host, port=PORT, middleware=http_middleware(),
            uvicorn_config=uvicorn_config())


if __name__ == "__main__":
    main()
