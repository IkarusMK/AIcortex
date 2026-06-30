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


# MCP Streamable HTTP protocol version we advertise on the handshake. Servers
# echo back the version they support; we adopt it for subsequent requests.
_PROTOCOL_VERSION = "2025-06-18"


class _HttpMCPError(Exception):
    """An upstream Streamable-HTTP MCP call failed (HTTP or JSON-RPC error)."""


def _auth_headers(cfg) -> dict:
    """Build the auth header dict for an upstream server (token resolved
    server-side from the vault, never stored in the config or returned)."""
    headers = {}
    tok_env = cfg.get("token_env")
    if tok_env:
        token = secrets_store.get_secret(tok_env)
        if token:
            header = cfg.get("auth_header") or "Authorization"
            scheme = cfg.get("auth_scheme", "Bearer")
            headers[header] = f"{scheme} {token}".strip() if scheme else token
    return headers


def _parse_jsonrpc(resp) -> dict:
    """Extract the JSON-RPC object from a Streamable-HTTP response, which per spec
    may be either ``application/json`` (a single object) OR ``text/event-stream``
    (the object delivered in an SSE ``data:`` event). Returns the JSON-RPC dict."""
    ctype = (resp.headers.get("content-type") or "").lower()
    body = resp.text
    if "text/event-stream" in ctype:
        found = None
        for line in body.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            try:
                cand = json.loads(line[5:].strip())
            except Exception:
                continue
            if isinstance(cand, dict) and ("result" in cand or "error" in cand):
                found = cand  # keep the last result/error event
        if found is None:
            raise _HttpMCPError("no JSON-RPC result in SSE response")
        return found
    try:
        return json.loads(body)
    except Exception as exc:
        raise _HttpMCPError(f"invalid JSON response: {exc}")


class _StreamableMCP:
    """A minimal Streamable-HTTP MCP *client* for registered upstream servers.

    Why not reuse fastmcp's transport here: some spec-compliant servers (e.g.
    Outline's built-in MCP server, issue #17) do NOT offer the optional
    server→client GET/SSE stream and answer the handshake GET with ``405 Method
    Not Allowed``. The MCP spec says a client MUST tolerate that and keep using
    plain POST. This client does exactly the spec's minimum: POST JSON-RPC with
    ``Accept: application/json, text/event-stream`` (the strictly-required Accept),
    follow redirects, carry the ``Mcp-Session-Id`` across requests, and never open
    a standalone GET stream — so it works whether or not the server provides one.
    Supports the three operations the gateway needs: initialize, tools/list,
    tools/call. (SSE-transport servers still go through fastmcp.)
    """

    def __init__(self, url: str, headers: dict, timeout: float = 30.0):
        self.url = url
        self.auth_headers = dict(headers or {})
        self.timeout = timeout
        self.session_id = None
        self.proto = _PROTOCOL_VERSION
        self._id = 0
        self._client = None

    def _headers(self, *, with_proto: bool = False) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",  # both, per spec (#17)
        }
        h.update(self.auth_headers)
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if with_proto and self.proto:
            h["MCP-Protocol-Version"] = self.proto
        return h

    async def _rpc(self, method: str, params=None, *, with_proto: bool = False):
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        resp = await self._client.post(
            self.url, json=payload, headers=self._headers(with_proto=with_proto))
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        status = getattr(resp, "status_code", 200)
        if status >= 400:
            reason = getattr(resp, "reason_phrase", "")
            raise _HttpMCPError(f"HTTP {status} {reason} on '{method}'")
        obj = _parse_jsonrpc(resp)
        if isinstance(obj, dict) and obj.get("error"):
            err = obj["error"] or {}
            raise _HttpMCPError(f"{method}: {err.get('code')} {err.get('message')}")
        return obj.get("result") if isinstance(obj, dict) else None

    async def _notify(self, method: str, params=None):
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        try:  # a notification has no response; tolerate any 2xx/4xx quietly
            await self._client.post(self.url, json=payload,
                                    headers=self._headers(with_proto=True))
        except Exception:
            pass

    async def __aenter__(self):
        import httpx
        self._client = httpx.AsyncClient(follow_redirects=True, timeout=self.timeout)
        init = await self._rpc("initialize", {
            "protocolVersion": self.proto,
            "capabilities": {},
            "clientInfo": {"name": "AICortex", "version": "1.5"},
        })
        if isinstance(init, dict) and init.get("protocolVersion"):
            self.proto = init["protocolVersion"]  # adopt the negotiated version
        await self._notify("notifications/initialized")
        return self

    async def __aexit__(self, *exc):
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass

    async def list_tools(self):
        res = await self._rpc("tools/list", {}, with_proto=True)
        return res.get("tools", []) if isinstance(res, dict) else []

    async def call_tool(self, name: str, args=None):
        return await self._rpc("tools/call",
                               {"name": name, "arguments": args or {}}, with_proto=True)


def _make_client(cfg):
    """Return an async-context client for a registered upstream server.

    - transport 'http' (streamable, the default) → our spec-minimal POST client
      (always sends the required Accept header, tolerates servers without a GET
      stream — fixes #17).
    - transport 'sse' (legacy) → the bundled fastmcp SSE client.
    Both expose ``async with client`` + ``list_tools`` / ``call_tool``.
    """
    headers = _auth_headers(cfg)
    url = cfg["url"]
    if (cfg.get("transport") or "http").lower() == "sse":
        from fastmcp import Client
        from fastmcp.client.transports import SSETransport
        return Client(SSETransport(url, headers=headers))
    return _StreamableMCP(url, headers)


def _tool_name_desc(t):
    """(name, first-line-description) for a tool from either our dict result or a
    fastmcp tool object."""
    if isinstance(t, dict):
        name = t.get("name", "")
        desc = (t.get("description") or "").strip()
    else:
        name = getattr(t, "name", "")
        desc = (getattr(t, "description", "") or "").strip()
    return name, (desc.splitlines()[0] if desc else "")


def _format_result(result) -> str:
    """Render a call result as text — handles our minimal-client dict result AND a
    fastmcp CallToolResult/content list."""
    # Our Streamable-HTTP client returns the raw JSON-RPC `result` dict.
    if isinstance(result, dict):
        if result.get("structuredContent") is not None:
            try:
                return json.dumps(result["structuredContent"], ensure_ascii=False,
                                  indent=2, default=str)
            except Exception:
                return str(result["structuredContent"])
        blocks = result.get("content")
        if isinstance(blocks, list):
            parts = []
            for b in blocks:
                if isinstance(b, dict):
                    parts.append(b.get("text") if b.get("text") is not None
                                 else json.dumps(b, ensure_ascii=False, default=str))
                else:
                    parts.append(str(b))
            out = "\n".join(parts)
            return out[:6000] + "\n…(truncated)" if len(out) > 6000 else out
        try:
            return json.dumps(result, ensure_ascii=False, indent=2, default=str)
        except Exception:
            return str(result)
    # fastmcp object (SSE path)
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
            name, desc = _tool_name_desc(t)
            out.append(f"- {name}: {desc}")
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
