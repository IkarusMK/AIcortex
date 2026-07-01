"""Update-safe config writer for the *_add registration tools.

The registration tools (service_add, scan_add, mqtt_add, webdav_add, …) are
UPSERTS: the same tool creates a new entry or updates an existing one. Naively
they rebuilt the whole config from the call's parameters and overwrote the file —
so *updating* one field (e.g. adding a category) silently wiped every field the
caller didn't restate (a token_env reference, a write_only ingest lock, TLS
settings). That's real data loss / a security regression.

``write_merged`` fixes that: on update it MERGES the new values onto the existing
config, keeping any field the caller left at its default (empty string / False /
0). New, non-default values win. This makes "just add a category" or "just change
the description" safe, without clobbering credentials or locks.

Tradeoff (documented): you can't *clear* a field back to its default via an update
(the old value is preserved) — to truly reset a field, ``*_delete`` and re-add.
This is intentionally the safe direction (you can't accidentally drop a token or
unlock an ingest-only sink by forgetting a parameter).
"""
import json
from pathlib import Path


def _is_default(v) -> bool:
    """A value that means 'caller didn't set this' (MCP fills defaults, so we
    can't see the difference between omitted and default — we treat them alike)."""
    return v is None or v == "" or v is False or v == 0 or v == []


def merge(old: dict, new: dict) -> dict:
    """Overlay ``new`` onto ``old``, keeping old values wherever the new value is a
    default. Returns a new dict (never mutates the inputs)."""
    if not isinstance(old, dict):
        return dict(new)
    out = dict(old)
    for k, v in new.items():
        if _is_default(v) and k in old:
            continue  # caller left it at default → preserve the existing value
        out[k] = v
    return out


def write_merged(path: Path, cfg: dict) -> None:
    """Write ``cfg`` to ``path`` as JSON, MERGING into any existing config so an
    update can't clobber fields the caller didn't restate."""
    existing = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:
            existing = {}
    path.write_text(json.dumps(merge(existing, cfg), indent=2), encoding="utf-8")
