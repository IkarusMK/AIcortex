"""File-based skill router for the MCP server.

Skills live as folders under SKILLS_DIR: ``<skill>/SKILL.md`` (YAML frontmatter
with name/description/tags + Markdown instructions) plus optional resource files.
The router lets the assistant *search* for a relevant skill and load only what it
needs (progressive disclosure). ``skill_write`` lets skills be authored remotely.
"""
import os
import re
from pathlib import Path
from typing import Annotated

import store

import yaml
from pydantic import Field

SKILLS_DIR = Path(os.environ.get("SKILLS_DIR", "/data/skills"))
# Starter skills: the image carries them next to the code (Dockerfile), a source
# checkout reads them from examples/skills.
_HERE = Path(__file__).resolve().parent
EXAMPLES_DIR = Path(os.environ.get("SKILL_EXAMPLES_DIR") or next(
    (p for p in (_HERE / "skill_examples", _HERE.parent / "examples" / "skills") if p.is_dir()),
    _HERE / "skill_examples"))


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:60] or "skill"


def _fallback_parse(block: str) -> dict:
    """Tolerant line-based frontmatter parse (same approach as memory.py):
    ``key: value`` per line, split on the FIRST colon only — so values that
    themselves contain colons (e.g. 'Quelle: Paul Hudson') survive intact.
    Used when strict YAML parsing fails, so no skill ever silently loses its
    category/description just because a value wasn't YAML-quoted."""
    meta: dict = {}
    for ln in block.strip().splitlines():
        if ":" in ln:
            k, v = ln.split(":", 1)
            k = k.strip().lower()
            if k:
                meta[k] = v.strip().strip('"').strip("'")
    return meta


def _parse(text: str):
    """Return (meta: dict, body: str) from SKILL.md with optional YAML frontmatter.

    Strict YAML first; if that fails (e.g. an unquoted ': ' inside a value),
    fall back to tolerant line-based parsing instead of dropping the metadata."""
    meta, body = {}, text
    split = store.split_frontmatter(text)
    if split:
        try:
            meta = yaml.safe_load(split[0]) or {}
        except Exception:
            meta = {}
        if not isinstance(meta, dict) or not meta:
            meta = _fallback_parse(split[0])
        body = split[1].lstrip("\n")
    return (meta if isinstance(meta, dict) else {}), body


def _frontmatter(name: str, description: str, category: str, tags: str,
                 author: str = "") -> str:
    """Serialize skill frontmatter as guaranteed-valid YAML. ``yaml.safe_dump``
    quotes values automatically when needed (colons, quotes, '#', leading
    symbols, unicode), so arbitrary descriptions can never corrupt the file."""
    fields = {
        "name": " ".join(str(name or "").split()),
        "description": " ".join(str(description or "").split()),
        "category": " ".join(str(category or "").split()),
        "tags": " ".join(str(tags or "").split()),
    }
    if author:
        fields["author"] = author
    dumped = yaml.safe_dump(fields, sort_keys=False, allow_unicode=True,
                            default_flow_style=False, width=100000)
    return f"---\n{dumped}---\n\n"


def _category(meta: dict) -> str:
    """A skill's category — from `category` or its synonym `cluster`."""
    c = str(meta.get("category") or meta.get("cluster") or "").strip()
    return c or "uncategorized"


