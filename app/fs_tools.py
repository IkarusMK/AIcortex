"""Workspace file tools — manage the /data/work scratch area.

The file hub of the connector: scans (scan_document), downloads (webdav/sftp) and
print sources all live under /data/work. These tools list / read (text) / view
(fs_view renders images & PDFs so the assistant can SEE them with vision, not just
OCR) / write / move / delete inside the workspace.

Hard sandbox: every path resolves under WORK_DIR (/data/work). Paths that escape
it are rejected — so memory, the vault, skills etc. are never touched here.
"""
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

# Image/PDF rendering for fs_view. Guarded so the connector still starts (and every
# text tool keeps working) even if an optional imaging wheel is ever missing.
try:
    import imaging
except Exception:  # pragma: no cover
    imaging = None

WORK_DIR = Path(os.environ.get("WORK_DIR", "/data/work")).resolve()
_MAX_READ = 200_000        # default bytes returned by fs_read (text)
_READ_CEILING = 5_000_000  # hard cap a caller may request, even with a big max_bytes
# Largest file fs_view will pull into memory to render (a 300-dpi colour A4 scan is a
# few MB; keep headroom for multi-page PDFs). Tunable via env.
_VIEW_CEILING = int(os.environ.get("FS_VIEW_CEILING_BYTES", str(30_000_000)))
_MAX_WRITE = int(os.environ.get("FS_MAX_WRITE_BYTES", str(5_000_000)))  # per fs_write
# Total workspace size cap (default 2 GB). Set FS_WORKSPACE_QUOTA_BYTES to tune.
_WORKSPACE_QUOTA = int(os.environ.get("FS_WORKSPACE_QUOTA_BYTES", str(2_000_000_000)))


def _usage() -> int:
    """Total bytes currently used under WORK_DIR (best effort)."""
    total = 0
    for root, _dirs, files in os.walk(WORK_DIR):
        for fn in files:
            try:
                total += (Path(root) / fn).stat().st_size
            except Exception:
                pass
    return total


def _resolve(rel: str):
    """Resolve a path relative to WORK_DIR (Path), or None if it escapes the sandbox."""
    rel = (rel or "").lstrip("/")
    p = (WORK_DIR / rel).resolve()
    if p != WORK_DIR and WORK_DIR not in p.parents:
        return None
    return p


def _size(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.0f}{u}" if u == "B" else f"{f:.1f}{u}"
        f /= 1024
    return f"{n}B"


