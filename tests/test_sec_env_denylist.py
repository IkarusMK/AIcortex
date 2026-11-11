"""The server's settings and the container's runtime variables are never a "secret".

Regression for the re-review finding "env fallback denylist": ``get_secret`` reads
the environment before the vault, and ``is_internal_env`` listed only some of the
server's own settings. This release's settings (OPERATORS, HSTS, RATE_LIMIT_*,
AUDIT_*, FORWARDED_ALLOW_IPS, MCP_REDIRECT_URIS …) and the image's runtime variables
(GPG_KEY …) could be named as an integration's secret and read. Now:

- the server's own setting names are never resolved, from anywhere;
- a name with one of the settings' prefixes (RATE_LIMIT, AUDIT_, HSTS, MCP_,
  FORWARDED_, WEBHOOK_, OPERATOR, UI_, API_, GPG_KEY, PYTHON_) is never read from the
  environment — a vault secret of that name still works;
- other environment-provided service tokens (e.g. HA_TOKEN) keep working.
"""
import json
import sys

import pytest
from cryptography.fernet import Fernet

FROM_ENV = ["GPG_KEY", "FORWARDED_ALLOW_IPS", "HSTS", "AUDIT_MAX_BYTES", "AUDIT_KEEP",
            "RATE_LIMIT", "RATE_LIMIT_AUTH", "OPERATORS", "OPERATOR_BOOTSTRAP_MINUTES",
            "MCP_REDIRECT_URIS", "TOOL_PROFILE_DEFAULT", "UI_THEME", "API_UPSTREAM_KEY",
            "WEBHOOK_SIGNING_KEY", "FORWARDED_PROTO"]


@pytest.fixture
def vault(monkeypatch, fresh_server):
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    return sys.modules["secrets_store"]


@pytest.mark.parametrize("name", FROM_ENV)
def test_settings_and_runtime_variables_are_not_read_from_the_environment(vault, monkeypatch, name):
    monkeypatch.setenv(name, "env-value")
    assert vault.get_secret(name) is None
    vault.vault_set_hosts(name, "", ["api.example.com"])     # bound, where binding is allowed
    assert vault.use(name, "https://api.example.com/x") is None


@pytest.mark.parametrize("name", ["FORWARDED_ALLOW_IPS", "OPERATORS", "HSTS", "AUDIT_MAX_BYTES",
                                  "RATE_LIMIT_HOOKS", "MCP_REDIRECT_URIS", "GPG_KEY",
                                  "OPERATOR_BOOTSTRAP_MINUTES", "TOOL_PROFILE_DEFAULT"])
def test_the_servers_own_settings_are_internal(vault, name):
    assert vault.is_internal_env(name)
    assert vault.vault_set_hosts(name, "", ["api.example.com"]).startswith("Refused")


def test_vault_secrets_of_those_names_and_legacy_env_tokens_still_work(vault, monkeypatch):
    monkeypatch.setenv("API_KEY", "env-value")
    vault.vault_set("API_KEY", "vault-value")
    assert vault.get_secret("API_KEY") == "vault-value"        # the vault, never the env
    entry = [e for e in vault.vault_entries() if e["name"] == "API_KEY"][0]
    assert entry["env_override"] is False
    monkeypatch.setenv("HA_TOKEN", "legacy-env-token")
    assert vault.get_secret("HA_TOKEN") == "legacy-env-token"  # a service token from .env
    folder = sys.modules["services"].SERVICES_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "s.json").write_text(json.dumps({"name": "s", "base_url": "https://api.example.com",
                                                "token_env": "UI_THEME"}))
    monkeypatch.setenv("UI_THEME", "dark")
    assert "UI_THEME" in vault.missing_references()           # the env value doesn't count
