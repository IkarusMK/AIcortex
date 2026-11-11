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

Two options per server:
- ``auth="oauth"``: the server wants a browser sign-in instead of a fixed token
  (mcp_oauth.py). Each person signs in once in the WebUI; a call uses the caller's
  OWN login, renewed on its own, and one rejected token gets one renewal and retry.
- ``read_only=True``: only tools the server's own listing marks ``readOnlyHint`` can be
  called — a second fence behind the server's own permissions.
"""
import asyncio
import json

import cfgstore
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import mcp_oauth
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
    """An upstream Streamable-HTTP MCP call failed (HTTP or JSON-RPC error). ``status``
    is the HTTP status when there was one (0 otherwise)."""

    def __init__(self, message: str = "", status: int = 0):
        super().__init__(message)
        self.status = status


class _Refused(Exception):
    """A call AICortex itself refuses (read-only server, tool not marked read-only)."""


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
    follow redirects on the server's own origin, carry the ``Mcp-Session-Id`` across
    requests, and never open a standalone GET stream — so it works whether or not the
    server provides one. Supports the three operations the gateway needs: initialize,
    tools/list, tools/call. (SSE-transport servers still go through fastmcp.)

    Egress: every request, redirect hops included, goes through netguard's pinned
    transport (resolved once, checked, connected to the checked address) — the
    getaddrinfo guard alone does not cover async clients (uvloop, IP literals). A
    redirect to another origin is refused: it would carry the auth header (a custom
    one survives httpx's cross-origin stripping) to a host the token is not bound to.
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
            raise _HttpMCPError(f"HTTP {status} {reason} on '{method}'", status)
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

    async def _stay_on_origin(self, request):
        """httpx request hook, run for every hop: only the configured origin (or its
        https upgrade) may be requested."""
        if not _same_origin(self.url, str(request.url)):
            raise _HttpMCPError(
                f"Refused: the server redirected to another address ({request.url.host}); "
                f"the gateway only follows redirects on the server's own origin.")

    async def __aenter__(self):
        import httpx
        self._client = httpx.AsyncClient(
            follow_redirects=True, timeout=self.timeout,
            transport=netguard.pinned_async_transport(),
            event_hooks={"request": [self._stay_on_origin]})
        try:
            init = await self._rpc("initialize", {
                "protocolVersion": self.proto,
                "capabilities": {},
                "clientInfo": {"name": "AICortex", "version": "1.5"},
            })
            if isinstance(init, dict) and init.get("protocolVersion"):
                self.proto = init["protocolVersion"]  # adopt the negotiated version
            await self._notify("notifications/initialized")
        except BaseException:
            # __aexit__ never runs when __aenter__ fails — close the client here.
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *exc):
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass

    async def list_tools(self, max_pages: int = 50):
        """Every tool, following ``nextCursor`` page by page (bounded)."""
        tools, cursor = [], None
        for _ in range(max_pages):
            res = await self._rpc("tools/list", {"cursor": cursor} if cursor else {},
                                  with_proto=True)
            if not isinstance(res, dict):
                break
            tools.extend(res.get("tools") or [])
            cursor = res.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, args=None):
        return await self._rpc("tools/call",
                               {"name": name, "arguments": args or {}}, with_proto=True)


def _origin(url: str) -> tuple:
    u = urlparse(url)
    scheme = (u.scheme or "").lower()
    return scheme, (u.hostname or "").lower(), u.port or {"https": 443, "http": 80}.get(scheme)


def _same_origin(base: str, url: str) -> bool:
    """`url` is on `base`'s origin, or is its https upgrade on the default ports."""
    a, b = _origin(base), _origin(url)
    return a == b or (a[1] == b[1] and a[0] == "http" and a[2] == 80
                      and b[0] == "https" and b[2] == 443)


