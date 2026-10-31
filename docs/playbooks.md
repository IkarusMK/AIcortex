# Playbooks

A playbook is multi-stage work where **a person approves every hand-off**. Each stage
produces an artifact — a short Markdown document such as `intent.md` or `plan.md` — that
the next stage reads. The AI does the legwork; the person decides when work moves on.

AICortex ships one template, **AI-native SDLC**, based on Anthropic's
["The AI-native SDLC playbook"](https://claude.com/resources/articles/the-ai-native-sdlc-playbook)
(21 August 2026) and written in our own words:

| Stage | Artifact | Approved by |
|---|---|---|
| Plan | `intent.md` — problem, proposed outcome, affected users and systems, constraints, open questions | Product owner |
| Design | `spec.md` — requirements and design, concerns for their owners | Product owner; tech lead for higher-risk changes |
| Build | `plan.md` — files that change, order of work, risks, proof | Engineer; architect for higher-risk changes |
| Test | `test-report.md` — build/test/lint output, eval results | Code owner |
| Deploy | `review.md` — findings by pass, release plan and rollback | Code owner; release manager for production |
| Maintain | `maintain.md` — what is watched, what happened, decision, lessons | Service owner or on-call |

It also lists the article's 14 *plays* (building blocks such as "plan mode by default"
or "hooks as approval gates") with their prerequisites; the six without prerequisites
are the place to start.

## Using it

**In the WebUI** (`/ui` → *Playbooks*): adopt the template in the *Library* tab (or write
your own with the stage editor), start a run for a project, and follow it in the *Runs*
tab. When the AI has submitted a stage's artifact you read it — text, repo path and
commit — and **approve** it or **reject** it with a note. You can also submit an
artifact yourself.

**From the AI** (through `integration_search` / `integration_call`, or listed directly
in the `full` tool profile):

| Tool | What it does |
|---|---|
| `playbook_list`, `playbook_search`, `playbook_load` | Browse the library; `load` shows every stage with goal, artifact, template and approval |
| `playbook_start(playbook, project, title)` | Start a run; returns the first stage's goal and template |
| `playbook_status(run_id \| project)`, `playbook_runs(project, status)` | Where runs stand and what to do next |
| `playbook_submit(run_id, artifact, ref_path, ref_commit, ref_url, note)` | Submit the current stage's artifact — text and/or where it is committed |
| `playbook_record_approval(run_id, decision, note)` | Record a decision the person gave **in this conversation** — only for stages that allow chat approval |
| `playbook_cancel(run_id)` | Cancel a run (history is kept) |
| `playbook_write`, `playbook_delete` | Manage the library (admin or the playbook's author) |

`cortex_start(project=…)` lists the project's active runs with their next step, so a
new conversation picks up where the last one stopped.

## Approval per stage

Each stage declares how it is approved:

- **`webui`** (default) — only a person in the AICortex WebUI can approve or reject.
- **`chat`** — the AI may record the person's own decision from the conversation. The
  decision is stored *via chat*. It is refused for agent keys, the scheduled-job runner
  and jobs acting for their owner: an AI never approves its own work.
- **`none`** — the stage moves on as soon as its artifact is submitted.

## Who sees what

Playbooks in the library are shared; a shared playbook is changed only by an admin or
its author. A run belongs to the person who started it; admins see all runs. Viewers
can read but not start, submit or decide.

## Storage and limits

- Library: `PLAYBOOKS_DIR/<name>/PLAYBOOK.md` (YAML header with the stages and plays,
  the guidance below it) — default `/data/playbooks`.
- Runs: `PLAYBOOKS_DIR/runs/<id>.json`, written atomically under a lock.
- An artifact holds at most 100,000 characters (link larger ones); each stage keeps its
  last 20 versions; a playbook has at most 20 stages.
