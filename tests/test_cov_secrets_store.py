"""Encrypted vault (app/secrets_store.py): refusals, per-user namespaces, rekey errors.

Proves the paths that protect the stored secrets: a vault that cannot be read is never
overwritten (set, delete and rekey all refuse and leave the bytes untouched); storing
without a key is refused; values never reach a message or the file in clear; the
server's own settings and other people's storage paths are never resolved as secrets;
a person's own secret wins over the shared one but is invisible to everyone else;
listings show names only, confined per role; broken metadata / configs / backups
degrade instead of failing.
"""
import json
import sys
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, fn):
        self.tools[fn.__name__] = fn
        return fn


@pytest.fixture
def vault(monkeypatch, fresh_server):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    for var in ("STORAGE_ENCRYPTION_KEY_OLD", "ALLOW_PLAINTEXT_VAULT", "HA_TOKEN", "NAS_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    mod = sys.modules["secrets_store"]
    fake = FakeMCP()
    mod.register(fake)
    return SimpleNamespace(mod=mod, t=fake.tools, authz=sys.modules["authz"],
                           tenancy=sys.modules["tenancy"], boot=sys.modules["bootstrap"])


def _boom(*_a, **_k):
    raise RuntimeError("lookup broke")


def _wrong_key(monkeypatch):
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())


# ── storing ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", ["", "   ", "a/b", "users/x/TOKEN"])
def test_secret_names_must_be_plain(vault, name):
    assert vault.mod.vault_set(name, "test-token").startswith("Refused: secret name")
    assert not vault.mod.VAULT_FILE.exists()


def test_without_a_key_nothing_is_stored_unless_plaintext_is_allowed(vault, monkeypatch):
    monkeypatch.delenv("STORAGE_ENCRYPTION_KEY")
    assert vault.mod.vault_set("HA_TOKEN", "test-token").startswith("Refusing to store")
    assert not vault.mod.VAULT_FILE.exists()
    monkeypatch.setenv("ALLOW_PLAINTEXT_VAULT", "1")
    assert vault.mod.vault_set("HA_TOKEN", "test-token").startswith("Stored 'HA_TOKEN'")
    assert vault.mod.vault_status() == {"state": "plaintext", "key": False, "rotating": False}


def test_stored_values_are_encrypted_and_never_echoed(vault):
    text = vault.mod.vault_set("HA_TOKEN", "test-token", description=" Home Assistant ", by="alice")
    assert "test-token" not in text and "shared secret" in text
    assert b"test-token" not in vault.mod.VAULT_FILE.read_bytes()
    assert vault.mod.get_secret("HA_TOKEN") == "test-token"
    meta = json.loads(vault.mod.VAULT_META.read_text())["HA_TOKEN"]
    assert meta["description"] == "Home Assistant" and meta["updated_by"] == "alice"
    vault.mod.vault_set("HA_TOKEN", "newer-token")                       # re-set keeps the note
    assert json.loads(vault.mod.VAULT_META.read_text())["HA_TOKEN"]["description"] == "Home Assistant"


def test_an_unreadable_vault_is_never_overwritten_or_shortened(vault, monkeypatch):
    vault.mod.vault_set("HA_TOKEN", "test-token")
    before = vault.mod.VAULT_FILE.read_bytes()
    _wrong_key(monkeypatch)
    assert "Refusing to write" in vault.mod.vault_set("NAS_TOKEN", "other-token")
    assert "Refusing to modify" in vault.mod.vault_delete("HA_TOKEN")
    assert vault.mod.vault_rekey().startswith("Refused: the vault can't be read")
    assert vault.mod.VAULT_FILE.read_bytes() == before
    assert vault.mod.get_secret("HA_TOKEN") is None                     # best-effort read: nothing


def test_a_vault_holding_json_that_is_not_an_object_is_unreadable(vault):
    vault.mod.VAULT_DIR.mkdir(parents=True, exist_ok=True)   # meta.json may live there already
    vault.mod.VAULT_FILE.write_text("[1, 2, 3]")
    assert vault.mod.vault_status()["state"] == "unreadable"
    assert vault.mod._read_all() == {}
    with pytest.raises(vault.mod.VaultUnreadable, match="not a JSON object"):
        vault.mod._read_all(strict=True)
    vault.mod.VAULT_FILE.write_bytes(b"\x00garbage")
    assert vault.mod.vault_status()["state"] == "unreadable"


