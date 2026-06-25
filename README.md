# AICortex

> A private, self-hosted brain for your LLM — on your own NAS.

Self-hosted [MCP](https://modelcontextprotocol.io) server that turns your NAS into a **personal LLM connector** — a persistent "brain" your assistant loads at the start of every session. It gives any MCP-capable LLM (Claude, ChatGPT, local models …) a durable identity and real reach, running in **your** network and plugging straight into the LLM apps you already use. Your data stays on your hardware.

Add it once as a *custom connector / MCP server* and your LLM gains:

- ⚡ **One-call onboarding** — a `bootstrap` tool any LLM calls first; in a single round-trip it loads the guide *and* a live catalog of everything on the brain (memory, skills, services, devices, scheduled jobs), so a fresh session on any device is never "blank"
- 🧠 **Self-learning memory** — typed, deduplicated facts that live on your NAS and follow you everywhere; the assistant writes back what it learns each session, with a review queue so autonomy never pollutes the brain ([more](#auto-memory--a-brain-that-learns-by-itself))
- 📱 **Work from anywhere** — the *same* brain on desktop **and** mobile, one account, one state
- 🦙 **Runs with a local model** — drive the whole brain from a fully local LLM (Ollama) via Open WebUI's native MCP; no cloud required, model *and* data stay on your hardware ([guide](docs/local-llm-openwebui.md))
- 🗂️ **A skill router** — your skills live on your NAS; the LLM *searches* them, loads the right one (progressive disclosure), and *learns* new ones at runtime (`skill_write`)
- 🛠️ **Tools as data** — register any HTTP API with `service_add`, call it via `call_service`; new integrations need no code and no redeploy
- 🔌 **Devices as data** — generic **MQTT** (`mqtt_*`) and **FTP/FTPS** (`ftp_*`) dispatchers bring non-HTTP devices (e.g. a printer or sensor on your LAN) in the same way — as data, no redeploy
- 🖨️ **Printing** — register a LAN printer (`print_add`) and print PDFs/images straight to it over IPP/AirPrint (`print_document`), by file or inline base64 — hand the assistant a document and it prints at home
- 📄 **Scanning** — scan on a LAN multifunction device over eSCL/AirScan (`scan_document`) and drop the result straight into Paperless-ngx — "scan this and file it" in one step
- 🔐 **Encrypted secret vault** — store API keys/tokens through the connector (works from mobile); encrypted at rest, never shown back
- 🛡️ **Safe by default** — fail-closed auth, an enforced-encryption vault, and an SSRF egress guard (private/metadata IPs blocked unless you allow-list them)
- 🧭 **Self-describing** — any connecting LLM receives usage instructions on connect + `bootstrap`/`guide` tools, and is told to confirm before physical/outbound actions
- 🤝 **Presence-aware multi-agent board** — desktop, a NAS-local model and your phone act as one team: live presence, capability-routed task *pull*, and context-preserving handoffs with the work session attached ([more](#multi-agent--one-brain-many-agents))
- 🔁 **Cross-LLM session handoff** — `session_save`/`session_load` keep a compact, timestamped log of where work stands, so a different model or device (Claude, ChatGPT, a scheduled run) can resume exactly where another stopped; stale sessions auto-expire so the NAS doesn't bloat
- ⏰ **Scheduling & autonomy** — define cron jobs *as data* (`cron_add`) from any device; a small NAS-side runner triggers an LLM run when a job is due and reports the result back to you. The connector holds the schedule; an LLM runtime executes it.

The model stays in its provider's cloud (or runs locally). **Your data, skills, and secrets stay on your NAS.** Your LLM talks to this server over an HTTPS connector; the server uses your local credentials internally and never hands them to the model.

> ✅ **Status: v1.0 — stable.** One-call `bootstrap` onboarding, self-learning **typed memory** (with a candidate review queue), the skill router (categorized), HTTP/MQTT/FTP/WebDAV/SSH/SMTP dispatchers, a sandboxed workspace file hub, IPP printing & eSCL scanning, an MCP gateway, **presence-aware multi-agent coordination** (capability-routed pull + context-preserving handoff), cross-LLM session handoff, cron-as-data scheduling, an encrypted secret vault, OAuth (via your own OIDC provider) and an SSRF egress guard are all live — and the connector is *self-describing* (it sends its usage guide on connect and tells every LLM to call `bootstrap` first and work exclusively through it). New capabilities are added as **data** — no redeploy. The autonomy *runner* (the NAS-side LLM runtime that fires scheduled jobs) is the one piece set up outside the connector — see [Autonomy & scheduling](#autonomy--scheduling). **Don't expose it publicly without [Authentication](#authentication).**

## How it works

```
LLM app — any MCP client (desktop / mobile)  ·  one or many agents
        │  custom connector / MCP server (HTTPS, from the model's cloud)
        ▼
Reverse proxy (Zoraxy / Caddy / nginx / Traefik …)
        │
        ▼
AICortex  (this container, on your NAS)
        │  uses local files & secrets
        ▼
Memory · Skills · HTTP services · MQTT & FTP devices · WebDAV cloud · Workspace files
   · SSH/SFTP · SMTP email · IPP printing · eSCL scanning · MCP gateway · Inbox/Tasks
   · Sessions · Cron · Secret vault   (every outbound call passes the SSRF egress guard)

Autonomy (optional): a NAS-side runner — a scheduled `claude -p` — polls
`cron_due`, runs each due job through the connector, then notifies you
(push / inbox). The connector stores the schedule; the runner is the LLM
runtime that actually fires it.
```

## Run it with a local model (Ollama)

AICortex is model-agnostic — and that includes **fully local** models. Point [Open WebUI](https://github.com/open-webui/open-webui) at the connector over its native MCP transport (Streamable-HTTP) and a local [Ollama](https://ollama.com) model gets the same memory, skills and tools as any cloud assistant. No connector changes, no cloud dependency: the model runs on your hardware, the brain on your NAS — nothing leaves your network.

This is where a self-hosted brain pays off most: a private assistant that *remembers*, *learns skills* and *acts on your devices*, end to end on your own infrastructure. Tool-calling quality depends on the local model you pick (bigger, tool-aware models are far more reliable); for heavy multi-step chains a strong cloud model still has the edge — and Open WebUI lets you switch per chat.

**→ Full guide: [docs/local-llm-openwebui.md](docs/local-llm-openwebui.md)**

## Capabilities (tools at a glance)

| Group | Tools | What it does |
|-------|-------|--------------|
| Onboarding | `bootstrap` | **Start here** — one call returns the guide + a live catalog of the whole brain (memory, skills, services, devices, cron) |
| Health | `ping` | Connectivity check |
| Memory | `memory_write` (typed) · `memory_read`/`list`/`search`/`delete` · `memory_note` · `memory_candidates` · `memory_promote`/`memory_reject` | Self-learning, **typed** facts on the NAS with dedup + a candidate review queue |
| Skills | `skill_search` · `skill_list` · `skill_load` · `skill_resource` · `skill_write` | Searchable know-how; learn new skills at runtime |
| Services (HTTP) | `service_add` · `service_list` · `call_service` | Register & call any HTTP API as data |
| Devices (MQTT) | `mqtt_add` · `mqtt_list` · `mqtt_publish` · `mqtt_get` | Talk to MQTT devices (e.g. a LAN printer or sensor) as data |
| Files (FTP/FTPS) | `ftp_add` · `ftp_list_endpoints` · `ftp_list` · `ftp_upload` | Up/list files over FTP/FTPS (e.g. push a file to a device) |
| Cloud (WebDAV) | `webdav_add` · `webdav_list` · `webdav_upload` · `webdav_download` · `webdav_mkdir` · `webdav_delete` | Move large files NAS↔cloud (Nextcloud/ownCloud) — streamed, app-password auth |
| Workspace files | `fs_list` · `fs_read` · `fs_write` · `fs_move` · `fs_delete` · `fs_info` | See & tidy the `/data/work` file hub (sandboxed to the workspace) |
| SSH / SFTP | `ssh_add` · `ssh_run` · `ssh_upload` · `ssh_download` · `ssh_list_dir` | Run remote commands & transfer files over SSH (hosts as data, vault creds) |
| Email (SMTP) | `mail_add` · `mail_list` · `mail_send` | Send mail/notifications with optional attachment (accounts as data) |
| Printing (IPP) | `print_add` · `print_list` · `print_delete` · `print_document` | Print PDFs/images to a LAN printer via IPP/AirPrint — by file or inline base64 |
| Scanning (eSCL) | `scan_add` · `scan_list` · `scan_delete` · `scan_document` | Scan on a LAN device via eSCL/AirScan → `/data/work`, optionally straight into Paperless |
| MCP gateway | `mcp_add` · `mcp_list` · `mcp_tools` · `mcp_call` | Use other MCP servers' tools as data |
| Multi-agent | `inbox_*` · `task_add`/`list`/`claim`/`update` · `task_next` · `task_handoff` · `agent_register`/`list` | **Presence-aware** team: capability-routed task *pull* & context-preserving handoff |
| Sessions | `session_save` · `session_list` · `session_load` · `session_delete` · `session_prune` | Cross-LLM handoff log — resume work from any model/device; auto-expires |
| Scheduling | `cron_add` · `cron_list` · `cron_delete` · `cron_due` · `cron_mark_run` | Cron jobs as data; a NAS runner triggers them |
| Secrets | `secret_set` · `secret_list` · `secret_delete` | Encrypted vault; values never returned |
| Guide | `guide` | Self-description (also sent as server `instructions` on connect) |

New capabilities are added as **data** (a skill, a service config, a secret) — not code, no redeploy. Full CRUD: every config module also has a delete (`skill_delete`, `service_delete`, `mqtt_delete`, `ftp_delete`, `mcp_delete`, `task_delete`, `agent_remove`, `inbox_delete`, plus `memory_delete` / `secret_delete`), so anything you can register you can also remove via the connector.

## Project structure

```
AICortex/
├── app/                # Server code (FastMCP)
│   ├── server.py       #   entrypoint — wires auth + registers tool modules
│   ├── bootstrap.py    #   'start here' tool — loads guide + live brain catalog
│   ├── memory.py       #   memory tools — typed, dedup, candidate review (auto-memory)
│   ├── learn.py        #   auto-memory: fail-open candidate-capture middleware (Tier B)
│   ├── skills.py       #   skill router
│   ├── services.py     #   generic allow-listed HTTP service caller
│   ├── mqtt_tools.py   #   generic MQTT dispatcher (devices as data)
│   ├── ftp_tools.py    #   generic FTP/FTPS transfer (push files to devices)
│   ├── webdav_tools.py #   WebDAV transfer (cloud drives as data, e.g. Nextcloud)
│   ├── fs_tools.py     #   workspace file hub (/data/work, sandboxed)
│   ├── ssh_tools.py    #   SSH commands + SFTP transfer (hosts as data)
│   ├── mail_tools.py   #   SMTP email/notifications (accounts as data)
│   ├── print_tools.py  #   IPP printing to LAN printers (printers as data)
│   ├── scan_tools.py   #   eSCL scanning (scanners as data) → /data/work / Paperless
│   ├── netguard.py     #   SSRF egress guard (allow-list internal ranges)
│   ├── mcp_gateway.py  #   gateway to other MCP servers (servers as data)
│   ├── coordination.py #   multi-agent — presence, task routing/handoff, inbox/registry
│   ├── cron.py         #   scheduled jobs as data (a NAS runner fires them)
│   ├── sessions.py     #   cross-LLM session handoff log (auto-expiring)
│   ├── secrets_store.py#   encrypted secret vault
│   ├── guide.py        #   self-describing usage guide (DE/EN)
│   └── requirements.txt
├── data/               # Persistent, human-readable state (git-ignored content)
│   ├── memory/         #   memory files — what the LLM remembers about you
│   ├── skills/         #   skill library — <skill>/SKILL.md the router searches
│   ├── services/       #   HTTP service configs (integrations as data)
│   ├── mqtt/           #   MQTT broker/device configs
│   ├── ftp/            #   FTP/FTPS endpoint configs
│   ├── mcp/            #   upstream MCP server configs
│   ├── webdav/         #   WebDAV endpoint configs (cloud drives)
│   ├── ssh/            #   SSH host configs
│   ├── mail/           #   SMTP account configs
│   ├── coordination/   #   multi-agent inbox / tasks / agents
│   ├── cron/           #   scheduled job configs (cron as data)
│   ├── sessions/       #   cross-LLM session handoff logs (auto-expiring)
│   ├── printers/       #   IPP printer configs (printers as data)
│   ├── scanners/       #   eSCL scanner configs (scanners as data)
│   ├── vault/          #   encrypted secrets (secret_set)
│   ├── auth/           #   OAuth client registrations (persisted)
│   └── work/           #   workspace file hub — scans, downloads, print sources (fs_*)
├── secrets/            # Local credentials (.env) — never leave the NAS
├── logs/               # Container logs
├── docs/               # Architecture & client project-instruction template
├── Dockerfile          # Baked image (deps installed at build time)
├── entrypoint.sh       # Drops privileges to PUID:PGID at runtime (gosu)
├── docker-compose.yml
└── .env.example
```

> **No `deps/` folder?** Correct — dependencies are baked **into the image** at build time, so there's no install-on-start volume. The `data/`, `logs/` and `secrets/` folders keep their structure via `.gitkeep`; their *contents* are git-ignored so nothing private is committed.

## Memory, skills & the skill router

This is the heart of the project — making the assistant *itself* portable, not just chat.

- **Memory** lives as plain files under `data/memory`. Tools (`memory_read` / `memory_write` / `memory_list`) let the LLM recall and update what it knows about you — the same on every device.
- **Skills** live as folders under `data/skills` (`<skill>/SKILL.md` + resources). The router tools — `skill_search` / `skill_load` / `skill_resource` — let the LLM find the right skill for a request and pull in **only what it needs** (progressive disclosure, the same idea as tool search).
- **Categories keep it cheap — and are mandatory.** Every skill carries a `category` (a synonym `cluster` is also read); `skill_write` **refuses an uncategorized skill** and snaps near-duplicate spellings (`Trading`/`trading`) onto the existing one, so the library can't drift. `skill_list()` returns categories + counts, `skill_list("<category>")` drills in, and `bootstrap` collapses the skill section to per-category counts once the library grows — so a 300-skill library stays a dozen lines, not a token dump. A small set of original starter skills lives in [`examples/skills/`](examples/skills/README.md) — copy them into `data/skills` to seed.
- **Call `bootstrap` first.** Its tool description tells any LLM to call it at the start of every session — one call loads the guide and a live catalog of the whole brain, so the assistant is oriented before it answers. For clients that don't call tools on their own, add a one-line instruction to your client's **project / system prompt** ("call the `bootstrap` tool first") — see [`docs/client-project-instructions.md`](docs/client-project-instructions.md). After that, "find the right skill / tool and apply it" just happens, from any device.

## Auto-memory — a brain that learns by itself

Most assistants forget the moment a chat ends. AICortex closes that loop: the brain **learns as you work** — and keeps itself tidy while doing it, so it grows instead of drifting into a junk drawer.

- **Typed, on purpose.** Every memory is one of four kinds — `user` (who you are / preferences), `feedback` (how you want the assistant to work), `project` (ongoing goals & status), `reference` (pointers to resources). `memory_write` **refuses an untyped memory** — the same house rule that keeps the skill library tidy — so the brain stays sorted by intent.
- **Learns in-session, at zero extra cost.** The model that's *already* talking to you distills the durable facts before the session ends and writes them back — no second model, no background loop, no metering. A short discipline baked into the connector's guide makes this automatic.
- **Dedup-first.** A write searches for overlapping entries and flags them, so related facts get merged into one file instead of multiplying into near-duplicates. Same title = merge.
- **A review queue, so autonomy never pollutes the brain.** Anything captured automatically — or staged with `memory_note` when the assistant is unsure — lands as a **candidate**, not live memory. `bootstrap` surfaces the count; you (or the assistant) `memory_promote` the keepers and `memory_reject` the rest. Curated memory stays clean by design.
- **Optional deterministic capture.** A single, fail-open, server-side hook can auto-stage candidates when durable things happen (a new service, device or scheduled job). It's **off by default** (`LEARN_AUTOCAPTURE=1` to enable), never touches secrets, and — because a failure there can never block a tool call or the boot — it's safe to leave on.

Old memories written before the type system are still read correctly; there's nothing to migrate.

## Tools & integrations (as data)

New integrations don't need new code. A **service** is a small config you register
at runtime with `service_add` (stored under `data/services`); `call_service` then
reaches it — only registered services are allowed, and the auth token is injected
server-side from a stored secret (`token_env`, set via `secret_set` into the
encrypted vault or via `.env`), never stored in service data or returned to the
model. Pair a service with a `skill_write` skill that explains how
to use it, and a new capability is live **without a redeploy**.

## Multi-agent — one brain, many agents

Run your assistants as a **team**: Claude on the desktop, a local model on the NAS, your phone — all sharing one brain and one task board, coordinating without stepping on each other.

- **Live presence.** `agent_register` doubles as a heartbeat, so `agent_list` shows who's **online / idle / away** right now (and `bootstrap` surfaces the team at the top). You always know who's available to take work.
- **Pull work, don't hunt for it.** `task_next(owner)` recommends the best open task for an agent — ranked *assigned-to-you → matches your capabilities → unassigned* — then `task_claim` takes it. Tasks carry capability tags (`needs`) so the right job reaches the right agent.
- **Hand off with full context.** `task_handoff(id, to)` reassigns a task, drops the recipient an inbox message, and **attaches the work session** (`session_id`) — they `session_load` and pick up exactly where the other stopped. Pass an empty target to release it back to the pool.
- **Namespaced memory.** Shared knowledge in `shared`, private notes in `agents/<name>` — agents collaborate without overwriting each other (and auto-memory keeps both tidy).
- **Shared inbox & registry.** `inbox_*` for messages, `agent_*` for the registry; task status flows `open → claimed → blocked → done`.

Sub-agent *spawning* stays client-side (the model lives in the cloud or on local hardware); the connector is the shared coordination layer they meet on. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Autonomy & scheduling

Schedules live on the NAS as **data** — create them from any device with `cron_add(name, schedule, prompt)` (5-field cron, server-local time); `cron_list` / `cron_delete` manage them. That part is built into the connector.

What the connector **can't** do is run the model itself — an LLM run must be triggered. So the autonomy *engine* is a small **NAS-side runner**:

1. System cron on the NAS runs a recurring agent invocation (e.g. `claude -p "<orchestrator>"`, or any LLM CLI/SDK) every minute.
2. That run calls `cron_due` → executes each due job's prompt **through this connector** (so it has every tool) → `cron_mark_run`.
3. It reports the result via your configured channel — or, if none is set, posts to the connector **inbox**, which you read in your LLM app.

**Runner runtime — two options:**

| Option | Cost | Notes |
|--------|------|-------|
| **Subscription** (`claude` CLI / OAuth) | none (uses your plan) | Consumer plans are meant for *interactive* use — unattended automation is a gray area with tight usage limits. Keep it to a few jobs/day. |
| **API key** (Agent SDK) | pay-per-use (pennies for light daily jobs) | The sanctioned, stable path for unattended runs. Key stays in the vault. |

> The runner is the **only** piece that lives outside the connector — the model/agency runs in its provider's cloud (or locally) and must be invoked. Everything it acts on (schedule, tools, memory, secrets) stays on the NAS.

Two ready-to-deploy runners live in [`runner/`](runner/README.md), so the autonomy layer works with **any application LLM**:

- **Claude Code backend** — performs the connector's OAuth login itself, so no connector change is needed (simplest for Claude users).
- **Generic any-LLM backend** ([`runner/generic/`](runner/generic/README.md)) — drives the connector from **any** model via [LiteLLM](https://github.com/BerriAI/litellm) (GPT, Gemini, Ollama, Claude, …). It authenticates with a static **`RUNNER_TOKEN`** that the connector accepts **alongside** OIDC (FastMCP `MultiAuth`) — interactive apps keep using OIDC unchanged; only headless machine clients use the token.

## Requirements

- A NAS or server running **Docker** (Compose v2).
- A **reverse proxy** that serves the container over public HTTPS — cloud-hosted LLM clients connect from their provider's cloud, so the endpoint must be reachable from the internet. (A purely local client/runner can reach it on the LAN.)
- A domain/subdomain pointing at your proxy.
- An **MCP-capable client** that supports custom connectors / MCP servers (e.g. Claude, ChatGPT, or any MCP client; some plans cap how many you can add).

## Quick start

```bash
git clone git@github.com:IkarusMK/AICortex.git
cd AICortex
cp .env.example .env        # adjust PUID / PGID / HOST_PORT / TZ
docker compose up -d --build
```

The MCP endpoint is served at `http://<host>:8787/mcp`.

> **Prebuilt image (no local build):** a multi-arch image is published to GHCR by
> CI. In `docker-compose.yml`, comment out `build: .`, uncomment
> `image: ghcr.io/ikarusmk/aicortex:latest`, then `docker compose pull && docker compose up -d`.
> (Make the GHCR package **public** once, under the repo's *Packages* settings, so the NAS can pull it without a token.)

### Expose it & add the connector

1. Point a subdomain (e.g. `agent.example.com`) at your reverse proxy.
2. Proxy that host to `http://<nas-ip>:8787` over HTTPS.
   - The upstream is **plain HTTP** — do *not* enable "TLS to upstream".
   - If your proxy uses geo-blocking, **allow your LLM provider's egress region** for this host (e.g. US for Anthropic-/OpenAI-hosted clients), or the connector cannot reach you.
3. In your MCP client (e.g. Claude): **add a custom connector / MCP server** → URL `https://agent.example.com/mcp`.
4. Test: ask the assistant to call the `ping` tool.

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
OAuth 2.1 flow (Dynamic Client Registration + PKCE) that the MCP client
speaks; your provider just does the actual login.

> ℹ️ **Don't** put browser/forward-auth (reverse-proxy SSO) in front of the
> `/mcp` endpoint — an MCP connector is a *machine* client and can't follow an
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
**`<BASE_URL>/auth/callback`**. Then (re-)add the custom connector in your client — it
will send you through your provider's login. **When the OIDC variables are unset,
the server binds to `127.0.0.1` only** (local testing); set `ALLOW_INSECURE=1` to
force an open bind without auth (not recommended).

> ⚠️ **Authentication ≠ authorization.** Any account that can log in to your IdP
> gets **full** access to every tool and all data — there is no per-user/role
> check. Point this at a **single-user or dedicated** provider; don't reuse a
> shared family/company IdP without restricting which subjects may log in.

## Security

- **Auth fails closed.** Without OIDC the server binds to `127.0.0.1` only (override with `ALLOW_INSECURE=1`). With OIDC, **enable it before exposing the proxy** — anyone who reaches `/mcp` can otherwise call every tool.
- **SSRF guard.** `service_add` / `mqtt_add` / `ftp_add` are tools the *model* can call, so the list of registered targets is not a trust boundary by itself. Every outbound host is resolved and **private / loopback / link-local / cloud-metadata addresses are blocked** unless they fall inside `INTERNAL_ALLOW_CIDRS` (operator-only, not settable by the model). Set it to the LAN/VPN ranges you actually use — e.g. `192.168.1.0/24` — otherwise calls to your own devices are blocked too.
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
| Login succeeds, but the client says *"returned an error when connecting"*; logs show `Token verified successfully` then **`Token missing required scopes`** | The proxy-issued MCP token doesn't carry the upstream OIDC scopes as claims. **Don't set `required_scopes`** — a successful login is enough. (Already removed in `server.py`.) |
| Logs show `Issued new FastMCP tokens` immediately followed by **`Bearer token rejected`** (401 `invalid_token`) | Behind a TLS-terminating proxy, uvicorn ignored `X-Forwarded-Proto`, so the server computed an `http://` URL and rejected its own `https`-audience tokens. Set **`FORWARDED_ALLOW_IPS: "*"`** (already in `docker-compose.yml`). |
| Log warns **`disk client_storage unavailable (Fernet key must be 32 url-safe base64-encoded bytes)`** | `STORAGE_ENCRYPTION_KEY` isn't a valid Fernet key (it's **not** the same as `JWT_SIGNING_KEY`). Generate: `python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"`. Or omit it for an unencrypted (still persistent) store. |
| Worked once, then `Bearer token rejected` for an **old client id** after recreating the container | The OAuth client store was ephemeral and got wiped. Persistent `data/auth` (this repo) fixes it. To clear a stuck client: remove the connector, fully quit & reopen the app, re-add. |
| Connector can't connect at all; proxy returns a login **web page** | You put reverse-proxy SSO / forward-auth in front of `/mcp`. A machine client can't do interactive login — **remove it**; auth belongs at the MCP layer (this server). |
| OIDC provider's consent/"Sign in" button spins forever, no `POST …/authorize` ever reaches the IdP; browser console shows `null is not an object (… scope.includes)` | The upstream `/authorize` request carried **no `scope`** (some IdP UIs, e.g. Pocket ID, crash on `scope=null`). Send one **without** re-introducing token-scope validation: `extra_authorize_params={"scope": "openid profile email"}` (already in `server.py`; override via `OIDC_SCOPE`). |
| `call_service` / `mqtt` / `ftp` to a **local device** returns *"Blocked by network policy"* | The SSRF guard blocks private IPs by default. Add the device's range to **`INTERNAL_ALLOW_CIDRS`** (e.g. `192.168.1.0/24`) and restart. |

## Roadmap

- [x] Walking skeleton: `ping` tool + remote MCP over HTTPS
- [x] Authentication: OAuth 2.1 via your own OIDC provider (Pocket ID, Authentik, Keycloak, Auth0, …)
- [x] Memory tools (`memory_write` / `memory_read` / `memory_list` / `memory_search` / `memory_delete`), scope-namespaced for multi-agent
- [x] Auto-memory: **typed** memories (`user`/`feedback`/`project`/`reference`, enforced) with dedup, a candidate review queue (`memory_note` / `memory_candidates` / `memory_promote` / `memory_reject`) and an optional fail-open auto-capture hook — the brain learns each session without polluting itself
- [x] Skill router (`skill_search` / `skill_list` / `skill_load` / `skill_resource` / `skill_write`)
- [x] Generic service caller (`call_service` / `service_add` / `service_list`) — integrations as data + skills, no redeploy
- [x] Encrypted secret vault (`secret_set` / `secret_list` / `secret_delete`) — set secrets via the connector; values encrypted at rest, never returned
- [x] Self-describing: server `instructions` on connect + a `guide` tool, so any LLM immediately knows what the connector is and how to use it
- [x] One-call onboarding: a `bootstrap` 'start here' tool that loads the guide + a live brain catalog in a single call, so a fresh session on any device is never blank
- [x] Generic device dispatchers — **MQTT** (`mqtt_*`) and **FTP/FTPS** (`ftp_*`), so non-HTTP LAN devices (printers, sensors, actuators …) are data too
- [x] IPP printing (`print_add` / `print_list` / `print_delete` / `print_document`) — print PDFs/images to a LAN printer via IPP/AirPrint, by file or inline base64; auto-upgrades to TLS/IPPS and auto-falls-back to octet-stream
- [x] eSCL scanning (`scan_add` / `scan_list` / `scan_delete` / `scan_document`) — scan on a LAN device via eSCL/AirScan to `/data/work`, optionally uploaded straight into Paperless-ngx
- [x] WebDAV transfer (`webdav_add` / `webdav_list` / `webdav_upload` / `webdav_download` / `webdav_mkdir` / `webdav_delete`) — stream large files NAS↔cloud (Nextcloud/ownCloud), app-password auth
- [x] Workspace file hub (`fs_list` / `fs_read` / `fs_write` / `fs_move` / `fs_delete` / `fs_info`) — see & tidy `/data/work`, hard-sandboxed to the workspace
- [x] SSH/SFTP (`ssh_add` / `ssh_run` / `ssh_upload` / `ssh_download` / `ssh_list_dir`) — remote commands & file transfer, hosts as data, vault creds
- [x] SMTP email (`mail_add` / `mail_list` / `mail_send`) — send mail/notifications with optional attachment, accounts as data
- [x] Hardening — fail-closed auth, enforced-encryption vault, SSRF egress guard (`INTERNAL_ALLOW_CIDRS`); VPS/VPN-friendly
- [x] MCP gateway — connect to other MCP servers as data (`mcp_add` / `mcp_list` / `mcp_tools` / `mcp_call`)
- [ ] Bundled service configs & skills (Home Assistant, Mealie, …)
- [x] Multi-agent coordination: shared inbox, task board & agent registry (`inbox_*` / `task_*` / `agent_*`) — now **presence-aware** (`agent_list` online/idle/away), with capability-routed pull (`task_next`) and context-preserving, session-linked handoff (`task_handoff`); sub-agent *spawning* stays client-side
- [x] Scheduling: cron jobs as data (`cron_add` / `cron_list` / `cron_delete` + `cron_due` / `cron_mark_run`)
- [x] Cross-LLM session handoff: timestamped, auto-expiring checkpoints (`session_save` / `session_list` / `session_load` / `session_delete` / `session_prune`) so any model/device resumes where another left off — the 5 most recent surfaced at the top of `bootstrap`
- [x] Autonomy runner: reference NAS-side runner with a swappable LLM backend ([`runner/`](runner/README.md)) — fires due jobs and notifies you
- [x] Prebuilt image on GHCR — multi-arch (amd64/arm64) build & push via GitHub Actions
- [x] Local-LLM client guide — drive AICortex from a local model (Ollama) via Open WebUI's native MCP + the `RUNNER_TOKEN` ([docs](docs/local-llm-openwebui.md))

## License

[MIT](LICENSE) © 2026 IkarusMK
