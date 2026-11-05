"""Entry points: cortex_start (short briefing), bootstrap (its alias), cortex_catalog.

A fresh model only knows what the server instructions and the tool list tell it.
``cortex_start`` gives it a short, caller-specific briefing (see start.py);
``bootstrap`` is kept as an alias so older client setups keep working;
``cortex_catalog`` is the full live list of everything registered — filtered to
what the caller may see — for when the briefing isn't enough.
"""
import json
import os
import re
from pathlib import Path
from typing import Annotated

from pydantic import Field

import sessions
import store
import memory
import coordination
import catalog_cache
import version

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", "/data/memory"))
SKILLS_DIR = Path(os.environ.get("SKILLS_DIR", "/data/skills"))
SERVICES_DIR = Path(os.environ.get("SERVICES_DIR", "/data/services"))
MQTT_DIR = Path(os.environ.get("MQTT_DIR", "/data/mqtt"))
FTP_DIR = Path(os.environ.get("FTP_DIR", "/data/ftp"))
MCP_DIR = Path(os.environ.get("MCP_DIR", "/data/mcp"))
CRON_DIR = Path(os.environ.get("CRON_DIR", "/data/cron"))
WEBHOOK_DIR = Path(os.environ.get("WEBHOOK_DIR", "/data/webhooks"))
PRINT_DIR = Path(os.environ.get("PRINT_DIR", "/data/printers"))
SCAN_DIR = Path(os.environ.get("SCAN_DIR", "/data/scanners"))
WEBDAV_DIR = Path(os.environ.get("WEBDAV_DIR", "/data/webdav"))
CALDAV_DIR = Path(os.environ.get("CALDAV_DIR", "/data/caldav"))
SSH_DIR = Path(os.environ.get("SSH_DIR", "/data/ssh"))
MAIL_DIR = Path(os.environ.get("MAIL_DIR", "/data/mail"))
IMAP_DIR = Path(os.environ.get("IMAP_DIR", "/data/imap"))


def _json_names(d: Path, *, fields: tuple[str, ...] = ("description",),
                allowed=None) -> list[str]:
    """Generic listing of *.json config entries: '<stem> — <field> …'.
    ``allowed(name)`` hides entries the caller may not use."""
    out = []
    for p in sorted(d.glob("*.json")):
        if allowed is not None and not allowed(p.stem):
            continue
        extra = ""
        try:
            c = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(c, dict):
                vals = [str(c[f]) for f in fields if c.get(f)]
                extra = " — " + " · ".join(vals) if vals else ""
        except Exception:
            extra = " — (unreadable)"
        out.append(f"  - {p.stem}{extra}")
    return out


def _json_grouped(d: Path, *, fields: tuple[str, ...] = ("description",),
                  allowed=None) -> list[str]:
    """Like _json_names, but if any config carries a `category`, GROUP the entries
    by it (the same tidy, skill-style catalog — faster to scan). Falls back to a
    flat list when nothing is categorized, so uncategorized sections stay clean."""
    entries: list[tuple[str, str]] = []  # (category, flat line)
    any_cat = False
    for p in sorted(d.glob("*.json")):
        cat, extra = "", ""
        try:
            c = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(c, dict):
                cat = str(c.get("category", "") or "").strip()
                if allowed is not None and not allowed(p.stem, cat):
                    continue
                if cat:
                    any_cat = True
                vals = [str(c[f]) for f in fields if c.get(f)]
                extra = " — " + " · ".join(vals) if vals else ""
        except Exception:
            extra = " — (unreadable)"
        entries.append((cat or "Uncategorized", f"  - {p.stem}{extra}"))
    if not any_cat:
        return [line for _, line in entries]
    out: list[str] = []
    cats = sorted({c for c, _ in entries}, key=lambda c: (c == "Uncategorized", c.lower()))
    for cat in cats:
        out.append(f"  [{cat}]")
        out.extend("  " + line for c, line in entries if c == cat)
    return out


