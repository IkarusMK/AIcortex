"""The per-user endpoint gate must read the parameter the tool really has.

Regression: the map expected ``device``/``endpoint`` for mqtt/ftp while the
tools take ``broker``/``server`` — the lookup always saw "" and per-user device
grants never matched; ``ftp_list`` was not gated at all.
"""
import asyncio
import sys

from fastmcp import Client

# Tools that take a registered endpoint name and therefore must be gated.
MUST_BE_GATED = {"mqtt_publish", "mqtt_get", "ftp_list", "ftp_upload", "ssh_run",
                 "webdav_list", "caldav_list_events", "imap_search", "mail_send",
                 "print_document", "scan_document", "mcp_call", "mcp_tools"}


def _schemas(server):
    async def go():
        async with Client(server.mcp) as client:
            return {t.name: t.input_schema.get("properties", {}) for t in await client.list_tools()}
    return asyncio.run(go())


def test_endpoint_map_matches_tool_parameters(fresh_server):
    authz = sys.modules["authz"]
    schemas = _schemas(fresh_server)
    for tool, (_kind, arg) in authz._ENDPOINT_TOOLS.items():
        assert tool in schemas, f"{tool} is in the map but not registered"
        assert arg in schemas[tool], f"{tool} has no parameter '{arg}'"


def test_every_endpoint_tool_is_gated(fresh_server):
    authz = sys.modules["authz"]
    assert MUST_BE_GATED <= set(authz._ENDPOINT_TOOLS)
