"""With sign-in configured, a broken policy check denies — it never waves a call through.

Regression for the re-review finding "authz middleware still fails open":
``AuthzMiddleware.on_call_tool`` allowed the call on ANY exception that was not a
policy denial (only leaving an audit line), the memory-scope confinement swallowed
its exceptions (a non-admin's memory call then ran unconfined), ``enforce_rest``
allowed on errors too, and ``on_list_tools`` showed the full list. Whoever can make
the identity, policy or area lookup throw got an unchecked call.

Now, with enforcement on AND sign-in configured (``authz.auth_configured``), each of
these fails CLOSED: the call is denied (and audited), the REST call is refused, the
tool list is empty. Only the local mode without sign-in keeps degrading open, as
before — it has no identities to check anyway.
"""
import asyncio
import json
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def az(monkeypatch, fresh_server):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    return sys.modules["authz"]


def _sign_in_configured(monkeypatch):
    monkeypatch.setenv("OIDC_CONFIG_URL", "https://id.example.com/.well-known/openid-configuration")
    monkeypatch.setenv("OIDC_CLIENT_ID", "aicortex")


def _boom(*_a, **_k):
    raise RuntimeError("lookup broke")


def _call_tool(az, tool, args=None):
    """Run the middleware's on_call_tool; returns (ran, args seen by the tool)."""
    mw = az.build_middleware()
    seen = {}

    async def call_next(ctx):
        seen.update(ctx.message.arguments)
        return "ran"
    ctx = SimpleNamespace(message=SimpleNamespace(name=tool, arguments=dict(args or {})))
    return asyncio.run(mw.on_call_tool(ctx, call_next)) == "ran", seen


def _audit_lines(az):
    if not az.AUDIT_FILE.exists():
        return []
    return [json.loads(ln) for ln in az.AUDIT_FILE.read_text(encoding="utf-8").splitlines()]


def test_an_identity_error_denies_the_call_once_sign_in_is_configured(az, monkeypatch):
    from fastmcp.exceptions import ToolError
    monkeypatch.setattr(az, "effective_identity", _boom)
    assert _call_tool(az, "memory_read")[0]                     # local mode: as before
    _sign_in_configured(monkeypatch)
    with pytest.raises(ToolError, match="authorization check failed"):
        _call_tool(az, "memory_read")
    last = _audit_lines(az)[-1]
    assert (last["tool"], last["decision"]) == ("memory_read", "deny")


def test_a_broken_memory_confinement_denies_instead_of_running_unconfined(az, monkeypatch):
    from fastmcp.exceptions import ToolError
    monkeypatch.setattr(az, "effective_identity", lambda: ("alice", "user"))
    monkeypatch.setattr(sys.modules["tenancy"], "confine_memory_scope", _boom)
    _sign_in_configured(monkeypatch)
    with pytest.raises(ToolError):
        _call_tool(az, "memory_read", {"scope": "shared"})
    assert _audit_lines(az)[-1]["decision"] == "deny"


def test_rest_scoping_errors_refuse_the_call_once_sign_in_is_configured(az, monkeypatch):
    monkeypatch.setattr(sys.modules["tenancy"], "endpoint_allowed", _boom)
    args = {"endpoint": "home"}
    assert az.enforce_rest("alice", "user", "caldav_list_events", dict(args))[0] is True
    _sign_in_configured(monkeypatch)
    ok, reason = az.enforce_rest("alice", "user", "caldav_list_events", dict(args))
    assert ok is False and "authorization check failed" in reason
    assert _audit_lines(az)[-1]["decision"] == "deny"


def test_a_broken_list_filter_shows_nothing_once_sign_in_is_configured(az, monkeypatch):
    mw = az.build_middleware()
    tools = [SimpleNamespace(name="ping"), SimpleNamespace(name="secret_set")]

    async def call_next(_ctx):
        return list(tools)
    monkeypatch.setattr(az, "effective_identity", _boom)
    assert asyncio.run(mw.on_list_tools(None, call_next)) == tools   # local mode: as before
    _sign_in_configured(monkeypatch)
    assert asyncio.run(mw.on_list_tools(None, call_next)) == []
