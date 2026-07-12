# Changelog

All notable changes to AICortex are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/). Full notes for each version are on
the [Releases](https://github.com/IkarusMK/AIcortex/releases) page.

## [1.12.2] — 2026-07-12
### Fixed
- **Connector login REALLY fixed: the OIDC proxy no longer forwards `resource` upstream —
  and the v1.12.1 pin alone was NOT enough.** Deeper finding: fastmcp **3.4.3 already
  contains** the new authorize flow (CIMD, `/consent`, RFC 8707 `resource` forwarding);
  v1.11.0 only *appeared* fine because existing connector sessions renew via refresh
  tokens — the last FRESH login predated the 3.4.x flow, so the regression stayed
  invisible until a reconnect was needed. The real, version-independent fix is
  configuration: the proxy is now built with **`forward_resource=False`** (Pocket ID
  never sees the parameter it rejects) and **`require_authorization_consent="external"`**
  (no `/consent` interstitial — the IdP's passkey login IS the consent, restoring the
  familiar flow). Both are signature-checked so a fastmcp without these kwargs can never
  break boot. New guard tests (`tests/test_oauth_upstream.py`) pin this contract against
  the installed fastmcp, so a future version bump that renames or breaks either knob
  fails CI instead of breaking logins in production.

## [1.12.1] — 2026-07-12
### Fixed
- **Claude custom-connector login broke after the v1.12.0 image rebuild — fastmcp is now
  EXACT-pinned to 3.4.3.** The v1.12.0 image silently picked up fastmcp **3.4.4** (the
  requirement was a loose `>=3.4.2,<4`), which changes the OIDC proxy's upstream
  behaviour: the RFC 8707 `resource` parameter that MCP clients (Claude) send with
  `/authorize` is now **forwarded to the upstream IdP**. IdPs without Resource-Indicator
  support (e.g. Pocket ID) reject the authorize request with `invalid_request — "The
  'resource' or 'scope' parameter is invalid"`, so the connector login fails after the
  (also new) `/consent` interstitial. Root cause proven end-to-end: container logs show
  the IdP callback error, and the same Pocket ID authorize URL succeeds without
  `resource` and fails with it. 3.4.3 dropped the parameter upstream, which is why
  v1.11.0 logged in fine — nothing in the deployment (callback URIs, env, client
  registration) was at fault. Lesson applied: the auth-critical dependency is pinned
  exactly and only bumped via a tested Dependabot PR — an image rebuild can no longer
  silently change the auth stack.

## [1.12.0] — 2026-07-12
### Added
- **Admin WebUI at `/ui` — manage the brain from a browser, no terminal needed.**
  Served by the same container alongside `/mcp` (no extra service, no extra port), in the
  AICortex banner look, language switchable **DE/EN**. Login is a standard OIDC
  authorization-code flow **with PKCE against your own IdP** (e.g. Pocket ID) — register
  ONE extra redirect URI: `<BASE_URL>/ui/callback`. Sessions are signed HttpOnly cookies;
  every management endpoint is **admin-only** (role via the same `authz.role_for` chain as
  MCP), mutations carry a CSRF header, and the app is fully self-contained (strict CSP, no
  CDNs, no build step). Without OIDC (local testing) the UI is open on the
  localhost-only bind, mirroring the server's own trust model. Pages:
  - **Overview** — version, enforce status, counts (skills/categories/secrets/services/
    devices/users).
  - **Vault** — see secret NAMES (values never leave the vault), add tokens/API keys/
    passwords (shared or per-user), delete. Write-only by design.
  - **Skills** — browse by category, view, create/edit (name, category, description,
    tags, Markdown), delete. Same house rules as `skill_write` (category required).
  - **Users** — roles (admin/user/viewer) + per-user areas (memory/vault/services/skills/
    device grants) in `policy.json`, add/edit/remove.
  - **Services & devices** — read-only inventory of EVERY registry (services, mqtt, ftp,
    printers, scanners, webdav, caldav, ssh, mail, imap, mcp, webhooks) with target and
    the referenced secret NAME. Only constructed fields leave the endpoint — an unknown
    config key can never leak through the UI.
  - **Logs** — the authz audit log (who called which tool, allowed/denied and why),
    newest first, filterable; reads at most the last 1 MB per request.
  The endpoints call the SAME module-level functions as the MCP tools
  (`secrets_store.vault_*`, `skills` helpers, `tenancy._write_policy`) — UI and assistant
  cannot drift. UI actions are audited (`ui:*` in the authz audit log; never a value).
  New: `app/webui.py`, `app/webui_static/`, `tests/test_webui.py`. Opt-out: `UI_ENABLED=0`.
### Fixed
- **Bambu FTPS upload now goes through `curl` — the v1.10.2 session-reuse shim was not
  enough.** Python's ftplib still failed to resume the TLS session on the DATA channel
  against the P1S SD store, so `STOR` kept hanging into "read operation timed out". FTPS
  uploads (implicit *and* explicit) now shell out to `curl` (config via stdin — the
  password never touches argv), which implements FTPS session reuse natively and is the
  community-proven path for these printers. The vetted egress IP is pinned into curl
  (`resolve=`) so the SSRF/anti-rebinding guarantee survives the external process; TLS
  knobs (`ca_bundle` > `tls_insecure` > verify) match `netguard.ssl_context`. Plain FTP
  keeps the ftplib path. `curl` added to the image. New tests: `tests/test_ftp_curl.py`.

## [1.11.0] — 2026-07-08
### Changed
- **Unified TLS handling across every integration.** A single `netguard.ssl_context(cfg)`
  now builds the SSL context for the socket-based clients (FTP, MQTT, IMAP, SMTP) from the
  SAME two knobs the HTTP clients already resolve via `netguard.tls_verify` — `ca_bundle`
  (pin a CA/cert — the safe way) and `tls_insecure` (verification off). Secure by default
  everywhere; only the admin-only `*_add` tools ever set these.
### Added
- **`ca_bundle` / `tls_insecure` now offered on ALL TLS endpoints.** Previously only
  `service_add` / `scan_add` / `webdav_add` / `caldav_add` had the full pair. `ftp_add` and
  `mqtt_add` gained `ca_bundle`; `imap_add` and `mail_add` gained BOTH — they had *no* TLS
  opt-out at all, so a self-signed LAN mail server was unreachable. One consistent
  convention across services, scan, webdav, caldav, ftp, mqtt, imap and smtp — no more
  per-device patching (includes the v1.10.2 FTPS session-reuse fix).

## [1.10.2] — 2026-07-08
### Fixed
- **FTPS upload to Bambu Lab printers (and other `require_ssl_reuse` servers) no longer
  hangs → "read operation timed out".** `ftp_upload` over implicit FTPS opened the data
  channel with a *fresh* TLS session; servers that require the data connection to **resume
  the control channel's TLS session** (vsftpd `require_ssl_reuse`, Bambu P1S SD store)
  stall such transfers. `_ImplicitFTP_TLS` now overrides `ntransfercmd` to wrap the data
  socket with `session=self.sock.session`, so the session is reused as required. This
  unblocks sending sliced `.gcode.3mf` jobs to the printer's SD for a `project_file` start.
### Fixed
- **`scan_document` left the scanner "busy" → the next scan failed with HTTP 503.** eSCL
  requires GETting `NextDocument` repeatedly until it returns **404** — that both yields
  each page AND tells the device the job is finished so it releases. The tool stopped after
  the first page, so the scanner never got the end-of-job signal and stayed occupied; the
  operator had to cancel at the device panel and the following scan came back 503. It now
  drains to 404 (also riding out `503` warm-up) and correctly handles multi-page ADF scans
  (page 1 keeps the given filename, extra pages get `-N`; each is pushed to Paperless when
  requested). New tests: `tests/test_scan_drain.py`.
### Audited — no change needed
- **IPP printing** was checked for the same class of bug (requested alongside the scan
  fix). `print_document` submits an **atomic IPP `Print-Job`** — all operation attributes
  + `end-of-attributes` + the document bytes go in a single POST with `Content-Length`, so
  there is no poll-until-done step to forget and the printer is not left waiting/occupied.
  The other device tools (mqtt / ftp / webdav / ssh) are fire-and-forget or atomic too;
  only eSCL scanning had the drain-until-done pattern.

## [1.10.0] — 2026-07-07
### Added
- **`fs_view` — the assistant can now SEE workspace files with vision, not just OCR.**
  A new workspace tool renders any image or PDF under `/data/work` into image content the
  model reads directly: scanned pages (`scan_document`), e-mail attachments
  (`imap_fetch save_attachments`), webdav/ftp downloads and print sources. This closes a
  real gap — until now a scan came back only as a saved path or (via Paperless) mangled
  OCR text, so multi-card scans and decorative titles were unreadable. `fs_view(path[,
  page, max_pages])` downscales raster images (Pillow, EXIF-aware) and rasterizes PDF
  pages (pypdfium2). Same hard sandbox as the other `fs_*` tools, a size cap
  (`FS_VIEW_CEILING_BYTES`, default 30 MB) and graceful degradation (a clear message if an
  optional wheel is missing) instead of a crash.
### Changed
- New deps: `pillow>=11,<13`, `pypdfium2>=4,<6` — both have manylinux **cp314** wheels,
  verified via `uv pip compile --only-binary :all:` against the runtime image's Python
  before adding (pypdfium2 is Apache/BSD, bundles PDFium, needs no system libs like poppler).
- Docstrings + bootstrap catalog: `scan_document` and `imap_fetch` now point at `fs_view`
  for viewing; the workspace catalog line lists it.
### Tests
- `tests/test_imaging.py` — image normalize/downscale + PDF render round-trips (synthetic
  inputs, no fixture files).

## [1.9.5] — 2026-07-07
### Added
- **Per-service TLS options** — `service_add` now accepts `tls_insecure` and `ca_bundle`,
  and `call_service` honours them via the existing `netguard.tls_verify()` resolver
  (secure by default, #10 pattern — same as `scan_add`/`webdav_add`). This makes
  self-signed LAN services (e.g. a Crafty panel, which failed with
  `SSL: CERTIFICATE_VERIFY_FAILED`) reachable without weakening the default:
  verification stays ON unless an admin explicitly opts out, `ca_bundle` (pinned cert)
  takes precedence over `tls_insecure`, and `service_list` visibly flags such services
  with `[TLS-INSECURE]`. Merge-safe: updating other fields never resets a configured
  TLS opt-out. New tests: `tests/test_service_tls.py`.

### Changed
- `netguard.tls_verify` docstring now lists `service_add` among the admin-only writers
  of TLS opt-out configs.

## [1.9.4] — 2026-07-06
### Fixed
- **auth/transport:** the connector no longer returns **421 Misdirected Request** on the
  OAuth discovery/registration routes behind a reverse proxy. fastmcp 3.4.3 enforces the
  MCP HTTP transport's Host/Origin DNS-rebinding guard, which rejected every request whose
  `Host` was the public domain (e.g. `agent.example.com`) — so Claude's client registration
  failed with "Registrierung beim Anmeldedienst fehlgeschlagen". The server now derives the
  Host/Origin allow-list from `BASE_URL` (already required for OIDC, so it can't drift) and
  passes it to `mcp.run(...)`; `localhost`/`127.0.0.1` stay allowed by the guard's defaults,
  and `MCP_ALLOWED_HOSTS` (comma-separated) can add more for edge setups. Protection stays
  **on** — unknown hosts are still rejected. Regression introduced by the 3.4.3 bump in 1.9.3.

## [1.9.3] — 2026-07-06
### Changed
- **Runtime bumped to Python 3.14** (`python:3.14-slim` base image) and dependencies
  refreshed to current majors: **paramiko 5** (SSH/SFTP), **py-key-value-aio 0.4.5**
  (encrypted DiskStore), **fastmcp 3.4.3**. Each was validated before merging — the exact
  APIs used by `server.py`/`ssh_tools.py` still resolve, all `linux/cp314` wheels exist
  (incl. `uvloop`), and the test scripts pass on 3.12 and 3.14. No behaviour or API change
  for clients; a maintenance/hardening refresh.
- CI actions pinned to current majors (checkout, buildx, login, metadata, build-push).

### Added
- **Real test signal on every PR and push** (`tests.yml`): installs the pinned deps on the
  runtime Python, byte-compiles the app, and runs the `tests/` scripts. `build.yml` only
  builds/pushes the image, so PRs previously had no automated check.
- **Dependabot patch auto-merge** (`dependabot-auto-merge.yml`): patch-level dependency PRs
  self-merge after an in-job gate (deps resolve + byte-compile + test scripts + advisory
  `pip-audit`); minor/major bumps stay open for manual review. Self-contained — no branch
  protection or required status checks needed.

### Fixed
- Test modules no longer hardcode an absolute local path on `sys.path`; the `app/` dir is
  resolved relative to each test file, so the suite runs in any checkout and in CI.

## [1.9.2] — 2026-07-06
### Fixed
- **skills:** the frontmatter parser now falls back to tolerant, line-based parsing (like
  `memory.py`) when strict YAML fails — so a skill whose `description` (or another value)
  contains a colon (e.g. `Quelle: Paul Hudson`) no longer silently loses its category and
  description and lands in "uncategorized". Already-affected files are repaired automatically
  on read, without rewriting them.
- **skills:** `skill_write` now serializes frontmatter via `yaml.safe_dump` (automatic
  quoting for colons, quotes, `#`, unicode) — invalid frontmatter can no longer be written.

## [1.9.1] — 2026-07-03
### Performance
**bootstrap no longer re-scans the whole brain on every call.** The catalog rebuilt every
section from scratch each time — most expensively reading ALL `SKILL.md` files just to
count categories (hundreds of full-file reads per call), which is what made a cold
bootstrap on NAS storage take minutes.

- New `catalog_cache`: each file-reading section (skills, memory, the JSON service/device
  registries) is cached, keyed by a **cheap stat signature** of its source directory
  (file count + newest mtime + total size — metadata only, **no file reads**). Unchanged →
  cached lines returned without opening a file; changed → only that one section
  re-renders. Persisted to disk (`CATALOG_CACHE_FILE`, default `/data/.catalog_cache.json`)
  so even the first bootstrap after a restart is fast. Fail-open: a cache error renders
  live — never wrong, only faster. Scales as memory/skills/… fill up over months.
- `sessions.recent()` reads the session files **once** (was twice — `_prune()` + `_all()`);
  pruning stays on save/list/load, not on the frequent bootstrap read.
- Output is byte-identical to before — tests assert cold == warm and that a content edit
  invalidates + recounts. New `tests/test_catalog_cache.py`.
- **`uvloop`** (libuv) is now the event loop: uvicorn auto-detects it and `server.py`
  installs it explicitly at startup (logged, so it's verifiable), for faster async I/O on
  the connector's real bottleneck — the network/event loop. Guarded fallback to the
  default asyncio loop where uvloop isn't available (e.g. Windows).

## [1.9.0] — 2026-07-03
### Added
**Native REST API** — a plain-HTTP layer next to `/mcp` so non-MCP clients (n8n,
LangChain, OpenAI-compatible tools, scripts) can call AICortex tools directly, through
the **same** authorization and per-user areas as an OIDC session (no second permission
model):

- `GET /api/v1/tools` (tools this key may call, with JSON schemas), `POST
  /api/v1/tools/<name>` (invoke; body = JSON args), `GET /api/v1/openapi.json`
  (auto-generated OpenAPI 3.1 of this key's tools). Served outside the MCP OAuth, like
  `/hooks/*`. Optional **SSE** (`?stream=1`) with heartbeats for long-running tools.
- **Per-user API keys** (`apikey_create` / `apikey_list` / `apikey_revoke`, admin-only):
  a key maps to an identity and runs the exact same authz/tenancy pipeline —
  **never admin by default**. Keys are **hashed at rest** (SHA-256 of a 256-bit secret,
  constant-time compare), **default-deny** with a per-key `scopes` allow-list, and a
  hard **denylist** (`secret_*`, `apikey_*`, `tenancy_*`) no key can reach. Optional
  expiry; full CRUD incl. revoke(=delete). Shown once at creation.
- Per-key **rate limiting** (`API_RATE_PER_MIN`, default 60/min) and body cap
  (`API_MAX_BODY_BYTES`). Whole layer toggles with `API_ENABLED`.
- A request-scoped identity (`contextvars`) so concurrent REST requests never bleed
  identity, and in-tool self-scoping (`service_list`/`secret_list`/memory) resolves to
  the key's owner. New: `docs/rest-api.md`, `tests/test_apikeys.py`.

**Proxy:** expose `/api/*` (like `/hooks/*`) past the reverse proxy without OIDC — never
`/mcp`.

## [1.8.0] — 2026-07-02
### Security
Full-repo security audit (manual review + NAS security skills: MCP-server audit, XXE,
indirect-prompt-injection, BFLA/BOLA). All findings fixed:

- **Per-user areas now cover DEVICE endpoints** (were: services/skills only). Under
  `AUTH_ENFORCE=1` a non-admin's use of `caldav`/`imap`/`webdav`/`ssh`/`mail`/`print`/
  `scan`/`mqtt`/`ftp`/`mcp` action tools is confined to the endpoints an admin
  assigned (**default-deny**) — closing an isolation gap (BOLA/BFLA) where these
  registries were unguarded. Grant with `tenancy_set(identity, grant="caldav=…;
  ssh=all")`. Enforced centrally in the authz middleware.
- **Inbound webhook payloads are labeled UNTRUSTED** in the inbox (indirect
  prompt-injection defense) — a reader is told to treat them as data, not instructions.
- **`ssh_upload` path check uses ancestry, not string prefix** (the `/data-backup`
  sibling bypass, missed in the 1.6.3 sweep).
- **CalDAV/WebDAV XML parsed with `defusedxml`** (entity-expansion / billion-laughs
  DoS from a malicious or compromised registered server).
- **Webhook hardening:** shared token read from the `X-Webhook-Token` **header only**
  (never a URL query — no secret in proxy logs); unknown-hook and bad-auth both return
  a uniform `401` (no hook-name enumeration); byte-safe constant-time compare.
- **IMAP search injection** — quotes/CRLF stripped from the free-text query.
- **Published port binds to loopback by default** (`BIND_ADDR`, default `127.0.0.1`)
  so `/mcp` and `/hooks/*` aren't reachable from the LAN past the reverse proxy.
- **Supply-chain:** `defusedxml` added + floors tightened, a `pip-audit` advisory gate
  in CI, and Dependabot for pip / GitHub-Actions / Docker.
- **Hardening:** act-as replay-guard evicts the oldest jti (no wholesale clear);
  documented the SSRF guard's reliance on the default (socket) DNS resolver.

Verified clean: no committed secrets/data (only `.gitkeep`), container runs non-root,
SSRF egress guard on every outbound path, act-as never escalates to admin, cron
owner-scoping BOLA-safe, `*_add`/`*_delete` admin-gated, vault fail-closed.

## [1.7.2] — 2026-07-02
### Changed
- **Onboarding brought up to v1.7.** The connector `guide` (loaded by `bootstrap`
  and sent as the server `instructions`) and `docs/client-project-instructions.md`
  now cover what a fresh LLM was missing: **per-user areas / default-deny** and how
  an admin grants them (`tenancy_set`), **cron act-as** (`owner`, `act_as_begin/end`,
  the per-job token), and a **complete CRUD delete list** (imap / caldav /
  caldav_delete_event / webhook / tenancy_unset). So a new session understands the
  multi-tenant model — not just how to call individual tools.

## [1.7.1] — 2026-07-02
### Added
- **`caldav_delete_event`** — completes CalDAV CRUD. Shipping `caldav_add_event`
  without a matching delete in 1.7.0 was an oversight; every write now has its
  delete. `caldav_list_events` additionally returns each event's **href** as the
  delete handle. State-changing → confirm first.

## [1.7.0] — 2026-07-02
### Added
- **Webhooks — inbound receiver + outbound sender.** Inbound: a public
  `POST /hooks/<name>` route (served alongside `/mcp/` via FastMCP `custom_route`,
  outside the MCP OAuth) lets external services push events that land in the inbox —
  making the brain event-driven. It authenticates itself with a per-hook **shared
  secret token and/or HMAC signature** (constant-time), rejects unknown/unsigned
  requests, caps body size, and only deposits into the inbox (never the tool
  surface). `webhook_add` / `webhook_list` / `webhook_delete` (admin). Outbound:
  `webhook_send(url, json_body)` — a thin SSRF-guarded POST for notifications.
  **Operator note:** expose only `/hooks/*` past the reverse proxy's auth, never
  `/mcp`.
- **CalDAV — calendars as data.** `caldav_add` / `caldav_list_calendars` /
  `caldav_list_events` / `caldav_add_event`: discover calendars, list events in a
  time range, and create an event (PUT of an iCalendar object) — over CalDAV
  (PROPFIND / REPORT calendar-query / PUT) with httpx, no extra dependency. Same
  posture as WebDAV: SSRF-guarded connects, TLS verified by default, app-password
  from the vault; `caldav_add`/`caldav_delete_endpoint` admin-only.
- **IMAP — read incoming email.** The read-side counterpart to SMTP `mail_send`:
  `imap_add` / `imap_list` / `imap_search` / `imap_fetch`. Reads are read-only
  (BODY.PEEK, `readonly` select) so nothing is marked seen; attachments optionally
  saved to `/data/work`. Same security posture as SMTP — host passes the SSRF guard
  and the connect is wrapped in `netguard.guard()`; `imap_add`/`imap_delete_account`
  are admin-only, the read tools are viewer-safe.
- **Running version is now observable.** A single `version.__version__` is logged
  at startup (`[AICortex] version X starting`), returned by `ping`, and shown in
  the `bootstrap` catalog header — so you can tell which build a container is
  actually running without fingerprinting its source. (The stamp always mirrors the
  newest release; it moves only when a release is cut.)
- **Per-user service/skill areas + per-user cron (act-as).** Tenancy now extends from
  private data to shared capabilities and scheduled jobs, gated by `AUTH_ENFORCE`
  (the separate `TENANCY_ISOLATE` switch is retired — "enforce means enforce").
  - `policy.json` users gain `services`/`skills` = `all` | `none` | allow-list of
    names/categories. Under enforce these are **default-deny** and **fail-closed**
    (errors → deny + audit); homelab mode (`AUTH_ENFORCE=0`) runs no checks.
  - Enforced in `call_service`/`service_list` and `skill_load`/`skill_resource`/
    `skill_search`/`skill_list`; managed via `tenancy_set(services=…, skills=…)`.
  - **Cron act-as:** a job can run as an owner. A non-admin schedules only as
    themselves; an admin as anyone. `cron_due` mints a short-lived per-job capability
    token (HMAC, HKDF-derived from `STORAGE_ENCRYPTION_KEY`, 5-min TTL, single-use);
    the runner presents it via `act_as_begin`/`act_as_end`, so it holds no standing
    authority and a running job is scoped to its owner at the owner's own privilege.
    See `docs/per-user-areas.md`.

### Changed
- **Isolation now rides on `AUTH_ENFORCE` (breaking).** The separate `TENANCY_ISOLATE`
  switch is **retired** — "enforce means enforce", one switch. With `AUTH_ENFORCE=1`
  (the default) non-admins are now confined (own memory scope + private vault) and
  **default-denied** services/skills until an admin grants them; homelab mode
  (`AUTH_ENFORCE=0`) is unchanged. A leftover `TENANCY_ISOLATE=1` in `.env` becomes a
  harmless no-op. **Admins are unaffected.** Docs updated across README, `.env.example`,
  `SECURITY.md`, `docs/authorization.md`, `docs/pocketid-setup.md`, `docs/secrets.md`.

## [1.6.3] — 2026-07-01
### Security
- **SSRF hardening on mail / scan / print (CRITICAL).** These dispatchers only
  ran the `check_host()` preflight but connected *outside* `netguard.guard()`, so
  the DNS-rebinding / TOCTOU window the guard exists to close was open exactly
  there. Their actual connects (SMTP, eSCL, IPP, Paperless upload) are now wrapped
  in the guard.
- **Guard also blocks cross-host redirects.** While a guard is active, **every**
  outbound DNS resolution is re-checked against the egress policy (not just the
  named host), so an already-registered endpoint can no longer 30x-redirect the
  client onto `169.254.169.254` or a LAN panel. Public and operator allow-listed
  ranges still pass.
- **Injective per-user namespace (HIGH).** `tenancy._safe()` sanitised distinct
  identities to the same slug (`bob@x.com` and `bob.x.com` → `bob_x_com`), which
  could fold two people onto one memory scope / vault namespace. It now appends a
  short hash of the raw identity, so namespaces are collision-free. (No migration:
  isolation is opt-in and no confined users exist yet.)
- **Path checks use ancestry, not string prefix (MEDIUM).** The `/data` sandbox
  checks in `mail_send`, `print_document` and `webdav_upload` used
  `str(p).startswith("/data")`, which also matched siblings like `/data-backup`.
  Now an ancestry check (`DATA_ROOT in p.parents`).
- **Auth fail-open now leaves an audit trail (LOW).** A degrade-to-allow in the
  authz middleware is recorded to the audit log instead of passing silently.
- **`secret_list` name-leak hardened (LOW).** A non-admin caller under isolation
  (including an unresolved one) no longer sees other users' secret *names* — only
  shared plus their own. Values were never exposed. `service_add`/`service_delete`
  were already admin-gated.

## [1.6.2] — 2026-06-30
### Changed
- Clearer failure messages for `scan_document` and `print_document`. The scanner
  error now lists **every transport it tried** and, on an HTTPS certificate
  failure, points at `tls_insecure` / `ca_bundle` for a self-signed device (instead
  of only showing the last attempt). The printer error **names the IPP status**,
  adds a reachability hint on connection errors, and suggests
  `application/octet-stream` when the format is rejected.

## [1.6.1] — 2026-06-30
### Fixed
- The `*_add` registration tools now **merge on update** instead of overwriting.
  Updating one field (e.g. adding a category) no longer wipes fields you didn't
  restate — a `token_env` reference, a `write_only` ingest lock, TLS settings are
  preserved. Shared `cfgstore.write_merged` across service/scan/mqtt/ftp/webdav/
  ssh/mail/print/mcp registration. Service `category` is optional when updating an
  already-categorized service (still required for a new one). To clear a field,
  `*_delete` and re-add.

## [1.6] — 2026-06-30
### Added
- **Tiered memory catalog** — `bootstrap` groups each memory scope by tier
  (🧭 Core / 📂 Projects / 🛠 Working style / 🔗 References), derived from the
  existing `type`; short-term/current state is the sessions layer. No migration.
- **Categorized services** — `service_add` now **requires a `category`** (refuses
  without one, like `skill_write`); the catalog and `service_list` group by it, via
  a generic renderer that falls back to a flat list for uncategorized sections.

## [1.5.2] — 2026-06-30
### Security
- Verify TLS **by default** for the scanner (eSCL) and WebDAV. Self-signed LAN
  devices opt out via the admin-only `tls_insecure` / `ca_bundle` options on
  `scan_add` / `webdav_add`, so a normal caller can't disable verification (#10).

## [1.5.1] — 2026-06-30
### Fixed
- Connect to spec-compliant Streamable HTTP MCP servers (e.g. Outline's built-in
  MCP server): a minimal POST-only client that always sends
  `Accept: application/json, text/event-stream`, follows redirects, carries the
  session id, and tolerates servers without a standalone GET stream (#17).

## [1.5] — 2026-06-30
### Added
- **Per-user data isolation** (opt-in `TENANCY_ISOLATE`): each non-admin caller is
  confined to their own memory scope (`users/<sub>`) and a private vault namespace;
  an admin provisions per-user secrets (`secret_set owner=…`) — users can't create
  their own.
- **Tenancy control plane** — admin tools `tenancy_set` / `tenancy_show` /
  `tenancy_list` / `tenancy_unset` / `tenancy_status`.

## [1.4] — 2026-06-30
### Added
- **Pocket ID-aware OIDC proxy** forwards the upstream identity (`sub`, `email`,
  `groups`), so Pocket ID groups drive roles end-to-end (per-person identity).

## [1.3] — 2026-06-29
### Added
- **Authorization layer** (on by default): roles (admin / user / viewer),
  deny-by-default tool permissions (registration/secrets/identity are admin-only),
  an audit log, per-credential identity binding, optional IdP role/group claim, and
  a `mail_send` recipient allow-list.

## [1.2.1] — 2026-06-29
### Security
- Connect-time **DNS-rebinding protection** — the egress IP policy is re-applied at
  connect, not just at preflight.

## [1.2] — 2026-06-29
### Security
- Hardening after an external review: **fail-closed encrypted vault** (keeps a
  `.bak`, refuses plaintext without a key), **TLS verification on by default** for
  FTP/MQTT/WebDAV, **SSH host-key pinning**, and **resource limits** on the
  workspace file tools and printing.

## [1.1] — 2026-06-25
### Added
- **Auto-memory** — typed memories with dedup and a candidate review queue, plus a
  fail-open auto-capture hook (the brain learns each session without polluting
  itself).
- **Presence-aware multi-agent coordination** — capability-routed task pull
  (`task_next`) and context-preserving, session-linked handoff (`task_handoff`),
  plus cross-LLM session handoff (`session_save` / `session_load`).

## [1.0.0] — 2026-06-24
### Added
- Initial release: a self-hosted MCP brain — `bootstrap` onboarding, typed memory,
  a skill router, HTTP/MQTT/FTP/WebDAV/SSH/SMTP dispatchers, a sandboxed workspace
  file hub, IPP printing, eSCL scanning, an MCP gateway, cron-as-data scheduling, an
  encrypted secret vault, OAuth via your own OIDC provider, and an SSRF egress guard.

[1.6.2]: https://github.com/IkarusMK/AIcortex/releases/tag/1.6.2
[1.6.1]: https://github.com/IkarusMK/AIcortex/releases/tag/1.6.1
[1.6]: https://github.com/IkarusMK/AIcortex/releases/tag/1.6
[1.5.2]: https://github.com/IkarusMK/AIcortex/releases/tag/1.5.2
[1.5.1]: https://github.com/IkarusMK/AIcortex/releases/tag/1.5.1
[1.5]: https://github.com/IkarusMK/AIcortex/releases/tag/1.5
[1.4]: https://github.com/IkarusMK/AIcortex/releases/tag/1.4
[1.3]: https://github.com/IkarusMK/AIcortex/releases/tag/1.3
[1.2.1]: https://github.com/IkarusMK/AIcortex/releases/tag/1.2.1
[1.2]: https://github.com/IkarusMK/AIcortex/releases/tag/1.2
[1.1]: https://github.com/IkarusMK/AIcortex/releases/tag/1.1
[1.0.0]: https://github.com/IkarusMK/AIcortex/releases/tag/v1.0.0
