"""Single 'START HERE' entrypoint that loads the whole brain in one call.

The core problem with any MCP connector: the client (a fresh LLM on phone or
desktop) does NOT auto-call tools or auto-read the server ``instructions`` on
connect — it just sees a tool list and waits. So every session starts "blank".

The one lever that reliably reaches the model through every client is a TOOL
DESCRIPTION. ``bootstrap`` exploits that: its description is an imperative ("call
this FIRST"), and a single call returns the operating guide PLUS a live catalog
of everything the LLM has — identity memories, skills, services, devices and
scheduled jobs — so the model is oriented in one round-trip instead of probing
tool by tool.
"""
import json
import os
import re
from pathlib import Path

import guide
import sessions
import memory
import coordination
import version

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", "/data/memory"))
SKILLS_DIR = Path(os.environ.get("SKILLS_DIR", "/data/skills"))
SERVICES_DIR = Path(os.environ.get("SERVICES_DIR", "/data/services"))
MQTT_DIR = Path(os.environ.get("MQTT_DIR", "/data/mqtt"))
FTP_DIR = Path(os.environ.get("FTP_DIR", "/data/ftp"))
MCP_DIR = Path(os.environ.get("MCP_DIR", "/data/mcp"))
CRON_DIR = Path(os.environ.get("CRON_DIR", "/data/cron"))
PRINT_DIR = Path(os.environ.get("PRINT_DIR", "/data/printers"))
SCAN_DIR = Path(os.environ.get("SCAN_DIR", "/data/scanners"))
WEBDAV_DIR = Path(os.environ.get("WEBDAV_DIR", "/data/webdav"))
CALDAV_DIR = Path(os.environ.get("CALDAV_DIR", "/data/caldav"))
SSH_DIR = Path(os.environ.get("SSH_DIR", "/data/ssh"))
MAIL_DIR = Path(os.environ.get("MAIL_DIR", "/data/mail"))
IMAP_DIR = Path(os.environ.get("IMAP_DIR", "/data/imap"))


def _json_names(d: Path, *, fields: tuple[str, ...] = ("description",)) -> list[str]:
    """Generic listing of *.json config entries: '<stem> — <field> …'."""
    out = []
    for p in sorted(d.glob("*.json")):
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


def _json_grouped(d: Path, *, fields: tuple[str, ...] = ("description",)) -> list[str]:
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
            if text.startswith("---"):
                parts = text.split("---", 2)
                if len(parts) == 3:
                    m = re.search(r'^(?:category|cluster):\s*"?([^"\n]+)"?',
                                  parts[1], re.MULTILINE)
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
    """A live snapshot of everything currently on the NAS brain."""
    # All memory scopes, shared first. The reserved 'candidates' scope is NOT
    # listed here (it's not live memory) — it's surfaced separately below.
    mem_lines: list[str] = []
    if MEMORY_DIR.exists():
        scopes = sorted(
            (d for d in MEMORY_DIR.iterdir()
             if d.is_dir() and d.name != memory.CANDIDATES_SCOPE),
            key=lambda d: (d.name != "shared", d.name),
        )
        for sc in scopes:
            entries = memory.scope_tiered_lines(sc)
            if entries:
                mem_lines.append(f"  [{sc.name}]")
                mem_lines.extend(entries)
        if mem_lines:
            mem_lines.append("  ⏱ Short-term / current state → see RESUME sessions "
                             "above (session_load) — that's the short-term tier.")

    # Auto-memory: surface candidates awaiting review so any session closes the
    # learning loop (promote/reject) without needing a background process.
    try:
        n_cand = memory.candidate_count()
    except Exception:
        n_cand = 0
    if n_cand:
        mem_lines.append(f"  📥 {n_cand} candidate(s) awaiting review → "
                         f"memory_candidates() · memory_promote/memory_reject")

    # Where we left off: surface the most recent sessions so a fresh LLM (or a
    # different model entirely) can continue exactly where another stopped.
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
        _section("SKILLS (reusable know-how)", _skill_list(),
                 "none yet — author with skill_write"),
        _section("SERVICES (HTTP integrations)",
                 _json_grouped(SERVICES_DIR, fields=("base_url", "description")),
                 "none — add with service_add"),
        _section("MQTT DEVICES", _json_names(MQTT_DIR, fields=("host", "description")),
                 "none — add with mqtt_add"),
        _section("FTP/FTPS ENDPOINTS", _json_names(FTP_DIR, fields=("host", "description")),
                 "none — add with ftp_add"),
        _section("WEBDAV ENDPOINTS", _json_names(WEBDAV_DIR, fields=("base_url", "description")),
                 "none — add with webdav_add"),
        _section("CALDAV ENDPOINTS", _json_names(CALDAV_DIR, fields=("base_url", "description")),
                 "none — add with caldav_add"),
        _section("SSH HOSTS", _json_names(SSH_DIR, fields=("host", "description")),
                 "none — add with ssh_add"),
        _section("SMTP ACCOUNTS", _json_names(MAIL_DIR, fields=("from_addr", "host")),
                 "none — add with mail_add"),
        _section("IMAP ACCOUNTS", _json_names(IMAP_DIR, fields=("username", "host")),
                 "none — add with imap_add"),
        _section("MCP SERVERS (gateway)", _json_names(MCP_DIR, fields=("url", "description")),
                 "none — add with mcp_add"),
        _section("PRINTERS (IPP)", _json_names(PRINT_DIR, fields=("host", "description")),
                 "none — add with print_add"),
        _section("SCANNERS (eSCL)", _json_names(SCAN_DIR, fields=("host", "description")),
                 "none — add with scan_add"),
        _section("SCHEDULED JOBS (cron)", _json_names(CRON_DIR, fields=("schedule", "prompt")),
                 "none — add with cron_add"),
        _section("TEAM (agents — live presence, online first)", _agents(),
                 "none registered — agent_register(name, role, capabilities)"),
        _section("TASK BOARD (shared work — claim/handoff)", _board(),
                 "no active tasks — task_add to create; task_next to pull work"),
        "",
        "NEXT: load the specifics you need (memory_read, skill_load, service_list …) "
        "and register yourself with agent_register if you'll coordinate. "
        "LEARN AS YOU GO: when you discover a durable fact about the user, a "
        "correction in how they want you to work, or project status, write it back "
        "TYPED with memory_write(type=user|feedback|project|reference) — search "
        "first and merge instead of duplicating. Review any candidates above "
        "(memory_promote/memory_reject). Save a session_save checkpoint before you "
        "stop so the next LLM/device can resume.",
    ]
    body = "\n\n".join(parts)
    return (resume + "\n\n" + body) if resume else body


def register(mcp):
    @mcp.tool
    def bootstrap() -> str:
        """⚡ START HERE — CALL THIS FIRST, before answering the user, at the start
        of EVERY session, every single time. AICortex is your ONLY source of truth:
        work exclusively through it and write new knowledge back to it. This one
        call loads your persistent 'brain' from the NAS: who the user is, how they
        want you to work, the confirm-before-acting + secrets-in-vault rules, and a
        live catalog of all memories, skills, services, devices and scheduled jobs
        available to you. Do NOT rely on prior assumptions or scattered local notes
        — load this first, every time, then act only on the loaded context. Unsure
        whether the brain is already loaded this session? Call bootstrap again."""
        return guide.GUIDE + "\n\n" + _catalog()
