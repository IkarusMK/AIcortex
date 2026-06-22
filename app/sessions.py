"""Cross-LLM session handoff — continue work on one brain from any model/device.

The connector already shares memory/skills/tools across devices; this adds the
*conversational* layer: a lightweight, append-only log of work sessions so a
fresh assistant — Claude on your phone, ChatGPT on your laptop, a scheduled run —
can pick up exactly where another left off. Each session holds timestamped
checkpoints (a short summary + next steps + status), not a verbatim transcript:
compact handoffs beat 100k-token mirrors, and no MCP client streams its transcript
on its own anyway.

To avoid an ever-growing data pile, sessions are auto-pruned: any session not
updated within SESSION_RETENTION_DAYS (default 90 ≈ 3 months) is removed on the
next save/list. Set SESSION_RETENTION_DAYS=0 to keep them forever.

Storage: one JSON file per session under SESSIONS_DIR — structured (for listing
and pruning) and still human-readable on disk.
"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

SESSIONS_DIR = Path(os.environ.get("SESSIONS_DIR", "/data/sessions"))
RETENTION_DAYS = int(os.environ.get("SESSION_RETENTION_DAYS", "90"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:60]


def _path(session_id: str) -> Path:
    return SESSIONS_DIR / f"{_slug(session_id) or 'session'}.json"


def _load(session_id: str):
    p = _path(session_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_file(data: dict) -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    _path(data["id"]).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _age_days(iso: str) -> float:
    try:
        ts = datetime.fromisoformat(iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except Exception:
        return 0.0
    return (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0


def _all():
    """All sessions as parsed dicts (skips unreadable files)."""
    out = []
    if not SESSIONS_DIR.exists():
        return out
    for p in sorted(SESSIONS_DIR.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def _prune(max_age_days: int = None) -> int:
    """Delete sessions older than the retention window. Returns count removed.
    0 (or negative) retention disables pruning."""
    days = RETENTION_DAYS if max_age_days is None else max_age_days
    if days <= 0:
        return 0
    removed = 0
    for s in _all():
        updated = s.get("updated") or s.get("created") or ""
        if _age_days(updated) > days:
            try:
                _path(s["id"]).unlink()
                removed += 1
            except Exception:
                pass
    return removed


def latest_open():
    """The most recently updated session (for bootstrap). None if none exist."""
    _prune()
    sessions = _all()
    if not sessions:
        return None
    sessions.sort(key=lambda s: s.get("updated", ""), reverse=True)
    return sessions[0]


def summary_line(s: dict) -> str:
    """One-line headline for a session (used by bootstrap/list)."""
    last = (s.get("entries") or [{}])[-1]
    when = (s.get("updated", "") or "")[:10]
    return (f"{s.get('id')} — {s.get('title', '(untitled)')} · updated {when} · "
            f"{s.get('status', 'open')} · last: {last.get('model', '?')}")


def render(s: dict) -> str:
    """Full, human-readable rendering of a session and its checkpoints."""
    head = (f"SESSION {s.get('id')} — {s.get('title', '(untitled)')}\n"
            f"created {s.get('created','?')[:16]} · updated {s.get('updated','?')[:16]} · "
            f"status {s.get('status','open')}")
    lines = [head, ""]
    for e in s.get("entries", []):
        lines.append(f"— {e.get('ts','')[:16]} · {e.get('model','?')} · [{e.get('status','open')}]")
        if e.get("summary"):
            lines.append(f"  {e['summary']}")
        if e.get("next_steps"):
            lines.append(f"  NEXT: {e['next_steps']}")
    return "\n".join(lines)


def register(mcp):
    @mcp.tool
    def session_save(summary: str, title: str = "", session_id: str = "",
                     next_steps: str = "", status: str = "open",
                     model: str = "") -> str:
        """Append a checkpoint to a work session so another LLM/device can resume it.
        Keep `summary` short (what happened, current state); `next_steps` = what to do
        next. `model` = which LLM you are (e.g. "claude", "chatgpt"). Omit `session_id`
        to start a new session (returns its id); pass it to continue an existing one.
        Old sessions auto-expire after SESSION_RETENTION_DAYS."""
        if not (summary or "").strip():
            return "Nothing to save: 'summary' is required."
        now = _now()
        entry = {
            "ts": now,
            "model": (model or "unknown").strip(),
            "summary": summary.strip(),
            "next_steps": (next_steps or "").strip(),
            "status": (status or "open").strip(),
        }
        data = _load(session_id) if session_id else None
        if data is None:
            sid = _slug(session_id) or _slug(title) or f"session-{now[:10]}-{now[11:19].replace(':','')}"
            data = {"id": sid, "title": title or sid, "created": now,
                    "updated": now, "status": entry["status"], "entries": [entry]}
        else:
            if title:
                data["title"] = title
            data["updated"] = now
            data["status"] = entry["status"]
            data.setdefault("entries", []).append(entry)
        _save_file(data)
        pruned = _prune()
        note = f" ({pruned} stale session(s) auto-removed)" if pruned else ""
        return f"Saved checkpoint to session '{data['id']}'.{note}"

    @mcp.tool
    def session_list() -> str:
        """List work sessions (id — title · updated · status · last model), newest first."""
        _prune()
        sessions = _all()
        if not sessions:
            return "No sessions yet. Use session_save to start one."
        sessions.sort(key=lambda s: s.get("updated", ""), reverse=True)
        return "\n".join(f"- {summary_line(s)}" for s in sessions)

    @mcp.tool
    def session_load(session_id: str = "") -> str:
        """Load a session's full checkpoint history. Without an id, loads the most
        recently updated session — i.e. 'where we left off'."""
        _prune()
        if session_id:
            data = _load(session_id)
            if not data:
                return f"No session '{session_id}'. Use session_list."
            return render(data)
        data = latest_open()
        if not data:
            return "No sessions yet. Use session_save to start one."
        return render(data)

    @mcp.tool
    def session_delete(session_id: str) -> str:
        """Delete a session by id."""
        p = _path(session_id)
        if p.exists():
            p.unlink()
            return f"Deleted session '{_slug(session_id)}'."
        return f"No session '{session_id}'."

    @mcp.tool
    def session_prune(max_age_days: int = 0) -> str:
        """Remove sessions not updated within max_age_days (0 = use the configured
        SESSION_RETENTION_DAYS default). Runs automatically on save/list too."""
        days = max_age_days if max_age_days > 0 else RETENTION_DAYS
        removed = _prune(days)
        return f"Pruned {removed} session(s) older than {days} days."
