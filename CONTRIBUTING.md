# Contributing to AICortex

Thanks for considering a contribution! AICortex is a small, focused MCP connector
— contributions that keep it that way are the most welcome.

## Philosophy (please read first)

- **New capabilities are added as DATA, not code.** A new HTTP API, device, cloud
  drive or scheduled job should be a `service_add` / `mqtt_add` / `webdav_add` /
  `cron_add` config plus a skill — *not* a new Python module. Only add code when a
  genuinely new **protocol** is needed.
- **Small, cohesive files** (~200–400 lines, 800 max). Organize by feature.
- **Secrets only in the vault** (`secret_set`), referenced by name. Never hardcode
  a secret, never put one in a commit, never echo one back.
- **Safe by default.** Verify TLS, fail closed on data integrity, fail open on
  optional features, cap resources at boundaries, and confirm before physical /
  outbound / destructive actions.

## Development

```bash
git clone git@github.com:IkarusMK/AIcortex.git
cd AIcortex
cp .env.example .env        # adjust PUID / PGID / HOST_PORT / TZ
docker compose up -d --build
# endpoint: http://<host>:8787/mcp
```

The app lives in `app/` (FastMCP). Each tool module exposes a `register(mcp)`
function wired in `server.py`. Persistent state is plain, human-readable files
under `data/` (one mount).

## Before you open a PR

- `python -m py_compile app/*.py` is clean.
- New logic has a small test (a tiny fake MCP that captures `@mcp.tool`
  functions is enough — no server/network needed).
- No secrets in the diff; no new hardcoded hosts/IPs (use config + the SSRF guard).
- Backward compatible: existing stored configs / data must keep working.
- Conventional commit messages (`feat:`, `fix:`, `docs:`, `refactor:`, `chore:`).

## Security

Found a vulnerability? Please **don't** open a public issue — see
[SECURITY.md](SECURITY.md) for private reporting.