def _existing_categories() -> list[str]:
    """The distinct categories currently in the library (sorted), excluding the
    'uncategorized' fallback — used to nudge authors to reuse an existing one."""
    cats = set()
    for sk in SKILLS_DIR.glob("*/SKILL.md"):
        try:
            meta, _ = _parse(sk.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        c = _category(meta)
        if c and c != "uncategorized":
            cats.add(c)
    return sorted(cats)


def _canonical_category(category: str) -> str:
    """Normalize a category and snap it onto an existing one if it matches
    case-insensitively — so 'Trading', 'trading' and ' trading ' never split the
    library into near-duplicate buckets."""
    cat = re.sub(r"\s+", " ", (category or "")).strip()
    if not cat:
        return ""
    low = cat.lower()
    for existing in _existing_categories():
        if existing.lower() == low:
            return existing  # reuse the spelling already in the library
    return cat


_INDEX: dict = {}   # SKILL.md path -> (mtime_ns, meta, body); re-read only when changed


def examples() -> list[dict]:
    """The bundled starter skills: name, description, category and whether the
    library already has a skill of that name."""
    out = []
    if not EXAMPLES_DIR.is_dir():
        return out
    for folder in sorted(EXAMPLES_DIR.iterdir()):
        page = folder / "SKILL.md"
        if not (folder.is_dir() and page.is_file()):
            continue
        meta, _ = _parse(page.read_text(encoding="utf-8", errors="replace"))
        name = _slug(str(meta.get("name") or folder.name))
        out.append({"name": name, "description": str(meta.get("description", "")),
                    "category": _category(meta), "source": folder,
                    "installed": (SKILLS_DIR / name / "SKILL.md").exists()})
    return out


def install_examples() -> tuple[list[str], list[str]]:
    """Copy every starter skill the library doesn't have yet. A skill of the same
    name — possibly edited — is never overwritten. Returns (added, skipped)."""
    import shutil
    added, skipped = [], []
    for ex in examples():
        if ex["installed"]:
            skipped.append(ex["name"])
            continue
        shutil.copytree(ex["source"], SKILLS_DIR / ex["name"])
        added.append(ex["name"])
    return added, skipped


def _indexed() -> list[tuple]:
    """[(folder name, meta, body)] for every skill, from a cache keyed by mtime —
    searching hundreds of skills no longer re-reads every file on every call."""
    out, seen = [], set()
    for sk in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        key = str(sk)
        seen.add(key)
        try:
            mtime = sk.stat().st_mtime_ns
        except OSError:
            continue
        hit = _INDEX.get(key)
        if not hit or hit[0] != mtime:
            meta, body = _parse(sk.read_text(encoding="utf-8", errors="replace"))
            hit = _INDEX[key] = (mtime, meta, body)
        out.append((sk.parent.name, hit[1], hit[2]))
    for gone in set(_INDEX) - seen:
        _INDEX.pop(gone, None)
    return out


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", (text or "").lower()) if len(w) >= 3}


def search(query: str, category: str = "", limit: int = 10) -> list[tuple]:
    """Skills relevant to `query` that the caller may use: [(score, name, description)],
    best first. Per query word: name 4, description 3, tags 2, body 1 — so a skill
    ABOUT the topic beats one that merely mentions it. Words under 3 letters are
    ignored (they match everything)."""
    import tenancy
    words = _words(query)
    cat = (category or "").strip().lower()
    results = []
    for folder, meta, body in _indexed():
        if cat and _category(meta).lower() != cat:
            continue
        # Per-user capability area: hide skills this caller may not use.
        if not tenancy.caller_skill_allowed(folder, _category(meta)):
            continue
        name_w = _words(f"{folder.replace('-', ' ')} {meta.get('name', '')}")
        desc_w, tag_w, body_w = (_words(str(meta.get("description", ""))),
                                 _words(str(meta.get("tags", ""))), _words(body))
        score = sum(4 * (w in name_w) + 3 * (w in desc_w) + 2 * (w in tag_w) + (w in body_w)
                    for w in words)
        if score:
            results.append((score, folder, str(meta.get("description", ""))))
    results.sort(key=lambda r: (-r[0], r[1]))
    return results[:limit]


def _may_change(folder: Path) -> tuple[bool, str]:
    """Shared skills belong to everyone: with enforcement on, only an admin or the
    skill's own author may overwrite or delete an existing one."""
    try:
        import authz
        if not authz.enabled() or not (folder / "SKILL.md").exists():
            return True, ""
        ident, role = authz.effective_identity()
        if role == "admin":
            return True, ""
        meta, _ = _parse((folder / "SKILL.md").read_text(encoding="utf-8", errors="replace"))
        author = str(meta.get("author", ""))
        if author and author.split("/")[0] == ident:
            return True, ""
        return False, (f"Refused: '{folder.name}' is a shared skill. Ask an admin to change "
                       f"it, or save your variant under another name.")
    except Exception:
        return True, ""


def register(mcp):
    @mcp.tool
    def skill_search(query: str,
                     category: Annotated[str, Field(description="Only skills in this category; "
                                                                "empty = all")] = "") -> str:
        """Find skills relevant to a task (ranked name — description). Call this
        before specialized work, then skill_load the best match."""
        results = search(query, category)
        if not results:
            return f"No skills matched '{query}'. (Use skill_write to add one.)"
        return "\n".join(f"- {n} — {d}" for _, n, d in results)

    @mcp.tool
    def skill_list(category: str = "") -> str:
        """Without a category: list the CATEGORIES with a skill count each (compact,
        scales to hundreds of skills). With a category: list the skills in it
        (name — description). Use skill_search to find a skill across all categories."""
        import tenancy
        items = sorted(SKILLS_DIR.glob("*/SKILL.md"))
        if not items:
            return "No skills yet. Use skill_write to add one."
        cat = (category or "").strip().lower()
        if not cat:
            counts: dict[str, int] = {}
            total = 0
            for sk in items:
                meta, _ = _parse(sk.read_text(encoding="utf-8", errors="replace"))
                c = _category(meta)
                if not tenancy.caller_skill_allowed(sk.parent.name, c):
                    continue
                counts[c] = counts.get(c, 0) + 1
                total += 1
            if not counts:
                return ("No skills available to you. An admin can grant access with "
                        "tenancy_set(identity, skills=...).")
            lines = [f"- {c} — {n} skill(s)"
                     for c, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
            return (f"{total} skills in {len(counts)} categories — "
                    f"call skill_list(\"<category>\") to see one, or skill_search(query):\n"
                    + "\n".join(lines))
        out = []
        for sk in items:
            meta, _ = _parse(sk.read_text(encoding="utf-8", errors="replace"))
            if _category(meta).lower() == cat and tenancy.caller_skill_allowed(
                    sk.parent.name, _category(meta)):
                out.append(f"- {sk.parent.name} — {meta.get('description', '')}")
        return "\n".join(out) if out else f"No skills in category '{category}'."

    @mcp.tool
    def skill_load(name: Annotated[str, Field(description="The name skill_search returned "
                                                          "(before the ' — ')")]) -> str:
        """Load a skill's full instructions by its name — find the name with skill_search."""
        import tenancy
        path = SKILLS_DIR / _slug(name) / "SKILL.md"
        if not path.exists():
            return f"No skill named '{name}'."
        text = path.read_text(encoding="utf-8", errors="replace")
        meta, _ = _parse(text)
        if not tenancy.caller_skill_allowed(path.parent.name, _category(meta)):
            return (f"Denied: skill '{name}' is not in your allowed set. An admin "
                    f"can grant it with tenancy_set(identity, skills=...).")
        return text

    @mcp.tool
    def skill_resource(name: str, filename: str) -> str:
        """Read a resource file bundled with a skill."""
        import tenancy
        folder = SKILLS_DIR / _slug(name)
        cat = ""
        md = folder / "SKILL.md"
        if md.exists():
            try:
                meta, _ = _parse(md.read_text(encoding="utf-8", errors="replace"))
                cat = _category(meta)
            except Exception:
                cat = ""
        if not tenancy.caller_skill_allowed(folder.name, cat):
            return (f"Denied: skill '{name}' is not in your allowed set. An admin "
                    f"can grant it with tenancy_set(identity, skills=...).")
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename or "")
        path = folder / safe
        if not path.exists() or not path.is_file():
            return f"No resource '{filename}' in skill '{name}'."
        return path.read_text(encoding="utf-8", errors="replace")

    @mcp.tool
    def skill_write(name: str, description: str, instructions: str, tags: str = "",
                    category: str = "") -> str:
        """Create or update a skill: writes <name>/SKILL.md with frontmatter
        (name, description, category, tags + Markdown instructions).

        HOUSE RULE — every skill MUST have a category. This keeps the shared
        library organized and keeps skill_list/bootstrap compact as it grows to
        hundreds of skills. REUSE an existing category (call skill_list first to see
        them); only invent a new one when nothing fits. A missing category is
        REFUSED — re-call with category="…". Categories are matched case-
        insensitively, so you can't accidentally split one into near-duplicates."""
        category = _canonical_category(category)
        if not category:
            existing = _existing_categories()
            hint = ("Reuse one of these existing categories: "
                    + ", ".join(existing)) if existing else (
                    "No categories yet — start a clear one, e.g. 'trading', "
                    "'home-automation', 'devops', 'documents'.")
            return ("Refused: a skill MUST be categorized (house rule — keeps the "
                    "library tidy and bootstrap compact). Re-call skill_write with "
                    f"category=\"…\". {hint}")
        folder = SKILLS_DIR / _slug(name)
        ok, why = _may_change(folder)
        if not ok:
            return why
        folder.mkdir(parents=True, exist_ok=True)
        try:
            import authz
            author = authz.caller_label()
        except Exception:
            author = ""
        fm = _frontmatter(name, description, category, tags,
                          author="" if author == "unknown" else author)
        store.write_text_atomic(folder / "SKILL.md", fm + (instructions or "").rstrip() + "\n")
        return f"Saved skill '{folder.name}' [{category}]."

    @mcp.tool
    def skill_delete(name: str) -> str:
        """Delete a skill and its folder by name."""
        import shutil
        folder = SKILLS_DIR / _slug(name)
        ok, why = _may_change(folder)
        if not ok:
            return why
        if folder.exists() and folder.is_dir():
            shutil.rmtree(folder)
            return f"Deleted skill '{folder.name}'."
        return f"No skill named '{name}'."
