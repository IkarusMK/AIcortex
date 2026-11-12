# INSTALL_FOR_AGENTS.md — AICortex

> **Human:** paste this one line into your coding agent (Claude Code, Cursor, etc.):
>
> ```
> Retrieve and follow the instructions at:
> https://raw.githubusercontent.com/IkarusMK/AIcortex/main/INSTALL_FOR_AGENTS.md
> ```
>
> The agent will install AICortex end to end and verify it. Steps only a human can
> do (DNS, reverse proxy UI, OIDC client, adding the connector in the LLM app) are
> clearly handed back to you.

---

## You are an AI coding agent. Follow these instructions exactly.

**Goal:** a running AICortex container on the user's NAS/server, reachable over
HTTPS, secured with the user's own OIDC provider, and verified with a successful
`cortex_start` call from their MCP client.

**Ground rules while installing:**
1. **Confirm before state-changing actions** (starting containers, overwriting files).
2. **Never print or store secrets in chat, code or git.** Secrets go into `.env`
   (installation only) or later into the AICortex vault (`secret_set`).
3. If the user runs commands themselves (hands-off setups), output ready-to-paste
   commands instead of executing.
4. Verify every phase before moving to the next. Do not skip verification.

---

## Phase 0 — Gather facts (ask the user, don't assume)

Ask, then wait for answers:

1. Where should AICortex run? (NAS/server with **Docker Compose v2** required)
2. Do you have a **domain/subdomain** and a **reverse proxy** (Zoraxy, Caddy,
   nginx, Traefik)? Cloud LLM clients connect from the provider's cloud, so the
   endpoint must be reachable from the internet.
3. Which **OIDC provider**? (Pocket ID, Authentik, Keycloak, Auth0 — anything with
   standard OIDC discovery.) If none: recommend Pocket ID and point to
   `docs/pocketid-setup.md`.
4. **Build locally or pull the prebuilt image** from GHCR? (Prebuilt = faster.)
5. **Homelab mode** (one trusted person, every tool open) or **Enterprise mode**
   (roles + per-user isolation)?

Verify prerequisites on the target machine:

```bash
docker --version && docker compose version
```

## Phase 1 — Clone & configure

```bash
git clone https://github.com/IkarusMK/AIcortex.git
cd AIcortex
cp .env.example .env
```

Edit `.env`: set `PUID` / `PGID` (owner of the data dir), `HOST_PORT`
(default 8787) and `TZ`. Leave the OIDC variables empty for now — without them the
server binds to `127.0.0.1` only, which is safe for first-boot testing.

## Phase 2 — Start & smoke-test

**Option A — build locally:**
```bash
docker compose up -d --build
```

**Option B — prebuilt image (no build):** in `docker-compose.yml` comment out
`build: .`, uncomment `image: ghcr.io/ikarusmk/aicortex:latest`, then:
```bash
docker compose pull && docker compose up -d
```

**Verify:**
```bash
docker compose logs --tail 30   # startup banner (incl. version), no errors
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8787/mcp   # expect an HTTP response, not a connection error
```

## Phase 3 — Reverse proxy (human step — hand this back)

Tell the user to:

1. Point a subdomain (e.g. `agent.example.com`) at the reverse proxy.
2. Proxy that host to `http://<nas-ip>:8787` over HTTPS. The upstream is **plain
   HTTP** — do **not** enable "TLS to upstream". If the proxy geo-blocks, allow
   the LLM provider's egress region.
3. Do **not** put SSO/forward-auth in front of `/mcp` — an MCP connector is a
   machine client and cannot follow interactive login redirects. Auth happens at
   the MCP layer (next phase).

Offer to generate the proxy config snippet for their specific proxy.

**Verify:** `curl -s -o /dev/null -w "%{http_code}\n" https://agent.example.com/mcp`
returns an HTTP status (401/406 is fine at this point — it means the proxy path works).

## Phase 4 — OIDC authentication

