"""Shared pytest bootstrap.

Puts the ``app/`` package dir on ``sys.path`` so ``import <module>`` resolves in
any checkout and in CI — computed relative to this file, never hardcoded.

Isolation note: several app modules (``tenancy``, ``authz``, ``apikeys``) compute
path constants (``POLICY_FILE``, ``APIKEY_DIR``) at import time from env vars, and
some are shared across test modules. Each test therefore sets its own env via the
``monkeypatch`` fixture (auto-restored) and ``importlib.reload``s the modules it
uses, so tests are independent of collection/run order.
"""
import os
import sys

import pytest

_APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

# Every data path an app module reads from the environment at import time.
_DATA_ENV = (
    "APIKEY_DIR", "AUTH_STORE_DIR", "CALDAV_DIR", "COORD_DIR", "CRON_DIR", "FTP_DIR",
    "IMAP_DIR", "MAIL_DIR", "MCP_DIR", "MEMORY_DIR", "MQTT_DIR", "PRINT_DIR",
    "SCAN_DIR", "SERVICES_DIR", "SESSIONS_DIR", "SKILLS_DIR", "SSH_DIR", "VAULT_DIR",
    "WEBDAV_DIR", "WEBHOOK_DIR", "WORK_DIR",
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
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "test HTTP server did not start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)
