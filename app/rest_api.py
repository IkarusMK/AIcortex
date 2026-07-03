"""Native REST layer — call AICortex tools over plain HTTP, no MCP client needed.

Three public routes served alongside ``/mcp/`` but OUTSIDE the MCP OAuth (like
``/hooks/*``), authenticated by a per-user API key instead (see apikeys.py):

    GET  /api/v1/tools            → the tools THIS key may call, with JSON schemas
    POST /api/v1/tools/<name>     → invoke a tool (body = JSON arguments)
    GET  /api/v1/openapi.json     → an OpenAPI 3.1 spec of this key's tools, so any
                                     function-calling framework (LangChain, n8n, an
                                     OpenAI-compatible client) can load AICortex directly

Security model — the key is the whole auth, so every gate applies on every call:
1. Bearer key in the ``Authorization`` header ONLY (never a query param → no log leak),
   verified constant-time, checked for expiry/disabled.
2. Per-key RATE LIMIT (429 + Retry-After).
3. Per-key SCOPE allow-list (default-deny) + a hard denylist (secret/key/tenancy tools
   are never reachable via a key).
4. The SAME authz + per-user areas as an OIDC session: the key's identity resolves a
   role (never admin by default) and the call runs through ``authz.enforce_rest`` and,
   during ``tool.run``, a request-scoped identity so in-tool self-scoping applies.

Streaming: pass ``?stream=1`` or ``Accept: text/event-stream`` to get SSE (heartbeats
while a long tool runs, then a final ``result``/``error`` event) — useful behind proxies
that time out slow POSTs. Otherwise a single JSON response.

Operator note: expose ``/api/*`` (and ``/hooks/*``) past the reverse proxy's auth, never
``/mcp``. Enabled by default; set ``API_ENABLED=0`` to turn the REST layer off entirely.
"""
import asyncio
import inspect
import json
import os

import apikeys
import authz
import version

_MAX_BODY = int(os.environ.get("API_MAX_BODY_BYTES", str(1_000_000)))
_SSE_HEARTBEAT = 15  # seconds between keepalive comments while a tool runs


