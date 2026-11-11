"""Shared pytest bootstrap.

Puts the ``app/`` package dir on ``sys.path`` so ``import <module>`` resolves in
any checkout and in CI — computed relative to this file, never hardcoded.

Isolation note: several app modules (``tenancy``, ``authz``, ``apikeys``) compute
path constants (``POLICY_FILE``, ``APIKEY_DIR``) at import time from env vars, and
some are shared across test modules. Each test therefore sets its own env via the
``monkeypatch`` fixture (auto-restored) and ``importlib.reload``s the modules it
uses, so tests are independent of collection/run order.
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

_APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

# Many tests hit /token, /register, /ui/login and /hooks/* from 127.0.0.1 in quick
# succession; the per-IP rate limiter (ratelimit.py, on by default in production)
# would turn them into 429s. tests/test_rate_limit.py switches it back on.
os.environ["RATE_LIMIT"] = "0"

# Every data path an app module reads from the environment at import time.
_DATA_ENV = (
    "APIKEY_DIR", "AUTH_STORE_DIR", "CALDAV_DIR", "COORD_DIR", "CRON_DIR", "FTP_DIR",
    "IMAP_DIR", "MAIL_DIR", "MCP_DIR", "MEMORY_DIR", "MQTT_DIR", "PRINT_DIR",
    "SCAN_DIR", "SERVICES_DIR", "SESSIONS_DIR", "SKILLS_DIR", "SSH_DIR", "VAULT_DIR",
    "WEBDAV_DIR", "WEBHOOK_DIR", "WORK_DIR", "PLAYBOOKS_DIR",
)


def _purge_app_modules():
    """Drop every module loaded from app/ so the next import re-reads the env."""
    app_dir = os.path.realpath(_APP)
    for name, mod in list(sys.modules.items()):
        path = getattr(mod, "__file__", None) or ""
        if path and os.path.realpath(path).startswith(app_dir + os.sep):
            del sys.modules[name]


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "oidc: keep the OIDC_* settings the test sets (fresh_server clears them otherwise)")


@pytest.fixture
def fresh_server(tmp_path, monkeypatch, request):
    """A freshly imported ``server`` module whose whole data tree lives in tmp_path.

    OIDC is cleared unless the test is marked ``@pytest.mark.oidc``, so by default
    the in-memory client connects as the local operator. Tests that need other
    auth settings set them before requesting this.
    """
    for var in _DATA_ENV:
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("CATALOG_CACHE_FILE", str(tmp_path / "catalog_cache.json"))
    if "TOOL_PROFILE_DEFAULT" not in os.environ:
        # Most tests exercise individual tools directly; the core profile has its
        # own tests (test_tool_profiles.py) and sets this itself.
        monkeypatch.setenv("TOOL_PROFILE_DEFAULT", "full")
    if request.node.get_closest_marker("oidc") is None:
        for var in ("OIDC_CONFIG_URL", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET"):
            monkeypatch.delenv(var, raising=False)
    _purge_app_modules()
    import server
    yield server
    _purge_app_modules()


@pytest.fixture
def http_server(fresh_server):
    """``fresh_server`` served over real HTTP on a free local port.

    Yields the base URL (no trailing slash). Needed wherever transport-level
    behaviour matters: bearer tokens, 401 challenges, host checks.
    """
    import socket
    import threading
    import time

    import uvicorn

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = fresh_server.mcp.http_app(path="/mcp", middleware=fresh_server.http_middleware())
    # The uvicorn settings server.py runs with (e.g. no "server" banner), when it has any.
    extra = getattr(fresh_server, "uvicorn_config", dict)()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           **extra))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "test HTTP server did not start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def fake_idp():
    """A minimal OIDC provider: just the discovery document and an empty JWKS."""
    server = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    docs = {
        "/.well-known/openid-configuration": {
            "issuer": base,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "jwks_uri": f"{base}/jwks",
            "response_types_supported": ["code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
        },
        "/jwks": {"keys": []},
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(docs.get(self.path, {})).encode()
            self.send_response(200 if self.path in docs else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server.RequestHandlerClass = Handler
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield base
    server.shutdown()
