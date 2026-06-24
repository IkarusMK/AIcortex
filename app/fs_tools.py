"""Workspace file tools — manage the /data/work scratch area.

The file hub of the connector: scans (scan_document), downloads (webdav/sftp) and
print sources all live under /data/work, but until now nothing could *see* or
tidy it. These tools list / read / write / move / delete inside the workspace.

Hard sandbox: every path resolves under WORK_DIR (/data/work). Paths that escape
it are rejected — so memory, the vault, skills etc. are never touched here.
"""
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

WORK_DIR = Path(os.environ.get("WORK_DIR", "/data/work")).resolve()
_MAX_READ = 200_000  # bytes returned by fs_read (text)


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
        try:
            data = p.read_bytes()[:max(1, int(max_bytes))]
            text = data.decode("utf-8", errors="replace")
        except Exception as exc:
            return f"Could not read: {exc}"
        more = "" if p.stat().st_size <= len(data) else f"\n…(truncated; {_size(p.stat().st_size)} total)"
        return text + more

    @mcp.tool
    def fs_write(path: str, content: str, append: bool = False) -> str:
        """Write (or append) a TEXT file in the workspace. Creates parent folders."""
        p = _resolve(path)
        if p is None:
            return "Path escapes the workspace."
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a" if append else "w", encoding="utf-8") as f:
                f.write(content or "")
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
    def fs_delete(path: str) -> str:
        """Delete a file or folder in the workspace. STATE-CHANGING — confirm first."""
        p = _resolve(path)
        if p is None or p == WORK_DIR:
            return "Refused (path escapes the workspace or is the root)."
        if not p.exists():
            return f"No such path '{path}'."
        try:
            if p.is_dir():
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
