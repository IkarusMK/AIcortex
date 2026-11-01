"""cortex_start — the one call a model makes at the start of a conversation.

Returns a short briefing for THIS caller (at most MAX_CHARS): who the user is,
how they want the assistant to work, and — for the given project — its memories,
open tasks and latest session; plus skills and integrations that match the task.
Everything bigger stays one call away (memory_search, skill_search, cortex_catalog),
so starting a conversation costs a few hundred tokens, not tens of thousands.

Only what the caller may see is used: memories come from the caller's own memory
area (tenancy), skills and integrations are filtered by the caller's grants.

Every general working rule (a feedback note without a project) is named, at least by
title — pinned ones first and with their text; a rule that belongs to a project only
shows up when that project is worked on.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import coordination
import memory
import playbooks
import sessions
import skills
import version

MAX_CHARS = 4000
# A note tagged with one of these is shown first, with its text.
PINNED_TAGS = frozenset({"pinned", "important", "verbindlich", "wichtig"})


def _caller():
    """(identity, role, memory scope) of the current caller; safe fallbacks."""
    try:
        import authz
        import tenancy
        ident, role = authz.effective_identity()
        return ident, role, tenancy.confine_memory_scope(ident, role, "shared")
    except Exception:
        return "unknown", "user", "shared"


def _clip(lines: list[str], budget: int) -> list[str]:
    """Keep whole lines while they fit in `budget` characters; note what was cut."""
    kept, used = [], 0
    for i, ln in enumerate(lines):
        if used + len(ln) + 1 > budget:
            kept.append(f"  … {len(lines) - i} more")
            break
        kept.append(ln)
        used += len(ln) + 1
    return kept


def _block(title: str, lines: list[str], budget: int) -> str:
    return title + "\n" + "\n".join(_clip(lines, budget)) if lines else ""


def _pinned(m: dict) -> bool:
    return any(str(t).strip().lower() in PINNED_TAGS for t in m.get("tags") or [])


def _names(ms: list[dict], budget: int) -> str:
    """The titles of `ms` on one line, as many as fit in `budget`."""
    head, kept = "  - also: ", []
    used = len(head)
    for i, m in enumerate(ms):
        if used + len(m["title"]) + 3 > budget:
            return head + " · ".join(kept) + f" … {len(ms) - i} more"
        kept.append(m["title"])
        used += len(m["title"]) + 3
    return head + " · ".join(kept)


def _named_block(title: str, ms: list[dict], line, budget: int) -> str:
    """Pinned notes first; full lines while the titles of the rest still fit, then
    the rest by title — so every note is at least named."""
    if not ms:
        return ""
    ms = sorted(ms, key=lambda m: (not _pinned(m), m["title"].lower()))
    out, used = [], 0
    for i, m in enumerate(ms):
        rest = ms[i + 1:]
        full = line(m)
        reserve = len(_names(rest, budget)) + 1 if rest else 0
        if used + len(full) + 1 + reserve > budget:
            out.append(_names(ms[i:], budget - used))
            break
        out.append(full)
        used += len(full) + 1
    return title + "\n" + "\n".join(out)


def _words(text: str) -> set[str]:
    return {w for w in "".join(c if c.isalnum() else " " for c in (text or "").lower()).split()
            if len(w) >= 3}


def _integrations(task: str, ident: str, role: str) -> list[str]:
    """Registered services/devices whose name, category or description match the task."""
    import bootstrap
    import tenancy
    want = _words(task)
    if not want:
        return []
    kinds = [("service", bootstrap.SERVICES_DIR), ("mqtt", bootstrap.MQTT_DIR),
             ("ftp", bootstrap.FTP_DIR), ("webdav", bootstrap.WEBDAV_DIR),
             ("caldav", bootstrap.CALDAV_DIR), ("print", bootstrap.PRINT_DIR),
             ("scan", bootstrap.SCAN_DIR), ("ssh", bootstrap.SSH_DIR),
             ("mail", bootstrap.MAIL_DIR), ("imap", bootstrap.IMAP_DIR),
             ("mcp", bootstrap.MCP_DIR)]
    out = []
    for kind, d in kinds:
        for p in sorted(Path(d).glob("*.json")):
            try:
                cfg = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            desc = str(cfg.get("description", "") or "")
            cat = str(cfg.get("category", "") or "")
            if not (want & _words(f"{p.stem} {cat} {desc}")):
                continue
            allowed = (tenancy.caller_service_allowed(p.stem, cat) if kind == "service"
                       else tenancy.endpoint_allowed(ident, role, kind, p.stem))
            if allowed:
                out.append(f"  - {kind} {p.stem}" + (f" — {desc[:90]}" if desc else ""))
    return out


def build(project: str = "", task: str = "") -> str:
    ident, role, scope = _caller()
    mems = memory.entries(scope)
    key = memory.project_key(project)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    parts = [f"AICortex v{version.__version__} — briefing"
             + (f" for project '{project}'" if key else "")
             + "\nReply in the user's language, even though this briefing is in English."]

    def line(m):
        return f"  - {m['title']}" + (f": {m['first']}" if m["first"] else "")

    def general(type_):
        return [m for m in mems if m["type"] == type_ and not memory.project_key(m["project"])]

    parts.append(_block("About the user:", [line(m) for m in mems if m["type"] == "user"], 700))
    parts.append(_named_block("How they want you to work:", general("feedback"), line, 1400))
    refs = general("reference")
    if refs:
        parts.append("Reference notes (memory_read by name):\n"
                     + _names(sorted(refs, key=lambda m: m["title"].lower()), 300))

    if key:
        pm = [line(m) for m in mems if memory.project_key(m["project"]) == key]
        parts.append(_block(f"Project notes ({project}):", pm, 900)
                     or f"Project notes ({project}): none yet — save them with "
                        f"memory_write(project=\"{project}\").")
        tasks = [f"  - [{t.get('status')}] {t.get('title')} (id {t.get('id')})"
                 for t in coordination.open_tasks(project)]
        parts.append(_block("Open tasks:", tasks, 350))
        runs = [f"  - {r['title']} ({r['playbook']}, run {r['id']}): {playbooks.next_step(r)}"
                for r in playbooks.runs_for(project=project)]
        parts.append(_block("Playbook runs (playbook_status for details):", runs, 450))
        s = sessions.latest_for_project(key)
        if s:
            last = (s.get("entries") or [{}])[-1]
            parts.append(_block("Last session:", [
                f"  - {s.get('id')} ({(s.get('updated') or '')[:10]}): {last.get('summary', '')[:160]}"]
                + ([f"    next: {last['next_steps'][:140]}"] if last.get("next_steps") else []), 350))
    else:
        names = sorted({m["project"].strip() for m in mems if m["project"].strip()}
                       | {t.get("project", "").strip() for t in coordination.open_tasks()
                          if t.get("project", "").strip()}, key=str.lower)
        if names:
            parts.append(_block("Known projects (call cortex_start with one of these):",
                                [f"  - {n}" for n in names], 400))

    if (task or "").strip():
        found = skills.search(task, limit=5)
        parts.append(_block("Skills for this task (skill_load to read one):",
                            [f"  - {n} — {d[:90]}" for _, n, d in found], 500))
        parts.append(_block("Integrations for this task:", _integrations(task, ident, role), 350))

    due = [f"  - {m['title']} (review {m['review']})" for m in mems
           if m["review"] and m["review"][:10] <= today]
    parts.append(_block("Due for review (update or confirm):", due, 300))

    parts.append("More: memory_search(query, project) · skill_search(query) · "
                 "cortex_catalog() for everything registered.")
    text = "\n\n".join(p for p in parts if p)
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS - 20] + "\n… (shortened)"
