# Security Policy

AICortex is a self-hosted connector that touches real data, devices and secrets,
so security is taken seriously. Thank you for helping keep it safe.

## Reporting a vulnerability

**Please do not open a public issue for a sensitive vulnerability.** Instead use
GitHub's private reporting:

➡️ **[Report a vulnerability privately](https://github.com/IkarusMK/AIcortex/security/advisories/new)**

Include where possible: affected file/endpoint, a short proof-of-concept, the
impact, and your suggested fix. You'll get an acknowledgement, and fixes are
shipped in a tagged release with credit (unless you prefer to stay anonymous).

Non-sensitive hardening ideas are welcome as normal issues.

## Supported versions

Fixes land on `main` and in the latest [release](https://github.com/IkarusMK/AIcortex/releases).
Run a recent release; there is no back-porting to older tags.

## Threat model (what this connector assumes)

AICortex is designed for a **single trusted operator** running it on their own
network, behind their **own OIDC provider**, with the connector reachable only
through an authenticated HTTPS endpoint.

- **The LLM is semi-trusted.** Tools are model-operable and a model can be
  prompt-injected, so model-driven dispatchers are guarded: only **registered**
  targets are reachable, every outbound host passes an **SSRF egress guard**
  (private/loopback/link-local/metadata IPs blocked unless allow-listed), and
  **secrets live in an encrypted vault**, injected server-side and never returned.
- **Authentication is required before exposure.** Without OIDC the server binds to
  localhost only. An **authorization layer (on by default; `AUTH_ENFORCE=0` to
  disable)** adds least-privilege on top: roles (admin / user / viewer),
  deny-by-default tool permissions (registration/secrets/identity tools are
  admin-only), per-credential identity binding and an audit log. The shared
  `RUNNER_TOKEN` is a non-admin "user"; an interactive OIDC operator is "admin".
  Roles can also be driven from an IdP role/group claim (`AUTH_ROLE_CLAIM`).
- **State-changing / physical / outbound actions** (device control, sending mail,
  deletes) are expected to be confirmed with the user before execution.

### In scope
Vault/secret handling, the SSRF guard, TLS verification, auth bypasses, path
traversal, resource exhaustion, data-loss bugs, and privilege issues in the
dispatchers.

### Out of scope / by design
- A model that the operator deliberately gave a credential using that credential.
- LAN devices the operator explicitly allow-listed and connects to with self-signed
  TLS (e.g. eSCL scanners, IPP printers) — no credentials transit those.
- Exposing the connector publicly without OIDC (don't).

## Hardening status

Following an external review, **v1.2** hardened the vault (fail-closed),
TLS-verification defaults (FTP/MQTT/WebDAV), SSH host-key pinning, resource
limits, and **connect-time DNS-rebinding protection** (the egress IP policy is
re-applied at connect, not just at preflight) — see the
[release notes](https://github.com/IkarusMK/AIcortex/releases).
**v1.3** adds the **authorization layer**, on by default (roles, deny-by-default
tool permissions, audit log, per-credential identity binding, optional IdP
role/group claim; `AUTH_ENFORCE=0` to disable) plus a `mail_send` recipient
allow-list. **v1.4** adds a PocketID-aware proxy that forwards the upstream
identity (`sub`/`groups`) so Pocket ID groups drive roles end-to-end. **Tracked
for a later release (v2.x):** full multi-user accounts with per-user data
isolation (own memory scope + vault namespace) and per-user API keys / rotation.
