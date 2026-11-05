# Scheduled jobs and runners — step by step

**In one sentence:** you tell your AI what should happen when, it stores the job in
AICortex, and when the job is due AICortex wakes a **runner** that carries it out and
reports back to your inbox.

```
"Tomorrow at 8: summarise yesterday's notes."
   → your AI: cron_add(...)            → the job is stored in AICortex
   → 08:00: AICortex wakes your runner → the runner does the job through AICortex
   → the result: "Last run" in Scheduled jobs and a message in your inbox
```

You set up a runner **once**. After that you never configure anything again — you
just ask.

---

## 1. Set up a runner (once)

WebUI → **Runners** → **New runner**. Pick the kind that fits:

| Kind | Runs on | You need |
|---|---|---|
| **Claude (wake a cloud task)** | your Claude plan, in Anthropic's cloud | a Claude plan with Claude Code on the web |
| **ChatGPT plan** *(admins)* | your ChatGPT Plus/Pro plan | to sign in with ChatGPT once |
| **API key** — OpenRouter, OpenAI, Anthropic, Gemini | the provider, billed per use | a key: typed in or picked from your vault |
| **OpenAI-compatible** *(admins)* | your own server, e.g. Ollama | its address, allowed in `INTERNAL_ALLOW_CIDRS` |

Your **first runner is your default**: jobs that don't name a runner go there. Have
more than one? Choose with **Make my default**.

### Claude (wake a cloud task)

1. In the runner form choose **Claude (wake a cloud task)**. It shows one sentence —
   click **Copy**.
2. On **claude.ai/code/routines** click **New task** (if asked: *Set up manually*):
   - **Name:** e.g. `AICortex`
   - **Prompt:** exactly the copied sentence — once, it covers every job
   - **Trigger:** **API**
   - **Advanced settings → Connectors:** keep **AICortex** (remove the others if you like)
   - **Create**, then **Generate token** on the API trigger. The address and the token
     are shown only once.

   Tip: in Claude Code, `/schedule` can create the task for you — you then only add the
   API trigger and generate the token in the browser.
3. Back in AICortex: paste the **API address** and the **token**, **Save**.
4. **Test** → after confirming, the runner shows **Open the Claude session ↗**: Claude
   answered a harmless check.

The token is stored encrypted in your vault and never shown again. If you generate a
new one later, the old one stops working — paste the new one into the runner.

### API key (e.g. OpenRouter)

1. Put the key in the vault (WebUI → **Vault**), e.g. as `OPENROUTER_API_KEY` — or type
   it straight into the runner form.
2. **New runner** → kind **OpenRouter** → key **From the vault: OPENROUTER_API_KEY** →
   **Model** as the provider names it (e.g. `vendor/model`), **Save**, **Test**.

The model must support **tool calling**, or it can't use AICortex. OpenRouter's free
models end in `:free`; not all of them call tools well, and they have daily limits — a
**Test** and one small job tell you whether a model works for you.

**Free models only** (ticked for a new OpenRouter runner): the runner then only takes a
model whose name ends in `:free` — checked when you save and again before every request,
so it can't run up a bill. For a runner from before 2.3.4, tick it under **Edit**. A
spending limit on the OpenRouter key itself is a good second fence.

### ChatGPT plan *(admins)*

**New runner** → **ChatGPT plan** → **Sign in with ChatGPT** → open the sign-in, sign
in and allow AICortex to use your plan. The browser then shows an error page from
`127.0.0.1` — that's expected: copy the whole address from the address bar into the
field and **Complete sign-in**.

### Your own model *(admins)*

**New runner** → **OpenAI-compatible** → address ending in `/v1`, e.g.
`http://192.168.1.20:11434/v1` for Ollama, the model name, **Save**, **Test**. A LAN
address must be allowed in `INTERNAL_ALLOW_CIDRS`. Small local models often stumble
over tools — try one small job first.

---

## 2. Hand out jobs

Just tell your AI — any app connected to AICortex with **your own sign-in** (Claude,
ChatGPT, Claude Code, …):

| You say | The job |
|---|---|
| "Tomorrow at 8: summarise yesterday's notes." | one-off, switches itself off after its run |
| "Every Monday at 7:30: list my open tasks." | `30 7 * * 1` |
| "On the 1st of each month at 9: check the release notes of my project." | `0 9 1 * *` |

What the AI does: `cron_add(name, schedule, prompt[, once, runner])`.
- **schedule** is a 5-field cron expression in the **server's time**
  (`minute hour day month weekday`).
- **once=True** for a one-off job ("tomorrow at 8" = `0 8 <day> <month> *`).
- **runner** only when the job should not go to your default runner (`runner_list`
  shows your runners).
- The **prompt must stand on its own**: the runner starts without your conversation.

Or yourself: WebUI → **Scheduled jobs** → **New job** (presets, a preview of the next
runs, *Only once*, runner).

---

## 3. See what happened

- **Scheduled jobs:** *Last run*; hover it for the result.
- **Runners:** the state of each runner; for Claude the link to the last session.
- **Inbox:** the runner sends a short summary to the job's recipient (default: you).

---

## Good to know

- **Jobs run unattended.** A runner only reads and queries, and does what the job's
  prompt explicitly allows — nothing physical, irreversible or outbound otherwise.
- **A Claude runner acts as you**, with your rights in AICortex and all your claude.ai
  connectors. It therefore only runs jobs **you wrote yourself** — never ones written
  with an agent key or by someone else.
- **Runners that AICortex asks itself** (API key, ChatGPT, own model) confine every job
  to its owner's area; each job gets five minutes.
- **Limits:** a Claude cloud task can be woken 30 times an hour (AICortex keeps two
  minutes between wake-ups); a prompt has at most 20,000 characters; a person has at
  most 10 runners.
- **Updates:** jobs, runners, playbooks and everything else you create live in AICortex's
  data folder and stay through every update.

## When something doesn't run

| You see | Why | What to do |
|---|---|---|
| Runner: *no usable key/token stored* | no key yet, or the vault entry holds spaces or line breaks | enter the key or token again |
| Claude: *Anthropic answered HTTP 401* | the token is wrong or was regenerated | generate a token, paste it into the runner |
| Claude: *HTTP 404* | the address is wrong or the cloud task was deleted | check the API address; recreate the task |
| The job never runs | the runner is off; no runner; a Claude runner and a job someone else wrote | switch it on; set a runner; write the job yourself |
| *Not sent: … too long* | the job's prompt doesn't fit a wake-up | shorten the prompt |
| ChatGPT: *usage limit* | the plan's limit is used up | wait — the message links to the usage page |
| Own model: *may not reach* | the address isn't allowed | add its network to `INTERNAL_ALLOW_CIDRS` |
| Model: *did not finish within 300 s* | the model is too slow or loops | a faster model or a shorter job |

More on rights: [authorization.md](authorization.md).
