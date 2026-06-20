"""Generic, allow-listed service caller.

This is the primitive that lets new integrations be added as DATA (a service
config) + a skill, instead of new Python code. A *service* is a small JSON file
under SERVICES_DIR describing a base_url and, optionally, the NAME of an
environment variable holding its auth token — so secrets stay in the server's
environment, are never stored in data, and are never returned to the model.

``call_service`` only reaches registered services (no arbitrary URLs).
"""
import json
import os
import re
from pathlib import Path

import httpx

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
                    auth_header: str = "Authorization") -> str:
        """Register/update a callable service (stored as DATA — no redeploy).
        token_env = the NAME of the secret holding the auth token (store it with
        secret_set). The token itself is never stored here.
        auth_header = which header carries the token (default "Authorization").
        For APIs with a custom header (e.g. n8n) use auth_header="X-N8N-API-KEY"
        and auth_scheme="" so the raw token is sent without a "Bearer " prefix."""
        try:
            SERVICES_DIR.mkdir(parents=True, exist_ok=True)
            cfg = {
                "name": name,
                "base_url": base_url.rstrip("/"),
                "token_env": token_env,
                "auth_scheme": auth_scheme,
                "auth_header": auth_header or "Authorization",
                "description": description,
            }
            _cfg_path(name).write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            note = ""
            if token_env and not secrets_store.get_secret(token_env):
                note = f" — to activate it, call secret_set('{token_env}', <value>) yourself (don't ask the user to edit .env)"
            return f"Registered service '{_slug(name)}'.{note}"
        except Exception as exc:
            return f"Could not register service: {exc}"

    @mcp.tool
    def service_list() -> str:
        """List configured services (name — base_url — description)."""
        if not SERVICES_DIR.exists():
            return "No services configured yet."
        items = sorted(SERVICES_DIR.glob("*.json"))
        if not items:
            return "No services configured yet. Use service_add."
        out = []
        for p in items:
            try:
                c = json.loads(p.read_text(encoding="utf-8"))
                out.append(f"- {p.stem} — {c.get('base_url', '')} — {c.get('description', '')}")
            except Exception:
                out.append(f"- {p.stem} — (unreadable config)")
        return "\n".join(out)

    @mcp.tool
    def call_service(service: str, path: str = "/", method: str = "GET",
                     json_body: dict = None, params: dict = None) -> str:
        """Call a registered service's API. Only configured services can be reached;
        the auth token is injected server-side from its token_env. Returns status + body."""
        cfg = _load(service)
        if not cfg:
            return f"Unknown service '{service}'. Use service_list / service_add."
        m = (method or "GET").upper()
        if m not in _ALLOWED_METHODS:
            return f"Method '{method}' not allowed."
        if "://" in (path or "") or (path or "").startswith("//"):
            return "Invalid path (must be relative to the service base_url)."
        url = cfg.get("base_url", "").rstrip("/") + "/" + (path or "").lstrip("/")
        headers = {}
        tok_env = cfg.get("token_env")
        if tok_env:
            token = secrets_store.get_secret(tok_env)
            if not token:
                return f"Service '{service}' needs secret '{tok_env}'. Store it via the secret_set tool first (do not edit .env)."
            header_name = cfg.get("auth_header") or "Authorization"
            scheme = cfg.get("auth_scheme", "Bearer")
            # With a scheme (e.g. Bearer) send "<scheme> <token>"; without one
            # (custom-header APIs like n8n) send the raw token value.
            headers[header_name] = f"{scheme} {token}".strip() if scheme else token
        try:
            r = httpx.request(m, url, json=json_body, params=params, headers=headers, timeout=30)
            body = r.text
            if len(body) > 4000:
                body = body[:4000] + "\n…(truncated)"
            return f"HTTP {r.status_code}\n{body}"
        except Exception as exc:
            return f"Request failed: {exc}"
