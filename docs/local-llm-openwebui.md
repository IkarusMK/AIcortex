# Use AICortex with a local LLM (Ollama) via Open WebUI

You can drive AICortex from a **local model** through [Open WebUI](https://github.com/open-webui/open-webui) — a self-hosted, ChatGPT-style web UI (mobile-friendly PWA). The model runs locally via [Ollama](https://ollama.com); AICortex provides the memory, skills and tools. Everything stays on your hardware.

> **No connector changes needed.** Open WebUI connects to your existing AICortex over its native MCP transport using the static `RUNNER_TOKEN` you already have. Nothing to rebuild or redeploy on the AICortex side — this is purely a client you put in front of it.

## How it fits together

```
Open WebUI (chat UI, your NAS)  ──native MCP (Streamable-HTTP)──▶  AICortex  ──▶  memory · skills · tools
        │                          Authorization: Bearer <RUNNER_TOKEN>
        ▼
Ollama (local models)
```

Open WebUI is the **MCP client** + chat surface; Ollama is the **model**; AICortex is the **brain/tools**. (Ollama by itself is *not* an MCP client — Open WebUI is the missing middle piece.)

## Requirements

- **Open WebUI ≥ 0.6.31** — native MCP support, **Streamable-HTTP only** (which is exactly what AICortex speaks). Older versions: see [mcpo fallback](#fallback-older-open-webui-via-mcpo).
- A reachable AICortex endpoint (`https://<your-aicortex-host>/mcp`) and its **`RUNNER_TOKEN`** (set in your AICortex `.env`; the connector accepts it alongside OIDC via MultiAuth).
- Ollama running somewhere (local or another host), with at least one model pulled.

## Setup (3 steps)

A fresh setup needs all three. **Already running Open WebUI + Ollama? Skip to step 2.**

### Step 1 — Bring up Open WebUI + Ollama  *(skip if you already have them)*

Add this block to your `docker-compose.yml` (or save it as its own `compose.yml`), then `docker compose up -d`. It's a copy-paste **extension you fill in** — adjust ports, the secret, and whether you need the bundled Ollama.

```yaml
services:
  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    container_name: open-webui
    ports:
      - "3000:8080"            # → http://<host>:3000
    environment:
      # REQUIRED: long random string, so the MCP Bearer token survives restarts.
      WEBUI_SECRET_KEY: "CHANGE-ME-to-a-long-random-string"
      # Point at the Ollama below, or at your existing one:
      OLLAMA_BASE_URL: "http://ollama:11434"
    volumes:
      - open-webui:/app/backend/data
    restart: unless-stopped
    depends_on:
      - ollama                  # remove if you use an existing/remote Ollama

  # OPTIONAL — only if you don't already run Ollama:
  ollama:
    image: ollama/ollama:latest
    container_name: ollama
    volumes:
      - ollama:/root/.ollama
    # GPU: add the device/deploy section from the Ollama Docker docs for acceleration.
    restart: unless-stopped

volumes:
  open-webui:
  ollama:
```

After `docker compose up -d`: open `http://<host>:3000`, create the first (admin) account, and pull a model — e.g. `docker exec -it ollama ollama pull llama3.1` (or from Open WebUI's model UI).

### Step 2 — Connect AICortex

1. In Open WebUI: **⚙️ Admin Settings → External Tools → + (Add Server)**.
2. **Type:** `MCP (Streamable HTTP)`.
3. **Server URL:** `https://<your-aicortex-host>/mcp`
4. **Auth:** `Bearer` → **Key:** your `RUNNER_TOKEN`.
   *(Alternative: `OAuth 2.1 (DCR)` works too — AICortex's OIDC proxy supports Dynamic Client Registration. Bearer is simpler.)*
5. **Save.** AICortex's tools now appear to the model.

> **Important:** because you store a token in the UI, set **`WEBUI_SECRET_KEY`** (in the compose above). Without it, the saved token is lost on every restart.
> If Open WebUI and AICortex run on the **same host/network**, you can use AICortex's internal address instead of the public URL — just make sure the container can reach it.

### Step 3 — Use it

Open a chat, pick your local (Ollama) model, and tell it to call **`bootstrap`** first — then it's oriented with your memory, skills and tools, just like any other AICortex client.

## Choosing a local model (read this — it matters)

Tool-calling is the hard part for local models. Be deliberate:

- **Pick a strong function-calling model.** Larger, instruction-tuned, tool-aware models (e.g. recent Llama 3.x, Qwen2.5, Mistral-class) are far more reliable than small 7–8B models at multi-step tool use. Bigger = steadier.
- **Curate the tool set.** AICortex exposes many tools; a small model can drown in them. Start by enabling only what you need for the task (e.g. `bootstrap` + memory + the one device you're using), and grow from there.
- **Set expectations honestly.** Local models are great for privacy and simple flows. For long, reliable multi-step tool chains, a strong cloud model (e.g. Claude) still wins. Open WebUI lets you switch models per chat, so you can keep both.
- **Always `bootstrap` first.** Tell the model (or pin it in Open WebUI's system prompt) to call `bootstrap` at the start of every chat — same rule as any AICortex client.

## Fallback: older Open WebUI via mcpo

If you can't run Open WebUI ≥ 0.6.31 (or you use another OpenAPI-only client), bridge AICortex with [`mcpo`](https://github.com/open-webui/mcpo), the official MCP→OpenAPI proxy:

```jsonc
// mcpo config.json — wraps AICortex as an OpenAPI tool server
{
  "mcpServers": {
    "aicortex": {
      "type": "streamable-http",
      "url": "https://<your-aicortex-host>/mcp",
      "headers": { "Authorization": "Bearer <RUNNER_TOKEN>" }
    }
  }
}
```

```bash
docker run -p 8000:8000 ghcr.io/open-webui/mcpo:main \
  --api-key "<a-proxy-key>" --config /config.json
```

Then add `http://<host>:8000` as an **OpenAPI tool server** in Open WebUI (with the proxy api-key). Native MCP (Option A) is preferred where available.

## Security notes

- The `RUNNER_TOKEN` is a full-access credential to AICortex — treat it like a password. Keep it only in your AICortex `.env` and in Open WebUI's encrypted store (hence `WEBUI_SECRET_KEY`). Never commit it.
- Don't expose Open WebUI or AICortex publicly without authentication in front of them.
- The same AICortex guardrails still apply: secrets live in the vault, and the model should confirm before physical/outbound/destructive actions.
