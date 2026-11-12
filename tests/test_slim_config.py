"""Slim configuration (2.7.1): secure values are built in, not configurable.

Up to 2.7.0 some seventy environment variables tuned limits or switched protections
off (AUTH_ENFORCE=0, ALLOW_INSECURE=1, ALLOW_PLAINTEXT_VAULT=1, OIDC_DEFAULT_ROLE=admin,
HSTS=off, RATE_LIMIT=0 …). Since 2.7.1 the values live in app/settings.py and a real
installation ignores those variables; the start names the ones still set, so they
can be deleted. The test suite keeps tuning them through ONE explicit switch
(AICORTEX_TEST_OVERRIDES=1, set in conftest.py) — without it, nothing changes.
"""
import re
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "app"

WEAKENING = {
    "AUTH_ENFORCE": "0", "ALLOW_PLAINTEXT_VAULT": "1",
    "OIDC_DEFAULT_ROLE": "admin", "RUNNER_ROLE": "admin", "HSTS": "off",
    "RATE_LIMIT": "0", "UI_SESSION_TTL": "999999", "UI_ENABLED": "0", "API_ENABLED": "0",
    "AUDIT_MAX_BYTES": "0", "WEBHOOK_MAX_BODY_BYTES": "999999999",
    "SSH_STRICT_HOST_KEYS": "0", "OPERATOR_BOOTSTRAP_MINUTES": "99999",
}


@pytest.fixture
def production(monkeypatch, request):
    """A freshly imported server as a real installation sees it: no test switch, and
    every removed variable set to its weakest value."""
    monkeypatch.delenv("AICORTEX_TEST_OVERRIDES", raising=False)
    for name, value in WEAKENING.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("BASE_URL", "https://cortex.example.com")
    request.getfixturevalue("fresh_server")
    for name in ("secheaders", "ratelimit", "rest_api", "webhook_tools", "webui"):
        __import__(name)                              # some are imported only on demand
    return sys.modules


def test_variables_that_weakened_security_change_nothing_in_production(production):
    authz, tenancy, settings = production["authz"], production["tenancy"], production["settings"]
    assert authz.enabled() is True and tenancy._enforced() is True
    assert authz._configured_default({}) == ""                  # no default admin
    assert authz.role_for("runner", True) == "user"
    assert production["secheaders"].hsts_value() == settings.HSTS
    assert production["ratelimit"].limits_from_env()             # rate limits stay on
    assert production["webui"].SESSION_TTL == settings.UI_SESSION_TTL
    assert production["webui"]._enabled() and production["rest_api"]._enabled()
    assert production["webhook_tools"]._MAX_BODY == settings.WEBHOOK_MAX_BODY_BYTES
    assert not settings.flag("ALLOW_PLAINTEXT_VAULT", False)


def test_the_test_switch_is_what_lets_the_suite_tune_a_value(monkeypatch):
    import settings
    monkeypatch.setenv("UI_SESSION_TTL", "60")
    monkeypatch.delenv("AICORTEX_TEST_OVERRIDES", raising=False)
    assert settings.knob("UI_SESSION_TTL", 43200, int) == 43200
    monkeypatch.setenv("AICORTEX_TEST_OVERRIDES", "1")
    assert settings.knob("UI_SESSION_TTL", 43200, int) == 60
    monkeypatch.setenv("UI_SESSION_TTL", "not-a-number")
    assert settings.knob("UI_SESSION_TTL", 43200, int) == 43200


def test_leftovers_are_named_in_one_line_and_the_test_switch_is_shouted(capsys):
    import settings
    line = settings.warn_leftovers({"AUTH_ENFORCE": "0", "HSTS": "off", "BASE_URL": "x",
                                    "MEMORY_DIR": "/data/memory"})
    out = capsys.readouterr().out
    assert "AUTH_ENFORCE" in line and "HSTS" in line
    assert "BASE_URL" not in line and "MEMORY_DIR" not in line   # still used / folders
    assert "WARNING" not in out
    settings.warn_leftovers({"AICORTEX_TEST_OVERRIDES": "1"})
    assert "WARNING" in capsys.readouterr().out
    assert settings.warn_leftovers({"BASE_URL": "x"}) == ""


def test_no_module_reads_a_removed_variable_from_the_environment():
    import settings
    removed = set(settings.NO_LONGER_USED)
    read = re.compile(r"""(?:os\.environ(?:\.get)?\(\s*|os\.environ\[|_env_int\(\s*)["']([A-Z][A-Z0-9_]+)["']""")
    found = {}
    for src in APP.glob("*.py"):
        if src.name == "settings.py":
            continue
        for name in read.findall(src.read_text(encoding="utf-8")):
            if name in removed:
                found.setdefault(name, []).append(src.name)
    assert not found, found


def test_data_folders_default_under_data_and_an_explicit_path_still_wins(monkeypatch):
    import settings
    monkeypatch.delenv("MEMORY_DIR", raising=False)
    monkeypatch.delenv("DATA_ROOT", raising=False)
    assert settings.data_dir("MEMORY_DIR", "memory") == Path("/data/memory")
    monkeypatch.setenv("MEMORY_DIR", "/srv/custom/memory")         # an older install
    assert settings.data_dir("MEMORY_DIR", "memory") == Path("/srv/custom/memory")