def test_a_missing_vault_reports_empty_with_the_key_flags(vault, monkeypatch):
    assert vault.mod.vault_status() == {"state": "empty", "key": True, "rotating": False}
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY_OLD", Fernet.generate_key().decode())
    assert vault.mod.vault_status()["rotating"] is True
    assert vault.mod._read_all() == {}


def test_a_failing_backup_copy_does_not_block_the_write(vault, monkeypatch):
    vault.mod.vault_set("HA_TOKEN", "test-token")
    monkeypatch.setattr(vault.mod.shutil, "copy2", _boom)
    assert vault.mod.vault_set("NAS_TOKEN", "other-token").startswith("Stored")
    assert vault.mod.get_secret("NAS_TOKEN") == "other-token"


def test_each_write_keeps_the_previous_generation_as_backup(vault):
    vault.mod.vault_set("HA_TOKEN", "first-token")
    vault.mod.vault_set("NAS_TOKEN", "second-token")
    backup = json.loads(Fernet(vault.mod.os.environ["STORAGE_ENCRYPTION_KEY"].encode())
                        .decrypt(vault.mod.VAULT_BAK.read_bytes()))
    assert backup == {"HA_TOKEN": "first-token"}


# ── rekey ────────────────────────────────────────────────────────────────────
def test_rekey_needs_a_key(vault, monkeypatch):
    monkeypatch.delenv("STORAGE_ENCRYPTION_KEY")
    assert vault.mod.vault_rekey() == "Refused: set STORAGE_ENCRYPTION_KEY first."


def test_rekey_reencrypts_vault_and_backup_with_the_new_key(vault, monkeypatch):
    old = vault.mod.os.environ["STORAGE_ENCRYPTION_KEY"]
    vault.mod.vault_set("HA_TOKEN", "test-token")
    vault.mod.vault_set("NAS_TOKEN", "other-token")
    new = Fernet.generate_key().decode()
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", new)
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY_OLD", old)
    assert vault.mod.vault_status()["rotating"] is True
    assert vault.mod.vault_rekey() == "Re-encrypted 2 secret(s) with the current key."
    monkeypatch.delenv("STORAGE_ENCRYPTION_KEY_OLD")
    for blob in (vault.mod.VAULT_FILE.read_bytes(), vault.mod.VAULT_BAK.read_bytes()):
        assert json.loads(Fernet(new.encode()).decrypt(blob))["HA_TOKEN"] == "test-token"


# ── reading ──────────────────────────────────────────────────────────────────
def test_environment_wins_over_the_vault_and_unknown_names_are_none(vault, monkeypatch):
    vault.mod.vault_set("HA_TOKEN", "vault-token")
    assert vault.mod.get_secret("HA_TOKEN") == "vault-token"
    monkeypatch.setenv("HA_TOKEN", "env-token")
    assert vault.mod.get_secret("HA_TOKEN") == "env-token"
    assert vault.mod.get_secret("NOPE") is None and vault.mod.get_secret("") is None


@pytest.mark.parametrize("name", ["STORAGE_ENCRYPTION_KEY", "STORAGE_ENCRYPTION_KEY_OLD",
                                  "JWT_SIGNING_KEY", "RUNNER_TOKEN", "OIDC_CLIENT_SECRET",
                                  "AUTH_STORE_DIR", "VAULT_DIR", "PATH", "FASTMCP_X", "PYTHONPATH"])
def test_the_servers_own_settings_are_never_handed_out_as_secrets(vault, monkeypatch, name):
    monkeypatch.setenv(name, "server-internal")
    assert vault.mod.is_internal_env(name)
    assert vault.mod.get_secret(name) is None
    monkeypatch.setenv("OTHER_TOKEN", "fine")
    assert not vault.mod.is_internal_env("OTHER_TOKEN") and vault.mod.get_secret("OTHER_TOKEN") == "fine"


