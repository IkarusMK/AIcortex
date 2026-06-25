"""Persistent, file-based memory tools for the MCP server.

Each memory is a plain Markdown file under MEMORY_DIR, organized by *scope*
(default ``shared``; per-agent scopes like ``agents/<id>`` enable multi-agent
setups). Files stay human-readable and debuggable on disk — no database.

AUTO-MEMORY (the brain learns by itself, without being told):
- Every memory is TYPED — user · feedback · project · reference — and carries
  lightweight YAML frontmatter (type, tags, source, created/updated, optional
  review date). ``memory_write`` REFUSES an untyped memory (house rule, same as
  ``skill_write`` requires a category) so the brain stays sorted instead of
  drifting into an undifferentiated pile.
- DEDUP-ASSIST: on write we look for overlapping existing entries and flag them
  so near-duplicates get merged into one file instead of multiplying.
- CANDIDATE STAGING: deterministic/auto-captured facts (see learn.py) and any
  "maybe worth keeping" note land in the reserved ``candidates`` scope — NEVER
  in live memory directly. They surface in bootstrap and are promoted to real
  memory only after review (``memory_promote``) or rejected (``memory_reject``).
  This keeps autonomy from polluting the curated brain.

Backward compatible: old memories written in the previous ``# Title`` format are
still read correctly; new writes use frontmatter.
"""
import os
import re
from datetime import datetime, timezone
from pathlib import Path

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", "/data/memory"))

# Reserved scope for not-yet-reviewed, auto/loosely captured facts. Excluded
# from the normal memory listing and the bootstrap MEMORY section; surfaced
# separately as "candidates awaiting review".
CANDIDATES_SCOPE = "candidates"

# The four canonical memory types (mirrors the user's own MEMORY.md taxonomy).
CANON_TYPES = ("user", "feedback", "project", "reference")
_TYPE_SYNONYMS = {
    # → user
    "preference": "user", "prefs": "user", "pref": "user", "identity": "user",
    "person": "user", "profile": "user",
    # → feedback
    "correction": "feedback", "guidance": "feedback", "instruction": "feedback",
    "rule": "feedback",
    # → project
    "projekt": "project", "status": "project", "ongoing": "project",
    "goal": "project", "task": "project",
    # → reference
    "ref": "reference", "resource": "reference", "link": "reference",
    "url": "reference", "referenz": "reference", "doc": "reference",
}


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


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


def canon_type(t: str) -> str:
    """Snap a free-text type onto one of the four canonical buckets, or "" if
    it doesn't map (→ caller refuses)."""
    t = (t or "").strip().lower()
    if not t:
        return ""
    if t in CANON_TYPES:
        return t
    return _TYPE_SYNONYMS.get(t, "")


def _tag_list(tags) -> list[str]:
    if isinstance(tags, (list, tuple)):
        items = tags
    else:
        items = re.split(r"[,\s]+", str(tags or ""))
    return [t.strip() for t in items if t and t.strip()]


def _frontmatter(*, title: str, type_: str, tags: list[str], source: str,
                 created: str, updated: str, review: str = "",
                 status: str = "") -> str:
    lines = ["---", f"title: {title}", f"type: {type_}"]
    if tags:
        lines.append("tags: " + ", ".join(tags))
    lines.append(f"source: {source or 'unknown'}")
    lines.append(f"created: {created}")
    lines.append(f"updated: {updated}")
    if review:
        lines.append(f"review: {review}")
    if status:
        lines.append(f"status: {status}")
    lines.append("---")
    return "\n".join(lines)


def _parse_frontmatter(block: str) -> dict:
    meta: dict = {}
    for ln in block.strip().splitlines():
        if ":" in ln:
            k, v = ln.split(":", 1)
            meta[k.strip().lower()] = v.strip()
    if "tags" in meta:
        meta["tags"] = _tag_list(meta["tags"])
    return meta


