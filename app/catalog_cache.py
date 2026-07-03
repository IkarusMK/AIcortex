"""Stat-signature cache for the bootstrap catalog — keep bootstrap cheap as data grows.

bootstrap re-rendered every catalog section from scratch on each call. The dominant
cost was `_skill_list()` reading ALL SKILL.md files just to count categories; memory,
sessions, services and the device registries hit the same wall as they fill up over
months (they scan a growing directory every single call).

Each section's rendered lines are cached, keyed by a CHEAP stat SIGNATURE of its source
directory — file count + newest mtime + total size, computed from `stat()` METADATA
only, with **no file reads**. When nothing changed, the cached lines are returned
without opening a single file; when the signature changes (a file added, removed or
edited), that one section is re-rendered (reads happen once) and re-cached.

The cache is persisted to disk, so even the FIRST bootstrap after a container restart is
fast. It stores only already-public catalog text (never secrets).

DESIGN SAFETY — fail-open: any cache error (unreadable cache file, un-signable dir)
falls back to rendering live. The cache can never make bootstrap *wrong*, only faster;
a corrupt cache is simply rebuilt on the next call.
"""
import json
import os
from pathlib import Path

_CACHE_FILE = Path(os.environ.get("CATALOG_CACHE_FILE", "/data/.catalog_cache.json"))

_mem = None      # in-process copy of the on-disk cache: {key: {"sig": [...], "lines": [...]}}
_dirty = False   # whether _mem has unsaved changes (flushed once per bootstrap)


def _load() -> dict:
    """Load the cache once per process, then keep it in memory. Corrupt/missing → {}."""
    global _mem
    if _mem is not None:
        return _mem
    try:
        data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        _mem = data if isinstance(data, dict) else {}
    except Exception:
        _mem = {}
    return _mem


def signature(dirs, pattern: str):
    """A cheap metadata-only signature of the files matching `pattern` under `dirs`:
    ``[count, newest_mtime_ns, total_size]``. Catches adds/removes (count) and edits
    (mtime/size) WITHOUT reading a single file. Returns None on error → caller renders
    live and does not cache (never risk a stale/wrong section)."""
    try:
        count, newest, total = 0, 0, 0
        for d in dirs:
            dp = Path(d)
            if not dp.exists():
                continue
            for p in dp.glob(pattern):
                st = p.stat()
                count += 1
                if st.st_mtime_ns > newest:
                    newest = st.st_mtime_ns
                total += st.st_size
        return [count, newest, total]
    except Exception:
        return None


def cached_lines(key: str, dirs, pattern: str, render_fn):
    """Return the rendered lines for a catalog section, served from cache when the source
    directory's stat signature is unchanged. On a miss (or first run) `render_fn()` is
    called and its result cached. Fail-open: an un-signable directory renders live."""
    global _dirty
    sig = signature(dirs, pattern)
    if sig is None:
        return render_fn()
    cache = _load()
    entry = cache.get(key)
    if isinstance(entry, dict) and entry.get("sig") == sig:
        return entry.get("lines", [])
    lines = render_fn()
    cache[key] = {"sig": sig, "lines": lines}
    _dirty = True
    return lines


def flush() -> None:
    """Persist the cache to disk if it changed (call once at the end of a bootstrap).
    Atomic write; fail-open — a write error just means the next bootstrap re-renders."""
    global _dirty
    if not _dirty:
        return
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_load(), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _CACHE_FILE)
        _dirty = False
    except Exception:
        pass


def invalidate() -> None:
    """Drop the whole cache (used by tests / a manual reset). Next bootstrap rebuilds."""
    global _mem, _dirty
    _mem = {}
    _dirty = True
