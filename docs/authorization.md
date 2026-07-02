# Authorization (roles & tool permissions)

AICortex authenticates callers (OIDC and/or the static `RUNNER_TOKEN`). On top of
that, the **authorization layer** decides *which* tools a caller may use. It is a
single central policy gate (a FastMCP middleware) implementing least-privilege:
deny-by-default tool permissions, per-credential identity binding, and an audit
log.

It is **on by default (secure by default)** and **fail-open** — if it ever can't
resolve a caller's identity it allows the call rather than locking anyone out.

## Roles

| Role | Can do |
|------|--------|
| **admin** | everything |
| **user** | everything **except** admin-only tools (registering services/devices/MCP servers, managing secrets, identities and per-user areas). A user **may** schedule their **own** cron jobs — those always run as that user (act-as). |
| **viewer** | read-only tools only (`*_list`, `*_search`, `*_read`, `*_load`, `bootstrap`, …) |

**Admin-only tools:** `service_add/delete`, `mqtt_add/delete`, `ftp_add/delete`,
`webdav_add/delete_endpoint`, `ssh_add/delete_endpoint`, `mail_add/delete_account`,
`imap_add/delete_account`, `print_add/delete`, `scan_add/delete`, `mcp_add/delete`,
`secret_set/delete`, `agent_register/remove`, and the per-user-area tools
(`tenancy_set/unset/show/list/status`).

> `cron_add`/`cron_delete` are **not** admin-only: a non-admin may schedule and
> delete their **own** jobs (owner-scoped), an admin any. `cron_due`/`cron_mark_run`
> and `act_as_begin`/`act_as_end` are the NAS runner's plumbing (runner/admin only).

## Defaults (what happens out of the box)

- An interactive **OIDC login → `admin`** (the human operator; never locked out).
- The shared **`RUNNER_TOKEN` → `user`** (the headless autonomy runner / a local
  model behind Open WebUI): it can use and read everything but **cannot register
  integrations, set secrets, or add cron jobs**. This is what contains a leaked
  token or a prompt-injected model.
- Every denial is written to `data/auth/audit.log`.

> **Note for the local-model setup:** Open WebUI connects with the `RUNNER_TOKEN`,
> so by default it runs as `user`. To let your local model register integrations
> or set secrets too, set `RUNNER_ROLE=admin` (see recipes) — or do those steps
> from your OIDC (admin) client.

## Configure with environment variables

| Variable | Default | Meaning |
|----------|---------|---------|
| `AUTH_ENFORCE` | `1` (on) | Set `0` to disable the layer entirely (everyone authenticated gets all tools). |
| `RUNNER_ROLE` | `user` | Role for the `RUNNER_TOKEN` (`admin`/`user`/`viewer`). |
| `OIDC_DEFAULT_ROLE` | `admin` | Role for OIDC logins with no explicit mapping. |
| `AUTH_ROLE_CLAIM` | `groups` | Token claim that carries the role/group (see *IdP-driven roles*). |
| `AUTH_AUDIT_ALL` | `0` | `1` also logs **allowed** calls, not just denials. |
| `MAIL_ALLOWED_RECIPIENTS` | _(unset)_ | Comma list of allowed `mail_send` recipients (addresses, `@domain`, or `domain`). Unset = unrestricted. |

## Per-identity roles — `data/auth/policy.json`

Optional. Most specific wins. Example:

```json
{
  "roles":  { "claude-desktop": "admin", "guest-client": "viewer" },
  "groups": { "AICortex-Admins": "admin", "Readers": "viewer" },
  "runner": "user",
  "default": "admin"
}
```

