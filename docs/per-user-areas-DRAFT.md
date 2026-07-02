# Per-user Service/Skill areas + per-user Cron (act-as) — DRAFT for review

> Task **f821eab9**. Prepared on branch `feature/per-user-areas` — **not pushed, not
> deployed**. Backward-compatible: with `TENANCY_ISOLATE` off (today's setup) nothing
> changes. Review the open decisions below, then we push + cut a release.

## What this adds

Tenancy so far confined **private data** (memory scope, vault namespace) per user.
This extends the same control plane to **shared capabilities** and to **scheduled
jobs**:

1. **Per-user service areas** — which registered services a user may see/call.
2. **Per-user skill areas** — which skills a user may see/load.
3. **Per-user cron (act-as)** — a job can run *as* a specific user, in that user's area.

## Data model (`data/auth/policy.json`, all optional)

```json
{
  "users": {
    "<sub>": {
      "memory":   "own" | "all",
      "vault":    "own" | "all",
      "services": "all" | "none" | ["github", "Documents"],
      "skills":   "all" | "none" | ["web-seite-lesen", "Programmierung"],
      "note": "…"
    }
  }
}
```

- `services` / `skills` allow-list entries match a **name OR a category** (so
  `"Documents"` grants every service in that category; `"github"` grants just that one).
- A comma/space string (`"github, Documents"`) is accepted and normalised to a list.

### Two deliberate default stances
| Class | Default | Why |
|------|---------|-----|
| memory, vault (**private data**) | `own` (confined) | two people must not see each other's notes/secrets by accident |
| services, skills (**shared capability**) | `all` | capabilities are meant to be shared; an admin *opts into* narrowing |

## Control plane (admin tools, unchanged surface)

`tenancy_set` now takes `services=` and `skills=`:

```
tenancy_set("alice-sub", services="github, Documents", skills="Web")
tenancy_set("bob-sub",   services="none")          # lock bob out of all services
tenancy_set("carol-sub", services="all")           # explicit full access
```

`tenancy_show` / `tenancy_list` / `tenancy_status` now display the service/skill areas too.

## Enforcement points

Enforced **inside the tools** (same pattern as `secret_list`), via new pure helpers
`tenancy.caller_service_allowed()` / `caller_skill_allowed()`:

- `call_service` — denies a service not in the caller's set.
- `service_list` — hides services the caller may not use.
- `skill_load` / `skill_resource` — deny a disallowed skill.
- `skill_search` / `skill_list` — hide disallowed skills (and adjust counts).

All checks are **fail-open** (isolation off, admin, unresolved caller, or any error →
allowed), consistent with the rest of tenancy/authz so a glitch never locks anyone out.

## Cron act-as

- `cron_add(..., owner="<sub>")` — stores the act-as identity.
- **Escalation guard** (`tenancy.act_as_owner`): empty owner = no act-as (runner
  default). An **admin** may set any owner; a **non-admin** may set only *themselves*.
- `cron_due` now returns `owner` per job so the runner knows whom to run as.
- `cron_list` shows the owner.

## Tests

`tests/test_per_user_areas.py` — 24 pure-function checks (allow-list by name &
category, `all`/`none`, defaults, admin bypass, isolation-off backward compat,
fail-open, act-as escalation guard, string-list parsing). All pass.

---

## OPEN DECISIONS — need your sign-off before push

1. **Default stance for services/skills = `all`.** I chose shared-by-default (narrow
   on demand). If you'd rather have enterprise **default-deny** (a new user sees
   nothing until granted), that's a one-line change in `_access_spec`. → *keep `all`?*

2. **`cron_add` is still admin-only** (it's in `authz.ADMIN_TOOLS`). So today only an
   admin can schedule — the act-as guard is ready but the "a normal user schedules
   their own job" path is closed. Do you want to **let non-admins create their own
   cron jobs** (they'd be forced to `owner = themselves`)? That means removing
   `cron_add`/`cron_delete` from `ADMIN_TOOLS`. → *open cron to users, or keep admin-only?*

3. **Runner act-as contract (NOT yet implemented — needs your trust model).** The
   connector now *exposes* `owner` in `cron_due`, but for a job to actually run *in*
   the owner's area the NAS runner must present that identity to the connector. Options:
   - a signed **act-as header** only the runner (RUNNER_TOKEN) may use, mapped to the
     owner's area server-side; or
   - the runner holds a per-user token.
   This is the one piece I deliberately stopped at — it changes how the runner
   authenticates. → *which trust model?* Once chosen I'll wire it in `authz`/`tenancy`.

4. **Fail-open on capability access.** Consistent with the codebase, but for a hard
   multi-tenant deployment you may want service/skill checks to **fail-closed**. →
   *keep fail-open (never lock out) or fail-closed for capabilities?*

## Deploy / verify (after we agree + push)

```bash
cd /volume2/docker/AIcortex && git pull && docker compose pull && docker compose up -d --force-recreate
# then, as admin:
tenancy_status
tenancy_set("<a-test-sub>", services="github")
tenancy_show("<a-test-sub>")
```
Version bump + CHANGELOG happen **as part of cutting the release** (per our rule:
the stamp only moves with a release).