def test_a_persons_own_secret_wins_and_is_invisible_to_others(vault):
    vault.mod.vault_set("HA_TOKEN", "shared-token")
    vault.mod.vault_set("HA_TOKEN", "alice-token", owner="alice")
    with vault.authz.rest_identity("alice", "user"):
        assert vault.mod.get_secret("HA_TOKEN") == "alice-token"
        assert vault.mod.get_own_secret("HA_TOKEN") == "alice-token"
    with vault.authz.rest_identity("bob", "user"):
        assert vault.mod.get_secret("HA_TOKEN") == "shared-token"
        assert vault.mod.get_own_secret("HA_TOKEN") is None
    with vault.authz.rest_identity("boss", "admin"):
        assert vault.mod.get_secret("HA_TOKEN") == "shared-token"
        assert vault.mod.get_own_secret("HA_TOKEN") is None              # admins have no namespace
    with vault.authz.rest_identity("alice", "user"):
        stored_path = f"{vault.tenancy.vault_namespace('alice', 'user')}/HA_TOKEN"
        assert vault.mod.get_secret(stored_path) is None                  # storage paths never resolve
        assert vault.mod.get_own_secret(stored_path) is None and vault.mod.get_own_secret("") is None


def test_namespace_lookup_failure_falls_back_to_shared_only(vault, monkeypatch):
    vault.mod.vault_set("HA_TOKEN", "shared-token")
    vault.mod.vault_set("HA_TOKEN", "alice-token", owner="alice")
    monkeypatch.setattr(vault.tenancy, "current_identity", _boom)
    assert vault.mod._caller_namespace() == ""
    assert vault.mod.get_secret("HA_TOKEN") == "shared-token"
    assert vault.mod.get_own_secret("HA_TOKEN") is None


# ── deleting ─────────────────────────────────────────────────────────────────
def test_delete_removes_shared_and_personal_secrets_and_their_notes(vault):
    vault.mod.vault_set("HA_TOKEN", "shared-token", description="shared")
    vault.mod.vault_set("HA_TOKEN", "alice-token", owner="alice", description="mine")
    assert vault.mod.vault_delete("HA_TOKEN", owner="alice") == "Deleted secret 'HA_TOKEN' from alice's vault."
    assert vault.mod.vault_delete("HA_TOKEN", owner="alice") == "No secret named 'HA_TOKEN' for alice."
    assert vault.mod.vault_delete(" HA_TOKEN ") == "Deleted secret 'HA_TOKEN'."
    assert vault.mod.vault_delete("HA_TOKEN") == "No secret named 'HA_TOKEN'."
    meta = json.loads(vault.mod.VAULT_META.read_text())
    assert {k: v for k, v in meta.items() if not k.startswith("_/")} == {}   # bookkeeping aside


def test_delete_accepts_the_namespace_segment_as_the_listing_shows_it(vault):
    vault.mod.vault_set("HA_TOKEN", "alice-token", owner="alice")
    (entry,) = vault.mod.vault_entries()
    assert vault.t["secret_delete"]("HA_TOKEN", entry["owner"]).startswith("Deleted secret")
    assert vault.mod.vault_entries() == []


# ── listings ─────────────────────────────────────────────────────────────────
def test_entries_carry_ownership_usage_and_env_override_but_no_values(vault, monkeypatch):
    vault.mod.vault_set("HA_TOKEN", "shared-token", by="boss")
    vault.mod.vault_set("NAS_TOKEN", "alice-token", owner="alice@example.com")
    vault.boot.SERVICES_DIR.mkdir(parents=True, exist_ok=True)
    (vault.boot.SERVICES_DIR / "ha.json").write_text(json.dumps({"token_env": " HA_TOKEN "}))
    monkeypatch.setenv("HA_TOKEN", "env-token")
    shared, personal = vault.mod.vault_entries()
    assert (shared["name"], shared["owner"], shared["used_by"]) == ("HA_TOKEN", "", ["service ha"])
    assert shared["env_override"] is True and shared["updated_by"] == "boss"
    assert personal["owner_label"] == "alice_example_com" and personal["used_by"] == []
    assert "token" not in json.dumps(vault.mod.vault_entries()).replace("HA_TOKEN", "").replace("NAS_TOKEN", "")


