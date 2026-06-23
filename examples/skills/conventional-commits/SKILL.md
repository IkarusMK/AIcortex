---
name: conventional-commits
description: Write clear, conventional git commit messages — type(scope): summary, imperative mood, a body that explains the why.
category: Coding
tags: git, commits, conventions
---

# Conventional Commits

## When to use
Before committing code. Keeps history readable and changelog-friendly.

## Format
```
<type>(<optional scope>): <imperative summary, ≤72 chars>

<body: what changed and WHY, wrapped ~72 cols. Not how — the diff shows how.>

<optional footer: BREAKING CHANGE: …, Refs: #123>
```

## Types
`feat` (feature) · `fix` (bug) · `refactor` · `perf` · `docs` · `test` · `chore` · `ci` · `build`

## Rules
- Imperative summary: "add", not "added"/"adds".
- One logical change per commit; split unrelated work.
- The body answers *why* and notes trade-offs/risks, not a play-by-play.
- Mark breaking changes explicitly in the footer.

## Examples
```
fix: stop double-charging on retried webhooks

Stripe re-sends events on timeout; we keyed off event time, not id, so a
retry created a second charge. Key idempotency on the event id instead.
```
