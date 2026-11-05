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
from typing import Annotated

from pydantic import Field

import store

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", "/data/memory"))

# Reserved scope for not-yet-reviewed, auto/loosely captured facts. Excluded
# from the normal memory listing and the bootstrap MEMORY section; surfaced
# separately as "candidates awaiting review".
CANDIDATES_SCOPE = "candidates"

# The four canonical memory types (mirrors the user's own MEMORY.md taxonomy).
CANON_TYPES = ("user", "feedback", "project", "reference")
# What the tools tell a model about `scope` (a confined person is kept to their own
# area by tenancy.confine_memory_scope, whatever they pass).
_SCOPE = Field(description='Memory area: "shared" (default) or "users/<id>"; '
                           "a person limited to their own area stays there anyway")
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
    """Filesystem-safe slug from a title. Letters of any script are kept (umlauts,
    CJK, …); a title with no letters at all gets a stable hash, so different
    titles never collapse onto one file."""
    s = re.sub(r"[^\w]+", "-", (text or "").lower()).strip("-_")[:60]
    if s:
        return s
    import hashlib
    return "m-" + hashlib.sha1((text or "").encode("utf-8"), usedforsecurity=False).hexdigest()[:10]


def _legacy_slug(text: str) -> str:
    """The file-name rule up to v1.12: ASCII only ("Größe" → "gr-e")."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:60]


def _note_path(directory: Path, name: str) -> Path:
    """The file of note `name` in `directory`. A note saved before v2 under the old
    ASCII name is used when only that file exists, so updating to v2 neither hides
    it nor duplicates it. (v1's catch-all untitled.md is never taken over.)"""
    path = directory / f"{_slug(name)}.md"
    legacy = _legacy_slug(name)
    if not path.exists() and legacy and (directory / f"{legacy}.md").exists():
        return directory / f"{legacy}.md"
    return path


def project_key(name: str) -> str:
    """Normalized project name for matching ("Birdhouse", "birdhouse " → "birdhouse")."""
    return re.sub(r"[^\w]+", "-", (name or "").lower()).strip("-_")[:60]


# Words too common to signal that two titles are about the same thing.
_STOPWORDS = frozenset("""
the and for with from that this into over about your their there what when where
which while will would should could have has had been being were they them then
than also just only more most some such very not are was der die das und mit von
für fuer auf aus bei nach oder aber auch eine einer eines einem einen dass wenn
wie was wer ist sind war hat haben wird werden zum zur über ueber unter noch
""".split())


def _words(text: str) -> list[str]:
    """Lower-case words of at least two letters, any script."""
    return [w for w in re.findall(r"\w+", (text or "").lower()) if len(w) >= 2]


def _significant(text: str) -> set[str]:
    return {w for w in _words(text) if len(w) >= 4 and w not in _STOPWORDS}


def scope_path(scope: str) -> Path:
    """The directory of a scope ("users/<id>" lives in "users_<id>"), guarded
    against traversal — without creating it. Every reader goes through this."""
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", scope or "shared") or "shared"
    return MEMORY_DIR / safe


def _scope_dir(scope: str) -> Path:
    """Resolve (and create) the directory for a scope, guarding against traversal."""
    d = scope_path(scope)
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
                 status: str = "", project: str = "", author: str = "",
                 version: int = 0) -> str:
    # One line per field: a line break inside a value would start a new field and
    # forge the server-stamped author/version (the parser keeps the last one).
    def one(v) -> str:
        return " ".join(str(v).split())

    lines = ["---", f"title: {one(title)}", f"type: {one(type_)}"]
    if project:
        lines.append(f"project: {one(project)}")
    if author:
        lines.append(f"author: {one(author)}")
    if version:
        lines.append(f"version: {int(version)}")
    if tags:
        lines.append("tags: " + ", ".join(one(t) for t in tags))
    lines.append(f"source: {one(source) or 'unknown'}")
    lines.append(f"created: {one(created)}")
    lines.append(f"updated: {one(updated)}")
    if review:
        lines.append(f"review: {one(review)}")
    if status:
        lines.append(f"status: {one(status)}")
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
    split = store.split_frontmatter(text)
    if split:
        meta = _parse_frontmatter(split[0])
        meta.setdefault("title", path.stem)
        meta.setdefault("type", "")
        return meta, split[1].lstrip("\n")
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


