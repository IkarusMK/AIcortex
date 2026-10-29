"""Tool profiles — how many tools a client sees up front.

- ``core`` (default): the ten tools a conversation needs, two of which —
  ``integration_search`` and ``integration_call`` — reach every other tool.
  Small models and clients with tool limits (OpenAI: 128 incl. the agent's own)
  stay within budget; Anthropic reports tool choice degrading past 30–50 tools.
- ``full``: every tool is listed (strong clients with their own tool search).

Nothing is removed: hidden tools stay callable directly and through the router.
The router runs calls through the server's middleware chain, so roles, key
scopes and per-user areas apply exactly as for a direct call.

Profile per caller, first match wins: the agent key's ``profile`` → the policy's
``profiles`` map (identity → profile) → ``TOOL_PROFILE_DEFAULT`` → ``core``.
"""
import os
import re

from fastmcp import Context
from fastmcp.exceptions import ToolError

CORE_TOOLS = (
    "cortex_start", "memory_search", "memory_read", "memory_write",
    "skill_search", "skill_load", "session_save", "task_list",
    "integration_search", "integration_call",
)
_ROUTER = ("integration_search", "integration_call")
_NAMES = {"core": "core", "kern": "core", "full": "full", "voll": "full"}


def normalize(profile) -> str:
    """'core' / 'full' (German 'kern' / 'voll' accepted), '' if unknown."""
    return _NAMES.get(str(profile or "").strip().lower(), "")


def caller_profile() -> str:
    import authz
    ident, _, claims = authz._identity()
    if isinstance(claims, dict) and normalize(claims.get("profile")):
        return normalize(claims["profile"])
    pol = authz._policy()
    by_identity = pol.get("profiles", {}) if isinstance(pol, dict) else {}
    return (normalize(by_identity.get(ident)) or normalize(os.environ.get("TOOL_PROFILE_DEFAULT"))
            or "core")


def build_middleware():
    from fastmcp.server.middleware import Middleware

    class ProfileMiddleware(Middleware):
        async def on_list_tools(self, context, call_next):
            tools = await call_next(context)
            if caller_profile() == "core":
                return [t for t in tools if t.name in CORE_TOOLS]
            return tools

    return ProfileMiddleware()


async def _callable_tools(ctx: Context) -> list:
    """Every tool THIS caller may run (role and key scopes), minus the router."""
    import authz
    tools = await ctx.fastmcp.list_tools(run_middleware=False)
    _, role = authz.effective_identity()
    return [t for t in tools if t.name not in _ROUTER and authz.key_allows(t.name)
            and (not authz.enabled() or authz.decide(role, t.name)[0])]


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(w) >= 3]


def _score(query_words: list[str], tool) -> int:
    name_words = set(_words(tool.name.replace("_", " ")))
    desc = (tool.description or "").lower()
    return sum(3 * (w in name_words) + (w in desc) for w in query_words)


def _signature(tool) -> str:
    """name(param*: type, …) — '*' marks a required parameter."""
    schema = tool.parameters or {}
    required = set(schema.get("required") or [])
    params = []
    for pname, spec in (schema.get("properties") or {}).items():
        kind = spec.get("type") or ("/".join(s.get("type", "?") for s in spec.get("anyOf", []))
                                    or "any")
        params.append(f"{pname}{'*' if pname in required else ''}: {kind}")
    return f"{tool.name}({', '.join(params)})"


def _summary(tool) -> str:
    first = (tool.description or "").strip().split("\n\n")[0]
    return " ".join(first.split())[:160]


def register(mcp):
    @mcp.tool
    async def integration_search(query: str, limit: int = 8, ctx: Context = None) -> str:
        """Find a tool for what you want to do among everything AICortex offers beyond
        the core set — devices (MQTT, FTP), services, files, mail, calendars, printing,
        scanning, SSH, webhooks, scheduling, agents and administration. Returns each
        match with its parameters (* = required); run one with integration_call.
        Example: integration_search("print a PDF on the office printer")."""
        words = _words(query)
        tools = [t for t in await _callable_tools(ctx) if t.name not in CORE_TOOLS]
        ranked = sorted(((_score(words, t), t) for t in tools), key=lambda st: (-st[0], st[1].name))
        hits = [t for s, t in ranked if s > 0][:max(1, min(int(limit or 8), 20))]
        if not hits:
            return (f"No tool matched '{query}'. Try other words (e.g. the device or "
                    f"action), or cortex_catalog for everything registered.")
        return "\n".join(f"- {_signature(t)} — {_summary(t)}" for t in hits)

    @mcp.tool
    async def integration_call(name: str, arguments: dict | None = None, ctx: Context = None):
        """Run a tool found with integration_search. `arguments` holds its parameters
        as an object. The same permissions apply as for calling the tool directly.
        Example: integration_call(name="print_document",
        arguments={"printer": "office", "file": "scan.pdf"})."""
        allowed = {t.name for t in await _callable_tools(ctx)}
        if name not in allowed:
            raise ToolError(f"No tool '{name}' that you may call. Find tools with "
                            f"integration_search(\"<what you want to do>\").")
        return await ctx.fastmcp.call_tool(name, arguments or {})
