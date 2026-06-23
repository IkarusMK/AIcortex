---
name: docker-image-hardening
description: Build small, secure container images — pinned base, non-root user, multi-stage builds, no secrets in layers, minimal attack surface.
category: DevOps
tags: docker, containers, security, ci
---

# Docker Image Hardening

## When to use
Writing or reviewing a Dockerfile for anything that ships.

## Checklist
- **Pin the base** to a digest or specific tag, not `latest`. Prefer `-slim` / distroless.
- **Multi-stage build**: compile in a builder stage, copy only artifacts into the final image.
- **Run as non-root**: create a user, `USER` it; drop to it before `CMD`.
- **No secrets in layers**: never `COPY .env` or bake tokens — pass at runtime / use a secret store. Layers are forever, even if a later layer deletes the file.
- **Minimize**: `--no-install-recommends`, clean apt lists in the same `RUN`, `.dockerignore` the build context.
- **Healthcheck** so the orchestrator knows when it's actually up.
- **Read-only where possible**: mount data volumes explicitly; avoid writing into the image FS at runtime.

## Smell tests
- Image > a few hundred MB for a small service → something heavy leaked in.
- `docker history` shows a secret or a huge dep layer → fix the Dockerfile, don't just squash.
