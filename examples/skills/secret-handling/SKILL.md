---
name: secret-handling
description: Handle API keys, tokens and passwords safely — store them in the vault, reference by name, never in chat, repo, memory, or code.
category: Security
tags: secrets, vault, security, credentials
---

# Secret Handling

## When to use
Any time an API key, token, or password appears or is needed.

## The rule (hard)
Secrets go in the **encrypted vault**, immediately, referenced **by name** — never
pasted into chat, committed to a repo, written into a memory, or hardcoded.

## How
- Store: `secret_set("SOME_API_KEY", <value>)` → encrypted at rest, never returned.
- Reference: a service/device points at the **name** (`token_env="SOME_API_KEY"`),
  and the server injects the value at call time. The model never sees it.
- List/remove: `secret_list` (names only) · `secret_delete(name)`.

## Don't
- Don't ask the user to paste a secret "just to check it".
- Don't put a secret in a service config, a memory, or a commit — those are not encrypted.
- Don't echo a secret back, even partially.

## If a secret leaks (chat, repo, logs)
Treat it as compromised: **rotate it** at the source, update the vault with the new
value, and remove the exposed copy. A leaked encryption key means re-encrypting the
vault — rotate carefully and re-store the affected secrets.
