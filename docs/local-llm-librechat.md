# Use AICortex with a local LLM (Ollama) via LibreChat

Drive AICortex from a **local model** through [LibreChat](https://www.librechat.ai) — a
self-hosted, multi-user chat UI with first-class **MCP** support (**per-person OAuth**)
and an **Agent Builder** for curating tools. The model runs locally via
[Ollama](https://ollama.com); AICortex provides the memory, skills, services and devices.
Everything stays on your hardware; cloud models are optional and opt-in.

## How it fits together

```
Browser ─▶ LibreChat (chat UI + MCP client, your NAS) ─▶ Ollama (local model)
                 │  MCP (Streamable-HTTP) + per-person OAuth
                 ▼
              AICortex ─▶ memory · skills · services · devices
```

LibreChat is the **MCP client** + chat surface; Ollama is the **model**; AICortex is the
**brain/tools**. (Ollama alone is *not* an MCP client — LibreChat is the missing middle.)

**No connector changes needed** — LibreChat connects to your existing AICortex over MCP
with **per-person OAuth** (the same OAuth 2.1 + dynamic client registration flow Claude.ai
uses, so nothing to pre-register). In enterprise mode (`AUTH_ENFORCE=1`), grant that
identity its areas like any user with `tenancy_set`.

> Prefer a *headless* client or a single static token instead of per-person OAuth? AICortex
> also accepts a static `RUNNER_TOKEN` (MultiAuth) — but OAuth is the cleaner, per-user path
> and what this guide uses.

## Prerequisites

- Docker (Compose v2) on your NAS/server.
- [Ollama](https://ollama.com) running with a **tool-capable** model pulled (recent
  Qwen/Llama-class; small <7B models struggle with multi-step tool use).
- A reachable AICortex endpoint (`https://<your-aicortex-host>/mcp`) with **OIDC** configured
  (per-person OAuth needs it).

## Setup

LibreChat is its **own** stack — never merge it into AICortex's `docker-compose.yml`. They
talk over the network like any client (AICortex stays isolated).

### 1 — Make Ollama reachable on the network

LibreChat runs in a container and must reach Ollama, which binds to `localhost` by default.
On the Ollama host:

- **macOS app:** `launchctl setenv OLLAMA_HOST 0.0.0.0`, then **quit + reopen** the Ollama app.
- **Linux service:** set `OLLAMA_HOST=0.0.0.0`.

Verify from another machine (use the host's LAN IP, not localhost):
```bash
curl http://<ollama-ip>:11434/v1/models     # should list your model
```
Give the Ollama host a static IP.

### 2 — Bring up the LibreChat stack

Create e.g. `docker/librechat/` with three files:

**`docker-compose.yml`**
```yaml
services:
  api:
    image: ghcr.io/danny-avila/librechat:latest
    container_name: librechat
    restart: unless-stopped
    ports:
      - "3080:3080"
    depends_on:
      - mongodb
    env_file:
      - .env
    volumes:
      - ./librechat.yaml:/app/librechat.yaml:ro
      - ./data/images:/app/client/public/images
      - ./data/uploads:/app/uploads
      - ./data/logs:/app/api/logs

  mongodb:
    container_name: librechat-mongodb
    image: mongo:7
    restart: unless-stopped
    environment:
      - MONGO_INITDB_ROOT_USERNAME=librechat
      - MONGO_INITDB_ROOT_PASSWORD=${MONGO_PASSWORD}
    volumes:
      - ./data/mongodb:/data/db      # DB stays internal — no ports mapping
```

**`.env`** — generate the secrets. Note `openssl rand -hex N` outputs **2·N hex chars**:
```bash
HOST=0.0.0.0
PORT=3080

# MongoDB with auth — put the SAME value in both places (the .env doesn't self-interpolate):
MONGO_PASSWORD=<openssl rand -hex 24>
MONGO_URI=mongodb://librechat:<same-value>@mongodb:27017/LibreChat?authSource=admin

# Security (required for startup):
CREDS_KEY=<openssl rand -hex 32>          # 64 hex chars
CREDS_IV=<openssl rand -hex 16>           # 32 hex chars
JWT_SECRET=<openssl rand -hex 32>         # 64 hex chars
JWT_REFRESH_SECRET=<openssl rand -hex 32> # 64 hex chars

# Auth / registration:
ALLOW_EMAIL_LOGIN=true
ALLOW_REGISTRATION=true                    # needed to create the first account
REGISTER_MAX=100                           # default is 5/hour — raise it for setup

# EXACTLY the URL you open LibreChat at — this drives the OAuth callback:
DOMAIN_CLIENT=http://<nas-ip>:3080
DOMAIN_SERVER=http://<nas-ip>:3080

SEARCH=false                               # Meilisearch off — one container less
# Generous windows for the OIDC login popup:
MCP_OAUTH_HANDLING_TIMEOUT=600000
MCP_OAUTH_FLOW_TTL=900000
```

**`librechat.yaml`**
```yaml
version: 1.2.8
cache: true
endpoints:
  custom:
    - name: "Ollama"
      apiKey: "ollama"
      baseURL: "http://<ollama-ip>:11434/v1"
      models:
        default: ["<your-model>"]
        fetch: true
      titleConvo: true
      titleModel: "current_model"
mcpServers:
  aicortex:
    type: streamable-http
    url: "https://<your-aicortex-host>/mcp"
    requiresOAuth: true
    timeout: 120000
    initTimeout: 30000
    serverInstructions: true    # AICortex's bootstrap rules land in the agent context
```

Then:
```bash
mkdir -p data/images data/uploads data/logs data/mongodb
docker compose up -d
docker compose logs -f api        # wait for: Server listening ... 3080
```

### 3 — Create your account

Open `http://<nas-ip>:3080`. The **login** page ("Welcome back") is normal — click **Sign
up** or go to **`/register`** (an incognito window avoids a stale session). The first account
is the admin. Afterwards set `ALLOW_REGISTRATION=false` and restart to close registration.

### 4 — Connect AICortex (per-person OAuth)

Pick a tool-capable model, then the `aicortex` MCP server appears in a dropdown below the
message box → select it → **Authenticate** (your OIDC/PocketID login opens). AICortex does
dynamic client registration, so the callback URL
`${DOMAIN_SERVER}/api/mcp/aicortex/oauth/callback` is registered automatically — no manual
step. A green dot = connected.

Test: ask the model *"call the `ping` tool"* → it should return the AICortex version.

### 5 — Build a Lite-Agent (recommended)

AICortex exposes 100+ tools; a mid-size local model drowns if it sees all of them. Use an
**Agent** with a curated subset:

1. Sidebar → **Agents** → create one on your Ollama model.
2. **Add MCP Server Tools** → `aicortex` → enable ~10–15 tools (e.g. `bootstrap`, `ping`,
   `memory_*`, `skill_search`, `skill_load`, `fs_read`, `fs_write`, `inbox_read`).
3. Instructions: *"Call `bootstrap` first, then follow its rules."*
4. Chat through this agent from now on.

## Optional — cloud models alongside local

Add more `endpoints.custom` entries (all OpenAI-compatible):

- **OpenRouter** — `baseURL: https://openrouter.ai/api/v1`, `dropParams: ["stop"]`, hundreds
  of models incl. Claude, one key.
- **Ollama Cloud** — `baseURL: https://ollama.com/v1` (the OpenAI layer is **`/v1`**, not
  `/api/v1`).

Keys go in `.env`, referenced as `${OPENROUTER_KEY}` etc.

> **Data separation:** cloud models send your prompts *and* AICortex tool results off your
> network. Use **local** models for anything touching AICortex tools / sensitive data; use
> cloud models for general chat.
>
> **Claude:** a Claude.ai *subscription* provides **no API access** — use Claude via
> OpenRouter or the Anthropic API (both billed per-token), not the flat subscription.

## Troubleshooting (real gotchas)

| Symptom | Cause & fix |
|---------|-------------|
| **"Welcome back" / can't register** | That's the login page. Use **`/register`** (incognito avoids a cached session). |
| **"Too many accounts created…"** | Registration rate limit (`REGISTER_MAX`, default 5/hour). Raise it **and restart the api container** — the limiter is in-memory, so a restart clears it. |
| **"Registration is not allowed"** | Set both `ALLOW_EMAIL_LOGIN=true` **and** `ALLOW_REGISTRATION=true`. |
| **App won't start / key errors** | `CREDS_KEY` must be **64 hex chars** (= `openssl rand -hex 32` — the arg is *bytes*, output is double); `CREDS_IV` **32 hex** (`-hex 16`). |
| **An old account shows up after a wipe** | Mongo only initialises (and creates the auth user) on an **empty** `data/mongodb`. Full reset: `docker compose down && rm -rf data/mongodb/*`. |
| **OAuth callback fails / spins** | `DOMAIN_CLIENT`/`DOMAIN_SERVER` must **exactly** match the URL you open LibreChat at (http vs https, IP vs hostname). |
| **Model ignores the tools** | Use a tool-capable model and a **Lite-Agent** with a small tool set (step 5). |

## Behind a reverse proxy (optional hardening)

Bind the published port to loopback (`127.0.0.1:3080:3080`) and put your proxy in front
(e.g. `chat.example.com` → `http://127.0.0.1:3080`), then set `DOMAIN_CLIENT`/`DOMAIN_SERVER`
to the `https://` URL and restart. The OAuth callback follows `DOMAIN_SERVER` automatically.
