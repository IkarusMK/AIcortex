"""Persistent, file-based memory tools for the MCP server.

Each memory is a plain Markdown file under MEMORY_DIR, organized by *scope*
(default ``shared``; per-agent scopes like ``agents/<id>`` enable multi-agent
setups). Files stay human-readable and debuggable on disk — no database.
"""
import os
import re
from datetime import datetime, timezone
from pathlib import Path

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", "/data/memory"))


def _slug(text: str) -> str:
    """Filesystem-safe slug from a title."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:60] or "untitled"


def _scope_dir(scope: str) -> Path:
    """Resolve (and create) the directory for a scope, guarding against traversal."""
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", scope or "shared") or "shared"
    d = MEMORY_DIR / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def register(mcp):
    """Register the memory_* tools on a FastMCP instance."""

    @mcp.tool
    def memory_write(title: str, content: str, scope: str = "shared") -> str:
        """Save a durable memory (a fact about the user, a preference, an ongoing
        project). Overwrites an existing memory with the same title."""
        path = _scope_dir(scope) / f"{_slug(title)}.md"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path.write_text(
            f"# {title}\n\n_updated: {ts} · scope: {scope}_\n\n{content}\n",
            encoding="utf-8",
        )
        return f"Saved memory '{path.stem}' in scope '{scope}'."

    @mcp.tool
    def memory_list(scope: str = "shared") -> str:
        """List saved memories in a scope (each line: name — title)."""
        items = sorted(_scope_dir(scope).glob("*.md"))
        if not items:
            return f"No memories in scope '{scope}' yet."
        lines = []
        for p in items:
            head = p.read_text(encoding="utf-8").splitlines()
            title = head[0].lstrip("# ").strip() if head else p.stem
            lines.append(f"- {p.stem} — {title}")
        return "\n".join(lines)

    @mcp.tool
    def memory_read(name: str, scope: str = "shared") -> str:
        """Read a memory's full content by its name (as shown by memory_list)."""
        path = _scope_dir(scope) / f"{_slug(name)}.md"
        if not path.exists():
            return f"No memory named '{name}' in scope '{scope}'."
        return path.read_text(encoding="utf-8")

    @mcp.tool
    def memory_search(query: str, scope: str = "shared") -> str:
        """Search a scope's memories for a keyword; returns matching names + snippets."""
        q = (query or "").lower()
        hits = []
        for p in sorted(_scope_dir(scope).glob("*.md")):
            text = p.read_text(encoding="utf-8")
            if q and q in text.lower():
                snippet = next((ln for ln in text.splitlines() if q in ln.lower()), "")
                hits.append(f"- {p.stem}: {snippet.strip()[:120]}")
        return "\n".join(hits) if hits else f"No matches for '{query}' in scope '{scope}'."

    @mcp.tool
    def memory_delete(name: str, scope: str = "shared") -> str:
        """Delete a memory by its name."""
        path = _scope_dir(scope) / f"{_slug(name)}.md"
        if not path.exists():
            return f"No memory named '{name}' in scope '{scope}'."
        path.unlink()
        return f"Deleted memory '{name}' from scope '{scope}'."
