# Autonomy runner (reference)

The connector holds the **schedule** (`cron_add` → `/data/cron`); it can't run the
model itself. A runner is the small NAS-side piece that actually **fires** due
jobs: once a minute it asks `cron_due`, runs each due job through the connector,
calls `cron_mark_run`, and reports the result.

## Two runners — pick one

| Runner | Path | Model | Set up |
|--------|------|-------|--------|
| **Any model (recommended)** | [`generic/`](generic/README.md) | chosen in the WebUI: Claude subscription, Anthropic, OpenRouter, OpenAI, Gemini | WebUI → *Scheduled jobs → Set up the runner* |
| **Claude Code only** | this folder | Claude (subscription token or API key) | `.env` of this folder |

Both ask `cron_due` without any model and wake the model only when a job is due.
The rest of this file documents the **Claude Code only** runner.

```
tick (on the minute, every RUNNER_INTERVAL)
   └─ run-once.sh → due.mjs: cron_due (no model)
         └─ only if jobs are due: claude -p "<orchestrator + due jobs>"
               └─ runs each job via the connector → cron_mark_run → notify
```

## Set it up

1. **A runner key:** WebUI → *Scheduled jobs → Set up the runner* → *Create runner
   key* (or use the `RUNNER_TOKEN` set on the connector).
2. **Model access**, one of:
   - Claude subscription: run `claude setup-token` on any machine where you are
     signed in to Claude Code and copy the token;
   - a pay-per-use Anthropic API key.
3. **`.env`** in this folder:
   ```
   CONNECTOR_URL=https://agent.example.com/mcp
   RUNNER_TOKEN=ak_…
   CLAUDE_CODE_OAUTH_TOKEN=…        # or ANTHROPIC_API_KEY=…
   ```
4. **Start it:**
   ```bash
   docker compose up -d --build
   docker compose logs -f          # watch the checks
   ```

No login inside the container is needed: the runner key reaches the connector,
the token or API key reaches the model.

Create a read-only test job from any client on the connector:

```
cron_add  name="runner-selftest"
          schedule="*/2 * * * *"
          prompt="Report the connector's service list (service_list) in one line."
          notify="user"
```

The summary lands via your notify channel or the connector **inbox**
(`inbox_read agent=user`). Delete it with `cron_delete name="runner-selftest"`.

## Older setups

Without `RUNNER_TOKEN` the runner works as before: once, inside the container,
`claude mcp add --transport http aicortex https://agent.example.com/mcp` and the
connector's OAuth login, plus a model login or `ANTHROPIC_API_KEY`. Then the model
is started every minute and asks `cron_due` itself — switching to a runner key is
recommended.

## ⚠️ Verify on first run

- **Permissions:** with `RUNNER_TOKEN` the agent may call the connector's tools
  (`--allowedTools mcp__aicortex`), nothing else, and never the runner plumbing
  (`cron_due`, `runner_config`). Start with a **read-only** test job.
- **Safety:** the orchestrator only performs actions a job explicitly authorized
  (see `orchestrator.txt`); physical/irreversible/outbound actions are skipped
  unless the job pre-authorizes them — no one can confirm mid-run.
- **Subscription vs API key (ToS):** a consumer subscription is meant for
  interactive use — unattended automation is a grey area with tight limits. The
  API key is the sanctioned, stable choice. The runner only wakes the model when a
  job is due, which keeps usage small either way.

## Swap the agent

Set `RUNNER_CMD` to any headless agent CLI that takes the prompt as its last
argument and has the connector tools available:

```yaml
RUNNER_CMD: "my-agent --headless --prompt"
```

That agent is responsible for its own connector auth and model auth. Everything
else — the schedule, tools, memory, secrets — stays on the NAS.
