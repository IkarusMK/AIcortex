"""File-based skill router for the MCP server.

Skills live as folders under SKILLS_DIR: ``<skill>/SKILL.md`` (YAML frontmatter
with name/description/tags + Markdown instructions) plus optional resource files.
The router lets the assistant *search* for a relevant skill and load only what it
needs (progressive disclosure). ``skill_write`` lets skills be authored remotely.
"""
import os
import re
from pathlib import Path

import yaml

SKILLS_DIR = Path(os.environ.get("SKILLS_DIR", "/data/skills"))


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:60] or "skill"


def _parse(text: str):
    """Return (meta: dict, body: str) from SKILL.md with optional YAML frontmatter."""
    meta, body = {}, text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            try:
                meta = yaml.safe_load(parts[1]) or {}
            except Exception:
                meta = {}
            body = parts[2].lstrip("\n")
    return (meta if isinstance(meta, dict) else {}), body


def _category(meta: dict) -> str:
    """A skill's category — from `category` or its synonym `cluster`."""
    c = (meta.get("category") or meta.get("cluster") or "").strip()
    return c or "uncategorized"


def register(mcp):
    @mcp.tool
    def skill_search(query: str, category: str = "") -> str:
        """Find skills relevant to a task (ranked name — description). Call this
        before specialized work, then skill_load the best match. Optionally narrow
        to one category (see skill_list for the categories)."""
        q = (query or "").lower()
        cat = (category or "").strip().lower()
        results = []
        for sk in sorted(SKILLS_DIR.glob("*/SKILL.md")):
            meta, body = _parse(sk.read_text(encoding="utf-8"))
            if cat and _category(meta).lower() != cat:
                continue
            hay = f"{sk.parent.name} {meta.get('name','')} {meta.get('description','')} {meta.get('tags','')} {body}".lower()
            score = sum(1 for w in q.split() if w in hay)
            if score:
                results.append((score, sk.parent.name, str(meta.get("description", ""))))
        if not results:
            return f"No skills matched '{query}'. (Use skill_write to add one.)"
        results.sort(reverse=True)
        return "\n".join(f"- {n} — {d}" for _, n, d in results[:10])

    @mcp.tool
    def skill_list(category: str = "") -> str:
        """Without a category: list the CATEGORIES with a skill count each (compact,
        scales to hundreds of skills). With a category: list the skills in it
        (name — description). Use skill_search to find a skill across all categories."""
        items = sorted(SKILLS_DIR.glob("*/SKILL.md"))
        if not items:
            return "No skills yet. Use skill_write to add one."
        cat = (category or "").strip().lower()
        if not cat:
            counts: dict[str, int] = {}
            for sk in items:
                meta, _ = _parse(sk.read_text(encoding="utf-8"))
                counts[_category(meta)] = counts.get(_category(meta), 0) + 1
            lines = [f"- {c} — {n} skill(s)"
                     for c, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
            return (f"{len(items)} skills in {len(counts)} categories — "
                    f"call skill_list(\"<category>\") to see one, or skill_search(query):\n"
                    + "\n".join(lines))
        out = []
        for sk in items:
            meta, _ = _parse(sk.read_text(encoding="utf-8"))
            if _category(meta).lower() == cat:
                out.append(f"- {sk.parent.name} — {meta.get('description', '')}")
        return "\n".join(out) if out else f"No skills in category '{category}'."

    @mcp.tool
    def skill_load(name: str) -> str:
        """Load a skill's full instructions by its name (from skill_search/list)."""
        path = SKILLS_DIR / _slug(name) / "SKILL.md"
        if not path.exists():
            return f"No skill named '{name}'."
        return path.read_text(encoding="utf-8")

    @mcp.tool
    def skill_resource(name: str, filename: str) -> str:
        """Read a resource file bundled with a skill."""
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename or "")
        path = SKILLS_DIR / _slug(name) / safe
        if not path.exists() or not path.is_file():
            return f"No resource '{filename}' in skill '{name}'."
        return path.read_text(encoding="utf-8")

    @mcp.tool
    def skill_write(name: str, description: str, instructions: str, tags: str = "",
                    category: str = "") -> str:
        """Create or update a skill: writes <name>/SKILL.md with frontmatter.
        Pass a category to keep the library organized (so skill_list/bootstrap stay
        compact as the library grows) — reuse an existing one from skill_list."""
        folder = SKILLS_DIR / _slug(name)
        folder.mkdir(parents=True, exist_ok=True)
        cat_line = f"category: {category}\n" if category else ""
        fm = f"---\nname: {name}\ndescription: {description}\n{cat_line}tags: {tags}\n---\n\n"
        (folder / "SKILL.md").write_text(fm + (instructions or "").rstrip() + "\n", encoding="utf-8")
        return f"Saved skill '{folder.name}'{f' [{category}]' if category else ''}."

    @mcp.tool
    def skill_delete(name: str) -> str:
        """Delete a skill and its folder by name."""
        import shutil
        folder = SKILLS_DIR / _slug(name)
        if folder.exists() and folder.is_dir():
            shutil.rmtree(folder)
            return f"Deleted skill '{folder.name}'."
        return f"No skill named '{name}'."
