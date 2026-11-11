"""The OAuth store's files are owner-only, and a missing encryption key is called out.

Regression for the re-review finding: without STORAGE_ENCRYPTION_KEY the OAuth proxy's
store (AUTH_STORE_DIR/oauth) keeps upstream tokens, refresh-token metadata and client
registrations as plain JSON, and its folders were created world-readable (0755) — any
other account on the host could list and, with a lax umask, read them. Now the
folders are 0700 and the files 0600 (existing ones are tightened at startup), and
the server warns at startup that the key is missing.
"""
import asyncio
import os
import stat

import pytest
from cryptography.fernet import Fernet


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def oauth_store(fresh_server):
    import oauth_store
    return oauth_store


def test_folders_and_files_are_owner_only(oauth_store, tmp_path, capsys):
    auth = tmp_path / "auth"
    store = oauth_store.build(str(auth), None)
    asyncio.run(store.put(key="client-a", value={"token": "t"}, collection="mcp-upstream-tokens"))
    root = auth / oauth_store.STORE_SUBDIR
    assert _mode(root) == 0o700
    for path in root.rglob("*"):
        assert _mode(path) == (0o700 if path.is_dir() else 0o600), path
    out = capsys.readouterr().out
    assert "STORAGE_ENCRYPTION_KEY" in out and "WARNING" in out


def test_an_existing_store_is_tightened_and_a_key_means_no_warning(oauth_store, tmp_path, capsys):
    root = tmp_path / "auth" / oauth_store.STORE_SUBDIR / "S_old-collection"
    root.mkdir(parents=True)
    (root / "entry.json").write_text("{}")
    os.chmod(root, 0o755)
    os.chmod(root / "entry.json", 0o644)
    oauth_store.build(str(tmp_path / "auth"), Fernet.generate_key().decode())
    assert _mode(root) == 0o700 and _mode(root / "entry.json") == 0o600
    assert _mode(root.parent) == 0o700
    assert "STORAGE_ENCRYPTION_KEY" not in capsys.readouterr().out
