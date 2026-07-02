# Client project instructions (template)

To make your LLM consistently use your NAS connector, add the block below to your
client's **project / custom instructions / system prompt**:

- **Desktop (Claude Code):** your `CLAUDE.md`, or a Project's custom instructions.
- **Claude mobile app:** Profile → custom/personal instructions.
- **Custom GPT / other agent:** its system prompt.

This is the "main skill" that tells the assistant to treat the NAS as its **only**
brain and to call `bootstrap` first, every session. Clients that surface the
connector's own `instructions` already get most of this — pinning the block makes
it explicit and reliable, and is the one thing that genuinely enforces
"bootstrap-first, exclusively AICortex" (the connector itself cannot force a client
to call a tool).

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
- **Open WebUI:** the connector lives under *Admin → External Tools*; make sure the
  model you picked is allowed to use it.

Once the tools are in the toolset, the assistant can call `bootstrap` — which the
pinned block below tells it to do first.

---

```
You have a personal NAS connector (the AICortex MCP server) that holds
my memory, my skills, and my tools. It is your ONLY source of truth — work
exclusively through it, never from scattered local notes or assumptions.

START
- At the very start of every session, call `bootstrap` first — every single time.
  One call loads the guide + a live catalog of all memory, skills, services,
  devices and sessions. Don't rely on prior assumptions about me — load
  `bootstrap`, then act. If unsure whether it's loaded this session, call it again.
- Write durable new knowledge back to the connector (memory_write / skill_write)
  so the brain grows instead of drifting.

MEMORY
- Recall what you know: `memory_list` / `memory_read` (scope "shared" by default)
  before assuming anything about me or my projects.
- When you learn a durable fact (a preference, a decision, an ongoing project),
  save it with `memory_write`. Keep entries short and specific.

SKILLS
- Before a specialized task, call `skill_search` with the topic.
- If a relevant skill comes back, `skill_load` it and follow it. Use
  `skill_resource` for any referenced files. Don't reinvent guidance a skill
  already provides.

TOOLS
- Prefer the connector's tools over guessing or doing it manually — home automation
  & devices (MQTT), documents & scanning, printing, cloud files (WebDAV), calendar
  (CalDAV), email (read via IMAP, send via SMTP), SSH, and inbound/outbound webhooks.
  Check `service_list` and the `bootstrap` catalog for what's registered.

ACTIONS
- Read/query freely. But CONFIRM with me before a physical, state-changing or
  outbound action — sending mail, printing, creating/deleting a calendar event,
  posting a webhook, deleting or overwriting anything.

SESSIONS
- Save a `session_save` checkpoint after each milestone and before you stop, so a
  different model or device can resume where you left off.

SECURITY
- All credentials live on the NAS. Never ask me to paste API keys or passwords
  into the chat; the server already has what it needs.
```

---

## Storage policy (where things live)

Everything that makes the assistant *yours* lives on the NAS connector — nothing scattered:

- **Skills** → search with `skill_search`, create with `skill_write` (never a local file). **Always categorize**: call `skill_list` first and reuse an existing `category`; only invent a new one when nothing fits. `skill_write` refuses an uncategorized skill — this house rule keeps the shared library tidy and `bootstrap` compact.
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
