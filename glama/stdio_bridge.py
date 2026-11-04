"""Let Glama (glama.ai) introspect AICortex.

Glama builds a server from this repository, starts it in a sandbox and asks it over
stdio for its tools (`mcp-proxy -- <command>`). AICortex speaks Streamable HTTP and
wants a login, so this bridge starts it on loopback in open mode — with throw-away
data in `.glama-data/` under the working directory, never `/data` — and answers on
stdio as a proxy to it. It is not a way to run AICortex: nothing here is in the image.

Glama build form (Admin → Dockerfile):
  Build steps:    ["uv venv .venv", "uv pip install --python .venv/bin/python -r app/requirements.txt"]
  CMD arguments:  [".venv/bin/python", "glama/stdio_bridge.py"]
  Environment:    none
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from fastmcp.server.server import create_proxy

APP = Path(__file__).resolve().parent.parent / "app"
START_TIMEOUT = 60  # seconds for AICortex to open its port

# Every data folder AICortex knows (each defaults to a path under /data);
# tests/test_glama_bridge.py fails when the app gains one that is missing here.
DATA_DIRS = ("APIKEY_DIR", "AUTH_STORE_DIR", "CALDAV_DIR", "COORD_DIR", "CRON_DIR",
             "FTP_DIR", "IMAP_DIR", "MAIL_DIR", "MCP_DIR", "MEMORY_DIR", "MQTT_DIR",
             "PLAYBOOKS_DIR", "PRINT_DIR", "SCAN_DIR", "SERVICES_DIR", "SESSIONS_DIR",
             "SKILLS_DIR", "SSH_DIR", "VAULT_DIR", "WEBDAV_DIR", "WEBHOOK_DIR", "WORK_DIR")
# Settings that would switch on a login or a runner are never passed on.
DROPPED_PREFIXES = ("OIDC_", "RUNNER_", "AUTH_")


def server_env(data: Path, port: int, base: dict[str, str]) -> dict[str, str]:
    """The environment for AICortex: no login, loopback only, all data under `data`."""
    env = {k: v for k, v in base.items() if not k.startswith(DROPPED_PREFIXES)}
    env.update({name: str(data / name.lower()) for name in DATA_DIRS})
    env.update({"DATA_ROOT": str(data), "CATALOG_CACHE_FILE": str(data / "catalog.json"),
                "ALLOW_INSECURE": "1", "MCP_HOST": "127.0.0.1", "MCP_PORT": str(port),
                "PYTHONUNBUFFERED": "1"})
    return env


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for(server: subprocess.Popen, port: int) -> None:
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        if server.poll() is not None:
            sys.exit(f"AICortex stopped while starting (exit code {server.returncode})")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            time.sleep(0.3)
    server.terminate()
    sys.exit(f"AICortex did not open port {port} within {START_TIMEOUT} s")


def main() -> None:
    port = _free_port()
    env = server_env(Path.cwd() / ".glama-data", port, dict(os.environ))
    # stdout belongs to the MCP stream: the server's own output goes to stderr.
    server = subprocess.Popen([sys.executable, "server.py"], cwd=APP, env=env,
                              stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr)
    try:
        _wait_for(server, port)
        create_proxy(f"http://127.0.0.1:{port}/mcp", name="AICortex").run(
            transport="stdio", show_banner=False)
    finally:
        server.terminate()


if __name__ == "__main__":
    main()
