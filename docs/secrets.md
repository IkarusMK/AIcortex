# Secrets & the encrypted vault — how AICortex keeps your credentials safe

AICortex touches real credentials (API tokens, app passwords, device codes). This
page explains exactly **where they live, how they're used, and how to set them**,
so you can be confident your secrets stay secret.

## The guarantees (short version)

- 🔒 **Encrypted at rest.** Vault values are encrypted with a Fernet key
  (`STORAGE_ENCRYPTION_KEY`) — the on-disk file is unreadable without it.
- 🙈 **Never returned to the model.** A secret's *value* is injected server-side
  into the outbound request and is **never** sent back to the LLM. `secret_list`
  shows **names only**.
- 💬 **Never in chat, repo, or memory.** Secrets are referenced by **name**, not
  value. `.env` and `data/` are git-ignored; memories and skills must never hold a
  secret.
- 🧱 **Fail-closed.** An unreadable/wrong-key vault is **never silently
  overwritten** — writes refuse and a `.bak` is kept. Plaintext storage is refused
  unless you explicitly opt in.
- 👤 **Per-user isolation (multi-user).** With `AUTH_ENFORCE=1`, an admin
  provisions per-user secrets; ordinary users can't create them and see only the
  shared names plus their own.

## Where secrets live

Two server-side stores, both private to your NAS and both git-ignored:

| Store | What | Set via |
|-------|------|---------|
| **Encrypted vault** | `data/vault/secrets.enc` (Fernet-encrypted) | `secret_set` tool — works from mobile |
| **`.env`** | environment variables on the NAS | edit the file on the NAS |

At lookup time the server checks **`.env` first, then the vault** — so either works,
and a `.env` value overrides a vault entry of the same name.

## How a secret is used (and never leaks back)

1. You register a service/device that **references a secret by name** (e.g. a
   service's `token_env: GITHUB_TOKEN`, or a WebDAV endpoint's `password_env`).
2. When the connector calls that service, it resolves the value **server-side** and
   puts it in the request's auth header.
3. The value is **never** placed in the tool's response, the chat, or any log.

`secret_list` returns names only; there is no tool that returns a secret's value.

## How to set a secret — two safe ways

Both keep the value off the chat and out of the repo. **Never paste a secret into a
chat message or commit it to git.**

**A — `secret_set` (recommended; works from any device, incl. mobile)**
```
secret_set("GITHUB_TOKEN", "<your-token>")
```
The value goes straight into the encrypted vault and is never shown again. Requires
`STORAGE_ENCRYPTION_KEY` to be set (otherwise the connector refuses, to avoid a
plaintext vault).

**B — `.env` on the NAS (operator-only; value never leaves the box)**
```env
GITHUB_TOKEN=<your-token>
```
Then `docker compose up -d`. Since lookups read `.env` first, this works the same.
Good when you're already on the NAS and want the value to never transit anything.

> Tip: an assistant should **never ask you to paste a secret into the chat** and
> should **never edit `.env` for you**. If it offers to, decline — set it yourself
> with one of the two ways above.

## Set the encryption key first

For an encrypted vault, set a Fernet key once in `.env`:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# put the output in .env as:
STORAGE_ENCRYPTION_KEY=...
```
Without it, `secret_set` refuses to store (so you never get a plaintext vault by
accident). `ALLOW_PLAINTEXT_VAULT=1` overrides this — **not recommended**.

## Multi-user: per-user secrets

With `AUTH_ENFORCE=1`, an **admin** can store a secret in a specific user's
private namespace:
```
secret_set("API_TOKEN", "<value>", owner="<their-pocketid-sub>")
```
That user's own service calls resolve their secret first, then the shared one.
Ordinary users **cannot** create or read other people's secrets. See
[authorization.md](authorization.md).

## Rotating & removing

- Replace: `secret_set("NAME", "<new-value>")` (overwrites).
- Remove: `secret_delete("NAME")`.
- **If a secret was ever exposed** (pasted somewhere, committed, logged): rotate it
  at the source (revoke the token / change the password) and re-store the new one.

## Best practice: least-privilege tokens

Scope every credential to the minimum:
- Prefer **fine-grained / scoped tokens** over account-wide ones.
- Limit to the **one resource** you need (e.g. a single repo, a single mailbox).
- Grant the **minimum permissions** and set a sensible **expiry**.

### Worked example — a GitHub token

1. Create a **fine-grained PAT** scoped to one repository, with only the
   permissions you need (e.g. *Issues: read/write*, *Contents: read*).
2. Store it without showing anyone:
   `secret_set("GITHUB_TOKEN", "<the-pat>")` (or put it in `.env`).
3. A `github` service references it by `token_env: GITHUB_TOKEN` — the connector
   uses it server-side, and it's never returned to the model.