1. **Human step:** register a confidential OAuth client in the OIDC provider with the
   redirect URIs `https://agent.example.com/auth/callback` and
   `https://agent.example.com/ui/callback`. (Pocket ID users: follow
   `docs/pocketid-setup.md` click by click — since Pocket ID 2.14 the client secret is
   created in the client's **Credentials** tab, not shown in the form.)
2. Fill `.env`:

```env
OIDC_CONFIG_URL=https://id.example.com/.well-known/openid-configuration
OIDC_CLIENT_ID=<from the provider>
OIDC_CLIENT_SECRET=<from the provider>
BASE_URL=https://agent.example.com
JWT_SIGNING_KEY=<run: openssl rand -hex 32>
```

3. Restart: `docker compose up -d`

## Phase 5 — Roles (nothing to switch)

Roles (admin / user / viewer), per-user memory + private vault and default-deny
service/skill areas are always on. The first person to sign in becomes the operator
(admin), or name operators with `OPERATORS=<sub>`. To map IdP groups to roles:
```env
OIDC_SCOPE=openid profile email groups
AUTH_ROLE_CLAIM=groups
```
Areas are managed with the `tenancy_*` admin tools or the WebUI. Full guides:
`docs/authorization.md` and `docs/per-user-areas.md`. Restart after changing.

## Phase 6 — Add the connector (human step — hand this back)

In the MCP client (Claude, ChatGPT, any MCP-capable app): **add a custom
connector / MCP server** with URL `https://agent.example.com/mcp`. The client will
be sent through the OIDC login once.

## Phase 7 — Final verification (the litmus test)

Ask the user to tell their assistant:

> "Call `cortex_start`."

- The answer starts with `AICortex v<version> — briefing` → connector, proxy and
  auth chain work, and it shows the running version.
- It lists who the user is, how they work and their projects → **installation
  complete.** On a fresh install the briefing is short because the memory is
  still empty.

(`ping` is not among the eleven tools a client sees up front — the default `core`
profile — so a model would first have to find it with `integration_search`.)

For clients that don't pass the server instructions to the model, pin the short
block from `docs/client-project-instructions.md` into the client's custom
instructions.

## Phase 8 — People, keys and more clients (human steps in the WebUI)

Walk the user through `docs/clients.md` → *Step by step: people, keys and the first
clients*. In short:

1. Open `https://agent.example.com/ui` (the bare host shows `404`) and sign in
   through the IdP. Every OIDC sign-in is admin unless a role is set.
2. **Users** page: set role and memory area per person. With a
   single owner, give the owner's identity the memory area **everything** — keys
   for that person otherwise see only their own, empty area, not the shared notes
   the chat apps write.
3. **Connect clients** page: one key per agent or CLI (Claude Code, Codex, Hermes,
   OpenClaw, LM Studio, …) — person, tools profile, allowed tools, expiry. The key
   is shown once; the page shows the setup snippet per client. Never paste the key
   into chat; the user stores it (macOS keychain, the client's secret store).
4. **Claude Code:** offer the start hook from `docs/clients.md` → *Claude Code*
   (`clients/claude-code/session_start.py`, key from the keychain, a key with
   allowed tools `cortex_start` is enough). Verify by running the hook once:

   ```bash
   echo '{"cwd":"'"$PWD"'","source":"startup"}' | python3 ~/.claude/hooks/aicortex_session_start.py
   ```

   The output must contain `AICortex v… — briefing` and no `systemMessage`
   warning.

## If something fails

Read `README.md → Troubleshooting`:

```bash
docker compose logs --tail 100        # startup banner + live errors
# verbose auth logging: docker-compose.yml → FASTMCP_LOG_LEVEL: "DEBUG"
```

Common causes: proxy speaking TLS to the upstream (must be plain HTTP), OIDC
redirect URI mismatch (`<BASE_URL>/auth/callback`), forward-auth in front of
`/mcp`, geo-blocking the LLM provider's egress.
