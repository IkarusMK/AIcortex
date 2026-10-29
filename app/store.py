"""Crash- and concurrency-safe file storage for the file-backed stores.

Tool calls run in parallel worker threads (and several agents call at once), so
every store that does read-modify-write needs three guarantees:

- ``locked(path)``: exclusive lock across threads AND processes (flock on a
  sidecar ``.lock`` file), held for the whole read-modify-write.
- ``write_text_atomic``: readers see either the old or the new file, never a
  half-written one (unique temp file in the same directory + ``os.replace``).
- ``read_json``: a file that exists but can't be parsed raises ``StoreCorrupt``.
  It never falls back to "empty" — that turned a torn read into a wiped store.
"""
import contextlib
import json
import os
import tempfile
import threading
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock; threads stay safe
    fcntl = None


class StoreCorrupt(RuntimeError):
    """A store file exists but is unreadable. Callers must not overwrite it."""


_thread_locks: dict = {}
_registry_lock = threading.Lock()
_held = threading.local()       # lock files this thread already holds -> depth


def _thread_lock(key: str) -> threading.RLock:
    with _registry_lock:
        lock = _thread_locks.get(key)
        if lock is None:
            lock = _thread_locks[key] = threading.RLock()
        return lock


@contextlib.contextmanager
def locked(path):
    """Hold an exclusive lock for ``path``. Re-entrant within one thread: only the
    outermost level takes the flock (a second flock on a new descriptor would
    block against our own first one)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.with_name(path.name + ".lock"))
    depth = getattr(_held, "depth", None)
    if depth is None:
        depth = _held.depth = {}
    with _thread_lock(key):
        if depth.get(key) or fcntl is None:
            depth[key] = depth.get(key, 0) + 1
            try:
                yield
            finally:
                depth[key] -= 1
            return
        with open(key, "a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            depth[key] = 1
            try:
                yield
            finally:
                depth[key] = 0
                fcntl.flock(fh, fcntl.LOCK_UN)


def write_bytes_atomic(path, data: bytes) -> None:
    """Replace ``path`` with ``data`` so no reader ever sees a partial file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_text_atomic(path, text: str) -> None:
    write_bytes_atomic(path, text.encode("utf-8"))


def serialized(lock_path):
    """Decorator: run the function while holding ``locked(lock_path())``. Takes a
    callable so module-level paths that tests re-point are read at call time."""
    import functools

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with locked(lock_path()):
                return fn(*args, **kwargs)
        return wrapper
    return deco


def write_json(path, data) -> None:
    write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2))


def read_json(path, default):
    """Parsed JSON of ``path``; ``default`` only when the file does not exist."""
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StoreCorrupt(
            f"{path.name} is unreadable ({exc.__class__.__name__}); it was left "
            f"untouched — restore it from a backup or fix it by hand") from exc
