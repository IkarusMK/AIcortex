"""Glama (glama.ai) can build AICortex and list its tools.

Glama starts a server from the repository in a sandbox and asks it over stdio for its
tools; the awesome-mcp-servers list only takes servers that pass this. AICortex speaks
HTTP behind a login, so glama/stdio_bridge.py runs it on loopback in open mode and
answers on stdio. Its data must stay under the working directory — Glama's sandbox
flags writes elsewhere — and no login or runner setting may reach it.
"""
import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "glama"))

import stdio_bridge  # noqa: E402


def _app_data_settings() -> set[str]:
    """Every setting in the app whose default is a path under /data."""
    pattern = re.compile(r'environ\.get\("([A-Z_]+)",\s*"/data[^"]*"\)')
    found = set()
    for source in (ROOT / "app").glob("*.py"):
        found |= set(pattern.findall(source.read_text(encoding="utf-8")))
    return found


def test_glama_json_names_the_maintainer():
    data = json.loads((ROOT / "glama.json").read_text(encoding="utf-8"))
    assert data == {"$schema": "https://glama.ai/mcp/schemas/server.json", "maintainers": ["IkarusMK"]}


def test_every_data_folder_of_the_app_is_redirected(tmp_path):
    env = stdio_bridge.server_env(tmp_path, 9999, {})
    missing = _app_data_settings() - set(env)
    assert not missing, f"add to DATA_DIRS in glama/stdio_bridge.py: {sorted(missing)}"
    for name in _app_data_settings():
        assert Path(env[name]).is_relative_to(tmp_path), name


def test_no_login_or_runner_setting_reaches_the_server(tmp_path):
    base = {"OIDC_CONFIG_URL": "https://id.example.com", "OIDC_CLIENT_ID": "x",
            "RUNNER_TOKEN": "t", "AUTH_ENFORCE": "1", "PATH": "/usr/bin"}
    env = stdio_bridge.server_env(tmp_path, 9999, base)
    assert not {"OIDC_CONFIG_URL", "OIDC_CLIENT_ID", "RUNNER_TOKEN", "AUTH_ENFORCE"} & set(env)
    assert env["PATH"] == "/usr/bin"
    assert env["MCP_HOST"] == "127.0.0.1" and env["MCP_PORT"] == "9999"
    assert env["ALLOW_INSECURE"] == "1"


def test_the_bridge_answers_tool_listing_over_stdio(tmp_path):
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    async def listing():
        transport = StdioTransport(command=sys.executable,
                                   args=[str(ROOT / "glama" / "stdio_bridge.py")], cwd=str(tmp_path))
        async with Client(transport, timeout=90) as client:
            return ([t.name for t in await client.list_tools()],
                    await client.list_resources(), await client.list_prompts())

    tools, resources, prompts = asyncio.run(listing())
    assert "cortex_start" in tools and "memory_write" in tools
    assert len(tools) == 11 and resources == [] and prompts == []
    assert {p.name for p in tmp_path.iterdir()} <= {".glama-data"}
