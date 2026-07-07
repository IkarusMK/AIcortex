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

_APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)
