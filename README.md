# ClaudeNasConnector

> Give Claude a Hermes-style agent home — on your own NAS.

Self-hosted [MCP](https://modelcontextprotocol.io) server that turns your NAS into a **personal Claude connector**. Like agent frameworks such as [Hermes](https://hermes-agent.nousresearch.com), it gives your assistant a persistent identity and real reach — but it runs in **your** network and plugs straight into the Claude apps you already use.

Add it once as a *custom connector* and Claude gains:

- 🧠 **Consistent memory** that lives on your NAS and follows you across every device
- 📱 **Work from anywhere** — the *same* brain on desktop **and** mobile, one account, one state
- 🛠️ **Your tools & skills** — home automation, document stores, a 3D printer, finance APIs … exposed as callable MCP tools and retrievable skills

The model stays in Anthropic's cloud. **Your data, skills, and secrets stay on your NAS.** Claude talks to this server over an HTTPS connector; the server uses your local credentials internally and never hands them to the model.

> ⚠️ **Status: early / skeleton.** It currently ships a working `ping` tool that proves the full chain (NAS → reverse proxy → Claude connector). Memory tools, the skill router, and authentication are on the roadmap below. **Do not expose this publicly without adding authentication first** (see [Security](#security)).

## How it works

```
Claude app (desktop / mobile)
        │  custom connector (HTTPS, from Anthropic's cloud)
        ▼
Reverse proxy (Zoraxy / Caddy / nginx / Traefik …)
        │
        ▼
ClaudeNasConnector  (this container, on your NAS)
        │  uses local files & secrets
        ▼
Memory files · Skills · Your tools & APIs
```

## Requirements

- A NAS or server running **Docker** (Compose v2).
- A **reverse proxy** that serves the container over public HTTPS — Claude connects from Anthropic's cloud, so the endpoint must be reachable from the internet.
- A domain/subdomain pointing at your proxy.
- A **Claude plan** that supports custom connectors (Free is limited to one; Pro/Max/Team/Enterprise support more).

## Quick start

```bash
git clone git@github.com:IkarusMK/ClaudeNasConnector.git
cd ClaudeNasConnector
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

Data is stored under `./data` (memory, skills, work) and logs under `./logs` by default.

## Security

- This server is reachable from the public internet via your proxy. **Add authentication before exposing it** — anyone who reaches the endpoint can call its tools.
- Keep all real credentials (API tokens, etc.) in `.env` / a secrets store **on your NAS**. They are used server-side and never sent to the model.
- `.env` and `data/` are git-ignored — never commit secrets.

## Roadmap

- [x] Walking skeleton: `ping` tool + remote MCP over HTTPS
- [ ] Authentication (OAuth / token) for the connector
- [ ] Memory tools (`memory_read` / `memory_write` / `memory_list`)
- [ ] Skill router (`skill_search` / `skill_load` / `skill_resource`)
- [ ] Built-in tool integrations (Home Assistant, etc.)
- [ ] Prebuilt image on GHCR

## License

[MIT](LICENSE) © 2026 IkarusMK
