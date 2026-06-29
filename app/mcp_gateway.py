"""MCP gateway — connect to OTHER MCP servers as DATA.

Same model as services.py (HTTP) and mqtt_tools.py (MQTT): a generic dispatcher
plus data configs, so a new upstream MCP server is added at RUNTIME with
``mcp_add`` and used via ``mcp_call`` — no new code, no redeploy.

The connector becomes an MCP *client* to the registered servers (reusing the
bundled ``fastmcp.Client`` — no extra dependency). Only servers you register are
reachable (allow-list), the URL passes the SSRF egress guard, and the auth token
is referenced by name and resolved server-side via ``secrets_store`` — never
stored in data, never returned to the model.

Configs live under MCP_DIR. Transport: remote ``http`` (streamable) or ``sse``.
"""
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import netguard
import secrets_store

MCP_DIR = Path(os.environ.get("MCP_DIR", "/data/mcp"))


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:60] or "server"


def _cfg_path(name: str) -> Path:
    return MCP_DIR / f"{_slug(name)}.json"


def _load(name: str):
    p = _cfg_path(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _make_client(cfg):
    """Build a fastmcp.Client for the registered upstream server (with auth)."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport, SSETransport

    url = cfg["url"]
    headers = {}
    tok_env = cfg.get("token_env")
    if tok_env:
        token = secrets_store.get_secret(tok_env)
        if token:
            header = cfg.get("auth_header") or "Authorization"
            scheme = cfg.get("auth_scheme", "Bearer")
            headers[header] = f"{scheme} {token}".strip() if scheme else token

    if (cfg.get("transport") or "http").lower() == "sse":
        transport = SSETransport(url, headers=headers)
    else:
        transport = StreamableHttpTransport(url, headers=headers)
    return Client(transport)


def _format_result(result) -> str:
    """Render a fastmcp call result (CallToolResult or content list) as text."""
    data = getattr(result, "data", None)
    if data is not None:
        try:
            return json.dumps(data, ensure_ascii=False, indent=2, default=str)
        except Exception:
            return str(data)
    content = getattr(result, "content", result)
    parts = []
    for block in (content or []):
        text = getattr(block, "text", None)
        parts.append(text if text is not None else str(block))
    out = "\n".join(parts) if parts else str(result)
    return out[:6000] + "\n…(truncated)" if len(out) > 6000 else out


def register(mcp):
    @mcp.tool
    def mcp_add(name: str, url: str, transport: str = "http", token_env: str = "",
                auth_header: str = "Authorization", auth_scheme: str = "Bearer",
                description: str = "") -> str:
        """Register/update an upstream MCP server as DATA (no redeploy).
        transport: "http" (streamable) or "sse". token_env = NAME of the secret
        holding a bearer token (store it with secret_set); never stored here.
        Only registered servers are reachable; the URL passes the SSRF guard."""
        try:
            MCP_DIR.mkdir(parents=True, exist_ok=True)
            cfg = {
                "name": name,
                "url": url,
                "transport": (transport or "http").lower(),
                "token_env": token_env,
                "auth_header": auth_header or "Authorization",
                "auth_scheme": auth_scheme,
                "description": description,
            }
            _cfg_path(name).write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            note = ""
            if token_env and not secrets_store.get_secret(token_env):
                note = f" — set the token with secret_set('{token_env}', <value>)"
            return f"Registered MCP server '{_slug(name)}'.{note}"
        except Exception as exc:
            return f"Could not register MCP server: {exc}"

    @mcp.tool
    def mcp_list() -> str:
        """List registered upstream MCP servers (name — url — description)."""
        if not MCP_DIR.exists():
            return "No MCP servers configured yet."
        items = sorted(MCP_DIR.glob("*.json"))
        if not items:
            return "No MCP servers configured yet. Use mcp_add."
        out = []
        for p in items:
            try:
                c = json.loads(p.read_text(encoding="utf-8"))
                out.append(f"- {p.stem} — {c.get('url', '')} — {c.get('description', '')}")
            except Exception:
                out.append(f"- {p.stem} — (unreadable config)")
        return "\n".join(out)

    @mcp.tool
    def mcp_delete(name: str) -> str:
        """Remove a registered upstream MCP server by name."""
        p = _cfg_path(name)
        if p.exists():
            p.unlink()
            return f"Deleted MCP server '{_slug(name)}'."
        return f"No MCP server '{name}'."

    @mcp.tool
    async def mcp_tools(server: str) -> str:
        """List the tools a registered upstream MCP server exposes (discovery)."""
        cfg = _load(server)
        if not cfg:
            return f"Unknown MCP server '{server}'. Use mcp_list / mcp_add."
        ok, reason = netguard.check_url(cfg.get("url", ""))
        if not ok:
            return f"Blocked by network policy — {reason}"
        try:
            client = _make_client(cfg)
            with netguard.guard(urlparse(cfg.get("url", "")).hostname or ""):
                async with client:
                    tools = await client.list_tools()
        except Exception as exc:
            return f"Could not reach '{server}': {exc}"
        if not tools:
            return f"'{server}' exposes no tools."
        out = []
        for t in tools:
            desc = (t.description or "").strip().splitlines()
            out.append(f"- {t.name}: {desc[0] if desc else ''}")
        return "\n".join(out)

    @mcp.tool
    async def mcp_call(server: str, tool: str, args: dict = None) -> str:
        """Call a tool on a registered upstream MCP server. STATE-CHANGING tools
        on the remote side — confirm with the user before invoking actions."""
        cfg = _load(server)
        if not cfg:
            return f"Unknown MCP server '{server}'. Use mcp_list / mcp_add."
        ok, reason = netguard.check_url(cfg.get("url", ""))
        if not ok:
            return f"Blocked by network policy — {reason}"
        try:
            client = _make_client(cfg)
            with netguard.guard(urlparse(cfg.get("url", "")).hostname or ""):
                async with client:
                    result = await client.call_tool(tool, args or {})
            return _format_result(result)
        except Exception as exc:
            return f"Call failed: {exc}"
