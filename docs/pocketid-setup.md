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
3. **Client type:** **Confidential Client** — AICortex has a backend and authenticates
   to Pocket ID with a client secret. A public client cannot have one.
4. **Callback URLs:** add exactly
   ```
   https://<aicortex-host>/auth/callback
   https://<aicortex-host>/ui/callback
   ```
   The first is for MCP clients, the second for the WebUI. Each must match `BASE_URL`
   plus the path character for character (scheme, host, no trailing slash). A mismatch
   is the #1 cause of login failures.
5. **Save**, then copy the **Client ID** from the **General** tab.
6. **Client secret:** open the **Credentials** tab → **Client secrets**. Since Pocket ID
   2.14 secrets live here rather than in the client form (several can be active at once,
   so you can rotate without downtime), and not every 2.x version creates one when the
   client is added — the card then says *This app has no client secrets yet.* Pick an
   **Expiration** (*No expiration* is simplest; with one, add a new secret and update
   `.env` before it runs out) and click **Add client secret**. The secret is shown
   **once** — copy it. You'll paste the ID and secret into AICortex's `.env` next.

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

That's the whole login setup. **Single trusted user?** You can stop here — you are
the operator (admin), and anyone else who signs in is a plain user.

---

## Step 3 — Roles & groups (optional, multi-user)

Only needed if **several people** share one AICortex and you want roles
(admin / user / viewer) and per-user data isolation.

1. **Pocket ID → User Groups:** create the groups you want to map, e.g.
   `AICortex-Admins` (and `AICortex-Users`, `AICortex-Viewers`), and add the right
   members. Make sure the group is associated with the AICortex client.
2. **Request the groups claim** — in AICortex `.env`:
   ```env
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
tail -n 10 <compose-dir>/data/auth/audit.log
```

You should see a line whose `identity` is your **Pocket ID `sub`** (a UUID) — not
`runner` or `unknown`. That confirms the Pocket ID-aware proxy is forwarding your
identity, so roles and isolation key off the real person.

> **No lockout:** you stay admin as an operator (`operators` in `policy.json`,
> filled on the first sign-in or the upgrade to 2.6.0). Other logins get `user`
> unless their group maps to a role. Also restrict the OIDC client to your group in
> Pocket ID (*Allowed User Groups*), so only your people reach AICortex at all.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Login redirects then fails | The **Callback URL** in Pocket ID doesn't exactly match `BASE_URL` + `/auth/callback`. Fix the URL in the client. |
| `Bearer token rejected` (401) right after login | Behind a TLS-terminating proxy — set `FORWARDED_ALLOW_IPS` to your proxy's source (the `docker-compose.yml` default trusts loopback and private networks; avoid `"*"`). |
| Pocket ID "Sign in" spins forever | Missing scope on `/authorize`. Already handled by AICortex; override only via `OIDC_SCOPE` if needed. |
| Roles don't take effect | Confirm the `groups` claim actually arrives (a refused call in the audit log shows your `sub`), the group name matches `policy.json`, and `OIDC_SCOPE` includes `groups`. |

More auth/connection symptoms are in the main [README → Troubleshooting](../README.md#troubleshooting).
