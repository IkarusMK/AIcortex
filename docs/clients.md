# Connecting AI clients

AICortex speaks MCP over **Streamable HTTP** at one URL: `https://<your-host>/mcp`.

> Tested end to end: **Hermes Agent** (`hermes mcp test`, bearer key). The other
> snippets follow each vendor's documentation as of 2026-10; if a client behaves
> differently, please open an issue.
There are two ways in:

| Way | For | How |
|---|---|---|
| **OAuth** (sign-in via your IdP) | Apps with a "custom connector" dialog: Claude (web, desktop, mobile), ChatGPT | Add the URL; the app opens your IdP's login. Needs OIDC configured (see `pocketid-setup.md`). |
| **Bearer token** (fixed header) | Agents, CLIs and SDKs: Claude Code, Codex, Claude API, OpenAI Responses API / Agents SDK, Hermes, OpenClaw, LibreChat, Open WebUI, LM Studio | Send `Authorization: Bearer <agent key>`. Works with or without OIDC. |

## Agent keys

Give every agent its own key, so its notes and tasks are attributed to it and a
leaked key can be revoked on its own:

```text
apikey_create(identity="<person's id>", name="hermes", scopes="all")
```

- `identity` is the person the agent works for (their IdP `sub`, as shown in the
  WebUI users list). The agent sees that person's memory, vault and grants.
- `scopes` limits the tools: `all`, aliases (`memory`, `skills`, `files`,
  `services`, `calendar`, `mail`, `sessions`, `tasks`), globs (`task_*`) or exact
  names. Secret, key and tenancy tools are never reachable with a key.
- A key never becomes admin by default: it gets the person's explicit role from
  the policy, else `user`.
- The key is shown once; only its hash is stored. Revoke with `apikey_revoke`.

`RUNNER_TOKEN` (from `.env`) is a separate fixed token for the scheduled-job
runner only.

Keep tokens out of chat logs: put them in the client's secret store or an
environment variable where the client supports that (shown below).

## Claude (web, desktop, mobile)

Settings → Connectors → **Add custom connector** → URL `https://<your-host>/mcp`
→ sign in at your IdP. The connector then syncs to all your Claude apps.

- Claude connects from Anthropic's cloud (`160.79.104.0/21`). Your reverse proxy
  **and** your IdP's discovery/token endpoints must be reachable from there.
- Sign-in starts only on a `401` that points at the protected-resource metadata;
  AICortex sends exactly that. The `resource` it advertises is `<BASE_URL>/mcp`,
  so `BASE_URL` must be the public URL you type into the dialog.

## Claude Code

Recommended setup — the key stays in the macOS keychain, the long-term memory is
loaded at every session start, and a missing AICortex is reported instead of
silently ignored.

1. **Key in the keychain** (asks for the key; paste the agent key from the WebUI):

   ```bash
   security add-generic-password -s aicortex -a "$USER" -w
   ```

2. **Connect the MCP server.** Export the key in your shell profile (`~/.zshrc`) and
   register the server with the variable *in single quotes*, so Claude Code stores
   `${AICORTEX_KEY}` and expands it at runtime instead of saving the key:

   ```bash
   export AICORTEX_KEY="$(security find-generic-password -s aicortex -w 2>/dev/null)"
   ```

   ```bash
   claude mcp add --transport http --scope user aicortex https://<your-host>/mcp --header 'Authorization: Bearer ${AICORTEX_KEY}'
   ```

   Or without a key: `claude mcp add --transport http aicortex https://<your-host>/mcp`
   and sign in via `/mcp` (OAuth).

3. **Load the long-term memory automatically.** Copy
   `clients/claude-code/session_start.py` from this repository (for example to
   `~/.claude/hooks/aicortex_session_start.py`) and add to `~/.claude/settings.json`:

   ```json
   {
     "env": {
       "AICORTEX_URL": "https://<your-host>",
       "AICORTEX_KEY_CMD": "security find-generic-password -s aicortex -w"
     },
     "hooks": {
       "SessionStart": [
         {"hooks": [{"type": "command", "timeout": 15,
                     "command": "python3 ~/.claude/hooks/aicortex_session_start.py"}]}
       ]
     }
   }
   ```

   At every start — and after `/clear` or a compaction — the hook fetches the
   `cortex_start` briefing for the current repository (name from a
   `.aicortex-project` file, else the repository folder) and adds it to the session.
   If AICortex can't be reached you see a warning and Claude is told to say so.

