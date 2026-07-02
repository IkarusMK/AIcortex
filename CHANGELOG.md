# Changelog

All notable changes to AICortex are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/). Full notes for each version are on
the [Releases](https://github.com/IkarusMK/AIcortex/releases) page.

## [Unreleased]
### Added
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
