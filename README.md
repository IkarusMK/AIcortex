# LLMConnector

> Give Claude a Hermes-style agent home — on your own NAS.

Self-hosted [MCP](https://modelcontextprotocol.io) server that turns your NAS into a **personal Claude connector**. Like agent frameworks such as [Hermes](https://hermes-agent.nousresearch.com), it gives your assistant a persistent identity and real reach — but it runs in **your** network and plugs straight into the Claude apps you already use.

Add it once as a *custom connector* and Claude gains:

- 🧠 **Consistent memory** that lives on your NAS and follows you across every device
- 📱 **Work from anywhere** — the *same* brain on desktop **and** mobile, one account, one state
- 🗂️ **A skill router** — your skills live on your NAS; Claude *searches* them, loads the right one (progressive disclosure), and *learns* new ones at runtime (`skill_write`)
- 🛠️ **Tools as data** — register any HTTP API with `service_add`, call it via `call_service`; new integrations need no code and no redeploy
- 🔌 **Devices as data** — generic **MQTT** (`mqtt_*`) and **FTP/FTPS** (`ftp_*`) dispatchers bring non-HTTP devices (e.g. a Bambu Lab printer in LAN mode) in the same way — as data, no redeploy
- 🔐 **Encrypted secret vault** — store API keys/tokens through the connector (works from mobile); encrypted at rest, never shown back
- 🛡️ **Safe by default** — fail-closed auth, an enforced-encryption vault, and an SSRF egress guard (private/metadata IPs blocked unless you allow-list them)
- 🧭 **Self-describing** — any connecting LLM receives usage instructions + a `guide` tool, and is told to confirm before physical/outbound actions
- 🤝 **Multi-agent ready** — shared memory + registry so several agents can share one brain

The model stays in Anthropic's cloud. **Your data, skills, and secrets stay on your NAS.** Claude talks to this server over an HTTPS connector; the server uses your local credentials internally and never hands them to the model.

> ✅ **Status: working.** Memory, the skill router, HTTP/MQTT/FTP dispatchers, an encrypted secret vault, OAuth (via your own OIDC provider) and an SSRF egress guard are all live — and the connector is *self-describing*. **Don't expose it publicly without [Authentication](#authentication).**

## How it works

```
Claude app (desktop / mobile)  ·  one or many agents
        │  custom connector (HTTPS, from Anthropic's cloud)
        ▼
Reverse proxy (Zoraxy / Caddy / nginx / Traefik …)
        │
        ▼
LLMConnector  (this container, on your NAS)
        │  uses local files & secrets
        ▼
Memory  ·  Skills  ·  HTTP services  ·  MQTT & FTP devices  ·  Secret vault
       (every outbound call passes the SSRF egress guard)
```

## Capabilities (tools at a glance)

| Group | Tools | What it does |
|-------|-------|--------------|
| Health | `ping` | Connectivity check |
| Memory | `memory_write` · `memory_read` · `memory_list` · `memory_search` · `memory_delete` | Durable, scope-namespaced facts on the NAS |
| Skills | `skill_search` · `skill_list` · `skill_load` · `skill_resource` · `skill_write` | Searchable know-how; learn new skills at runtime |
| Services (HTTP) | `service_add` · `service_list` · `call_service` | Register & call any HTTP API as data |
| Devices (MQTT) | `mqtt_add` · `mqtt_list` · `mqtt_publish` · `mqtt_get` | Talk to MQTT devices (e.g. Bambu LAN) as data |
| Files (FTP/FTPS) | `ftp_add` · `ftp_list_endpoints` · `ftp_list` · `ftp_upload` | Up/list files over FTP/FTPS (e.g. send a print job) |
| MCP gateway | `mcp_add` · `mcp_list` · `mcp_tools` · `mcp_call` | Use other MCP servers' tools as data |
| Multi-agent | `inbox_post`/`read`/`ack` · `task_add`/`list`/`claim`/`update` · `agent_register`/`list` | Shared inbox, task board & agent registry |
| Scheduling | `cron_add` · `cron_list` · `cron_delete` · `cron_due` · `cron_mark_run` | Cron jobs as data; a NAS runner triggers them |
| Secrets | `secret_set` · `secret_list` · `secret_delete` | Encrypted vault; values never returned |
| Guide | `guide` | Self-description (also sent as server `instructions` on connect) |

New capabilities are added as **data** (a skill, a service config, a secret) — not code, no redeploy. Full CRUD: every config module also has a delete (`skill_delete`, `service_delete`, `mqtt_delete`, `ftp_delete`, `mcp_delete`, `task_delete`, `agent_remove`, `inbox_delete`, plus `memory_delete` / `secret_delete`), so anything you can register you can also remove via the connector.

## Project structure

```
LLMConnector/
├── app/                # Server code (FastMCP)
│   ├── server.py       #   entrypoint — wires auth + registers tool modules
│   ├── memory.py       #   memory tools
│   ├── skills.py       #   skill router
│   ├── services.py     #   generic allow-listed HTTP service caller
│   ├── mqtt_tools.py   #   generic MQTT dispatcher (devices as data)
│   ├── ftp_tools.py    #   generic FTP/FTPS transfer (e.g. send print jobs)
│   ├── netguard.py     #   SSRF egress guard (allow-list internal ranges)
│   ├── mcp_gateway.py  #   gateway to other MCP servers (servers as data)
│   ├── coordination.py #   multi-agent inbox / task board / agent registry
│   ├── secrets_store.py#   encrypted secret vault
│   ├── guide.py        #   self-describing usage guide (DE/EN)
│   └── requirements.txt
├── data/               # Persistent, human-readable state (git-ignored content)
│   ├── memory/         #   memory files — what Claude remembers about you
│   ├── skills/         #   skill library — <skill>/SKILL.md the router searches
│   ├── services/       #   HTTP service configs (integrations as data)
│   ├── mqtt/           #   MQTT broker/device configs
│   ├── ftp/            #   FTP/FTPS endpoint configs
│   ├── mcp/            #   upstream MCP server configs
│   ├── coordination/   #   multi-agent inbox / tasks / agents
│   ├── vault/          #   encrypted secrets (secret_set)
│   ├── auth/           #   OAuth client registrations (persisted)
│   └── work/           #   file workflows / scratch (CAD, exports, large files)
├── secrets/            # Local credentials (.env) — never leave the NAS
├── logs/               # Container logs
├── docs/               # Architecture & Claude project-instruction template
├── Dockerfile          # Baked image (deps installed at build time)
├── entrypoint.sh       # Drops privileges to PUID:PGID at runtime (gosu)
├── docker-compose.yml
└── .env.example
```

> **No `deps/` folder?** Correct — dependencies are baked **into the image** at build time, so there's no install-on-start volume. The `data/`, `logs/` and `secrets/` folders keep their structure via `.gitkeep`; their *contents* are git-ignored so nothing private is committed.

## Memory, skills & the skill router

This is the heart of the project — making Claude *itself* portable, not just chat.

- **Memory** lives as plain files under `data/memory`. Tools (`memory_read` / `memory_write` / `memory_list`) let Claude recall and update what it knows about you — the same on every device.
- **Skills** live as folders under `data/skills` (`<skill>/SKILL.md` + resources). The router tools — `skill_search` / `skill_load` / `skill_resource` — let Claude find the right skill for a request and pull in **only what it needs** (progressive disclosure, the same idea as tool search).
- **Wire it up once.** Add a short instruction to your Claude **Project** so the assistant always consults the router first — see [`docs/claude-project-instructions.md`](docs/claude-project-instructions.md). After that, "find the right skill / tool and apply it" just happens, from any device.

## Tools & integrations (as data)

New integrations don't need new code. A **service** is a small config you register
at runtime with `service_add` (stored under `data/services`); `call_service` then
reaches it — only registered services are allowed, and the auth token is injected
server-side from a stored secret (`token_env`, set via `secret_set` into the
encrypted vault or via `.env`), never stored in service data or returned to the
model. Pair a service with a `skill_write` skill that explains how
to use it, and a new capability is live **without a redeploy**.

## Multi-agent ready

The design lets several agents share one NAS brain without stepping on each other:

- **Namespaced memory** — memory is addressed by scope, so agents share common knowledge while keeping private notes (`shared` vs. per-agent).
- **Shared skill & tool registry** — every agent queries the same `skill_search` and tool set; add a capability once, all agents get it.
- **Per-agent workspaces** — isolated working directories under `data/work` for parallel tasks.
- **Agent inbox + task board** — `inbox_*` (append-only messages), `task_*` (claimable task board) and `agent_*` (registry) let several agents coordinate, à la Hermes.

Sub-agent *spawning* stays client-side (the model lives in the cloud); the connector is the shared coordination layer. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Requirements

- A NAS or server running **Docker** (Compose v2).
- A **reverse proxy** that serves the container over public HTTPS — Claude connects from Anthropic's cloud, so the endpoint must be reachable from the internet.
- A domain/subdomain pointing at your proxy.
- A **Claude plan** that supports custom connectors (Free is limited to one; Pro/Max/Team/Enterprise support more).

## Quick start

```bash
git clone git@github.com:IkarusMK/LLMConnector.git
cd LLMConnector
cp .env.example .env        # adjust PUID / PGID / HOST_PORT / TZ
docker compose up -d --build
```

The MCP endpoint is served at `http://<host>:8787/mcp`.

> **Prebuilt image (no local build):** a multi-arch image is published to GHCR by
> CI. In `docker-compose.yml`, comment out `build: .`, uncomment
> `image: ghcr.io/ikarusmk/llmconnector:latest`, then `docker compose pull && docker compose up -d`.
> (Make the GHCR package **public** once, under the repo's *Packages* settings, so the NAS can pull it without a token.)

### Expose it & add the connector

1. Point a subdomain (e.g. `agent.example.com`) at your reverse proxy.
2. Proxy that host to `http://<nas-ip>:8787` over HTTPS.
   - The upstream is **plain HTTP** — do *not* enable "TLS to upstream".
   - If your proxy uses geo-blocking, **allow Anthropic's region (US)** for this host, or the connector cannot reach you.
3. In the Claude app: **Settings → Connectors → Add custom connector** → URL `https://agent.example.com/mcp`.
4. Test: ask Claude to call the `ping` tool.

## Configuration

All config lives in `.env` (copy from `.env.example`):

| Variable    | Default | Description |
|-------------|---------|-------------|
| `HOST_PORT` | `8787`  | Host port the server is published on (the container always listens on `8787` internally) |
| `PUID`      | `1000`  | User ID the process runs as (file ownership) |
| `PGID`      | `1000`  | Group ID the process runs as |
| `TZ`        | `UTC`   | Container timezone |

## Authentication

Protect the connector with OAuth before you expose it. It uses **your own OIDC
identity provider** as the login backend — Pocket ID, Authentik, Keycloak, Auth0,
anything with standard OIDC discovery. FastMCP's OIDC proxy handles the MCP-side
OAuth 2.1 flow (Dynamic Client Registration + PKCE) that the Claude connector
speaks; your provider just does the actual login.

> ℹ️ **Don't** put browser/forward-auth (reverse-proxy SSO) in front of the
> `/mcp` endpoint — the Claude connector is a *machine* client and can't follow an
> interactive login redirect. Authentication must happen at the MCP layer, which
> is exactly what this does.

Enable it by setting these in `.env` (see `.env.example`):

| Variable | Example |
|----------|---------|
| `OIDC_CONFIG_URL` | `https://id.example.com/.well-known/openid-configuration` |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | from a client you register in your provider |
| `BASE_URL` | `https://agent.example.com` (this server's public URL) |
| `JWT_SIGNING_KEY` | `openssl rand -hex 32` |

Register the OAuth client in your provider with redirect URI
**`<BASE_URL>/auth/callback`**. Then (re-)add the custom connector in Claude — it
will send you through your provider's login. **When the OIDC variables are unset,
the server binds to `127.0.0.1` only** (local testing); set `ALLOW_INSECURE=1` to
force an open bind without auth (not recommended).

> ⚠️ **Authentication ≠ authorization.** Any account that can log in to your IdP
> gets **full** access to every tool and all data — there is no per-user/role
> check. Point this at a **single-user or dedicated** provider; don't reuse a
> shared family/company IdP without restricting which subjects may log in.

## Security

- **Auth fails closed.** Without OIDC the server binds to `127.0.0.1` only (override with `ALLOW_INSECURE=1`). With OIDC, **enable it before exposing the proxy** — anyone who reaches `/mcp` can otherwise call every tool.
- **SSRF guard.** `service_add` / `mqtt_add` / `ftp_add` are tools the *model* can call, so the list of registered targets is not a trust boundary by itself. Every outbound host is resolved and **private / loopback / link-local / cloud-metadata addresses are blocked** unless they fall inside `INTERNAL_ALLOW_CIDRS` (operator-only, not settable by the model). Set it to the LAN/VPN ranges you actually use — e.g. `192.168.178.0/24` — otherwise calls to your own devices are blocked too.
- **Encrypted vault, enforced.** Integration/device secrets go in the **vault** via `secret_set` (encrypted at rest in `data/vault`, referenced by name, never returned to the model, settable from mobile). `secret_set` **refuses to store plaintext** unless `STORAGE_ENCRYPTION_KEY` is set (or you opt in with `ALLOW_PLAINTEXT_VAULT=1`). `.env` is only for bootstrap config (OIDC secret, `JWT_SIGNING_KEY`) — don't ask the assistant to edit it for integration secrets.
- **Forwarded headers.** `FORWARDED_ALLOW_IPS` defaults to `*` (fine on an isolated Docker network). If the container is directly reachable, scope it to your proxy's source IP/subnet.
- `.env` and `data/` contents are git-ignored — never commit secrets.

## Deploying on a VPS or over a VPN

The connector is just an HTTPS MCP endpoint, so it runs anywhere Docker does — your NAS, a VPS, or a cloud box — and reaches devices over whatever network you give it. The SSRF guard makes that safe:

- **On a VPS (public APIs only):** same reverse proxy + OIDC, set `BASE_URL` to its public URL, and leave `INTERNAL_ALLOW_CIDRS` **empty** so it can only reach public services.
- **Reach home devices from a VPS:** link the VPS and your LAN with a VPN (WireGuard, Tailscale, …) and add the **VPN/remote subnet** to `INTERNAL_ALLOW_CIDRS` (e.g. `100.64.0.0/10` for Tailscale, or your WireGuard range). The guard then permits exactly those hosts and nothing else.
- **Multi-site / enterprise:** scope `FORWARDED_ALLOW_IPS` to the proxy, list only trusted subnets in `INTERNAL_ALLOW_CIDRS`, and use a dedicated OIDC client (see the authorization note above).

## Troubleshooting

Hard-won notes from running this behind a reverse proxy with an OIDC provider.
Enable verbose logs to see the real reason for an auth failure:

```yaml
# docker-compose.yml → environment:
FASTMCP_LOG_LEVEL: "DEBUG"
```

| Symptom | Cause & fix |
|---------|-------------|
| Login succeeds, but Claude says *"returned an error when connecting"*; logs show `Token verified successfully` then **`Token missing required scopes`** | The proxy-issued MCP token doesn't carry the upstream OIDC scopes as claims. **Don't set `required_scopes`** — a successful login is enough. (Already removed in `server.py`.) |
| Logs show `Issued new FastMCP tokens` immediately followed by **`Bearer token rejected`** (401 `invalid_token`) | Behind a TLS-terminating proxy, uvicorn ignored `X-Forwarded-Proto`, so the server computed an `http://` URL and rejected its own `https`-audience tokens. Set **`FORWARDED_ALLOW_IPS: "*"`** (already in `docker-compose.yml`). |
| Log warns **`disk client_storage unavailable (Fernet key must be 32 url-safe base64-encoded bytes)`** | `STORAGE_ENCRYPTION_KEY` isn't a valid Fernet key (it's **not** the same as `JWT_SIGNING_KEY`). Generate: `python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"`. Or omit it for an unencrypted (still persistent) store. |
| Worked once, then `Bearer token rejected` for an **old client id** after recreating the container | The OAuth client store was ephemeral and got wiped. Persistent `data/auth` (this repo) fixes it. To clear a stuck client on Claude's side: remove the connector, fully quit & reopen the app, re-add. |
| Connector can't connect at all; proxy returns a login **web page** | You put reverse-proxy SSO / forward-auth in front of `/mcp`. A machine client can't do interactive login — **remove it**; auth belongs at the MCP layer (this server). |
| OIDC provider's consent/"Sign in" button spins forever, no `POST …/authorize` ever reaches the IdP; browser console shows `null is not an object (… scope.includes)` | The upstream `/authorize` request carried **no `scope`** (some IdP UIs, e.g. Pocket ID, crash on `scope=null`). Send one **without** re-introducing token-scope validation: `extra_authorize_params={"scope": "openid profile email"}` (already in `server.py`; override via `OIDC_SCOPE`). |
| `call_service` / `mqtt` / `ftp` to a **local device** returns *"Blocked by network policy"* | The SSRF guard blocks private IPs by default. Add the device's range to **`INTERNAL_ALLOW_CIDRS`** (e.g. `192.168.178.0/24`) and restart. |

## Roadmap

- [x] Walking skeleton: `ping` tool + remote MCP over HTTPS
- [x] Authentication: OAuth 2.1 via your own OIDC provider (Pocket ID, Authentik, Keycloak, Auth0, …)
- [x] Memory tools (`memory_write` / `memory_read` / `memory_list` / `memory_search` / `memory_delete`), scope-namespaced for multi-agent
- [x] Skill router (`skill_search` / `skill_list` / `skill_load` / `skill_resource` / `skill_write`)
- [x] Generic service caller (`call_service` / `service_add` / `service_list`) — integrations as data + skills, no redeploy
- [x] Encrypted secret vault (`secret_set` / `secret_list` / `secret_delete`) — set secrets via the connector; values encrypted at rest, never returned
- [x] Self-describing: server `instructions` on connect + a `guide` tool, so any LLM immediately knows what the connector is and how to use it
- [x] Generic device dispatchers — **MQTT** (`mqtt_*`) and **FTP/FTPS** (`ftp_*`), so non-HTTP devices (e.g. Bambu Lab LAN) are data too
- [x] Hardening — fail-closed auth, enforced-encryption vault, SSRF egress guard (`INTERNAL_ALLOW_CIDRS`); VPS/VPN-friendly
- [x] MCP gateway — connect to other MCP servers as data (`mcp_add` / `mcp_list` / `mcp_tools` / `mcp_call`)
- [ ] Bundled service configs & skills (Home Assistant, Mealie, …)
- [x] Multi-agent coordination: shared inbox, task board & agent registry (`inbox_*` / `task_*` / `agent_*`) — sub-agent *spawning* stays client-side
- [x] Prebuilt image on GHCR — multi-arch (amd64/arm64) build & push via GitHub Actions

## License

[MIT](LICENSE) © 2026 IkarusMK
