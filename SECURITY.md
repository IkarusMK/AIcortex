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
  localhost only. *Note:* authentication is not fine-grained authorization — any
  identity that can log in currently gets the full tool set (a per-credential
  authorization layer is on the roadmap; see below).
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
TLS-verification defaults (FTP/MQTT/WebDAV), SSH host-key pinning, and resource
limits — see the [release notes](https://github.com/IkarusMK/AIcortex/releases).
**Tracked for a later release:** a per-credential authorization layer (tool
allow-lists / read-only mode / operator-only admin tools) and DNS-rebinding
pinning.