# Catalog display tiers, derived from the memory `type` — long-lived → shorter.
# The SHORT-TERM tier ("what we're doing right now") isn't a memory type at all:
# it's the SESSIONS layer (session_save/load), surfaced separately in bootstrap.
_TIER_ORDER = ("user", "project", "feedback", "reference", "")
_TIER_LABEL = {
    "user": "🧭 Core — who/where you are · preferences (long-term)",
    "project": "📂 Projects & focus — recent work & interests (mid-term)",
    "feedback": "🛠 Working style — how to work",
    "reference": "🔗 References — pointers & resources",
    "": "• Other",
}


def scope_tiered_lines(scope_dir: Path) -> list[str]:
    """Memory entries in a scope, GROUPED BY TIER (derived from `type`) for the
    catalog — long-lived Core first, so a fresh LLM sees identity before ephemera.
    Returns lines already indented for bootstrap's per-scope block."""
    buckets: dict[str, list[str]] = {}
    for p in sorted(scope_dir.glob("*.md")):
        meta, _ = read_meta(p)
        t = canon_type(meta.get("type", "")) or ""
        buckets.setdefault(t, []).append(
            f"      - {p.stem} — {meta.get('title', p.stem)}")
    out: list[str] = []
    ordered = list(_TIER_ORDER) + [t for t in buckets if t not in _TIER_ORDER]
    for t in ordered:
        if buckets.get(t):
            out.append(f"    {_TIER_LABEL.get(t, '• ' + t)}")
            out.extend(buckets[t])
    return out


def candidate_count() -> int:
    """How many memory candidates are staged for review."""
    d = MEMORY_DIR / CANDIDATES_SCOPE
    if not d.exists():
        return 0
    return sum(1 for _ in d.glob("*.md"))


def _caller_label() -> str:
    """'person/agent' of the current caller for attribution ('' if unknown)."""
    try:
        import authz
        label = authz.caller_label()
        return "" if label == "unknown" else label
    except Exception:
        return ""


def _version(meta: dict) -> int:
    try:
        return int(meta.get("version") or 0)
    except ValueError:
        return 0


def _overlap_hits(scope: str, title: str, exclude_slug: str) -> list[str]:
    """Existing memories in a scope whose title shares significant words with
    `title` (cheap near-duplicate detector for the dedup hint)."""
    words = _significant(title)
    if not words:
        return []
    hits = []
    for p in sorted(_scope_dir(scope).glob("*.md")):
        if p.stem == exclude_slug:
            continue
        other = _significant(read_meta(p)[0].get("title") or "")
        shared = words & other
        # flag only when the titles share at least half of the shorter one's
        # meaningful words — one common word ("printer") alone isn't a duplicate
        if shared and len(shared) / min(len(words), len(other)) >= 0.5:
            hits.append(p.stem)
    return hits[:5]


def _entry(path: Path, meta: dict, body: str) -> dict:
    first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    return {"name": path.stem, "title": meta.get("title", path.stem),
            "type": canon_type(meta.get("type", "")), "project": meta.get("project", ""),
            "tags": list(meta.get("tags", [])), "review": meta.get("review", ""),
            "version": _version(meta), "author": meta.get("author", ""),
            "source": meta.get("source", ""), "created": meta.get("created", ""),
            "updated": meta.get("updated", ""), "first": first[:110]}


def entries(scope: str) -> list[dict]:
    """Every note of one scope as a dict (name, title, type, project, tags, review,
    version, author, source, created, updated, first line) — for the briefing, the
    WebUI and other callers that need structure rather than text."""
    d = scope_path(scope)
    if not d.is_dir():
        return []
    return [_entry(p, *read_meta(p)) for p in sorted(d.glob("*.md"))]


