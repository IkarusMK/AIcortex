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

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", "/data/memory"))
SKILLS_DIR = Path(os.environ.get("SKILLS_DIR", "/data/skills"))
SERVICES_DIR = Path(os.environ.get("SERVICES_DIR", "/data/services"))
MQTT_DIR = Path(os.environ.get("MQTT_DIR", "/data/mqtt"))
FTP_DIR = Path(os.environ.get("FTP_DIR", "/data/ftp"))
MCP_DIR = Path(os.environ.get("MCP_DIR", "/data/mcp"))
CRON_DIR = Path(os.environ.get("CRON_DIR", "/data/cron"))
PRINT_DIR = Path(os.environ.get("PRINT_DIR", "/data/printers"))
AGENTS_FILE = Path(os.environ.get("COORD_DIR", "/data/coordination")) / "agents.json"


def _md_titles(scope_dir: Path) -> list[str]:
    """Memory entries in a scope: '<name> — <title>' from each .md's first line."""
    out = []
    for p in sorted(scope_dir.glob("*.md")):
        try:
            first = p.read_text(encoding="utf-8").splitlines()
            title = first[0].lstrip("# ").strip() if first else p.stem
        except Exception:
            title = p.stem
        out.append(f"  - {p.stem} — {title}")
    return out


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


def _skill_list() -> list[str]:
    out = []
    for sk in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        desc = ""
        try:
            text = sk.read_text(encoding="utf-8")
            if text.startswith("---"):
                parts = text.split("---", 2)
                if len(parts) == 3:
                    m = re.search(r"^description:\s*(.+)$", parts[1], re.MULTILINE)
                    desc = (" — " + m.group(1).strip()) if m else ""
        except Exception:
            pass
        out.append(f"  - {sk.parent.name}{desc}")
    return out


def _agents() -> list[str]:
    try:
        data = json.loads(AGENTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = data.values() if isinstance(data, dict) else data
    out = []
    for a in rows or []:
        if isinstance(a, dict):
            out.append(f"  - {a.get('name', '?')} — {a.get('role', '')}")
    return out


def _section(title: str, lines: list[str], empty: str) -> str:
    return f"{title}:\n" + ("\n".join(lines) if lines else f"  ({empty})")


def _catalog() -> str:
    """A live snapshot of everything currently on the NAS brain."""
    # All memory scopes, shared first.
    mem_lines: list[str] = []
    if MEMORY_DIR.exists():
        scopes = sorted(
            (d for d in MEMORY_DIR.iterdir() if d.is_dir()),
            key=lambda d: (d.name != "shared", d.name),
        )
        for sc in scopes:
            entries = _md_titles(sc)
            if entries:
                mem_lines.append(f"  [{sc.name}]")
                mem_lines.extend(entries)

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
        "===== LIVE BRAIN CATALOG (current NAS state) =====",
        _section("MEMORY (facts about the user & projects)", mem_lines,
                 "empty — nothing stored yet; capture facts with memory_write"),
        _section("SKILLS (reusable know-how)", _skill_list(),
                 "none yet — author with skill_write"),
        _section("SERVICES (HTTP integrations)",
                 _json_names(SERVICES_DIR, fields=("base_url", "description")),
                 "none — add with service_add"),
        _section("MQTT DEVICES", _json_names(MQTT_DIR, fields=("host", "description")),
                 "none — add with mqtt_add"),
        _section("FTP/FTPS ENDPOINTS", _json_names(FTP_DIR, fields=("host", "description")),
                 "none — add with ftp_add"),
        _section("MCP SERVERS (gateway)", _json_names(MCP_DIR, fields=("url", "description")),
                 "none — add with mcp_add"),
        _section("PRINTERS (IPP)", _json_names(PRINT_DIR, fields=("host", "description")),
                 "none — add with print_add"),
        _section("SCHEDULED JOBS (cron)", _json_names(CRON_DIR, fields=("schedule", "prompt")),
                 "none — add with cron_add"),
        _section("REGISTERED AGENTS", _agents(), "none registered"),
        "",
        "NEXT: load the specifics you need (memory_read, skill_load, service_list …) "
        "and register yourself with agent_register if you'll coordinate. "
        "Store new durable knowledge back with memory_write / skill_write so the "
        "brain grows instead of drifting. Save a session_save checkpoint before you "
        "stop so the next LLM/device can resume.",
    ]
    body = "\n\n".join(parts)
    return (resume + "\n\n" + body) if resume else body


def register(mcp):
    @mcp.tool
    def bootstrap() -> str:
        """⚡ START HERE — CALL THIS FIRST, before answering the user, at the start
        of EVERY session. This loads your persistent 'brain' from the NAS: who the
        user is, how they want you to work, the confirm-before-acting rules, and a
        live catalog of all memories, skills, services, devices and scheduled jobs
        available to you. Do NOT rely on prior assumptions about the user — load
        this first, every time, then act on the loaded context."""
        return guide.GUIDE + "\n\n" + _catalog()