def _make_client(cfg, headers=None):
    """Return an async-context client for a registered upstream server.

    - transport 'http' (streamable, the default) → our spec-minimal POST client
      (always sends the required Accept header, tolerates servers without a GET
      stream — fixes #17).
    - transport 'sse' (legacy) → the bundled fastmcp SSE client, its HTTP client built
      on netguard's pinned transport (same egress check as the HTTP path).
    Both expose ``async with client`` + ``list_tools`` / ``call_tool``.
    `headers` defaults to the vault-token header of the config.
    """
    headers = _auth_headers(cfg) if headers is None else headers
    url = cfg["url"]
    if (cfg.get("transport") or "http").lower() == "sse":
        from fastmcp import Client
        from fastmcp.client.transports import SSETransport
        return Client(SSETransport(url, headers=headers,
                                   httpx_client_factory=netguard.mcp_client_factory))
    return _StreamableMCP(url, headers)


def _flag(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return value is True or value == 1


def _read_only(t) -> bool:
    """Whether the server's own listing marks a tool read-only (MCP annotations)."""
    ann = t.get("annotations") if isinstance(t, dict) else getattr(t, "annotations", None)
    if isinstance(ann, dict):
        return ann.get("readOnlyHint") is True
    return getattr(ann, "readOnlyHint", None) is True


async def _headers(server: str, cfg: dict, force: bool = False) -> dict:
    """Auth headers for one connection: the caller's OAuth login, else the vault token."""
    if not mcp_oauth.is_oauth(cfg):
        return _auth_headers(cfg)
    owner = mcp_oauth.caller()      # resolved here, in the caller's own context
    token = await asyncio.to_thread(mcp_oauth.bearer, server, cfg, owner, force)
    return {"Authorization": f"Bearer {token}"}


async def _run(server: str, cfg: dict, work):
    """Open a connection to `server` and return ``await work(client)``. A token the
    server rejects (401) on an OAuth server is renewed once and the work repeated."""
    host = urlparse(cfg.get("url", "")).hostname or ""
    for attempt in (0, 1):
        client = _make_client(cfg, await _headers(server, cfg, force=attempt == 1))
        try:
            with netguard.guard(host):
                async with client:
                    return await work(client)
        except _HttpMCPError as exc:
            if attempt == 0 and exc.status == 401 and mcp_oauth.is_oauth(cfg):
                continue
            raise


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


def _loosens_read_only(name: str, read_only: bool) -> bool:
    """Would this mcp_add switch read_only OFF on a server where it is on? Only a value
    the caller really passed counts (an omitted one keeps the stored setting)."""
    old = _load(name)
    if read_only or not (old and _flag(old.get("read_only"))):
        return False
    given = cfgstore._GIVEN.get()
    return given is not None and "read_only" in given


def _check_add(name: str, url: str, token_env: str, auth: str, read_only: bool) -> str:
    """Why an mcp_add is refused, or ""."""
    if auth not in ("", "oauth"):
        return "Refused: auth must be empty (a token_env or none) or 'oauth'."
    if auth == "oauth" and token_env:
        return ("Refused: an OAuth server takes no token_env — the token comes from each "
                "person's own sign-in.")
    if (token_env or "").upper().startswith(mcp_oauth.SECRET_PREFIX):
        return (f"Refused: secret names starting with {mcp_oauth.SECRET_PREFIX} are reserved "
                f"for OAuth sign-ins.")
    if auth == "oauth":
        try:
            mcp_oauth._safe_url(url, "MCP server address", plain_ok=True)
        except mcp_oauth.OAuthError as exc:
            return f"Refused: {exc} Plain http is accepted only inside the LAN."
    if _loosens_read_only(name, read_only):
        import authz
        if not authz.from_webui():
            return ("Refused: read_only stays on. Switching it off needs a person in the "
                    "AICortex WebUI (Services → Edit), not a tool call.")
    return ""


def _list_line(stem: str, c: dict) -> str:
    flags = [f for f, on in (("OAuth", mcp_oauth.is_oauth(c)), ("read-only", _flag(c.get("read_only"))))
             if on]
    line = f"- {stem} — {c.get('url', '')} — {c.get('description', '')}"
    return f"{line} [{', '.join(flags)}]" if flags else line


def _tool_lines(tools, read_only: bool) -> str:
    out = []
    for t in tools:
        name, desc = _tool_name_desc(t)
        mark = (" [read-only]" if _read_only(t)
                else " [blocked: not marked read-only]" if read_only else "")
        out.append(f"- {name}: {desc}{mark}")
    return "\n".join(out)


def _call_work(server: str, cfg: dict, tool: str, args):
    """The work of one mcp_call: on a read-only server, first make sure the server's own
    listing marks `tool` read-only."""
    async def work(client):
        if _flag(cfg.get("read_only")):
            listed = {_tool_name_desc(t)[0]: t for t in await client.list_tools()}
            if tool not in listed or not _read_only(listed[tool]):
                raise _Refused(
                    f"Refused: '{tool}' is not marked read-only by '{_slug(server)}', and this "
                    f"server is set to read-only in AICortex (mcp_add read_only).")
        return await client.call_tool(tool, args or {})
    return work


def register(mcp):
    @mcp.tool
    def mcp_add(name: str, url: str, transport: str = "http", token_env: str = "",
                auth_header: str = "Authorization", auth_scheme: str = "Bearer",
                description: str = "", auth: str = "", read_only: bool = False) -> str:
        """Register/update an upstream MCP server as DATA (no redeploy).
        transport: "http" (streamable) or "sse". token_env = NAME of the secret
        holding a bearer token (store it with secret_set); never stored here.
        auth="oauth" instead, for a server that wants a browser sign-in (OAuth, often
        with 2FA): each person signs in once in the AICortex WebUI (Services → Sign in)
        and calls use their own login. read_only=true: only tools the server marks
        read-only can be called. Only registered servers are reachable; the URL passes
        the SSRF guard."""
        auth = (auth or "").strip().lower()
        refused = _check_add(name, url, token_env, auth, bool(read_only))
        if refused:
            return refused
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
                "auth": auth,
                "read_only": bool(read_only),
            }
            cfgstore.write_merged(_cfg_path(name), cfg)
            if auth == "oauth":
                return (f"Registered MCP server '{_slug(name)}'. Sign in: AICortex WebUI → "
                        f"Services → {_slug(name)} → Sign in (each person signs in with their "
                        f"own account).")
            note = ""
            if token_env and not secrets_store.get_secret(token_env):
                note = f" — set the token with secret_set('{token_env}', <value>)"
            return f"Registered MCP server '{_slug(name)}'.{note}"
        except Exception as exc:
            return f"Could not register MCP server: {exc}"

    @mcp.tool
    def mcp_list() -> str:
        """List registered upstream MCP servers (name — url — description), marked
        [OAuth] / [read-only] where that applies."""
        if not MCP_DIR.exists():
            return "No MCP servers configured yet."
        items = sorted(MCP_DIR.glob("*.json"))
        if not items:
            return "No MCP servers configured yet. Use mcp_add."
        out = []
        for p in items:
            try:
                out.append(_list_line(p.stem, json.loads(p.read_text(encoding="utf-8"))))
            except Exception:
                out.append(f"- {p.stem} — (unreadable config)")
        return "\n".join(out)

    @mcp.tool
    def mcp_delete(name: str) -> str:
        """Remove a registered upstream MCP server by name (and everyone's OAuth
        sign-in for it)."""
        p = _cfg_path(name)
        if p.exists():
            p.unlink()
            gone = mcp_oauth.forget(name)
            extra = f" Removed {gone} sign-in(s)." if gone else ""
            return f"Deleted MCP server '{_slug(name)}'.{extra}"
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
        async def work(client):
            return await client.list_tools()
        try:
            tools = await _run(_slug(server), cfg, work)
        except mcp_oauth.OAuthError as exc:
            return str(exc)
        except Exception as exc:
            return f"Could not reach '{server}': {exc}"
        if not tools:
            return f"'{server}' exposes no tools."
        return _tool_lines(tools, _flag(cfg.get("read_only")))

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
            result = await _run(_slug(server), cfg, _call_work(server, cfg, tool, args))
            return _format_result(result)
        except (mcp_oauth.OAuthError, _Refused) as exc:
            return str(exc)
        except Exception as exc:
            return f"Call failed: {exc}"
