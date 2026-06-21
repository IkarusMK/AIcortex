# Generic autonomy runner (any LLM)

LLM-agnostic runner: connects to the connector over MCP with the **static
`RUNNER_TOKEN`**, exposes the connector's tools to **any** model via
[LiteLLM](https://github.com/BerriAI/litellm) (OpenAI, Anthropic, Google/Gemini,
Ollama, Azure, …), and runs the same `cron_due → execute → cron_mark_run →
notify` loop as the Claude backend.

Use this when your application LLM is **not** Claude, or when you want a single
provider-agnostic agent. (Claude users can also just use the simpler Claude Code
backend in [`../`](../README.md), which logs in via OAuth and needs no token.)

## Prerequisite: enable the runner token on the connector

On the **connector** (not here), set a token and restart:

```
# in the connector's .env
RUNNER_TOKEN=$(openssl rand -hex 32)
```

The connector then accepts this token **alongside** OIDC (MultiAuth) — apps keep
using OIDC, the runner uses the token.

## Run it

```bash
cd runner/generic
# minimal .env
cat > .env <<EOF
CONNECTOR_URL=https://agent.example.com/mcp
RUNNER_TOKEN=<same value as on the connector>
LLM_MODEL=gpt-4o-mini            # or anthropic/claude-3-5-sonnet-latest, gemini/gemini-1.5-pro, ollama/llama3.1
OPENAI_API_KEY=sk-...            # the key for YOUR chosen provider
RUNNER_INTERVAL=60
EOF

docker compose up -d --build
docker compose logs -f
```

Create a **read-only** test job from any client on the connector, then watch:

```
cron_add name="runner-selftest" schedule="*/2 * * * *" \
  prompt="Report Home Assistant status: call_service home-assistant path=api/ and summarize." \
  notify="user"
```

The summary arrives via your notify channel or the connector **inbox**
(`inbox_read agent=user`). Remove it with `cron_delete name="runner-selftest"`.

## Notes / verify on first run

- **Model choice:** `LLM_MODEL` is a LiteLLM model string; set the matching
  provider key. Tool-calling quality varies by model — use a capable one.
- **Safety:** the orchestrator only performs actions a job explicitly authorized;
  physical/irreversible/outbound actions are skipped (no one can confirm mid-run).
  Start with read-only jobs.
- **Token security:** `RUNNER_TOKEN` is a full-access credential to the connector
  — keep it in `.env` only, rotate if leaked, never commit it.