def read_meta(path: Path) -> tuple[dict, str]:
    """Return (meta, body) for a memory file. Understands the new frontmatter
    format AND the legacy ``# Title`` format (so old files keep working)."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return {"title": path.stem, "type": ""}, ""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            meta = _parse_frontmatter(parts[1])
            meta.setdefault("title", path.stem)
            meta.setdefault("type", "")
            return meta, parts[2].lstrip("\n")
    # Legacy: first '# ...' line is the title; whole text is the body.
    lines = text.splitlines()
    title = lines[0].lstrip("# ").strip() if lines else path.stem
    return {"title": title, "type": ""}, text


def _label(meta: dict) -> str:
    t = (meta.get("type") or "").strip()
    return f"[{t}] " if t else ""


def scope_title_lines(scope_dir: Path) -> list[str]:
    """'  - name — [type] title' for each memory in a scope dir (for bootstrap)."""
    out = []
    for p in sorted(scope_dir.glob("*.md")):
        meta, _ = read_meta(p)
        out.append(f"  - {p.stem} — {_label(meta)}{meta.get('title', p.stem)}")
    return out


def candidate_count() -> int:
    """How many memory candidates are staged for review."""
    d = MEMORY_DIR / CANDIDATES_SCOPE
    if not d.exists():
        return 0
    return sum(1 for _ in d.glob("*.md"))


def _overlap_hits(scope: str, title: str, exclude_slug: str) -> list[str]:
    """Existing memories in a scope whose title shares significant words with
    `title` (cheap near-duplicate detector for the dedup hint)."""
    words = {w for w in re.split(r"[^a-z0-9]+", (title or "").lower())
             if len(w) >= 4}
    if not words:
        return []
    hits = []
    for p in sorted(_scope_dir(scope).glob("*.md")):
        if p.stem == exclude_slug:
            continue
        meta, _ = read_meta(p)
        other = {w for w in re.split(r"[^a-z0-9]+", (meta.get("title") or "").lower())
                 if len(w) >= 4}
        if words & other:
            hits.append(p.stem)
    return hits[:5]


def stage_candidate(title: str, content: str, type_: str = "reference",
                    source: str = "auto", tags="") -> str:
    """Write a candidate (not live!) into the reserved candidates scope. Used by
    learn.py (auto-capture) and by the memory_note tool. Returns the slug."""
    t = canon_type(type_) or "reference"
    slug = _slug(title)
    path = _scope_dir(CANDIDATES_SCOPE) / f"{slug}.md"
    today = _today()
    created = today
    if path.exists():  # keep original created date if we overwrite
        meta, _ = read_meta(path)
        created = meta.get("created", today)
    fm = _frontmatter(title=title, type_=t, tags=_tag_list(tags), source=source,
                      created=created, updated=today, status="candidate")
    path.write_text(f"{fm}\n\n{content.strip()}\n", encoding="utf-8")
    return slug


def register(mcp):
    """Register the memory_* tools on a FastMCP instance."""

    @mcp.tool
    def memory_write(title: str, content: str, type: str = "",
                     scope: str = "shared", tags: str = "",
                     source: str = "", review: str = "") -> str:
        """Save a durable, TYPED memory and write it back to the brain.
        type (REQUIRED) = one of: user (who the user is / preferences) ·
        feedback (how you should work — corrections & confirmed approaches) ·
        project (ongoing work, goals, status — give it a `review` date) ·
        reference (pointers to resources/URLs/IDs). An untyped memory is REFUSED
        (house rule, like skill_write needs a category) so the brain stays sorted.
        Only store DURABLE facts (not session chatter), nothing already in
        code/git, and NEVER secrets (those go to the vault via secret_set).
        Search first — overwriting the same title MERGES; for related-but-different
        facts the dedup hint flags overlaps so you can merge instead of duplicate.
        Overwrites an existing memory with the same title."""
        t = canon_type(type)
        if not t:
            return ("Refused: a memory MUST be typed (house rule, so the brain "
                    f"stays sorted). Pass type = one of: {', '.join(CANON_TYPES)}. "
                    "Guidance: user = who the user is / preferences · feedback = "
                    "how to work (corrections, confirmed approaches) · project = "
                    "ongoing work/goals (add a review date) · reference = pointers "
                    "to resources/URLs/IDs.")
        slug = _slug(title)
        path = _scope_dir(scope) / f"{slug}.md"
        today = _today()
        created = today
        if path.exists():
            meta, _ = read_meta(path)
            created = meta.get("created", today)
        fm = _frontmatter(title=title, type_=t, tags=_tag_list(tags),
                          source=source or "session", created=created,
                          updated=today, review=review.strip())
        path.write_text(f"{fm}\n\n{content.strip()}\n", encoding="utf-8")
        hits = _overlap_hits(scope, title, slug)
        note = ""
        if hits:
            note = ("\n⚠ possible overlap with: " + ", ".join(hits)
                    + " — consider merging (memory_read them, fold into one, "
                      "memory_delete the rest) to avoid duplicates.")
        return f"Saved [{t}] memory '{slug}' in scope '{scope}'.{note}"

    @mcp.tool
    def memory_note(title: str, content: str, type: str = "reference",
                    tags: str = "") -> str:
        """Stage a CANDIDATE memory (not live) for later review — for 'might be
        worth remembering' facts you're unsure about. It lands in the candidates
        queue (shown in bootstrap); promote it with memory_promote or drop it with
        memory_reject. For facts you're sure about, use memory_write directly."""
        slug = stage_candidate(title, content, type_=type, source="note", tags=tags)
        return (f"Staged candidate '{slug}' for review "
                f"(memory_candidates to list · memory_promote/memory_reject to decide).")

    @mcp.tool
    def memory_list(scope: str = "shared") -> str:
        """List saved memories in a scope (each line: name — [type] title)."""
        items = sorted(_scope_dir(scope).glob("*.md"))
        if not items:
            return f"No memories in scope '{scope}' yet."
        lines = []
        for p in items:
            meta, _ = read_meta(p)
            lines.append(f"- {p.stem} — {_label(meta)}{meta.get('title', p.stem)}")
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
                snippet = next((ln for ln in text.splitlines()
                                if q in ln.lower() and not ln.startswith(("---", "title:", "type:"))),
                               "")
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

    # ── Candidate review (auto-memory) ─────────────────────────────────────
    @mcp.tool
    def memory_candidates() -> str:
        """List memory candidates awaiting review (auto-captured or staged via
        memory_note). Promote the keepers (memory_promote), reject the rest
        (memory_reject) — this is how the brain learns without polluting itself."""
        d = _scope_dir(CANDIDATES_SCOPE)
        items = sorted(d.glob("*.md"))
        if not items:
            return "No memory candidates awaiting review."
        lines = []
        for p in items:
            meta, body = read_meta(p)
            snippet = " ".join(body.split())[:120]
            lines.append(f"- {p.stem} — {_label(meta)}{meta.get('title', p.stem)} "
                         f"(source: {meta.get('source', '?')}) — {snippet}")
        return "\n".join(lines)

    @mcp.tool
    def memory_promote(name: str, scope: str = "shared", type: str = "") -> str:
        """Promote a reviewed candidate into live memory (default scope 'shared').
        Optionally override its type. Removes it from the candidates queue."""
        src = _scope_dir(CANDIDATES_SCOPE) / f"{_slug(name)}.md"
        if not src.exists():
            return f"No candidate named '{name}'. Use memory_candidates."
        meta, body = read_meta(src)
        t = canon_type(type) or canon_type(meta.get("type", "")) or "reference"
        title = meta.get("title", name)
        today = _today()
        dst = _scope_dir(scope) / f"{_slug(title)}.md"
        fm = _frontmatter(title=title, type_=t, tags=meta.get("tags", []),
                          source=(meta.get("source", "auto") + " · promoted"),
                          created=meta.get("created", today), updated=today)
        dst.write_text(f"{fm}\n\n{body.strip()}\n", encoding="utf-8")
        src.unlink()
        return f"Promoted candidate → [{t}] memory '{dst.stem}' in scope '{scope}'."

    @mcp.tool
    def memory_reject(name: str) -> str:
        """Reject (delete) a memory candidate without promoting it."""
        src = _scope_dir(CANDIDATES_SCOPE) / f"{_slug(name)}.md"
        if not src.exists():
            return f"No candidate named '{name}'."
        src.unlink()
        return f"Rejected candidate '{name}'."
