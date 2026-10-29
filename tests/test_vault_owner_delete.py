"""A personal secret can be deleted with the owner value the vault lists.

Regression: listings (WebUI and secret_list) show the stored namespace segment
``<slug>-<hash>``; deleting with that value hashed it a second time, never found
the key and answered "No secret named ..." — personal secrets were undeletable.
"""
import sys

from cryptography.fernet import Fernet


def test_delete_personal_secret_with_listed_owner(monkeypatch, request):
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    request.getfixturevalue("fresh_server")
    vault = sys.modules["secrets_store"]

    vault.vault_set("HA_TOKEN", "abc", owner="alice@example.com")
    listed = [e for e in vault.vault_entries() if e["name"] == "HA_TOKEN"]
    assert len(listed) == 1

    msg = vault.vault_delete("HA_TOKEN", listed[0]["owner"])

    assert msg.startswith("Deleted"), msg
    assert not [e for e in vault.vault_entries() if e["name"] == "HA_TOKEN"]


def test_delete_personal_secret_with_raw_identity_still_works(monkeypatch, request):
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    request.getfixturevalue("fresh_server")
    vault = sys.modules["secrets_store"]

    vault.vault_set("HA_TOKEN", "abc", owner="alice@example.com")
    assert vault.vault_delete("HA_TOKEN", "alice@example.com").startswith("Deleted")
