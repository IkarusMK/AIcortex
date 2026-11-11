"""The OAuth proxy's store no longer uses diskcache/pickle; existing logins survive.

Found in the 2026-11 review: FastMCP's OAuth state (registered clients such as the
Claude and ChatGPT connectors, refresh-token metadata, upstream tokens) lived in a
``DiskStore`` — diskcache, which unpickles values it reads (PYSEC-2026-2447 /
CVE-2025-69872, no fixed release). Whoever could drop a crafted row into
AUTH_STORE_DIR got code execution in the server on the next lookup.

Now the store is a FileTreeStore (plain JSON files, FastMCP's own default backend)
in AUTH_STORE_DIR/oauth, still Fernet-encrypted with STORAGE_ENCRYPTION_KEY. An old
store is migrated once at startup — read straight from its SQLite table, never
through diskcache, never unpickling — and its files are moved to
AUTH_STORE_DIR/diskstore.migrated-2.6.0. policy.json and audit.log, which share
that folder, stay where they are.

The old stores in these tests are built exactly the way 2.5.0's
``server._client_storage`` built them (DiskStore + FernetEncryptionWrapper) and filled
by FastMCP's own OAuthProxy.
"""
import asyncio
import stat
import os
import subprocess
import sys
import time

import pytest

pytest.importorskip("diskcache", reason="needed only to build a pre-2.6.0 store")

from cryptography.fernet import Fernet  # noqa: E402
from key_value.aio.stores.filetree import FileTreeStore  # noqa: E402  (owner-only subclass)

KEY = Fernet.generate_key().decode()
CLIENTS = "mcp-oauth-proxy-clients"
REFRESH = "mcp-refresh-tokens"
CODES = "mcp-authorization-codes"
REDIRECT = "https://claude.example/api/mcp/auth_callback"
ARCHIVE = "diskstore.migrated-2.6.0"


class _Payload:
    """Pickles to "os.makedirs(<marker>)": proof of code execution on unpickling."""

    def __init__(self, marker):
        self.marker = str(marker)

    def __reduce__(self):
        return (os.makedirs, (self.marker,))


def _proxy(storage):
    from fastmcp.server.auth import StaticTokenVerifier
    from fastmcp.server.auth.oauth_proxy import OAuthProxy
    return OAuthProxy(
        upstream_authorization_endpoint="https://idp.example/authorize",
        upstream_token_endpoint="https://idp.example/token",
        upstream_client_id="upstream-client", upstream_client_secret="upstream-secret",
        base_url="https://agent.example.net", jwt_signing_key="0" * 64,
        token_verifier=StaticTokenVerifier(tokens={}), client_storage=storage)


def _client(client_id):
    from mcp.shared.auth import OAuthClientInformationFull
    from pydantic import AnyUrl
    return OAuthClientInformationFull(
        client_id=client_id, redirect_uris=[AnyUrl(REDIRECT)],
        grant_types=["authorization_code", "refresh_token"], response_types=["code"],
        token_endpoint_auth_method="none")


def _write_old_store(auth_dir, key, client_id="client-a", extra=None):
    """A 2.5.0 store: DiskStore (+ Fernet when ``key``), filled through OAuthProxy."""
    from key_value.aio.stores.disk import DiskStore
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

    async def fill():
        raw = DiskStore(directory=str(auth_dir))
        async with raw:
            store = FernetEncryptionWrapper(key_value=raw, fernet=Fernet(key)) if key else raw
            await _proxy(store).register_client(_client(client_id))
            await store.put(key="rt-hash-1", value={"sub": "alice"}, collection=REFRESH, ttl=3600)
            await store.put(key="code-1", value={"code": "x"}, collection=CODES, ttl=0.3)
            if extra:
                await extra(store)

    asyncio.run(fill())
    time.sleep(0.4)                                  # let code-1 expire


def _inner(store):
    return getattr(store, "key_value", store)


@pytest.fixture
def auth_env(fresh_server, tmp_path, monkeypatch):
    auth_dir = tmp_path / "auth_store_dir"            # what conftest set AUTH_STORE_DIR to
    assert os.environ["AUTH_STORE_DIR"] == str(auth_dir)
    auth_dir.mkdir(parents=True, exist_ok=True)
    (auth_dir / "policy.json").write_text('{"users": {}}')
    (auth_dir / "audit.log").write_text("2026-11-01 deny example\n")
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", KEY)
    return fresh_server, auth_dir