**How the memory is split.** Claude Code keeps the *short-term* memory of the work at
hand (its own memory and `CLAUDE.md`). AICortex holds the *long-term* memory every app
and agent shares: who you are, how you work, your projects and decisions — and
checkpoints of big projects (`session_save`) that need to survive between sessions.
The hook tells Claude this rule at every start.

## Claude API (Messages API MCP connector)

```json
"mcp_servers": [{
  "type": "url",
  "url": "https://<your-host>/mcp",
  "name": "aicortex",
  "authorization_token": "<agent key>"
}]
```

## ChatGPT

Settings → Apps & Connectors → developer mode → **Create** → URL
`https://<your-host>/mcp`, authentication **OAuth**. ChatGPT does not accept a
fixed bearer token; it registers itself (CIMD, falls back to DCR) and opens your
IdP's login.

## OpenAI Codex

`~/.codex/config.toml`:

```toml
[mcp_servers.aicortex]
url = "https://<your-host>/mcp"
bearer_token_env_var = "AICORTEX_KEY"
```

## OpenAI Responses API

```json
{"type": "mcp", "server_label": "aicortex",
 "server_url": "https://<your-host>/mcp",
 "authorization": "<agent key>", "require_approval": "never"}
```

The token is not stored by OpenAI — send it with every request.

## OpenAI Agents SDK (Python)

```python
from agents.mcp import MCPServerStreamableHttp

aicortex = MCPServerStreamableHttp(params={
    "url": "https://<your-host>/mcp",
    "headers": {"Authorization": f"Bearer {os.environ['AICORTEX_KEY']}"},
})
```

## Hermes Agent

`~/.hermes/config.yaml`:

```yaml
mcp_servers:
  aicortex:
    url: "https://<your-host>/mcp"
    headers:
      Authorization: "Bearer <agent key>"
```

Check it with `hermes mcp test aicortex`. Hermes names the tools
`mcp_aicortex_<tool>`.

## OpenClaw

Set the transport explicitly — without it OpenClaw falls back to the old SSE
transport, which AICortex does not serve:

```json
"mcp": {"servers": {"aicortex": {
  "transport": "streamable-http",
  "url": "https://<your-host>/mcp",
  "headers": {"Authorization": "Bearer ${AICORTEX_KEY}"}
}}}
```

## LibreChat

`librechat.yaml` — set `type`, otherwise an `https` URL is treated as SSE, and
switch on `serverInstructions` so the model gets AICortex's short instructions:

```yaml
mcpServers:
  aicortex:
    type: streamable-http
    url: https://<your-host>/mcp
    headers:
      Authorization: "Bearer ${AICORTEX_KEY}"
    serverInstructions: true
```

For per-person sign-in instead of a shared key, see `local-llm-librechat.md`.

## Open WebUI

Admin settings → External tools → **Add** → type **MCP (Streamable HTTP)** →
URL `https://<your-host>/mcp`, auth **Bearer**, key = the agent key.

## LM Studio

`mcp.json` (Program → Install → Edit mcp.json):

```json
{"mcpServers": {"aicortex": {
  "url": "https://<your-host>/mcp",
  "headers": {"Authorization": "Bearer <agent key>"}
}}}
```

## On the LAN

Local clients can use `http://<nas-ip>:8787/mcp` directly (no reverse proxy).
Native clients may use any host name (LAN IP, Docker name, VPN name). Browser
requests and the WebUI must come from an IP address, localhost, a name without a dot,
a `.local` / `.lan` / `.home.arpa` / `.internal` name or the `BASE_URL` host (DNS
rebinding protection); add other names to `MCP_ALLOWED_ORIGINS`. `MCP_ALLOWED_HOSTS`
makes the host check strict for every request.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `401` with a token | Key revoked, expired, mistyped, or `Bearer ` prefix missing. |
| Client reports SSE / `405` / `404` | Client fell back to the old SSE transport — set `streamable-http`. |
| `421 Misdirected Request` | `MCP_ALLOWED_HOSTS` is set and doesn't include the host you used — or the WebUI was opened under a dotted name that isn't listed in `MCP_ALLOWED_ORIGINS`. |
| `403 Forbidden Origin` | A browser page on another site sent the request (DNS rebinding protection); list your own origin in `MCP_ALLOWED_ORIGINS` if it is legitimate. |
| Fewer tools than expected | The key's scopes or the person's role limit them (by design). |
| Claude/ChatGPT sign-in fails | `BASE_URL` ≠ the URL in the dialog, or the IdP isn't reachable from the provider's cloud. |
