"""Persistent store for the OAuth proxy's state, and the one-time move off diskcache.

FastMCP's OAuth proxy keeps everything it needs between requests in one key-value
store: registered clients (the Claude and ChatGPT connectors …), authorization codes,
refresh-token metadata, upstream tokens. AICortex keeps it under AUTH_STORE_DIR so
logins survive container restarts, Fernet-encrypted when STORAGE_ENCRYPTION_KEY is set.

Up to 2.5 that was a DiskStore — diskcache, which unpickles whatever it reads
(PYSEC-2026-2447 / CVE-2025-69872, no fixed release): a crafted row in the folder
meant code execution in the server. From 2.6.0 it is a FileTreeStore, FastMCP's own
default backend — one JSON file per entry in AUTH_STORE_DIR/oauth.

An old store is migrated once, at startup: its SQLite table is read directly (never
through diskcache) and only plain-text rows — all DiskStore ever wrote — are copied,
values verbatim (still encrypted), with their remaining lifetime; expired rows and
anything pickled are skipped. An entry the new store already holds is never
overwritten. The old files are then moved to AUTH_STORE_DIR/diskstore.migrated-2.6.0
(the folder itself stays: policy.json and audit.log live there too). If anything
fails, the old files stay where they are and the next start tries again.
"""
import asyncio
import concurrent.futures
import json
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

STORE_SUBDIR = "oauth"
ARCHIVE_NAME = "diskstore.migrated-2.6.0"
_OLD_DB = "cache.db"
_OLD_DB_FILES = (_OLD_DB, f"{_OLD_DB}-wal", f"{_OLD_DB}-shm", f"{_OLD_DB}-journal")
_OLD_VALUE_DIR = re.compile(r"^[0-9a-f]{2}$")   # diskcache's files for large values
_MODE_RAW, _MODE_TEXT = 1, 3                     # diskcache value modes we accept
_SEPARATOR = "::"                                # DiskStore's "<collection>::<key>"


def _file_store(directory: Path):
    from key_value.aio.stores.filetree import (
        FileTreeStore,
        FileTreeV1CollectionSanitizationStrategy,
        FileTreeV1KeySanitizationStrategy,
    )
    return FileTreeStore(
        data_directory=directory,
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(directory),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(directory))


def build(auth_dir: str, encryption_key: str | None = None):
    """The store for the OAuth proxy, after migrating an old DiskStore if there is one."""
    root = Path(auth_dir)
    directory = root / STORE_SUBDIR
    directory.mkdir(parents=True, exist_ok=True)
    try:
        migrate(root, directory)
    except Exception as exc:
        print(f"[AICortex] WARNING: OAuth store migration failed ({type(exc).__name__}: "
              f"{exc}); the old store stays in {root} and is tried again on the next start")
    store = _file_store(directory)
    if encryption_key:
        from cryptography.fernet import Fernet
        from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
        store = FernetEncryptionWrapper(key_value=store, fernet=Fernet(encryption_key))
    return store


def migrate(root: Path, directory: Path) -> dict | None:
    """Copy an old DiskStore in ``root`` into the FileTreeStore in ``directory`` and
    move its files aside. None when there is nothing to migrate."""
    if not (root / _OLD_DB).is_file():
        return None
    entries, skipped = read_old_store(root)
    # The store API is async and this runs at import time, possibly inside a running
    # event loop: the copy gets its own loop in a worker thread.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        copied = pool.submit(asyncio.run, _copy(entries, directory)).result()
    archive = _archive_old_files(root)
    print(f"[AICortex] OAuth store: migrated {copied} entries from the old DiskStore to "
          f"{directory} ({len(entries) - copied} were already there, {skipped} expired or "
          f"unreadable rows skipped); the old files are kept in {archive}")
    return {"copied": copied, "skipped": skipped, "archive": archive}


def read_old_store(root: Path) -> tuple[list[tuple[str, str, dict, float | None]], int]:
    """(collection, key, value, ttl seconds) for every live plain-text row of the old
    diskcache table, and how many rows were skipped. Never unpickles anything."""
    now = time.time()
    with closing(sqlite3.connect(root / _OLD_DB)) as db:
        db.execute("PRAGMA trusted_schema = OFF")
        rows = db.execute(
            "SELECT key, raw, expire_time, mode, filename, value FROM Cache").fetchall()
    entries = [e for e in (_entry(root, row, now) for row in rows) if e]
    return entries, len(rows) - len(entries)


def _entry(root: Path, row, now: float):
    key, raw, expire_time, mode, filename, value = row
    if not raw or not isinstance(key, str) or _SEPARATOR not in key:
        return None                                  # a pickled key: not ours
    if expire_time is not None and expire_time <= now:
        return None
    if mode == _MODE_RAW:
        text = value
    elif mode == _MODE_TEXT:
        text = _text_file(root, filename)
    else:
        return None                                  # pickled / binary value: not ours
    try:
        doc = json.loads(text)
        stored = doc["value"]
        stored = json.loads(stored) if isinstance(stored, str) else stored
    except (ValueError, TypeError, KeyError):
        return None
    if not isinstance(stored, dict):
        return None
    # DiskStore also wrote the exact names into the document; the compound key is
    # the fallback (and the check: a document can't move itself elsewhere).
    collection, name = doc.get("collection"), doc.get("key")
    if not (isinstance(collection, str) and isinstance(name, str)
            and f"{collection}{_SEPARATOR}{name}" == key):
        collection, name = key.split(_SEPARATOR, 1)
    ttl = None if expire_time is None else expire_time - now
    return collection, name, stored, ttl


def _text_file(root: Path, filename) -> str | None:
    """A large value diskcache kept in its own file — only inside ``root``."""
    if not isinstance(filename, str):
        return None
    path = (root / filename).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


async def _copy(entries, directory: Path) -> int:
    """Put every entry the new store doesn't hold yet; returns how many were put."""
    store = _file_store(directory)
    copied = 0
    for collection, key, value, ttl in entries:
        if await store.get(key=key, collection=collection) is None:
            await store.put(key=key, value=value, collection=collection, ttl=ttl)
            copied += 1
    return copied


def _archive_old_files(root: Path) -> Path:
    """Move diskcache's files (never anything else in ``root``) into a fresh archive
    folder, so the old store is not read again."""
    archive = root / ARCHIVE_NAME
    n = 0
    while archive.exists():
        n += 1
        archive = root / f"{ARCHIVE_NAME}.{n}"
    archive.mkdir()
    for item in root.iterdir():
        if item.name in _OLD_DB_FILES or (item.is_dir() and _OLD_VALUE_DIR.match(item.name)):
            item.rename(archive / item.name)
    return archive