def test_references_skip_broken_configs_and_missing_ones_are_reported(vault, monkeypatch):
    for kind_dir, name, content in ((vault.boot.SERVICES_DIR, "a.json", '{"token_env": "HA_TOKEN"}'),
                                    (vault.boot.MQTT_DIR, "b.json", '{"password_env": "NAS_TOKEN"}'),
                                    (vault.boot.SSH_DIR, "c.json", "{broken"),
                                    (vault.boot.MAIL_DIR, "d.json", '["not", "an", "object"]'),
                                    (vault.boot.FTP_DIR, "e.json", '{"password_env": "  "}')):
        kind_dir.mkdir(parents=True, exist_ok=True)
        (kind_dir / name).write_text(content)
    assert vault.mod.secret_references() == {"HA_TOKEN": ["service a"], "NAS_TOKEN": ["mqtt b"]}
    assert vault.mod.missing_references() == ["HA_TOKEN", "NAS_TOKEN"]
    vault.mod.vault_set("HA_TOKEN", "alice-token", owner="alice")        # a personal copy counts
    monkeypatch.setenv("NAS_TOKEN", "env-token")                         # so does the environment
    assert vault.mod.missing_references() == []


def test_corrupt_metadata_degrades_to_no_notes_and_blocks_writes(vault):
    vault.mod.vault_set("HA_TOKEN", "test-token", description="note")
    vault.mod.VAULT_META.write_text("{corrupt")
    assert vault.mod.vault_entries()[0]["description"] == ""
    # A write would start meta.json over — and unbind every secret from its hosts.
    assert vault.mod.vault_set("HA_TOKEN", "newer-token", description="fresh").startswith("Refusing")
    assert vault.mod.VAULT_META.read_text() == "{corrupt"


# ── the tools ────────────────────────────────────────────────────────────────
def test_secret_set_attributes_the_caller_and_survives_a_label_failure(vault, monkeypatch):
    with vault.authz.rest_identity("boss", "admin"):
        text = vault.t["secret_set"]("HA_TOKEN", "test-token", owner="alice", description="d")
    assert "alice's private vault" in text and "test-token" not in text
    (meta,) = (v for k, v in json.loads(vault.mod.VAULT_META.read_text()).items()
               if not k.startswith("_/"))                                   # bookkeeping aside
    assert meta["updated_by"] == "boss"
    monkeypatch.setattr(vault.authz, "caller_label", _boom)
    assert vault.t["secret_set"]("NAS_TOKEN", "other-token").startswith("Stored 'NAS_TOKEN'")
    assert json.loads(vault.mod.VAULT_META.read_text())["NAS_TOKEN"]["updated_by"] == ""


def test_secret_list_shows_names_only_confined_by_role(vault, monkeypatch):
    assert vault.t["secret_list"]() == "No secrets stored yet."
    vault.mod.vault_set("HA_TOKEN", "shared-token")
    vault.mod.vault_set("NAS_TOKEN", "alice-token", owner="alice")
    vault.mod.vault_set("NAS_TOKEN", "bob-token", owner="bob")
    with vault.authz.rest_identity("boss", "admin"):
        everything = vault.t["secret_list"]()
    assert everything.count("\n") == 2 and "(shared)" in everything and "(user: alice-" in everything
    with vault.authz.rest_identity("alice", "user"):
        mine = vault.t["secret_list"]()
    assert mine.splitlines() == ["- HA_TOKEN  (shared)", "- NAS_TOKEN  (yours)"]
    with monkeypatch.context() as m:
        m.setattr(vault.authz, "effective_identity", _boom)              # unresolved caller → shared only
        assert vault.t["secret_list"]() == "- HA_TOKEN  (shared)"
    monkeypatch.setenv("AUTH_ENFORCE", "0")                              # homelab: one shared brain
    assert vault.t["secret_list"]().count("\n") == 2
    assert vault.mod._fmt_key("users/odd", "") == "- users/odd  (shared)"
    assert "test-token" not in everything
