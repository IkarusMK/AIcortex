# Changelog

All notable changes to AICortex are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/). Full notes for each version are on
the [Releases](https://github.com/IkarusMK/AIcortex/releases) page.

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