def _enabled() -> bool:
    return os.environ.get("API_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _bearer(request) -> str:
    """Extract the API key from ``Authorization: Bearer <key>`` (header only)."""
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return ""


def _wants_stream(request) -> bool:
    q = request.query_params.get("stream", "").strip().lower()
    if q in ("1", "true", "yes", "sse"):
        return True
    return "text/event-stream" in request.headers.get("accept", "").lower()


def _base_url(request) -> str:
    base = os.environ.get("BASE_URL", "").rstrip("/")
    if base:
        return f"{base}/api/v1"
    return str(request.base_url).rstrip("/") + "/api/v1"


def _audit(rec: dict, tool: str, decision: str, reason: str) -> None:
    try:
        authz.audit(rec.get("sub", "?"), "apikey", f"api:{tool}", decision,
                    f"key={rec.get('keyid', '?')}: {reason}")
    except Exception:
        pass


def _result_payload(result):
    """Turn a FastMCP ToolResult into a clean JSON value. AICortex tools return a
    string, which FastMCP wraps as ``structured_content={"result": <str>}`` — unwrap
    that to the bare value; pass any other structured dict through; else join text."""
    sc = getattr(result, "structured_content", None)
    if isinstance(sc, dict):
        if set(sc.keys()) == {"result"}:
            return sc["result"]
        return sc
    texts = []
    for c in getattr(result, "content", None) or []:
        t = getattr(c, "text", None)
        if t is not None:
            texts.append(t)
    return "\n".join(texts) if texts else None


async def _all_tools(mcp) -> list:
    """Best-effort enumerate the server's tools (for discovery/OpenAPI). Robust to
    FastMCP accessor differences; the critical INVOKE path uses mcp.get_tool instead,
    so an empty list here only yields an empty catalog, never a security gap."""
    for attr in ("list_tools", "get_tools"):
        fn = getattr(mcp, attr, None)
        if not callable(fn):
            continue
        try:
            res = fn()
            if inspect.isawaitable(res):
                res = await res
            return list(res.values()) if isinstance(res, dict) else list(res)
        except Exception:
            continue
    lp = getattr(mcp, "_local_provider", None) or getattr(mcp, "local_provider", None)
    if lp is not None:
        for attr in ("list_tools", "get_tools"):
            fn = getattr(lp, attr, None)
            if not callable(fn):
                continue
            try:
                res = fn()
                if inspect.isawaitable(res):
                    res = await res
                return list(res.values()) if isinstance(res, dict) else list(res)
            except Exception:
                continue
    return []


def _visible(rec: dict, role: str, name: str) -> bool:
    """Whether a key may see/call `name`: not hard-denied, within the key's scope,
    and permitted by the identity's role."""
    if apikeys.hard_denied(name) or not apikeys.scope_allows(rec.get("scopes", []), name):
        return False
    try:
        ok, _ = authz.decide(role, name)
        return ok
    except Exception:
        return False


def register(mcp):
    from starlette.responses import JSONResponse, StreamingResponse

    def _json(obj, status: int = 200, hdr: dict = None):
        return JSONResponse(obj, status_code=status, headers=hdr or {})

    async def _auth(request):
        """Returns (rec, role) on success, or a JSONResponse to short-circuit."""
        key = _bearer(request)
        rec = apikeys.verify(key) if key else None
        if not rec:
            return _json({"error": "unauthorized"}, 401, {"WWW-Authenticate": "Bearer"})
        ok, retry = apikeys.rate_ok(rec["keyid"])
        if not ok:
            return _json({"error": "rate limited", "retry_after": retry}, 429,
                         {"Retry-After": str(retry)})
        return rec, authz.role_for_apikey(rec["sub"])

    # ── GET /api/v1/tools — the tools THIS key may call ──────────────────────
    @mcp.custom_route("/api/v1/tools", methods=["GET"])
    async def _list_tools(request):
        if not _enabled():
            return _json({"error": "REST API disabled"}, 404)
        authd = await _auth(request)
        if isinstance(authd, JSONResponse):
            return authd
        rec, role = authd
        out = []
        for t in await _all_tools(mcp):
            n = getattr(t, "name", "")
            if not n or not _visible(rec, role, n):
                continue
            out.append({"name": n,
                        "description": getattr(t, "description", "") or "",
                        "input_schema": getattr(t, "parameters", {}) or {}})
        out.sort(key=lambda d: d["name"])
        return _json({"tools": out, "count": len(out)})

    # ── GET /api/v1/openapi.json — spec of this key's tools ───────────────────
    @mcp.custom_route("/api/v1/openapi.json", methods=["GET"])
    async def _openapi(request):
        if not _enabled():
            return _json({"error": "REST API disabled"}, 404)
        authd = await _auth(request)
        if isinstance(authd, JSONResponse):
            return authd
        rec, role = authd
        paths = {}
        for t in await _all_tools(mcp):
            n = getattr(t, "name", "")
            if not n or not _visible(rec, role, n):
                continue
            summary = (getattr(t, "description", "") or n).strip().split("\n")[0][:120]
            paths[f"/api/v1/tools/{n}"] = {
                "post": {
                    "operationId": n,
                    "summary": summary,
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {
                            "schema": getattr(t, "parameters", {}) or {"type": "object"}}},
                    },
                    "responses": {"200": {
                        "description": "tool result",
                        "content": {"application/json": {"schema": {"type": "object"}}}}},
                    "security": [{"bearerAuth": []}],
                }
            }
        spec = {
            "openapi": "3.1.0",
            "info": {
                "title": "AICortex REST API",
                "version": version.__version__,
                "description": "Call AICortex tools over HTTP with a per-user API key. "
                               "Only the tools this key is scoped for are listed.",
            },
            "servers": [{"url": _base_url(request)}],
            "components": {"securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer"}}},
            "security": [{"bearerAuth": []}],
            "paths": paths,
        }
        return _json(spec)

    # ── POST /api/v1/tools/<name> — invoke a tool ────────────────────────────
    @mcp.custom_route("/api/v1/tools/{name}", methods=["POST"])
    async def _invoke(request):
        if not _enabled():
            return _json({"error": "REST API disabled"}, 404)
        authd = await _auth(request)
        if isinstance(authd, JSONResponse):
            return authd
        rec, role = authd
        name = request.path_params.get("name", "")
        # Scope gate (default-deny) + hard denylist — before touching anything.
        if apikeys.hard_denied(name) or not apikeys.scope_allows(rec.get("scopes", []), name):
            _audit(rec, name, "deny", "out of key scope")
            return _json({"error": "forbidden",
                          "detail": "tool not in this key's scope"}, 403)
        raw = await request.body()
        if len(raw) > _MAX_BODY:
            return _json({"error": "payload too large"}, 413)
        try:
            args = json.loads(raw) if raw.strip() else {}
        except Exception:
            return _json({"error": "invalid JSON body"}, 400)
        if not isinstance(args, dict):
            return _json({"error": "body must be a JSON object of tool arguments"}, 400)
        sub = rec["sub"]
        # Same policy gate as the MCP middleware (role decide + areas + scoping).
        ok, reason = authz.enforce_rest(sub, role, name, args)
        if not ok:
            return _json({"error": "forbidden", "detail": reason}, 403)
        if _wants_stream(request):
            return _stream(mcp, rec, sub, role, name, args)
        try:
            with authz.rest_identity(sub, role):
                tool = await mcp.get_tool(name)
                if tool is None:
                    return _json({"error": "no such tool", "tool": name}, 404)
                result = await tool.run(args)
            _audit(rec, name, "allow", "ok")
            return _json({"ok": True, "tool": name, "result": _result_payload(result)})
        except Exception as exc:
            _audit(rec, name, "error", str(exc))
            return _json({"error": "tool error", "detail": str(exc)}, 500)

    def _stream(mcp, rec, sub, role, name, args):
        """SSE: heartbeat while the tool runs, then one `result` (or `error`) event."""
        async def gen():
            yield b": connected\n\n"

            async def run():
                with authz.rest_identity(sub, role):
                    tool = await mcp.get_tool(name)
                    if tool is None:
                        raise LookupError(f"no such tool: {name}")
                    return await tool.run(args)

            task = asyncio.ensure_future(run())
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=_SSE_HEARTBEAT)
                if not done:
                    yield b": keepalive\n\n"
            try:
                payload = {"ok": True, "tool": name, "result": _result_payload(task.result())}
                _audit(rec, name, "allow", "ok (sse)")
                yield _sse("result", payload)
            except Exception as exc:
                _audit(rec, name, "error", str(exc))
                yield _sse("error", {"error": "tool error", "detail": str(exc)})

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})


def _sse(event: str, data) -> bytes:
    return (f"event: {event}\n"
            f"data: {json.dumps(data, ensure_ascii=False)}\n\n").encode("utf-8")
