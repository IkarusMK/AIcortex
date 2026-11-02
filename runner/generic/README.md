# AICortex runner (any model)

> **Usually not needed:** switch on the *built-in runner* in the WebUI (*Scheduled
> jobs → Set up the runner*) — it runs every model access, a Claude subscription
> token included, without a container. Use this runner if you want the Claude Code
> CLI itself or the model outside the connector.

The runner carries out the scheduled jobs that live on the connector. Once a
minute, on the minute, it asks `cron_due` — **without any model**, so an idle
minute costs nothing. Only when a job is due does it fetch the model access you
set up in the WebUI and start the model with the due jobs:

| Model access (WebUI) | How the runner runs it |
|---|---|
| Claude subscription (token from `claude setup-token`) | Claude Code CLI, with the connector as its only MCP server |
| Claude Code with an Anthropic API key | Claude Code CLI, as above |
| OpenRouter, Anthropic, OpenAI, Gemini (model name + key) | [LiteLLM](https://github.com/BerriAI/litellm) with the connector's tools |

The model key stays encrypted in the connector's vault. Only the runner can fetch
it (`runner_config`), once per run; the model working on a job never sees it.

## Set it up

1. **WebUI → Scheduled jobs → Set up the runner** (admin):
   - choose the model access, enter the model name if asked, paste the key, save;
   - **Create runner key** — copy the two lines it shows (shown only once):
     ```
     CONNECTOR_URL=https://agent.example.com/mcp
     RUNNER_TOKEN=ak_…
     ```
   For a Claude subscription, create the token on any machine where you are
   signed in to Claude Code: `claude setup-token`.

2. **Start the runner** in this folder, with those two lines in `.env`:
   ```bash
   docker compose up -d --build
   docker compose logs -f
   ```
   On a NAS with a compose UI you can skip the build: use the ready image
   `ghcr.io/ikarusmk/aicortex-runner` (same version tags as the connector) in
   place of `build: .`.

3. **Check it.** The *Scheduled jobs* page shows when the runner last checked in.
   Create a read-only test job, for example every two minutes:
   ```
   cron_add  name="runner-selftest"  schedule="*/2 * * * *"
             prompt="Report the connector's service list (service_list) in one line."
             notify="user"
   ```
   The summary arrives via your notify channel or the connector **inbox**
   (`inbox_read agent=user`). Delete it with `cron_delete name="runner-selftest"`.

## How a run works

- The runner aligns its checks to the clock; if a run takes longer, the next
  check simply waits for the next minute, and the connector hands out a missed
  minute for up to five minutes, once.
- A job with an **owner** runs in that person's area: the runner passes the
  job's short-lived act-as token to `act_as_begin` before the job and ends it
  afterwards.
- **Safety:** the runner works unattended and cannot ask anyone. It only performs
  read/query actions and actions a job's prompt explicitly authorized;
  physical, irreversible or outbound actions are skipped otherwise.

## Without the WebUI setup

Older setups keep working: without a model set up in the WebUI the runner uses
`LLM_MODEL` (any LiteLLM model string, e.g. `gpt-4o-mini`,
`anthropic/claude-sonnet-4-5`, `openrouter/…`) and that provider's key from the
`.env`. `RUNNER_TOKEN` may also be the static token set on the connector.

## Notes

- **Runner key vs. RUNNER_TOKEN:** both identify the runner (a least-privilege
  `user` unless `RUNNER_ROLE` says otherwise). A runner key needs no connector
  restart and can be revoked in the WebUI.
- **Subscription or API key:** Anthropic intends API keys for unattended
  automation; a consumer subscription is a grey area with tight limits. The
  runner only wakes the model when a job is due, which keeps usage small.
- **Model choice:** tool-calling quality varies by model — use a capable one.
