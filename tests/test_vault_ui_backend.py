"""The vault is readable and operable for an admin: state, context, rotation.

Before: an unreadable vault looked empty, a secret was a bare name (no note, no
who/when, no idea which service uses it), and the key could not be rotated.
"""
import json
import sys

from cryptography.fernet import Fernet


def _vault(monkeypatch, request, key=None):
    if key:
        monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", key)
    request.getfixturevalue("fresh_server")
    return sys.modules["secrets_store"]


def test_status_tells_encrypted_from_unreadable(monkeypatch, request):
    key = Fernet.generate_key().decode()
    vault = _vault(monkeypatch, request, key)
    assert vault.vault_status()["state"] == "empty"
    vault.vault_set("HA_TOKEN", "abc")
    assert vault.vault_status()["state"] == "encrypted"
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())   # wrong key
    assert vault.vault_status()["state"] == "unreadable"


def test_entries_carry_description_author_and_usage(monkeypatch, request):
    vault = _vault(monkeypatch, request, Fernet.generate_key().decode())
    services = sys.modules["services"]
    services.SERVICES_DIR.mkdir(parents=True, exist_ok=True)
    (services.SERVICES_DIR / "home-assistant.json").write_text(json.dumps(
        {"name": "home-assistant", "base_url": "http://ha", "token_env": "HA_TOKEN"}), encoding="utf-8")
    (services.SERVICES_DIR / "paperless.json").write_text(json.dumps(
        {"name": "paperless", "base_url": "http://pl", "token_env": "PAPERLESS_TOKEN"}), encoding="utf-8")

    vault.vault_set("HA_TOKEN", "abc", description="Long-lived HA token", by="dana")
    entry = [e for e in vault.vault_entries() if e["name"] == "HA_TOKEN"][0]

    assert entry["description"] == "Long-lived HA token"
    assert entry["updated_by"] == "dana" and entry["updated"]
    assert entry["used_by"] == ["service home-assistant"]
    assert "PAPERLESS_TOKEN" in vault.missing_references()     # referenced, never stored


def test_personal_owner_has_a_readable_label(monkeypatch, request):
    vault = _vault(monkeypatch, request, Fernet.generate_key().decode())
    vault.vault_set("HA_TOKEN", "abc", owner="alice@example.com")
    entry = [e for e in vault.vault_entries() if e["name"] == "HA_TOKEN"][0]
    assert entry["owner_label"] == "alice_example_com"


def test_key_rotation_with_old_key(monkeypatch, request):
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    vault = _vault(monkeypatch, request, old)
    vault.vault_set("HA_TOKEN", "abc")

    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", new)
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY_OLD", old)
    assert vault.get_secret("HA_TOKEN") == "abc"               # readable during rotation
    assert vault.vault_rekey().startswith("Re-encrypted")

    monkeypatch.delenv("STORAGE_ENCRYPTION_KEY_OLD")
    assert vault.get_secret("HA_TOKEN") == "abc"               # now only the new key needed


def test_plaintext_vault_can_be_encrypted(monkeypatch, request):
    monkeypatch.setenv("ALLOW_PLAINTEXT_VAULT", "1")
    vault = _vault(monkeypatch, request)
    vault.vault_set("HA_TOKEN", "abc")
    assert vault.vault_status()["state"] == "plaintext"

    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    assert vault.get_secret("HA_TOKEN") == "abc"
    vault.vault_rekey()
    assert vault.vault_status()["state"] == "encrypted"
    assert b"abc" not in vault.VAULT_FILE.read_bytes()


def test_webui_secrets_endpoint_reports_status_and_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("SERVICES_DIR", str(tmp_path / "services"))
    monkeypatch.delenv("OIDC_CONFIG_URL", raising=False)
    monkeypatch.delenv("OIDC_CLIENT_ID", raising=False)
    import test_webui as tw
    webui, mcp = tw._webui(monkeypatch, tmp_path)
    status, body = tw._call(mcp, "/ui/api/secrets")
    assert status == 200
    assert body["status"]["state"] == "empty"
    assert body["missing"] == []