def register(mcp):
    @mcp.tool
    def fs_list(path: str = "") -> str:
        """List the workspace (/data/work) or a sub-folder. Shows name · size · type,
        newest first. path is relative to /data/work (empty = the root)."""
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        d = _resolve(path)
        if d is None:
            return "Path escapes the workspace."
        if not d.exists():
            return f"No such path '{path}'."
        if d.is_file():
            return f"{d.name} · {_size(d.stat().st_size)} · file"
        items = sorted(d.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        if not items:
            return "(empty)"
        out = []
        for p in items:
            st = p.stat()
            kind = "dir" if p.is_dir() else "file"
            when = datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M")
            out.append(f"- {p.name}{'/' if p.is_dir() else ''} · "
                       f"{_size(st.st_size) if p.is_file() else '—'} · {when}")
        return "\n".join(out)

    @mcp.tool
    def fs_read(path: str, max_bytes: int = _MAX_READ) -> str:
        """Read a TEXT file from the workspace (truncated to max_bytes). For binary
        files use the transfer tools (webdav/sftp/print) instead."""
        p = _resolve(path)
        if p is None:
            return "Path escapes the workspace."
        if not p.is_file():
            return f"No file at '{path}'."
        # Read only up to the cap from disk (don't load a multi-GB file into RAM
        # just to slice it afterwards). max_bytes is itself capped at _READ_CEILING.
        n = max(1, min(int(max_bytes), _READ_CEILING))
        try:
            with p.open("rb") as fh:
                data = fh.read(n)
            text = data.decode("utf-8", errors="replace")
        except Exception as exc:
            return f"Could not read: {exc}"
        total = p.stat().st_size
        more = "" if total <= len(data) else f"\n…(truncated; returned {_size(len(data))} of {_size(total)})"
        return text + more

    @mcp.tool
    def fs_view(path: str, page: int = 0, max_pages: int = 4):
        """SEE a workspace image or PDF with vision (not OCR). Returns the file as
        image content the assistant reads directly — scanned pages (scan_document),
        e-mail attachments (imap_fetch save_attachments), webdav/ftp downloads, print
        sources. Raster images are downscaled; PDFs are rendered from `page` (0-based)
        up to `max_pages`. For TEXT files use fs_read instead."""
        if imaging is None or not imaging.available():
            return "Image viewing is unavailable on this server (Pillow not installed)."
        p = _resolve(path)
        if p is None:
            return "Path escapes the workspace."
        if not p.is_file():
            return f"No file at '{path}'."
        size = p.stat().st_size
        if size > _VIEW_CEILING:
            return (f"Refused: '{path}' is {_size(size)}, over the {_size(_VIEW_CEILING)} "
                    "view cap. Move it out (webdav) or view a smaller copy.")
        try:
            data = p.read_bytes()
            jpegs, note = imaging.render(data, p.suffix.lower(),
                                         first=max(0, int(page)),
                                         count=max(1, int(max_pages)))
        except Exception as exc:
            return f"Could not render '{path}': {type(exc).__name__}: {exc}"
        if not jpegs:
            return note or f"'{path}' is not a viewable image/PDF (for text use fs_read)."
        blocks = []
        if note:
            blocks.append(imaging.text_block(note))
        blocks.extend(imaging.image_block(j) for j in jpegs)
        return blocks if len(blocks) > 1 else blocks[0]

    @mcp.tool
    def fs_write(path: str, content: str, append: bool = False) -> str:
        """Write (or append) a TEXT file in the workspace. Creates parent folders."""
        p = _resolve(path)
        if p is None:
            return "Path escapes the workspace."
        content = content or ""
        nbytes = len(content.encode("utf-8"))
        if nbytes > _MAX_WRITE:
            return (f"Refused: content is {_size(nbytes)}, over the {_size(_MAX_WRITE)} "
                    "per-write limit (use the transfer tools for large files).")
        # Reject before touching disk if it would blow the workspace quota.
        existing = p.stat().st_size if (append and p.is_file()) else 0
        if _usage() - existing + nbytes > _WORKSPACE_QUOTA:
            return (f"Refused: workspace is {_size(_usage())} of the {_size(_WORKSPACE_QUOTA)} "
                    "quota — this write would exceed it. Tidy /data/work (fs_delete) first.")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a" if append else "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as exc:
            return f"Could not write: {exc}"
        return f"{'Appended to' if append else 'Wrote'} '{path}' ({_size(p.stat().st_size)})."

    @mcp.tool
    def fs_move(source: str, dest: str) -> str:
        """Move/rename a file or folder within the workspace."""
        s, d = _resolve(source), _resolve(dest)
        if s is None or d is None:
            return "Path escapes the workspace."
        if not s.exists():
            return f"No such source '{source}'."
        try:
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(s), str(d))
        except Exception as exc:
            return f"Move failed: {exc}"
        return f"Moved '{source}' → '{dest}'."

    @mcp.tool
    def fs_delete(path: str, confirm: bool = False) -> str:
        """Delete a file or folder in the workspace. STATE-CHANGING — confirm first.
        Deleting a NON-EMPTY folder (recursive) requires confirm=true as a guard
        against wiping the whole hub by accident."""
        p = _resolve(path)
        if p is None or p == WORK_DIR:
            return "Refused (path escapes the workspace or is the root)."
        if not p.exists():
            return f"No such path '{path}'."
        try:
            if p.is_dir():
                if any(p.iterdir()) and not confirm:
                    return (f"Refused: '{path}' is a non-empty folder. Re-call with "
                            "confirm=true to delete it and ALL its contents.")
                shutil.rmtree(p)
            else:
                p.unlink()
        except Exception as exc:
            return f"Delete failed: {exc}"
        return f"Deleted '{path}'."

    @mcp.tool
    def fs_info(path: str = "") -> str:
        """Workspace usage summary, or details for one path (size, modified, type)."""
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        if path:
            p = _resolve(path)
            if p is None or not p.exists():
                return f"No such path '{path}'."
            st = p.stat()
            when = datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M")
            return f"{path} · {'dir' if p.is_dir() else 'file'} · {_size(st.st_size)} · modified {when}"
        total = 0
        count = 0
        for root, _dirs, files in os.walk(WORK_DIR):
            for fn in files:
                try:
                    total += (Path(root) / fn).stat().st_size
                    count += 1
                except Exception:
                    pass
        return f"Workspace /data/work — {count} file(s), {_size(total)} total."