def search_entries(scope: str, query: str, project: str = "", type_: str = "",
                   tag: str = "", limit: int = 20) -> list[dict]:
    """Notes of `scope` matching the words of `query` (any order), best first — a hit
    in the title counts 3, tags/project 2, the text 1 — each with a `snippet`.
    Without words: every note passing the project/type/tag filters."""
    words = _words(query)
    want_project, want_type = project_key(project), canon_type(type_)
    want_tag = (tag or "").strip().lower()
    scored = []
    for p in sorted(_scope_dir(scope).glob("*.md")):
        meta, body = read_meta(p)
        if want_project and project_key(meta.get("project", "")) != want_project:
            continue
        if want_type and canon_type(meta.get("type", "")) != want_type:
            continue
        tags = [t.lower() for t in meta.get("tags", [])]
        if want_tag and want_tag not in tags:
            continue
        title_w = set(_words(meta.get("title", p.stem)))
        meta_w = set(_words(" ".join(tags) + " " + meta.get("project", "")))
        body_w = set(_words(body))
        score = sum(3 * (w in title_w) + 2 * (w in meta_w) + (w in body_w) for w in words)
        if score or not words:
            snippet = next((ln.strip() for ln in body.splitlines()
                            if ln.strip() and any(w in ln.lower() for w in words)),
                           next((ln.strip() for ln in body.splitlines() if ln.strip()), ""))
            scored.append((-score, p.stem, dict(_entry(p, meta, body), snippet=snippet[:120])))
    scored.sort(key=lambda s: (s[0], s[1]))
    return [e for _, _, e in scored[:max(1, int(limit or 20))]]


def stage_candidate(title: str, content: str, type_: str = "reference",
                    source: str = "auto", tags="") -> str:
    """Write a candidate (not live!) into the reserved candidates scope. Used by
    learn.py (auto-capture) and by the memory_note tool. Returns the slug."""
    t = canon_type(type_) or "reference"
    path = _note_path(_scope_dir(CANDIDATES_SCOPE), title)
    slug = path.stem
    today = _today()
    created = today
    if path.exists():  # keep original created date if we overwrite
        meta, _ = read_meta(path)
        created = meta.get("created", today)
    fm = _frontmatter(title=title, type_=t, tags=_tag_list(tags), source=source,
                      created=created, updated=today, status="candidate")
    store.write_text_atomic(path, f"{fm}\n\n{content.strip()}\n")
    return slug


