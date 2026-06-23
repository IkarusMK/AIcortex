---
name: rest-api-conventions
description: Design predictable REST APIs — noun resources, correct status codes, pagination, consistent errors, and versioning.
category: Coding
tags: api, rest, http, design
---

# REST API Conventions

## When to use
Designing or reviewing an HTTP/JSON API.

## Resources & methods
- **Nouns, plural**: `/orders`, `/orders/{id}/items`. No verbs in paths.
- `GET` read (safe, idempotent) · `POST` create · `PUT`/`PATCH` replace/update ·
  `DELETE` remove. `POST` for non-CRUD actions: `/orders/{id}/refund`.

## Status codes
`200` ok · `201` created (+ `Location`) · `204` no content · `400` bad input ·
`401` unauthenticated · `403` forbidden · `404` not found · `409` conflict ·
`422` validation · `429` rate-limited · `5xx` server. Don't return `200` with an error body.

## Lists
- **Paginate** everything that can grow: `?limit=&cursor=` (cursor) or `?page=&per_page=`.
- Filtering/sorting via query params: `?status=open&sort=-created_at`.
- Return pagination metadata (total / next cursor) consistently.

## Errors (one shape everywhere)
```json
{ "error": { "code": "validation_error", "message": "…", "details": [ … ] } }
```

## Also
- **Version** at the edge: `/v1/…`. Don't break v1 in place.
- Be consistent: same field casing, same date format (ISO 8601 UTC), same error shape.
- Make writes **idempotent** where clients may retry (idempotency keys).
