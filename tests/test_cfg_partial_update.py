"""Partial updates of registered services/devices do what the caller said.

Regressions (measured on the NAS, 2026-10-28):
- service_add(auth_scheme="") on an existing service kept "Bearer" → 401;
- an update that did not mention auth_header reset a custom header to
  "Authorization", because the tool's default overwrote the stored value.
"""
import asyncio
import json
import sys

from fastmcp import Client


def _service(server, *calls):
    async def go():
        async with Client(server.mcp) as client:
            for args in calls:
                await client.call_tool("service_add", args)
    asyncio.run(go())
    services = sys.modules["services"]
    return json.loads((services.SERVICES_DIR / "birdhouse.json").read_text(encoding="utf-8"))


BASE = {"name": "birdhouse", "base_url": "http://nas:8100", "category": "Birdhouse"}


def test_explicit_empty_value_is_stored(fresh_server):
    cfg = _service(fresh_server, dict(BASE, token_env="BIRDHOUSE_TOKEN"),
                   {"name": "birdhouse", "base_url": "http://nas:8100", "auth_scheme": ""})
    assert cfg["auth_scheme"] == ""
    assert cfg["token_env"] == "BIRDHOUSE_TOKEN"          # untouched field kept


def test_unmentioned_field_keeps_its_custom_value(fresh_server):
    cfg = _service(fresh_server,
                   dict(BASE, auth_header="X-Birdhouse-Token", auth_scheme=""),
                   {"name": "birdhouse", "base_url": "http://nas:8100", "description": "CAD"})
    assert cfg["auth_header"] == "X-Birdhouse-Token"
    assert cfg["auth_scheme"] == ""
    assert cfg["description"] == "CAD"


def test_flag_can_be_switched_off_again(fresh_server):
    cfg = _service(fresh_server, dict(BASE, write_only=True),
                   {"name": "birdhouse", "base_url": "http://nas:8100", "write_only": False})
    assert cfg["write_only"] is False


def test_new_service_gets_defaults(fresh_server):
    cfg = _service(fresh_server, dict(BASE))
    assert cfg["auth_header"] == "Authorization"
    assert cfg["auth_scheme"] == "Bearer"
