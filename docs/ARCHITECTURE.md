# Architecture

LLMConnector keeps the **model in Anthropic's cloud** and everything that
makes the assistant *yours* — memory, skills, tools, secrets — **on your NAS**.
Claude reaches it through a single authenticated MCP custom connector.

```
Claude (cloud model)  ──HTTPS connector──>  reverse proxy  ──>  this server (NAS)
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

The assistant is told (via the Claude project instructions) to call
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

| Seam                     | Status   | Notes |
|--------------------------|----------|-------|
| Namespaced memory        | designed | scope-addressed, see above |
| Shared skill/tool registry | designed | all agents query the same `skill_search` + tools |
| Per-agent workspaces     | designed | isolated dirs under `data/work/` |
| Agent inbox              | planned  | append-only agent-to-agent / agent-to-user channel |
| Sub-agent orchestration  | planned  | spawn/coordinate; builds on the seams above |

The goal is **no rewrite later**: the seams exist now; the orchestration layer
plugs in when it's actually needed.
