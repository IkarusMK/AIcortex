"""Update-safe config writer for the *_add registration tools.

The registration tools (service_add, scan_add, mqtt_add, webdav_add, …) are
UPSERTS: the same tool creates a new entry or updates an existing one, and every
config key is named exactly like the tool parameter that sets it.

``write_merged`` applies an update the way the caller meant it:

- a parameter the caller PASSED is stored as given — including "" / False / 0,
  so a field can be cleared or a flag switched off again;
- a parameter the caller did NOT pass keeps the stored value (a tool default
  such as auth_header="Authorization" never overwrites a custom value);
- on a new entry, parameters that weren't passed get the tool's defaults.

Which parameters were passed comes from the raw tool-call arguments, recorded by
``build_middleware`` (fastmcp hands the function every parameter with defaults
filled in, so the function itself can't tell); the WebUI declares them with
``given``. Any other direct call falls back to the older rule: default-looking
values keep the stored one.
"""
import contextlib
import contextvars
import json
from pathlib import Path

import store

_GIVEN: contextvars.ContextVar = contextvars.ContextVar("cfg_given_args", default=None)


def _is_default(v) -> bool:
    """A value that looks like 'not set' (used only when the passed args are unknown)."""
    return v is None or v == "" or v is False or v == 0 or v == []


def merge(old: dict, new: dict, given=None) -> dict:
    """Overlay ``new`` onto ``old``. ``given`` = names the caller actually passed
    (None = unknown). Returns a new dict (never mutates the inputs)."""
    if not isinstance(old, dict) or not old:
        return dict(new)
    out = dict(old)
    for k, v in new.items():
        if k in old:
            if given is not None and k not in given:
                continue  # caller didn't mention it → keep the stored value
            if given is None and _is_default(v):
                continue  # unknown caller intent → don't let a default clobber it
        out[k] = v
    return out


def write_merged(path: Path, cfg: dict) -> None:
    """Write ``cfg`` to ``path`` as JSON, merged into any existing config so an
    update only changes what the caller asked to change."""
    with store.locked(path):
        existing = store.read_json(path, {})
        if not isinstance(existing, dict):
            existing = {}
        store.write_text_atomic(path, json.dumps(merge(existing, cfg, _GIVEN.get()), indent=2))


@contextlib.contextmanager
def given(names):
    """Declare which parameters a non-MCP caller (the WebUI) actually passed, so an
    update through it follows the same rules as a tool call."""
    token = _GIVEN.set(set(names))
    try:
        yield
    finally:
        _GIVEN.reset(token)


def build_middleware():
    """Middleware that records which arguments the client passed to a tool call."""
    from fastmcp.server.middleware import Middleware

    class GivenArgsMiddleware(Middleware):
        async def on_call_tool(self, context, call_next):
            args = getattr(getattr(context, "message", None), "arguments", None) or {}
            token = _GIVEN.set(frozenset(args))
            try:
                return await call_next(context)
            finally:
                _GIVEN.reset(token)

    return GivenArgsMiddleware()
