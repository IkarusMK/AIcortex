# Per-user Service/Skill areas + per-user Cron (act-as)

> Task **f821eab9**, branch `feature/per-user-areas`. Extends tenancy from private
> data (memory/vault) to shared capabilities (services/skills) and to scheduled jobs
> that run **as** a specific user. Reviewed decisions from 2026-07-02 are baked in.

## The switch — one flag: `AUTH_ENFORCE`

Areas ride on the same switch as authorization ("enforce means enforce" — no separate
`TENANCY_ISOLATE`):

- **`AUTH_ENFORCE=0` (homelab):** no checks at all. Every caller uses all data and all
  capabilities, exactly as before. A defect can never lock anyone out.
- **`AUTH_ENFORCE=1` (default, enterprise):**
  - **private data** (memory, vault): each non-admin is confined to their own scope;
  - **shared capabilities** (services, skills): **default-deny** — a user reaches only
    what an admin assigned;
  - **admins** are never confined.

## Data model (`data/auth/policy.json`)

```json
{
  "users": {
    "<sub>": {
      "memory":   "own" | "all",
      "vault":    "own" | "all",
      "services": "all" | "none" | ["github", "Documents"],
      "skills":   "all" | "none" | ["web-seite-lesen", "Programmierung"]
    }
  },
  "roles": { "<sub>": "admin" | "user" | "viewer" }
}
```

`services`/`skills` entries match a **name OR a category**. Under enforce, a user with
no entry (or no `services`/`skills` field) gets **nothing** for that class.

**Device endpoints too (since 1.8.0).** The same model covers the device registries —
`caldav`, `imap`, `webdav`, `ssh`, `mail`, `print`, `scan`, `mqtt`, `ftp`, `mcp`. Add a
per-class key to the user's entry (`"caldav": ["nextcloud-cal"]`, `"ssh": "all"`, …) or
set them with `tenancy_set(identity, grant="caldav=nextcloud-cal; ssh=all; imap=none")`.
Default-deny under enforce; enforced centrally in the authz middleware for every device
**action** tool (`caldav_add_event`, `ssh_run`, `imap_search`, `webdav_download`, …).

Admin control plane: `tenancy_set(identity, services="github, Documents", skills="Web")`,
`tenancy_show`, `tenancy_list`, `tenancy_status`.

## Enforcement (fail-closed)

Enforced inside the tools (like `secret_list`) via `tenancy.caller_service_allowed()` /
`caller_skill_allowed()`:

- `call_service` denies a disallowed service; `service_list` hides them.
- `skill_load`/`skill_resource` deny; `skill_search`/`skill_list` hide (and adjust counts).

**Fail-closed under enforce:** any error in the check path → **deny**, written loudly
to the audit log (`area-check failed → deny: …`). In homelab mode no checks run, so a
defect never strands anyone.

## Cron act-as — capability token per job

A job may run **as** an owner, confined to that owner's area. The runner never holds a
standing power of attorney — it gets a short-lived, per-job token.

- `cron_add(..., owner=…)`:
  - a **non-admin** may schedule only as **themselves** (job is force-tagged with their
    sub; no escalation);
  - an **admin** may set any owner, or leave it empty (runs as the runner default).
  - `cron_add`/`cron_delete` are no longer admin-only; `cron_list`/`cron_delete` show/act
    on only the caller's own jobs (admin sees all).
- `cron_due` (runner/admin only, blocked during an active run) mints a per-job token
  (`actas.py`: HMAC-SHA256, key HKDF-derived from `STORAGE_ENCRYPTION_KEY`; claims
  `job/sub/iat/exp/jti`; 5-min TTL). If it can't mint (no key), the job is **withheld**,
  never handed over unconfined.

### Runner contract (long-lived, sequential runner)

Per due job the runner:

1. `act_as_begin(act_as_token, job_id)` — validates (signature, expiry, job match,
   single-use) and switches the connector to the owner. All subsequent tool calls are
   gated + scoped as the owner (their memory/vault/services/skills), at the owner's own
   privilege (never the default-admin).
2. runs the job's `prompt`;
3. `act_as_end()` — drops the binding and **invalidates the token** (single-use);
4. `cron_mark_run(id)`.

`cron_due` / `act_as_begin` are refused **while a run is active**, so a running job
(whose calls share the runner's connection) can't harvest other jobs' tokens or nest
identities.

## Tests

- `tests/test_per_user_areas.py` — 31 checks: homelab vs enforce, default-deny,
  allow-list by name/category, admin bypass, corrupt-policy fail-closed, act-as owner
  guard, `effective_identity` (owner never default-admin), end-to-end act-as capability
  scoping, single-use replay guard.
- `tests/test_actas.py` — 10 checks: token issue/verify, tamper, expiry, no-key
  fail-closed, key rotation.

## Deploy / verify (after merge + release)

```bash
cd /volume2/docker/AIcortex && git pull && docker compose pull && docker compose up -d --force-recreate
# as admin:
tenancy_set("<sub>", services="github")   # grant
tenancy_show("<sub>")
```

Version bump + this doc's CHANGELOG line happen **when the release is cut** (the version
stamp only moves with a release).
