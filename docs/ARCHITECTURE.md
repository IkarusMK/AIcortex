# Architecture

AICortex keeps the **model in its provider's cloud (or local)** and everything that
makes the assistant *yours* — memory, skills, tools, secrets — **on your NAS**.
Your LLM reaches it through a single authenticated MCP custom connector.

```
LLM (cloud or local model)  ──HTTPS connector──>  reverse proxy  ──>  this server (NAS)
                                                                   │
                                   ┌───────────────┬───────────────┼───────────────┐
                                   ▼               ▼               ▼               ▼
                                memory          skills          tools           secrets
                              (files)         (files)        (code)          (local only)
```

## Design principles

- **File-based, human-readable state.** Memory and skills are plain files, not a
  database — so you can read, edit, back up, and debug them directly. (A future
  semantic index would be an *embedded* store in the same volume, not a DB server.)
- **Secrets never reach the model.** Credentials live in `secrets/` on the NAS;
  the server uses them server-side and only returns results.
- **Progressive disclosure.** Instead of loading everything, the assistant
  *searches* memory and skills and pulls in only what a task needs.

## Skill router

Skills are folders under `data/skills/`:

```
data/skills/
└── <skill-name>/
    ├── SKILL.md         # frontmatter: name, description, tags + the instructions
    └── ...              # optional reference files / resources
```

Tools:

| Tool             | Purpose                                              |
|------------------|------------------------------------------------------|
| `skill_search`   | Rank skills by relevance to a query (name + tags + description) |
| `skill_load`     | Return the full `SKILL.md` for a chosen skill        |
| `skill_resource` | Return a specific reference file from a skill        |

The assistant is told (via the client project instructions) to call
`skill_search` before specialized tasks, then `skill_load` the best match. This
is how the full skill library becomes usable from the desktop and mobile apps
without bundling it into the client.

## Memory namespacing (multi-agent seam)

Memory is addressed by **scope** so multiple agents can share one brain:

- `shared/` — common knowledge every agent sees (who you are, preferences, projects)
- `agents/<agent_id>/` — private notes for a specific agent

The memory tools take an optional `scope` argument (default: `shared`). A single
agent simply uses `shared`; multi-agent setups gain isolation **for free**, with
no schema change.

## Multi-agent readiness

| Seam | Status | Notes |
|---|---|---|
| Identity per agent | live | each agent gets its own key (WebUI → Connect clients); it acts for its person and is stamped as `person/agent` on memories, tasks and messages |
| Agent registry | live | `agent_register` for everyone; an agent name belongs to the person who registered it |
| Shared memory per person | live | all agents of one person share that person's memory area; `project` groups notes, tasks and sessions |
| Concurrent writes | live | file stores are locked and written atomically; `memory_write(expected_version=…)` refuses lost updates |
| Task board | live | leased claims (`task_claim(lease_minutes=…)`), handoff with a fresh lease, per-person views under enforcement |
| Agent inbox | live | `inbox_post` / `inbox_read` / `inbox_ack`, per-person views |
| Shared areas across people | planned | today: an admin grants `memory=all`; project-scoped sharing is the next step |
| Sub-agent orchestration | client-side | sub-agents of one client share its key; give a long-running sub-agent its own key to tell them apart |