def test_old_store_is_migrated_and_logins_survive(auth_env):
    server, auth_dir = auth_env
    _write_old_store(auth_dir, KEY)

    store = server._client_storage()

    assert isinstance(_inner(store), FileTreeStore)
    assert not (auth_dir / "cache.db").exists()
    assert (auth_dir / ARCHIVE / "cache.db").exists()
    assert (auth_dir / "policy.json").read_text() == '{"users": {}}'
    assert (auth_dir / "audit.log").read_text() == "2026-11-01 deny example\n"

    async def check():
        client = await _proxy(store).get_client("client-a")
        assert client is not None and [str(u) for u in client.redirect_uris] == [REDIRECT]
        value, ttl = await store.ttl(key="rt-hash-1", collection=REFRESH)
        assert value == {"sub": "alice"} and 3000 < ttl <= 3600
        assert await store.get(key="code-1", collection=CODES) is None       # expired
        # still encrypted at rest with the same key
        raw = await _inner(store).get(key="rt-hash-1", collection=REFRESH)
        assert "__encrypted_data__" in raw

    asyncio.run(check())


def test_unencrypted_old_store_is_migrated_too(auth_env, monkeypatch):
    server, auth_dir = auth_env
    monkeypatch.delenv("STORAGE_ENCRYPTION_KEY")
    _write_old_store(auth_dir, None)

    store = server._client_storage()

    assert isinstance(store, FileTreeStore)
    client = asyncio.run(_proxy(store).get_client("client-a"))
    assert client is not None


def test_a_pickled_row_is_never_unpickled(auth_env, tmp_path):
    """The CVE path: a crafted pickle in the old store must not run — not during the
    migration and not on a later lookup of that key."""
    from diskcache import Cache
    server, auth_dir = auth_env
    marker = tmp_path / "pwned"
    _write_old_store(auth_dir, KEY)
    with Cache(directory=str(auth_dir), eviction_policy="none") as cache:
        cache.set(f"{CLIENTS}::evil", _Payload(marker))

    store = server._client_storage()
    assert asyncio.run(store.get(key="evil", collection=CLIENTS)) is None
    assert not marker.exists()
    assert asyncio.run(_proxy(store).get_client("client-a")) is not None


def test_the_app_no_longer_loads_diskcache(auth_env):
    _, auth_dir = auth_env
    _write_old_store(auth_dir, KEY)
    app_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import server; "
            "server._client_storage(); print('diskcache' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code, app_dir], capture_output=True,
                         text=True, timeout=120, env=dict(os.environ))
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "False"


def test_migration_runs_once_and_never_overwrites_newer_entries(auth_env):
    server, auth_dir = auth_env
    _write_old_store(auth_dir, KEY)
    store = server._client_storage()
    asyncio.run(store.put(key="rt-hash-1", value={"sub": "alice", "rotated": True},
                          collection=REFRESH, ttl=3600))

    assert isinstance(_inner(server._client_storage()), FileTreeStore)   # 2nd start
    assert sorted(p.name for p in auth_dir.iterdir() if p.name.startswith("diskstore")) == [ARCHIVE]

    # A leftover old store reappears (e.g. 2.5 was started once more): its other
    # entries are added, but what the new store already holds is kept.
    async def other(old):
        await _proxy(old).register_client(_client("client-b"))
    _write_old_store(auth_dir, KEY, client_id="client-a", extra=other)
    store = server._client_storage()

    async def check():
        assert (await store.get(key="rt-hash-1", collection=REFRESH))["rotated"] is True
        assert await _proxy(store).get_client("client-b") is not None

    asyncio.run(check())
    assert sorted(p.name for p in auth_dir.iterdir() if p.name.startswith("diskstore")) == [
        ARCHIVE, f"{ARCHIVE}.1"]


def test_unreadable_old_store_is_left_in_place(auth_env, capsys):
    server, auth_dir = auth_env
    (auth_dir / "cache.db").write_bytes(b"this is not a SQLite database" * 10)

    store = server._client_storage()

    assert isinstance(_inner(store), FileTreeStore)
    assert (auth_dir / "cache.db").exists()
    assert not (auth_dir / ARCHIVE).exists()
    assert "WARNING" in capsys.readouterr().out
    asyncio.run(store.put(key="k", value={"v": 1}, collection=CLIENTS))
    assert asyncio.run(store.get(key="k", collection=CLIENTS)) == {"v": 1}


def test_fresh_install_needs_no_migration(auth_env):
    server, auth_dir = auth_env
    store = server._client_storage()
    assert isinstance(_inner(store), FileTreeStore)
    assert not (auth_dir / ARCHIVE).exists()
    asyncio.run(_proxy(store).register_client(_client("client-c")))
    assert asyncio.run(_proxy(server._client_storage()).get_client("client-c")) is not None


def test_the_archive_of_the_old_store_is_owner_only(auth_env):
    # The archive keeps the old tokens until the operator deletes it — on a NAS share
    # with "others" read access it must not be readable by anyone else.
    server, auth_dir = auth_env
    _write_old_store(auth_dir, KEY)
    server._client_storage()
    archive = auth_dir / ARCHIVE
    assert stat.S_IMODE(archive.stat().st_mode) == 0o700
    for path in archive.rglob("*"):
        want = 0o700 if path.is_dir() else 0o600
        assert stat.S_IMODE(path.stat().st_mode) == want, path.name