def register(mcp):
    """Register the memory_* tools on a FastMCP instance."""

    @mcp.tool
    def memory_write(title: Annotated[str, Field(description="Short title; it names the memory")],
                     content: Annotated[str, Field(description="The fact itself, in Markdown")],
                     type: Annotated[str, Field(description="Required — one of the four types above",
                                                json_schema_extra={"enum": list(CANON_TYPES)})],
                     scope: Annotated[str, _SCOPE] = "shared",
                     tags: Annotated[str, Field(description='Comma-separated, e.g. "garden, mqtt"')] = "",
                     source: Annotated[str, Field(description='Where the fact comes from, e.g. a URL; '
                                                              'default "session"')] = "",
                     review: Annotated[str, Field(description="YYYY-MM-DD: from then on cortex_start "
                                                              "lists it as due for review")] = "",
                     project: Annotated[str, Field(description='Project this fact belongs to, e.g. '
                                                               '"Birdhouse"; empty = general')] = "",
                     expected_version: Annotated[int, Field(description="The version you read with "
                                                            "memory_read; refused if it changed since. "
                                                            "0 = don't check")] = 0) -> str:
        """Save a durable, TYPED memory and write it back to the brain.
        type (REQUIRED) = one of: user (who the user is / preferences) ·
        feedback (how you should work — corrections & confirmed approaches) ·
        project (ongoing work, goals, status — give it a `review` date) ·
        reference (pointers to resources/URLs/IDs). An untyped memory is REFUSED
        so the brain stays sorted. Only store DURABLE facts (not session chatter),
        nothing already in code/git, and NEVER secrets (those go to the vault:
        secret_set via integration_call). Writing the same title again REPLACES that
        memory (read it first and fold the old content in); the dedup hint flags
        related titles to merge."""
        t = canon_type(type)
        if not t:
            return ("Refused: a memory MUST be typed (house rule, so the brain "
                    f"stays sorted). Pass type = one of: {', '.join(CANON_TYPES)}. "
                    "Guidance: user = who the user is / preferences · feedback = "
                    "how to work (corrections, confirmed approaches) · project = "
                    "ongoing work/goals (add a review date) · reference = pointers "
                    "to resources/URLs/IDs.")
        path = _note_path(_scope_dir(scope), title)
        slug = path.stem
        today = _today()
        with store.locked(path):
            created, current, last_author = today, 0, ""
            if path.exists():
                meta, _ = read_meta(path)
                created = meta.get("created", today)
                current, last_author = _version(meta), meta.get("author", "")
            if expected_version and int(expected_version) != current:
                return (f"Not saved: '{slug}' was changed since you read it (now version "
                        f"{current}{', by ' + last_author if last_author else ''}). "
                        f"memory_read it again, merge your change and retry with "
                        f"expected_version={current}.")
            version = current + 1
            fm = _frontmatter(title=title, type_=t, tags=_tag_list(tags),
                              source=source or "session", created=created,
                              updated=today, review=review.strip(),
                              project=(project or "").strip(),
                              author=_caller_label(), version=version)
            store.write_text_atomic(path, f"{fm}\n\n{content.strip()}\n")
        hits = _overlap_hits(scope, title, slug)
        note = ""
        if hits:
            note = ("\n⚠ possible overlap with: " + ", ".join(hits)
                    + " — consider merging (memory_read them, fold into one, "
                      "memory_delete the rest) to avoid duplicates.")
        return f"Saved [{t}] memory '{slug}' in scope '{scope}' (version {version}).{note}"

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
    def memory_read(name: Annotated[str, Field(description="The name memory_search returned "
                                                           "(before the ' — ')")],
                    scope: Annotated[str, _SCOPE] = "shared") -> str:
        """Read a memory's full content and version by its name — find the name with
        memory_search. An unknown name returns "No memory named …"."""
        path = _note_path(_scope_dir(scope), name)
        if not path.exists():
            return f"No memory named '{name}' in scope '{scope}'."
        return path.read_text(encoding="utf-8")

    @mcp.tool
    def memory_search(query: Annotated[str, Field(description="Words to look for, any order")],
                      scope: Annotated[str, _SCOPE] = "shared",
                      project: Annotated[str, Field(description="Only this project's memories")] = "",
                      type: Annotated[str, Field(description="Only this type: user, feedback, "
                                                             "project or reference")] = "",
                      tag: Annotated[str, Field(description="Only memories with this tag")] = "",
                      limit: Annotated[int, Field(description="Most results, 1–100")] = 20) -> str:
        """Find memories by words (any order); best matches first — a hit in the
        title counts most, then tags/project, then the text. Returns name, title and
        a snippet; memory_read gives the full text."""
        found = search_entries(scope, query, project=project, type_=type, tag=tag,
                               limit=max(1, min(int(limit or 20), 100)))
        if not found:
            return f"No matches for '{query}' in scope '{scope}'."
        return "\n".join(f"- {e['name']} — {e['title']}: {e['snippet']}" for e in found)

    @mcp.tool
    def memory_delete(name: str, scope: str = "shared") -> str:
        """Delete a memory by its name."""
        path = _note_path(_scope_dir(scope), name)
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
        src = _note_path(_scope_dir(CANDIDATES_SCOPE), name)
        if not src.exists():
            return f"No candidate named '{name}'. Use memory_candidates."
        meta, body = read_meta(src)
        t = canon_type(type) or canon_type(meta.get("type", "")) or "reference"
        title = meta.get("title", name)
        today = _today()
        dst = _note_path(_scope_dir(scope), title)
        fm = _frontmatter(title=title, type_=t, tags=meta.get("tags", []),
                          source=(meta.get("source", "auto") + " · promoted"),
                          created=meta.get("created", today), updated=today)
        store.write_text_atomic(dst, f"{fm}\n\n{body.strip()}\n")
        src.unlink()
        return f"Promoted candidate → [{t}] memory '{dst.stem}' in scope '{scope}'."

    @mcp.tool
    def memory_reject(name: str) -> str:
        """Reject (delete) a memory candidate without promoting it."""
        src = _note_path(_scope_dir(CANDIDATES_SCOPE), name)
        if not src.exists():
            return f"No candidate named '{name}'."
        src.unlink()
        return f"Rejected candidate '{name}'."
