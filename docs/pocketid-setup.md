# Set up Pocket ID as the login for AICortex

[Pocket ID](https://github.com/pocket-id/pocket-id) is a lightweight, self-hosted
OIDC provider. AICortex ships with a **Pocket ID-aware proxy** that forwards the
upstream identity (`sub`, `email`, `groups`), so per-person roles and per-user data
isolation work end-to-end. This guide is the concrete, click-by-click setup.

Throughout, replace the placeholders:

| Placeholder | Meaning |
|-------------|---------|
| `<pocketid-host>` | your Pocket ID URL, e.g. `id.example.com` |
| `<aicortex-host>` | AICortex's public URL, e.g. `agent.example.com` |

> **Prerequisites:** Pocket ID is running and reachable, and AICortex is served
> over **HTTPS** at `https://<aicortex-host>` through your reverse proxy (see the
> main README → *Quick start*). The login flow needs that public HTTPS URL.

---

## Step 1 — Create the OIDC client in Pocket ID

1. Sign in to Pocket ID as an admin → **OIDC Clients** → **Add OIDC Client**.
2. **Name:** `AICortex` (anything you like).
3. **Callback URL(s):** add exactly
   ```
   https://<aicortex-host>/auth/callback
   ```
   This must match `BASE_URL` + `/auth/callback` character-for-character (scheme,
   host, no trailing slash). A mismatch is the #1 cause of login failures.
4. Leave it as a **confidential client** (i.e. *not* "Public" / PKCE-only) so a
   **client secret** is generated — AICortex authenticates to Pocket ID with it.
5. **Save**, then copy the **Client ID** and **Client Secret** (the secret is shown
   once). You'll paste both into AICortex's `.env` next.

Your Pocket ID **discovery URL** is:
```
https://<pocketid-host>/.well-known/openid-configuration
```

---

## Step 2 — Point AICortex at Pocket ID (`.env`)

In AICortex's `.env` (see `.env.example`):

```env
OIDC_CONFIG_URL=https://<pocketid-host>/.well-known/openid-configuration
OIDC_CLIENT_ID=<the Client ID from step 1>
OIDC_CLIENT_SECRET=<the Client Secret from step 1>
BASE_URL=https://<aicortex-host>
JWT_SIGNING_KEY=<run: openssl rand -hex 32>
```

Then:

```bash
docker compose up -d        # restart AICortex with the new env
```

Finally, **(re-)add the connector** in your LLM client (URL
`https://<aicortex-host>/mcp`). It will send you through the Pocket ID login. After
a successful login you're in — you do **not** need to configure scopes for a basic
single-user setup; a successful login is enough.

> AICortex already sends a sane `scope` to Pocket ID's `/authorize`
> (`openid profile email`). This avoids a known Pocket ID quirk where a missing
> scope makes the login UI hang (`scope=null`). You don't need to do anything for
> this — it's handled — just don't strip the scope.

That's the whole login setup. **Single trusted user?** You can stop here — set
`AUTH_ENFORCE=0` for the simplest "everyone who logs in gets everything" homelab
posture.

---

## Step 3 — Roles & groups (optional, multi-user)

Only needed if **several people** share one AICortex and you want roles
(admin / user / viewer) and per-user data isolation.

1. **Pocket ID → User Groups:** create the groups you want to map, e.g.
   `AICortex-Admins` (and `AICortex-Users`, `AICortex-Viewers`), and add the right
   members. Make sure the group is associated with the AICortex client.
2. **Request the groups claim** — in AICortex `.env`:
   ```env
   AUTH_ENFORCE=1                          # the one switch: roles + per-user isolation + areas
   OIDC_SCOPE=openid profile email groups  # ask Pocket ID for the groups claim
   AUTH_ROLE_CLAIM=groups                  # which claim carries the role
   ```
3. **Map groups → roles** in `data/auth/policy.json`:
   ```json
   { "groups": { "AICortex-Admins": "admin", "AICortex-Viewers": "viewer" } }
   ```
4. Restart and re-add the connector.

See [authorization.md](authorization.md) for the full role/area model and the
`tenancy_*` admin tools.

---

## Step 4 — Verify it's working

Make one tool call from your client, then check the audit log on the NAS:

```bash
tail -n 10 <compose-dir>/data/auth/audit.log     # set AUTH_AUDIT_ALL=1 to log allowed calls too
```

You should see a line whose `identity` is your **Pocket ID `sub`** (a UUID) — not
`runner` or `unknown`. That confirms the Pocket ID-aware proxy is forwarding your
identity, so roles and isolation key off the real person.

> **Staged rollout (no lockout):** keep `OIDC_DEFAULT_ROLE=admin` (the default)
> until the audit log confirms your group resolves to `admin`. Only then tighten it
> to `user`/`viewer` so non-grouped logins get least privilege.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Login redirects then fails | The **Callback URL** in Pocket ID doesn't exactly match `BASE_URL` + `/auth/callback`. Fix the URL in the client. |
| `Bearer token rejected` (401) right after login | Behind a TLS-terminating proxy — set `FORWARDED_ALLOW_IPS: "*"` (already in `docker-compose.yml`). |
| Pocket ID "Sign in" spins forever | Missing scope on `/authorize`. Already handled by AICortex; override only via `OIDC_SCOPE` if needed. |
| Roles don't take effect | Confirm the `groups` claim actually arrives (audit log shows your `sub`; set `AUTH_AUDIT_ALL=1`), the group name matches `policy.json`, and `OIDC_SCOPE` includes `groups`. |

More auth/connection symptoms are in the main [README → Troubleshooting](../README.md#troubleshooting).
