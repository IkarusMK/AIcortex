# Starter skills

A small, categorized set of **original** example skills to seed your skill library
and show how the category-aware router works. Each is a folder with a `SKILL.md`
(frontmatter `name` / `description` / `category` / `tags` + the instructions).

| Skill | Category |
|-------|----------|
| `conventional-commits` | Coding |
| `rest-api-conventions` | Coding |
| `docker-image-hardening` | DevOps |
| `home-assistant-rest` | Home Automation |
| `blameless-postmortem` | Documentation |
| `sql-index-review` | Data |
| `secret-handling` | Security |

## Seed them into your library
The image carries them: in the WebUI, **Skills → Add starter skills** copies every
one your library doesn't have yet (an existing skill of the same name is never
overwritten).

Without the WebUI, copy the folders into your live skill library (`data/skills`), then they show up
in `skill_list()` / `skill_search()` and grouped by category in `bootstrap`:

```bash
cp -r examples/skills/*/ data/skills/
# on the NAS the container chowns data/skills on (re)start:
docker compose up -d
```

## Add your own
`skill_write(name, description, instructions, tags, category)` — reuse an existing
category (see `skill_list()`), or start a new one. Keeping a `category` on every
skill is what keeps the library cheap to browse as it grows to hundreds.