def _skill_list() -> list[str]:
    """Compact skill overview that scales: list names while the library is small,
    collapse to per-category counts once it grows (so bootstrap stays cheap even
    with hundreds of skills). Categories come from the `category`/`cluster` field."""
    skills = sorted(SKILLS_DIR.glob("*/SKILL.md"))
    if not skills:
        return []
    counts: dict[str, int] = {}
    for sk in skills:
        cat = "uncategorized"
        try:
            text = sk.read_text(encoding="utf-8")
            split = store.split_frontmatter(text)
            if split:
                m = re.search(r'^(?:category|cluster):\s*"?([^"\n]+)"?',
                              split[0], re.MULTILINE)
                if m:
                    cat = m.group(1).strip()
        except Exception:
            pass
        counts[cat] = counts.get(cat, 0) + 1
    if len(skills) <= 12:
        return [f"  - {sk.parent.name}" for sk in skills]
    lines = [f"  {c}: {n}" for c, n in
             sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    lines.append(f"  → {len(skills)} skills total · "
                 f"skill_list(\"<category>\") or skill_search(query) to drill in")
    return lines


def _agents() -> list[str]:
    try:
        return coordination.agent_rows()
    except Exception:
        return []


def _board() -> list[str]:
    try:
        return coordination.board_overview()
    except Exception:
        return []


def _section(title: str, lines: list[str], empty: str) -> str:
    return f"{title}:\n" + ("\n".join(lines) if lines else f"  ({empty})")


def _catalog() -> str:
    """A live snapshot of everything currently on the NAS brain.

    Sections that READ FILES (memory scopes, skills, the JSON service/device
    registries) are served through ``catalog_cache``: their rendered lines are reused
    while the source directory's cheap stat signature is unchanged, so a warm bootstrap
    opens ~no files (this is what turns a minutes-long cold scan into milliseconds and
    keeps it fast as the brain fills up). Sections that are cheap or change on every
    save (candidate count, recent sessions, team presence, task board) stay LIVE."""
    import start
    import tenancy
    ident, role, scope = start._caller()

    # Registries are small; they're listed live and filtered to what THIS caller
    # may use (services by service grant, devices by endpoint grant).
    def _cj(kind: str, d: Path, fields: tuple, grouped: bool = False) -> list[str]:
        if grouped:
            return _json_grouped(d, fields=fields,
                                 allowed=lambda name, cat: tenancy.caller_service_allowed(name, cat))
        return _json_names(d, fields=fields,
                           allowed=lambda name: tenancy.endpoint_allowed(ident, role, kind, name))

    # Only the caller's own memory area (never other people's titles). Reading the
    # files is the cost, so it's cached per scope by the directory's stat signature.
    scope_dir = memory.scope_path(scope)

    def _render_mem_scope() -> list[str]:
        if not scope_dir.is_dir():
            return []
        return memory.scope_tiered_lines(scope_dir)

    mem_lines = list(catalog_cache.cached_lines(
        f"memory:{scope}", [scope_dir], "*.md", _render_mem_scope))

    # Auto-memory: candidates awaiting review — always LIVE (never cached), so a freshly
    # staged candidate always shows. Appended after the cached scope lines.
    try:
        n_cand = memory.candidate_count()
    except Exception:
        n_cand = 0
    if n_cand:
        mem_lines.append(f"  📥 {n_cand} candidate(s) awaiting review → "
                         f"memory_candidates() · memory_promote/memory_reject")

    # Where we left off: surface the most recent sessions so a fresh LLM (or a
    # different model entirely) can continue exactly where another stopped. Cheap and
    # changes on every save → kept live.
    resume = ""
    try:
        recent = sessions.recent(5)
        if recent:
            blocks = ["===== RESUME — recent sessions (newest first) ====="]
            for s in recent:
                last = (s.get("entries") or [{}])[-1]
                blocks.append(
                    f"• {sessions.summary_line(s)}\n"
                    f"    last: {last.get('summary', '')}"
                    + (f"\n    NEXT: {last['next_steps']}" if last.get("next_steps") else "")
                )
            blocks.append("→ session_load(<id>) for full history (no id = the newest); "
                          "session_save to add your own checkpoint.")
            resume = "\n".join(blocks)
    except Exception:
        resume = ""

    parts = [
        f"===== LIVE BRAIN CATALOG (AICortex v{version.__version__} · current NAS state) =====",
        _section("MEMORY (facts about the user & projects)", mem_lines,
                 "empty — nothing stored yet; capture facts with memory_write"),
        _section("SKILLS (reusable know-how)",
                 catalog_cache.cached_lines("skills", [SKILLS_DIR], "*/SKILL.md", _skill_list),
                 "none yet — author with skill_write"),
        _section("SERVICES (HTTP integrations)",
                 _cj("service", SERVICES_DIR, ("base_url", "description"), grouped=True),
                 "none — add with service_add"),
        _section("MQTT DEVICES", _cj("mqtt", MQTT_DIR, ("host", "description")),
                 "none — add with mqtt_add"),
        _section("FTP/FTPS ENDPOINTS", _cj("ftp", FTP_DIR, ("host", "description")),
                 "none — add with ftp_add"),
        _section("WEBDAV ENDPOINTS", _cj("webdav", WEBDAV_DIR, ("base_url", "description")),
                 "none — add with webdav_add"),
        _section("CALDAV ENDPOINTS", _cj("caldav", CALDAV_DIR, ("base_url", "description")),
                 "none — add with caldav_add"),
        _section("SSH HOSTS", _cj("ssh", SSH_DIR, ("host", "description")),
                 "none — add with ssh_add"),
        _section("SMTP ACCOUNTS", _cj("mail", MAIL_DIR, ("from_addr", "host")),
                 "none — add with mail_add"),
        _section("IMAP ACCOUNTS", _cj("imap", IMAP_DIR, ("username", "host")),
                 "none — add with imap_add"),
        _section("MCP SERVERS (gateway)", _cj("mcp", MCP_DIR, ("url", "description")),
                 "none — add with mcp_add"),
        _section("PRINTERS (IPP)", _cj("print", PRINT_DIR, ("host", "description")),
                 "none — add with print_add"),
        _section("SCANNERS (eSCL)", _cj("scan", SCAN_DIR, ("host", "description")),
                 "none — add with scan_add"),
        _section("SCHEDULED JOBS (cron)", _cron_lines(),
                 "none — add with cron_add"),
        _section("INBOUND WEBHOOKS (POST /hooks/<name> → inbox)",
                 _json_names(WEBHOOK_DIR, fields=("notify", "description")),
                 "none — add with webhook_add"),
        _section("TEAM (agents — live presence, online first)", _agents(),
                 "none registered — agent_register(name, role, capabilities)"),
        _section("TASK BOARD (shared work — claim/handoff)", _board(),
                 "no active tasks — task_add to create; task_next to pull work"),
        "",
        "Details: memory_read, skill_load, service_list. Save durable facts with "
        "memory_write(type, project); review candidates with memory_promote / "
        "memory_reject; checkpoint with session_save before you stop.",
    ]
    body = "\n\n".join(parts)
    catalog_cache.flush()   # persist any re-rendered sections for the next bootstrap
    return (resume + "\n\n" + body) if resume else body


def _cron_lines() -> list[str]:
    """The caller's scheduled jobs (an admin: all), one line each."""
    try:
        import cron
        jobs = cron.visible_jobs()
    except Exception:
        return ["  (cron store unreadable — see cron_list)"]
    return [f"  - {j.get('id')} — {j.get('schedule')} · {str(j.get('prompt', ''))[:80]}"
            for j in jobs if isinstance(j, dict)]


_PROJECT = Field(description='Project the conversation is about, e.g. "Birdhouse"; '
                                'empty = list the known projects')
_TASK = Field(description="What you are about to do, in a few words; finds matching "
                          "skills and integrations")


def register(mcp):
    import start

    @mcp.tool
    def cortex_start(project: Annotated[str, _PROJECT] = "",
                     task: Annotated[str, _TASK] = "") -> str:
        """Call this first in every conversation. Returns a briefing: who the user is
        and how they want you to work; for `project` its notes, open tasks and last
        session (a project with no notes yet says so); for `task` matching skills and
        integrations. Without arguments: that profile and the known projects. At most
        4,000 characters — longer lists are cut and marked "shortened". bootstrap is
        the same for older setups; later in the conversation use memory_search for a
        fact and task_list for the task board. Example:
        cortex_start(project="Birdhouse", task="slice a bracket for the printer")."""
        return start.build(project, task)

    @mcp.tool
    def bootstrap(project: Annotated[str, _PROJECT] = "",
                  task: Annotated[str, _TASK] = "") -> str:
        """Start-of-conversation briefing, identical to cortex_start: who the user is,
        how they want you to work and the project's notes, tasks and last session.
        Kept so project instructions that say "call bootstrap" keep working; new
        setups call cortex_start."""
        return start.build(project, task)

    @mcp.tool
    def cortex_catalog() -> str:
        """Everything registered that you may use: your memories by type, skill
        categories, services, devices, scheduled jobs, webhooks, agents, the task
        board and recent sessions. Longer than cortex_start — call it when you need
        the full picture."""
        return _catalog()
