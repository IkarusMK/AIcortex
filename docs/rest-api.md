# Native REST API (v1.9.0)

> A plain-HTTP layer next to `/mcp` so **non-MCP clients** — n8n, LangChain, an
> OpenAI-compatible client, a shell script — can call AICortex tools directly, through
> the **same** authorization and per-user areas as an OIDC session. No second
> permission model: a REST key resolves to an identity and runs the exact same gate.

## Why

MCP is great for LLM apps, but a lot of automation lives elsewhere (workflow engines,
back-end services, cron scripts). Rather than build a parallel integration for each,
AICortex exposes its whole tool surface over HTTP and **auto-generates an OpenAPI spec**,
so any framework with function-calling / OpenAPI import can load the brain in one step.

## Endpoints

Served alongside `/mcp/` but **outside** the MCP OAuth (like `/hooks/*`), authenticated
by a per-user API key:

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/v1/tools` | GET | The tools **this key** may call, each with its JSON input schema |
| `/api/v1/tools/{name}` | POST | Invoke a tool — request body is the JSON arguments object |
| `/api/v1/openapi.json` | GET | OpenAPI 3.1 spec of **this key's** tools (one `POST` operation per tool) |

All three require `Authorization: Bearer <key>`. The tool list and the OpenAPI spec are
**scoped to the key** — a key scoped to `memory` only sees the memory tools.

### Invoke — request / response

```bash
curl -X POST https://agent.example.com/api/v1/tools/memory_search \
  -H "Authorization: Bearer ak_ab12cd34ef56_XXXXXXXX…" \
  -H "Content-Type: application/json" \
  -d '{"query": "trading rules"}'
```

```json
{ "ok": true, "tool": "memory_search", "result": "…tool output…" }
```

AICortex tools return a string; the REST layer unwraps FastMCP's `{"result": …}`
envelope to a clean `result` value. Errors are JSON with an HTTP status:

| Status | Meaning |
|--------|---------|
| `400` | Body isn't a JSON object |
| `401` | Missing/invalid/expired/disabled key |
| `403` | Tool outside the key's `scopes`, or denied by the identity's role / device area |
| `404` | No such tool, or `API_ENABLED=0` |
| `413` | Body over `API_MAX_BODY_BYTES` |
| `429` | Per-key rate limit exceeded (`Retry-After` header) |
| `500` | The tool raised |

### Streaming (SSE)

Pass `?stream=1` or `Accept: text/event-stream` to get Server-Sent Events: a `connected`
comment, `keepalive` comments every 15 s while a long tool runs (so proxies don't time
out a slow POST), then one terminal event:

```
event: result
data: {"ok": true, "tool": "…", "result": "…"}
```

(or `event: error` with `{"error": "...", "detail": "..."}`).

## API keys

Keys are the whole auth for REST, so they're least-privilege by construction.

### Managing keys (admin, over MCP)

```
apikey_create(identity="alice", name="n8n", scopes="memory, fs_read", ttl_days=90)
apikey_list                       # keyid prefixes, identity, scopes, expiry, status
apikey_revoke("ab12cd34ef56")     # delete — takes effect immediately
```

- `identity` = the person's Pocket ID `sub` (or a client_id). The key runs **as** that
  identity, through their per-user area.
- `scopes` (**required**, default-deny) = a comma-separated allow-list: exact tool names,
  `<prefix>_*` globs, friendly **aliases** (`memory`, `skills`, `files`, `calendar`,
  `mail`, `tasks`, `sessions`, `services`), or `all`.
- `ttl_days` > 0 sets an expiry (like the act-as capability tokens).
- The full key is shown **once** at creation — only its hash is stored.

### Key format & storage

`ak_<keyid>_<secret>` — `keyid` (12 hex) is a public lookup prefix; `<secret>` is 32
random bytes (256-bit). Only `SHA-256(secret)` is persisted (high entropy → a plain
digest is sufficient, GitHub-style), compared in **constant time**. One JSON record per
key under `APIKEY_DIR` (default `/data/apikeys`), so `apikey_list` can show a stable
prefix without ever revealing the secret. Revocation deletes the record.

## Security model

A REST call passes **every** gate, in order:

1. **Authentication** — bearer key from the `Authorization` header **only** (never a
   query param, so it can't leak into proxy/access logs); constant-time hash compare;
   expiry + disabled checks.
2. **Rate limit** — per-key sliding window (`API_RATE_PER_MIN`, default 60/min).
3. **Scope** — the key's `scopes` allow-list (default-deny) **plus** a hard **denylist**
   that no key can override, even with `scopes="all"`: `secret_set`/`secret_delete`,
   `apikey_*`, `tenancy_*`. Those stay OIDC-admin-only.
4. **Role + areas** — the identity's role (resolved from `policy.json`, **never admin by
   default** — like the RUNNER_TOKEN), then `authz.enforce_rest`: the *same* decisions as
   the MCP middleware (role allow/deny, memory-scope confinement, per-user device-endpoint
   areas, sender/`created_by` attribution stamping). During `tool.run`, a **request-scoped
   identity** (a `contextvars` var, so concurrent requests never bleed) makes in-tool
   self-scoping (`service_list`, `secret_list`, memory) resolve to the key's owner.

There is **no second permission model** — a key is just another way to present an
identity to the existing authz/tenancy pipeline.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `API_ENABLED` | `1` | Turn the whole REST layer on/off |
| `API_RATE_PER_MIN` | `60` | Per-key rate limit (requests/minute; `0` = unlimited) |
| `API_MAX_BODY_BYTES` | `1000000` | Max request body for a tool call |
| `APIKEY_DIR` | `/data/apikeys` | Where key records are stored |

**Proxy:** forward `/api/*` (and `/hooks/*`) past the reverse proxy **without** OIDC — the
API authenticates by key. **Never** expose `/mcp` that way.

## Tests

`tests/test_apikeys.py` — key mint/verify, constant-time hash, expiry/disabled, scope
allow-list + hard denylist, rate limit, full CRUD; and the authz side: API-key role never
defaults to admin, the request-scoped identity contextvar, and `enforce_rest` mirroring
the middleware gate (role, memory confinement, endpoint default-deny, attribution).
