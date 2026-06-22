# Autonomy runner (reference)

The connector holds the **schedule** (`cron_add` → `/data/cron`); it can't run the
model itself. This runner is the small NAS-side piece that actually **fires**
due jobs: a tick loop runs an LLM agent that calls `cron_due`, executes each due
job through the connector, calls `cron_mark_run`, and reports the result.

It is intentionally **decoupled and LLM-agnostic**.

## Two backends — pick one

| Backend | Path | Auth to connector | LLM | Best for |
|---------|------|-------------------|-----|----------|
| **Claude Code** | this folder | the CLI does the connector **OAuth** itself (no token needed) | Claude (sub or API key) | Claude users; simplest, no connector change |
| **Generic (any LLM)** | [`generic/`](generic/README.md) | static **`RUNNER_TOKEN`** (MultiAuth on the connector) | **any** via LiteLLM (GPT, Gemini, Ollama, Claude…) | non-Claude LLMs / one provider-agnostic agent |

The rest of this file documents the **Claude Code** backend. For any other LLM,
use [`generic/`](generic/README.md).

```
system tick (loop, every RUNNER_INTERVAL)
   └─ run-once.sh → $RUNNER_CMD "<orchestrator.txt>"
         └─ agent: cron_due → run each job via connector → cron_mark_run → notify
```

## Why this is separate from the connector

The connector is OIDC-protected for interactive apps. A headless runner can't do
a browser login — so instead of changing the connector's auth, the **reference
backend (Claude Code) performs the connector's OAuth login once itself** and
reuses the stored token. The *model* auth is independent: use a pay-per-use API
key (the "Mittelweg") or a subscription login.

## One-time setup (interactive)

> Run these on the NAS, in the `runner/` folder. The login steps are interactive
> and only needed once; after that the loop runs unattended.

```bash
mkdir -p config logs
# 1. Build the runner image
docker compose build

# 2. Open a one-off shell in the runner (overrides the loop entrypoint)
docker compose run --rm -it --entrypoint sh aicortex-runner

#    Inside the container:
# 2a. Model auth — pick ONE:
#     • API key (Mittelweg):  already injected if ANTHROPIC_API_KEY is set in .env
#     • Subscription:         run `claude` once and complete the OAuth login
# 2b. Add the connector and complete ITS OAuth (Pocket ID) login:
claude mcp add --transport http aicortex https://agent.example.com/mcp
claude            # triggers the connector OAuth on first tool use; approve it
# 2c. Verify the tools are visible, then exit:
#     ask: "call cron_list"  → should return the connector's response
exit
```

## Run it

```bash
docker compose up -d
docker compose logs -f          # watch the ticks
```

Create a test job (from any LLM client on the connector):

```
cron_add  name="runner-selftest"
          schedule="*/2 * * * *"          # every 2 minutes
          prompt="Report the current Home Assistant status: call_service home-assistant path=api/ and summarize."
          notify="user"
```

Within ~2 min you should see the runner pick it up; the summary lands via your
notify channel or the connector **inbox** (`inbox_read agent=user`). Delete it
with `cron_delete name="runner-selftest"` when done.

## ⚠️ Verify on first run (honest caveats)

- **Headless permissions:** unattended `claude -p` must be allowed to call the
  connector's MCP tools without prompting. Configure allowed tools / permission
  mode for Claude Code (e.g. an `allowedTools` allow-list in the runner's
  `/config`, or the appropriate non-interactive permission mode). Start with a
  **read-only** test job (like the self-test) before allowing actions.
- **Safety:** the orchestrator only performs actions a job explicitly authorized
  (see `orchestrator.txt`); physical/irreversible/outbound actions are skipped
  unless the job pre-authorizes them — because no one can confirm mid-run.
- **OAuth refresh:** the connector login token may need re-doing occasionally;
  if ticks start failing with auth errors, repeat step 2b.
- **Subscription vs API key (ToS):** a consumer subscription is meant for
  interactive use — unattended automation is a gray area with tight limits. For
  unattended runs the **API key** path is the sanctioned, stable choice.

## Swap the LLM backend

Set `RUNNER_CMD` to any headless agent CLI that (a) takes the prompt as its last
arg and (b) has the connector tools available. Example for a different agent:

```yaml
RUNNER_CMD: "my-agent --headless --prompt"
```

That agent is responsible for its own connector auth (token/login) and model
auth. Everything else — the schedule, tools, memory, secrets — stays on the NAS.
