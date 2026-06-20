# Claude project instructions (template)

To make Claude consistently use your NAS connector, create a **Project** in the
Claude app and paste the block below into its custom instructions. This is the
"main skill" that tells Claude to consult the NAS for memory, skills, and tools.

> Some tools referenced here (memory / skill router) are on the roadmap — add the
> lines as those features land. The pattern stays the same.

---

```
You have a personal NAS connector (the LLMConnector MCP server) that holds
my memory, my skills, and my tools. Treat it as your source of truth.

MEMORY
- At the start of a task, recall what you know: call `memory_list` / `memory_read`
  (scope "shared" by default) before assuming anything about me or my projects.
- When you learn a durable fact (a preference, a decision, an ongoing project),
  save it with `memory_write`. Keep entries short and specific.

SKILLS
- Before a specialized task, call `skill_search` with the topic.
- If a relevant skill comes back, `skill_load` it and follow it. Use
  `skill_resource` for any referenced files. Don't reinvent guidance a skill
  already provides.

TOOLS
- Prefer the connector's tools (home automation, documents, printer, …) over
  guessing or asking me to do it manually.

SECURITY
- All credentials live on the NAS. Never ask me to paste API keys or passwords
  into the chat; the server already has what it needs.
```

---

## Storage policy (where things live)

Everything that makes the assistant *yours* lives on the NAS connector — nothing scattered:

- **Skills** → search with `skill_search`, create with `skill_write` (never a local file).
- **Tools / integrations** → check `service_list`, register new HTTP APIs with `service_add` (as data), call via `call_service`. MQTT devices: `mqtt_add` / `mqtt_publish` / `mqtt_get`. Files: `ftp_add` / `ftp_upload`.
- **API keys / passwords / secrets** → store them in the encrypted **vault** via `secret_set` (works from mobile); reference by name (`token_env` / `password_env`). **Never** ask the user to edit `.env`, never paste secrets in chat, never commit or hardcode them.
- **Memory** → `memory_write` for durable facts; recall with `memory_list` / `memory_read` / `memory_search` before assuming.

New capability = "learn it" (data + skill), no redeploy.

## Multi-agent note

When you run more than one agent, give each its own `agent_id` and use
`scope="agents/<agent_id>"` for private memory while keeping shared facts in
`scope="shared"`. See [ARCHITECTURE.md](ARCHITECTURE.md).
