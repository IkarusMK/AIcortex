"""Auto-Memory — Tier B: deterministic, central capture of durable events.

This is the *server-side* half of auto-memory (Tier A is the in-session
discipline in guide.py, where the LLM that's already talking distills facts and
writes them back). Here, a single FastMCP middleware watches every successful
tool call from ONE central place — no edits scattered across the device/service
modules — and, when a durable structural action happens (a new service/device/
endpoint or a scheduled job), STAGES a memory *candidate*. Candidates never touch
live memory; they queue for review (memory_candidates → memory_promote/reject),
so autonomy can never pollute the curated brain.

DESIGN SAFETY:
- Fail-open: any error in the middleware is swallowed; it never blocks or alters
  a tool call. The connector keeps working even if this whole layer misbehaves.
- On by default: set LEARN_AUTOCAPTURE=0 to disable. Safe to leave on because
  captures are STAGED as candidates (never written to live memory), so they can't
  pollute the curated brain — you just review and promote/reject them.
- No new process / no new container: this is a class added to the existing
  FastMCP instance, exactly like a tool registration.
"""
import os

import memory

# tool name -> (memory type, human label) for durable registrations worth noting.
_DURABLE = {
    "service_add": ("reference", "HTTP service"),
    "mqtt_add": ("reference", "MQTT device"),
    "ftp_add": ("reference", "FTP/FTPS endpoint"),
    "webdav_add": ("reference", "WebDAV endpoint"),
    "ssh_add": ("reference", "SSH host"),
    "print_add": ("reference", "printer"),
    "scan_add": ("reference", "scanner"),
    "mcp_add": ("reference", "MCP server"),
    "mail_add": ("reference", "SMTP account"),
    "cron_add": ("project", "scheduled job"),
}

# argument keys we may surface as context (NEVER secrets — token_env etc. are
# names only, but we keep the allow-list tight and skip anything sensitive).
_CONTEXT_KEYS = ("base_url", "host", "url", "from_addr", "schedule", "description")


def _enabled() -> bool:
    # On by default — candidates are STAGED for review, never written live, so
    # auto-capture can't pollute the curated brain. Set LEARN_AUTOCAPTURE=0 to
    # turn it off.
    return os.environ.get("LEARN_AUTOCAPTURE", "1").strip().lower() in ("1", "true", "yes")


def _stage(tool_name: str, args: dict) -> None:
    spec = _DURABLE.get(tool_name)
    if not spec:
        return
    type_, label = spec
    name = (args.get("name") or "").strip()
    if not name:
        return
    ctx = [f"{k}={args[k]}" for k in _CONTEXT_KEYS if args.get(k)]
    content = (f"Auto-noticed: registered {label} '{name}'"
               + (f" ({', '.join(ctx)})" if ctx else "") + ".\n\n"
               "If there's a durable reason/context worth keeping, promote and "
               "enrich this; otherwise reject it — it's already in the live "
               "bootstrap catalog.")
    memory.stage_candidate(title=f"{label}: {name}", content=content,
                           type_=type_, source="auto")


def _extract(context):
    """Best-effort (name, arguments) from a middleware context across FastMCP
    minor versions. Returns (None, {}) if it can't, so the caller no-ops."""
    msg = getattr(context, "message", None)
    name = getattr(msg, "name", None) if msg is not None else None
    args = getattr(msg, "arguments", None) if msg is not None else None
    return name, (args or {})


def build_middleware():
    """Return a FastMCP Middleware instance, or None if middleware isn't
    available / can't be constructed (caller then simply skips it)."""
    try:
        from fastmcp.server.middleware import Middleware
    except Exception:
        return None

    class LearnMiddleware(Middleware):
        async def on_call_tool(self, context, call_next):
            result = await call_next(context)  # run the real tool first
            try:
                if _enabled():
                    name, args = _extract(context)
                    if name and name in _DURABLE:
                        _stage(name, args)
            except Exception:
                pass  # fail-open: never let capture affect the tool result
            return result

    try:
        return LearnMiddleware()
    except Exception:
        return None
