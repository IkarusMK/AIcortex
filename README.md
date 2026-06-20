# LLMConnector

> Give Claude a Hermes-style agent home — on your own NAS.

Self-hosted [MCP](https://modelcontextprotocol.io) server that turns your NAS into a **personal Claude connector**. Like agent frameworks such as [Hermes](https://hermes-agent.nousresearch.com), it gives your assistant a persistent identity and real reach — but it runs in **your** network and plugs straight into the Claude apps you already use.

Add it once as a *custom connector* and Claude gains:

- 🧠 **Consistent memory** that lives on your NAS and follows you across every device
- 📱 **Work from anywhere** — the *same* brain on desktop **and** mobile, one account, one state
- 🗂️ **A skill router** — your skills live on your NAS; Claude *searches* them, loads the right one (progressive disclosure), and *learns* new ones at runtime (`skill_write`)
- 🛠️ **Tools as data** — register any API with `service_add` and call it via `call_service`; new integrations need no code and no redeploy
- 🔐 **Encrypted secret vault** — store API keys/tokens through the connector (works from mobile); encrypted at rest, never shown back
- 🧭 **Self-describing** — any connecting LLM receives usage instructions + a `guide` tool, and is told to confirm before physical/outbound actions
- 🤝 **Multi-agent ready** — shared memory + registry so several agents can share one brain

The model stays in Anthropic's cloud. **Your data, skills, and secrets stay on your NAS.** Claude talks to this server over an HTTPS connector; the server uses your local credentials internally and never hands them to the model.

> ✅ **Status: working.** Memory, the skill router, the generic service caller, an encrypted secret vault, and OAuth (via your own OIDC provider) are all live — and the connector is *self-describing*. **Don't expose it publicly without [Authentication](#authentication).**

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
Memory  ·  Skills (searchable)  ·  Services & APIs  ·  Secret vault
```

## Capabilities (tools at a glance)

| Group | Tools | What it does |
|-------|-------|--------------|
| Health | `ping` | Connectivity check |
| Memory | `memory_write` · `memory_read` · `memory_list` · `memory_search` · `memory_delete` | Durable, scope-namespaced facts on the NAS |
| Skills | `skill_search` · `skill_list` · `skill_load` · `skill_resource` · `skill_write` | Searchable know-how; learn new skills at runtime |
| Services | `service_add` · `service_list` · `call_service` | Register & call any API as data |
| Secrets | `secret_set` · `secret_list` · `secret_delete` | Encrypted vault; values never returned |
| Guide | `guide` | Self-description (also sent as server `instructions` on connect) |

New capabilities are added as **data** (a skill, a service config, a secret) — not code, no redeploy.

## Project structure

```
LLMConnector/
├── app/                # Server code (FastMCP)
│   ├── server.py       #   entrypoint — wires auth + registers tool modules
│   ├── memory.py       #   memory tools
│   ├── skills.py       #   skill router
│   ├── services.py     #   generic allow-listed service caller
│   ├── secrets_store.py#   encrypted secret vault
│   ├── guide.py        #   self-describing usage guide (DE/EN)
│   └── requirements.txt
├── data/               # Persistent, human-readable state (git-ignored content)
│   ├── memory/         #   memory files — what Claude remembers about you
│   ├── skills/         #   skill library — <skill>/SKILL.md the router searches
│   ├── services/       #   service configs (integrations as data)
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
- **(Planned) agent inbox** — an append-only channel for agent-to-agent and agent-to-you messages, à la Hermes.

Full orchestration (spawning/coordinating sub-agents) is on the roadmap; the seams above are in place so it can land **without a rewrite**. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

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
will send you through your provider's login. When the OIDC variables are unset the
server runs open (local testing only).

## Security

- This server is reachable from the public internet via your proxy. **Enable [Authentication](#authentication) before exposing it** — anyone who reaches the endpoint can otherwise call its tools.
- Keep all real credentials on your NAS. For integration/device secrets (API tokens, device passwords) use the **encrypted vault** via `secret_set` — encrypted at rest in `data/vault`, referenced by name (`token_env` / `password_env`), **never returned to the model**, and settable from mobile. `.env` is only for server *bootstrap* config that must exist before the vault loads (e.g. the OIDC client secret, `JWT_SIGNING_KEY`). Don't ask the assistant to edit `.env` for integration secrets — that's what the vault is for.
- `.env` and `data/` contents are git-ignored — never commit secrets.

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

## Roadmap

- [x] Walking skeleton: `ping` tool + remote MCP over HTTPS
- [x] Authentication: OAuth 2.1 via your own OIDC provider (Pocket ID, Authentik, Keycloak, Auth0, …)
- [x] Memory tools (`memory_write` / `memory_read` / `memory_list` / `memory_search` / `memory_delete`), scope-namespaced for multi-agent
- [x] Skill router (`skill_search` / `skill_list` / `skill_load` / `skill_resource` / `skill_write`)
- [x] Generic service caller (`call_service` / `service_add` / `service_list`) — integrations as data + skills, no redeploy
- [x] Encrypted secret vault (`secret_set` / `secret_list` / `secret_delete`) — set secrets via the connector; values encrypted at rest, never returned
- [x] Self-describing: server `instructions` on connect + a `guide` tool, so any LLM immediately knows what the connector is and how to use it
- [ ] Bundled service configs & skills (Home Assistant, Mealie, …)
- [ ] Multi-agent: agent inbox + sub-agent orchestration
- [ ] Prebuilt image on GHCR

## License

[MIT](LICENSE) © 2026 IkarusMK
