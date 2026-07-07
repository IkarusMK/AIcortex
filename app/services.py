"""Generic, allow-listed service caller.

This is the primitive that lets new integrations be added as DATA (a service
config) + a skill, instead of new Python code. A *service* is a small JSON file
under SERVICES_DIR describing a base_url and, optionally, the NAME of an
environment variable holding its auth token — so secrets stay in the server's
environment, are never stored in data, and are never returned to the model.

``call_service`` only reaches registered services (no arbitrary URLs).
"""
import json

import cfgstore
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx

import netguard
import secrets_store

SERVICES_DIR = Path(os.environ.get("SERVICES_DIR", "/data/services"))

_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:60] or "service"


def _cfg_path(name: str) -> Path:
    return SERVICES_DIR / f"{_slug(name)}.json"


def _load(name: str):
    p = _cfg_path(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def register(mcp):
    @mcp.tool
    def service_add(name: str, base_url: str, token_env: str = "",
                    auth_scheme: str = "Bearer", description: str = "",
                    auth_header: str = "Authorization",
                    write_only: bool = False, category: str = "",
                    tls_insecure: bool = False, ca_bundle: str = "") -> str:
        """Register/update a callable service (stored as DATA — no redeploy).
        token_env = the NAME of the secret holding the auth token (store it with
        secret_set). The token itself is never stored here.
        auth_header = which header carries the token (default "Authorization").
        For APIs that use a custom header instead of Bearer auth, set auth_header
        to that header name and auth_scheme="" so the raw token is sent without a
        "Bearer " prefix.
        category (REQUIRED for a new service) = a free-text group for the catalog
        (e.g. "Smart Home", "Dev", "Documents", "Web") — services are listed grouped
        by it, like skills, so they stay easy to find as the list grows. Call
        service_list first and REUSE an existing category where it fits; service_add
        REFUSES a missing one. Updates are MERGE-safe: fields you don't pass keep
        their existing value, so you can update one field without restating the rest
        (to clear a field, service_delete and re-add).

        TLS is VERIFIED by default (#10, same as scan_add/webdav_add). For a
        self-signed LAN service, point ``ca_bundle`` at its CA/cert file (the
        safe way), or set ``tls_insecure=true`` to skip verification. This tool
        is admin-only, so only an operator can relax TLS."""
        if not category.strip():
            existing = _load(name)
            if not (existing and str(existing.get("category", "")).strip()):
                return ("Refused: a new service MUST have a category (house rule, so "
                        "the catalog stays tidy & findable — same as skills). Pass "
                        "category=\"…\" (e.g. \"Smart Home\", \"Dev\", \"Documents\", "
                        "\"Web\"); call service_list first and REUSE an existing one "
                        "where it fits.")
        try:
            SERVICES_DIR.mkdir(parents=True, exist_ok=True)
            cfg = {
                "name": name,
                "base_url": base_url.rstrip("/"),
                "token_env": token_env,
                "auth_scheme": auth_scheme,
                "auth_header": auth_header or "Authorization",
                "description": description,
                "category": category.strip(),
                "write_only": bool(write_only),
                "tls_insecure": bool(tls_insecure),
                "ca_bundle": ca_bundle.strip(),
            }
            cfgstore.write_merged(_cfg_path(name), cfg)
            note = ""
            if token_env and not secrets_store.get_secret(token_env):
                note = f" — to activate it, call secret_set('{token_env}', <value>) yourself (don't ask the user to edit .env)"
            return f"Registered service '{_slug(name)}'.{note}"
        except Exception as exc:
            return f"Could not register service: {exc}"

    @mcp.tool
    def service_list() -> str:
        """List configured services, grouped by category (name — base_url — description)."""
        if not SERVICES_DIR.exists():
            return "No services configured yet."
        items = sorted(SERVICES_DIR.glob("*.json"))
        if not items:
            return "No services configured yet. Use service_add."
        import tenancy
        rows: list[tuple[str, str]] = []  # (category, line)
        for p in items:
            try:
                c = json.loads(p.read_text(encoding="utf-8"))
                cat = str(c.get("category", "") or "").strip() or "Uncategorized"
                # Per-user capability area: hide services this caller may not use.
                if not tenancy.caller_service_allowed(p.stem, cat):
                    continue
                lock = " — [INGEST-ONLY / write_only]" if c.get("write_only") else ""
                if c.get("tls_insecure"):
                    lock += " — [TLS-INSECURE]"
                rows.append((cat, f"- {p.stem} — {c.get('base_url', '')} — "
                                  f"{c.get('description', '')}{lock}"))
            except Exception:
                rows.append(("Uncategorized", f"- {p.stem} — (unreadable config)"))
        if not rows:
            return ("No services available to you. An admin can grant access with "
                    "tenancy_set(identity, services=...).")
        if all(cat == "Uncategorized" for cat, _ in rows):
            return "\n".join(line for _, line in rows)
        out: list[str] = []
        cats = sorted({c for c, _ in rows}, key=lambda c: (c == "Uncategorized", c.lower()))
        for cat in cats:
            out.append(f"[{cat}]")
            out.extend("  " + line for c, line in rows if c == cat)
        return "\n".join(out)

    @mcp.tool
    def service_delete(name: str) -> str:
        """Remove a registered service by name."""
        p = _cfg_path(name)
        if p.exists():
            p.unlink()
            return f"Deleted service '{_slug(name)}'."
        return f"No service '{name}'."

    @mcp.tool
    def call_service(service: str, path: str = "/", method: str = "GET",
                     json_body: dict = None, params: dict = None) -> str:
        """Call a registered service's API. Only configured services can be reached;
        the auth token is injected server-side from its token_env. Returns status + body."""
        cfg = _load(service)
        if not cfg:
            return f"Unknown service '{service}'. Use service_list / service_add."
        # Per-user capability area: a narrowed caller may only reach the services an
        # admin allow-listed for them. Fail-open (unresolved caller → allowed).
        import tenancy
        if not tenancy.caller_service_allowed(service, cfg.get("category", "")):
            return (f"Denied: service '{service}' is not in your allowed set. "
                    f"An admin can grant it with tenancy_set(identity, services=...).")
        if cfg.get("write_only"):
            return (f"Refused: service '{service}' is INGEST-ONLY (write_only) — reading "
                    f"from it via call_service is blocked by policy, for every client, "
                    f"and this cannot be overridden from here. Data can only be deposited "
                    f"through dedicated ingest tools (e.g. scan_document), never read back.")
        m = (method or "GET").upper()
        if m not in _ALLOWED_METHODS:
            return f"Method '{method}' not allowed."
        if "://" in (path or "") or (path or "").startswith("//"):
            return "Invalid path (must be relative to the service base_url)."
        url = cfg.get("base_url", "").rstrip("/") + "/" + (path or "").lstrip("/")
        ok, reason = netguard.check_url(url)
        if not ok:
            return f"Blocked by network policy — {reason}"
        headers = {}
        tok_env = cfg.get("token_env")
        if tok_env:
            token = secrets_store.get_secret(tok_env)
            if not token:
                return f"Service '{service}' needs secret '{tok_env}'. Store it via the secret_set tool first (do not edit .env)."
            header_name = cfg.get("auth_header") or "Authorization"
            scheme = cfg.get("auth_scheme", "Bearer")
            # With a scheme (e.g. Bearer) send "<scheme> <token>"; without one
            # (custom-header APIs) send the raw token value.
            headers[header_name] = f"{scheme} {token}".strip() if scheme else token
        try:
            with netguard.guard(urlparse(url).hostname or ""):
                r = httpx.request(m, url, json=json_body, params=params, headers=headers,
                                  timeout=30, verify=netguard.tls_verify(cfg))
            body = r.text
            if len(body) > 4000:
                body = body[:4000] + "\n…(truncated)"
            return f"HTTP {r.status_code}\n{body}"
        except Exception as exc:
            return f"Request failed: {exc}"
