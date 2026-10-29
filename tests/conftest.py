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


@pytest.fixture
def fresh_server(tmp_path, monkeypatch):
    """A freshly imported ``server`` module whose whole data tree lives in tmp_path.

    No OIDC and no RUNNER_TOKEN, so the in-memory client connects as the local
    operator. Tests that need other auth settings set them before requesting this.
    """
    for var in _DATA_ENV:
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("CATALOG_CACHE_FILE", str(tmp_path / "catalog_cache.json"))
    for var in ("OIDC_CONFIG_URL", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    _purge_app_modules()
    import server
    yield server
    _purge_app_modules()
