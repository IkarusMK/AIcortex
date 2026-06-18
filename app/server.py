"""ClaudeNasConnector — MCP server.

A self-hosted MCP server you add to the Claude apps as a custom connector.
It currently exposes a single ``ping`` tool (walking skeleton); memory tools
and a skill router follow. See the roadmap in README.md.
"""
import os

from fastmcp import FastMCP

MEMORY_DIR = os.environ.get("MEMORY_DIR", "/data/memory")
SKILLS_DIR = os.environ.get("SKILLS_DIR", "/data/skills")
HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "8787"))

mcp = FastMCP("ClaudeNasConnector")


@mcp.tool
def ping(name: str = "world") -> str:
    """Health check — confirms the connector is reachable."""
    return f"Hello {name}, your NAS MCP server is alive! 🎉"


# --- Roadmap: memory_read / memory_write / memory_list ---
# --- Roadmap: skill_search / skill_load / skill_resource ---


if __name__ == "__main__":
    # Streamable-HTTP transport — what Claude custom connectors speak.
    # Endpoint: http://HOST:PORT/mcp
    mcp.run(transport="http", host=HOST, port=PORT)
