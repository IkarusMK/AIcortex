"""tools/list shows a caller only the tools its role may call.

Regression: the policy gate only ran on tools/call, so every client saw all
admin-only tools (secret_set, service_add, tenancy_set, ...) and then had them
refused — wasted calls and a confusing picture for the model.
"""
import asyncio
import sys

from fastmcp import Client


def _listed(server):
    async def go():
        async with Client(server.mcp) as client:
            return {t.name for t in await client.list_tools()}
    return asyncio.run(go())


def test_non_admin_does_not_see_admin_tools(monkeypatch, request):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    server = request.getfixturevalue("fresh_server")
    authz = sys.modules["authz"]
    monkeypatch.setattr(authz, "_identity", lambda: ("runner", True, {}))   # role "user"

    listed = _listed(server)

    assert not (listed & authz.ADMIN_TOOLS), sorted(listed & authz.ADMIN_TOOLS)
    assert {"memory_write", "skill_search", "call_service"} <= listed


def test_admin_sees_everything(monkeypatch, request):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    server = request.getfixturevalue("fresh_server")
    authz = sys.modules["authz"]
    monkeypatch.setattr(authz, "_identity", lambda: ("boss", False, {}))
    monkeypatch.setattr(authz, "role_for", lambda ident, is_runner, claims=None: "admin")

    assert authz.ADMIN_TOOLS <= _listed(server)