- `roles` — map a specific caller identity (the token's `client_id`) to a role.
- `groups` — map an **IdP group name** to a role (used with the role claim).
- `runner` / `default` — fallbacks (env vars override these).

**Precedence:** explicit `roles[identity]` → IdP role/group claim → runner/default.

## IdP-driven roles (e.g. Pocket ID groups)

The clean enterprise path is to manage roles in your identity provider: create
groups in Pocket ID (e.g. `AICortex-Admins`), add a groups/custom claim, and map
that group to a role here via `groups` in `policy.json` (or name the group
`admin`/`user`/`viewer` directly). AICortex reads the claim named by
`AUTH_ROLE_CLAIM` (default `groups`) and uses the highest-privilege match.

> **v1.4 — active for PocketID.** AICortex ships a PocketID-aware proxy that
> forwards the upstream identity (`sub`, `email`, `groups`) into the token under
> `upstream_claims`, so per-person identity and group→role mapping work end-to-end.
> Set it up (staged, no lockout):
> 1. **PocketID:** create groups (`AICortex-Admins`, `AICortex-Viewers`, …), add a
>    **groups claim** (it travels in the profile scope), assign users.
> 2. **Request it:** set `OIDC_SCOPE=openid profile email groups`.
> 3. **Map it:** `data/auth/policy.json` → `"groups": {"AICortex-Admins":"admin", …}`.
> 4. **Verify:** with `AUTH_AUDIT_ALL=1`, one tool call logs your PocketID `sub` as
>    `identity` and the role derived from your group in `data/auth/audit.log`.
> 5. **Tighten:** keep `OIDC_DEFAULT_ROLE=admin` until step 4 confirms your group
>    resolves to admin, then set it to `user` (or `viewer`) so non-grouped logins
>    get least privilege.
>
> The proxy is fail-safe: if the upstream token can't be read it simply omits the
> claims (behaves like the stock proxy), so the login path is never at risk.

## Per-user data isolation (multi-tenant)

Roles decide *which tools* a caller may use. **Areas** decide *which data and which
capabilities* they get within those tools — so people sharing one brain don't read
each other's notes, and not everyone may call every service.

It rides on the **same switch as authorization** — `AUTH_ENFORCE` (default on; no
separate `TENANCY_ISOLATE`). In homelab mode (`AUTH_ENFORCE=0`) nothing is confined;
with `AUTH_ENFORCE=1`, for each **non-admin** identity (the forwarded Pocket ID
`sub`, so it's per-**person**):

- **Private data** — confined to their **own memory scope** `users/<sub>` and **own
  vault namespace**. They can't read `shared` or another user's notes/secrets.
- **Shared capabilities** — **default-deny**: they reach only the **services and
  skills** an admin assigned (by name or category). Nothing assigned → nothing.
- **Admins** are never confined (full access to everything).

**Two safety stances:** private data is **fail-open** (a resolution glitch degrades
to the requested scope, never a lockout); shared capabilities are **fail-closed**
(any error in the check → deny, logged loudly to `audit.log`). In homelab mode no
checks run, so a defect can't strand anyone. Full detail: **[per-user-areas.md](per-user-areas.md)**.

Manage areas the easy way — **admin connector tools** (the assistant is the
dashboard, works from any device; stored as data, no redeploy):

| Tool | Does |
|------|------|
| `tenancy_status` | is enforcement on? how many users are configured? |
| `tenancy_set(identity, memory=…, vault=…, services=…, skills=…, note=…)` | create/update a user's area |
| `tenancy_show(identity)` / `tenancy_list` | inspect resolved areas |
| `tenancy_unset(identity)` | revert a user to the default |

`identity` is the person's Pocket ID `sub`. These are **admin-only**. `services` /
`skills` take `all`, `none`, or a comma-list of names and/or categories. Under the
hood they edit `data/auth/policy.json` (which you can also hand-edit):

```json
{
  "users": {
    "a1b2c3-pocketid-sub": {
      "memory": "own", "vault": "own",       // private data confined (the default)
      "services": ["github", "Documents"],    // only these services (name or category)
      "skills": "all"                          // every skill
    },
    "trusted-colleague": { "memory": "all", "vault": "all", "services": "all", "skills": "all" }
  }
}
```

### Per-user vault

With isolation on, each non-admin caller also gets a **private vault namespace**.
The model is *admin provisions, user consumes* — users can't create secrets:

- `secret_set(name, value, owner="<sub>")` (admin-only) stores a secret in that
  user's namespace; without `owner` it's a shared secret.
- At call time, `get_secret` resolves the **caller's own** secret first, then the
  shared one — so an admin can give one user their own token for a shared service.
- `secret_list` shows a confined user only the shared secrets plus their own
  (names only, never values); an admin sees all, tagged by owner.

Grant a user the full (admin-like) vault by setting `"vault": "all"` in their
`policy.json` entry (default is `"own"`).

### Assigning rights — a worked example (generic)

Enterprise mode (`AUTH_ENFORCE=1`), goal: admins full access; staff who may use the
document tools but not the dev/API services; read-only guests.

**1 · Roles from IdP groups.** Create the groups in your provider, request the claim,
map them once:
```json
// data/auth/policy.json
{ "groups": { "AICortex-Admins": "admin", "AICortex-Staff": "user", "AICortex-Guests": "viewer" } }
```
Set `OIDC_SCOPE=openid profile email groups`, and `OIDC_DEFAULT_ROLE=user` so a login
with no group gets least privilege.

**2 · Per-person override (optional).** Pin one identity regardless of its groups:
```json
{ "roles": { "<a-pocketid-sub>": "admin" } }
```

**3 · Per-user areas.** Roles gate the *tools*; areas gate the *data & capabilities*.
Grant a staff member only the document services + one skill — from any device, no
redeploy:
```
tenancy_set("<staff-sub>", services="paperless, Documents", skills="web-reader")
tenancy_show("<staff-sub>")      # check what actually resolves
```
They now reach only those services/skills (everything else default-denied), plus
their own memory + vault. Widen with `services="all"`, lock out with `"none"`, or
revert entirely with `tenancy_unset("<staff-sub>")`.

**4 · Verify.** With `AUTH_AUDIT_ALL=1`, one call from that user logs their `sub`,
the resolved role and the allow/deny decision in `data/auth/audit.log`.

## Recipes

**Let the local model (RUNNER_TOKEN) be admin too**
```bash
# in .env
RUNNER_ROLE=admin
```

**Add a read-only viewer client**
```json
// data/auth/policy.json
{ "roles": { "<that-client-id>": "viewer" } }
```

**Turn the whole layer off (old all-access behaviour)**
```bash
# in .env
AUTH_ENFORCE=0
```

**Full audit (log every allowed call, not just denials)**
```bash
AUTH_AUDIT_ALL=1
```

**Restrict who mail_send may email**
```bash
MAIL_ALLOWED_RECIPIENTS=@yourcompany.com, alerts@example.com
```

## Audit log

`data/auth/audit.log` is JSON-lines, one record per decision:

```json
{"ts":"2026-06-29T20:11:04+00:00","identity":"runner","role":"user","tool":"service_add","decision":"deny","reason":"admin-only tool (registration/secrets/identity)"}
```

Denials are always logged; allowed calls only with `AUTH_AUDIT_ALL=1`.

## Finding the caller's identity

The identity used for `roles[identity]` is the token's `client_id`. The easiest
way to see what a given client presents is to make a denied call and read the
`identity` field in `data/auth/audit.log`, then map it in `policy.json`.
