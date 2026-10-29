# Client project instructions (template)

To make your LLM consistently use your NAS connector, add the block below to your
client's **project / custom instructions / system prompt**:

- **Desktop (Claude Code):** your `CLAUDE.md`, or a Project's custom instructions.
- **Claude mobile app:** Profile → custom/personal instructions.
- **Custom GPT / other agent:** its system prompt.

Most clients already pass the connector's short server instructions to the model
(Claude Code, Codex, ChatGPT, OpenClaw; LibreChat with `serverInstructions: true`),
and those tell it to call `cortex_start` at the start of a conversation. Pinning
the block below makes that explicit for clients that don't, and lets you name the
project once per client or project folder — the connector itself cannot force a
client to call a tool.

## First: enable the connector's tools in the chat

MCP clients only let the model **call** a connector's tools when they're enabled
for **that conversation** — adding the connector isn't always enough. If the
assistant says something like *"the AICortex tools aren't loaded as callable
functions in this session"* (and offers to reconnect or work manually), that's
exactly this: the tools aren't in the chat's active toolset. It's a client setting,
not an AICortex problem — and the model is right to say so rather than guess.

- **Claude web / mobile app:** open the conversation's tools / connectors menu (the
  `+` or tools icon) and make sure **AICortex is toggled on as a tool source** for
  the chat — not merely "present". If it won't stick, remove the connector, fully
  reopen the app, re-add it, and start a **fresh** chat.
- **LibreChat:** the `aicortex` MCP server appears in the chat model's tool dropdown
  (or add it to an Agent via *Add MCP Server Tools*); select it and **Authenticate** once
  via OAuth, then the model you picked can use it.

Once the tools are in the toolset, the assistant can call `cortex_start` — which
the pinned block below tells it to do first.

---

```
I use AICortex (an MCP connector) as my shared memory, skill library and tool hub
across all my AI apps.

- At the start of a conversation call cortex_start(project="<project name>",
  task="<what I asked for>"). It tells you who I am, how I want you to work and
  what's open in the project.
- Look things up before assuming (memory_search, skill_search). Save durable facts
  with memory_write (type + project) and a session_save checkpoint before you stop.
- Credentials stay in the AICortex vault — never ask me to paste them.
- Ask me before sending, posting, printing or deleting anything.
```

---

## Storage policy (where things live)

Everything that makes the assistant *yours* lives on the NAS connector — nothing scattered:

- **Skills** → search with `skill_search`, create with `skill_write` (never a local file). **Always categorize**: call `skill_list` first and reuse an existing `category`; only invent a new one when nothing fits. `skill_write` refuses an uncategorized skill — this house rule keeps the shared library tidy and `cortex_catalog` compact.
- **Tools / integrations** → check `service_list`, register new HTTP APIs with `service_add` (as data), call via `call_service`. **Give each service a `category`** (e.g. "Smart Home", "Dev", "Documents") — `service_add` refuses without one, so the catalog stays grouped and findable, exactly like skills. MQTT devices: `mqtt_add` / `mqtt_publish` / `mqtt_get`. Files: `ftp_add` / `ftp_upload`, cloud: `webdav_*`. Printing: `print_add` / `print_document`; scanning: `scan_add` / `scan_document`. Email: `mail_send` (SMTP) + `imap_search` / `imap_fetch` (IMAP read). Calendar: `caldav_list_events` / `caldav_add_event` (CalDAV). Webhooks: `webhook_add` (inbound → inbox) / `webhook_send` (outbound). Every register has a matching delete.
- **API keys / passwords / secrets** → store them in the encrypted **vault** via `secret_set` (works from mobile); reference by name (`token_env` / `password_env`). **Never** ask the user to edit `.env`, never paste secrets in chat, never commit or hardcode them.
- **Memory** → `memory_write` for durable facts (each is **typed** — `user` / `feedback` / `project` / `reference`; the catalog groups them into tiers 🧭 Core → 📂 Projects → 🛠 Working style → 🔗 References, with short-term/current state in the sessions layer). Recall with `memory_list` / `memory_read` / `memory_search` before assuming.

New capability = "learn it" (data + skill), no redeploy.

## Multi-agent note

When you run more than one agent, give each its own `agent_id` and use
`scope="agents/<agent_id>"` for private memory while keeping shared facts in
`scope="shared"`. See [ARCHITECTURE.md](ARCHITECTURE.md).

## Access & areas (multi-user)

If several people share one AICortex (enterprise mode, `AUTH_ENFORCE=1`), each
non-admin is confined to their own memory + vault and reaches only the
services/skills an **admin** granted them. An unexpected *"denied"* means a missing
area, not a bug — an admin grants access as data with
`tenancy_set(identity, services=…, skills=…)`. In homelab mode (`AUTH_ENFORCE=0`)
there are no limits. Scheduled work for a specific person runs **act-as** in that
user's area (`cron_add(..., owner=<sub>)`). Full guide: [authorization.md](authorization.md) · [per-user-areas.md](per-user-areas.md).
