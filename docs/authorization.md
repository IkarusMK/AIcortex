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
| **user** | everything **except** admin-only tools (registering services/devices/MCP servers, scheduling cron, managing secrets and agents) |
| **viewer** | read-only tools only (`*_list`, `*_search`, `*_read`, `*_load`, `bootstrap`, …) |

**Admin-only tools:** `service_add/delete`, `mqtt_add/delete`, `ftp_add/delete`,
`webdav_add/delete_endpoint`, `ssh_add/delete_endpoint`, `mail_add/delete_account`,
`print_add/delete`, `scan_add/delete`, `mcp_add/delete`, `cron_add/delete`,
`secret_set/delete`, `agent_register/remove`.

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
