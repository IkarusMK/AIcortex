"""Multi-agent coordination layer — shared inbox, task board & agent registry.

The connector can't spawn agents (the model lives in the cloud or runs locally),
so this is the shared substrate several LLM agents/devices use to coordinate:
  • an append-only INBOX (agent↔agent / agent↔user),
  • a claimable TASK board,
  • an AGENT registry.
All as data under COORD_DIR — no code per workflow, no redeploy. Memory scopes
remain the shared/per-agent knowledge layer; this adds messaging & task hand-off.
"""
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

COORD_DIR = Path(os.environ.get("COORD_DIR", "/data/coordination"))

# Presence thresholds: an agent is "online" if seen within ONLINE_SECS, "idle"
# within IDLE_SECS, else "away". last_seen is refreshed by agent_register.
ONLINE_SECS = int(os.environ.get("AGENT_ONLINE_SECS", "300"))    # 5 min
IDLE_SECS = int(os.environ.get("AGENT_IDLE_SECS", "1800"))       # 30 min
_TASK_STATUS = ("open", "claimed", "blocked", "done")
INBOX_FILE = COORD_DIR / "inbox.json"
TASKS_FILE = COORD_DIR / "tasks.json"
AGENTS_FILE = COORD_DIR / "agents.json"


def _read(path: Path):
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _write(path: Path, data) -> None:
    COORD_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _id() -> str:
    return uuid.uuid4().hex[:8]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _age_secs(iso: str) -> float:
    try:
        ts = datetime.fromisoformat(iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return 1e12


def _presence(a: dict) -> str:
    age = _age_secs(a.get("last_seen", ""))
    if age <= ONLINE_SECS:
        return "online"
    if age <= IDLE_SECS:
        return "idle"
    return "away"


def _caps_set(s) -> set:
    if isinstance(s, (list, tuple)):
        s = " ".join(s)
    return {c for c in re.split(r"[,\s]+", (s or "").lower()) if c}


def _agent_caps(name: str) -> set:
    for a in _read(AGENTS_FILE):
        if a.get("name") == name:
            return _caps_set(a.get("capabilities", ""))
    return set()


# ── Public helpers for bootstrap (live team/board snapshot) ─────────────────
def agent_rows() -> list[str]:
    """Registered agents with presence, online first (for bootstrap)."""
    items = _read(AGENTS_FILE)
    rank = {"online": 0, "idle": 1, "away": 2}
    items.sort(key=lambda a: (rank.get(_presence(a), 3), a.get("name", "")))
    out = []
    for a in items:
        caps = f" · caps: {a['capabilities']}" if a.get("capabilities") else ""
        out.append(f"  - {a.get('name', '?')} [{_presence(a)}] — {a.get('role', '')}{caps}")
    return out


def board_counts() -> dict:
    counts: dict = {}
    for t in _read(TASKS_FILE):
        s = t.get("status", "open")
        counts[s] = counts.get(s, 0) + 1
    return counts


def board_overview(limit: int = 5) -> list[str]:
    """Compact task-board snapshot (summary + active tasks) for bootstrap."""
    tasks = _read(TASKS_FILE)
    active = [t for t in tasks if t.get("status") != "done"]
    if not active:
        return []
    counts = board_counts()
    summary = " · ".join(f"{n} {s}" for s, n in sorted(counts.items()))
    out = [f"  {summary}"]
    for t in sorted(active, key=lambda t: t.get("ts", ""))[:limit]:
        sess = f" · sess:{t['session_id']}" if t.get("session_id") else ""
        out.append(f"  - [{t['id']}] {t.get('status')} owner={t.get('owner') or '-'} "
                   f"— {t.get('title')}{sess}")
    return out


def register(mcp):
    # ── Inbox ──────────────────────────────────────────────────────────
    @mcp.tool
    def inbox_post(to: str, body: str, subject: str = "", sender: str = "") -> str:
        """Post a message to an agent (or 'user' / 'all'). Append-only.
        `sender` = your agent name. Recipients read it with inbox_read."""
        items = _read(INBOX_FILE)
        msg = {"id": _id(), "ts": _now(), "to": to, "from": sender or "unknown",
               "subject": subject, "body": body, "read": False}
        items.append(msg)
        _write(INBOX_FILE, items)
        return f"Posted message {msg['id']} to '{to}'."

    @mcp.tool
    def inbox_read(agent: str, unread_only: bool = True, limit: int = 20) -> str:
        """Read messages addressed to `agent` (also matches 'all'), newest last.
        Marks nothing read — call inbox_ack(id) when handled."""
        items = _read(INBOX_FILE)
        sel = [m for m in items if m.get("to") in (agent, "all")
               and (not unread_only or not m.get("read"))][-max(1, limit):]
        if not sel:
            return f"No {'unread ' if unread_only else ''}messages for '{agent}'."
        return "\n".join(
            f"[{m['id']}] {m['ts']} from {m.get('from')} — {m.get('subject', '')}: {m.get('body', '')}"
            for m in sel)

    @mcp.tool
    def inbox_ack(message_id: str) -> str:
        """Mark a message as read/handled by id."""
        items = _read(INBOX_FILE)
        for m in items:
            if m.get("id") == message_id:
                m["read"] = True
                _write(INBOX_FILE, items)
                return f"Marked {message_id} read."
        return f"No message {message_id}."

    @mcp.tool
    def inbox_delete(message_id: str = "", purge_read: bool = False) -> str:
        """Delete one message by id, or purge all read messages (purge_read=True)."""
        items = _read(INBOX_FILE)
        if purge_read:
            kept = [m for m in items if not m.get("read")]
            _write(INBOX_FILE, kept)
            return f"Purged {len(items) - len(kept)} read message(s)."
        kept = [m for m in items if m.get("id") != message_id]
        if len(kept) == len(items):
            return f"No message {message_id}."
        _write(INBOX_FILE, kept)
        return f"Deleted message {message_id}."

    # ── Task board ─────────────────────────────────────────────────────
    @mcp.tool
    def task_add(title: str, detail: str = "", created_by: str = "",
                 needs: str = "", for_agent: str = "", session_id: str = "") -> str:
        """Add a task to the shared board (status=open) for any agent to claim.
        needs = capability tag(s) an agent should have (e.g. "mql5, build"); used by
        task_next to route work. for_agent = assign it to a specific agent by name.
        session_id = link to a work session (sessions.py) so whoever picks it up can
        session_load the full context — the basis for clean handoffs."""
        items = _read(TASKS_FILE)
        t = {"id": _id(), "ts": _now(), "title": title, "detail": detail,
             "status": "open", "owner": "", "created_by": created_by or "unknown",
             "needs": needs, "assigned": for_agent, "session_id": session_id,
             "updated": _now(), "notes": []}
        items.append(t)
        _write(TASKS_FILE, items)
        extra = []
        if for_agent:
            extra.append(f"→ {for_agent}")
        if needs:
            extra.append(f"needs: {needs}")
        suffix = f" ({', '.join(extra)})" if extra else ""
        return f"Added task {t['id']}: {title}{suffix}"

    @mcp.tool
    def task_list(status: str = "", owner: str = "") -> str:
        """List tasks, optionally filtered by status (open/claimed/blocked/done) or owner."""
        items = _read(TASKS_FILE)
        sel = [t for t in items
               if (not status or t.get("status") == status)
               and (not owner or t.get("owner") == owner)]
        if not sel:
            return "No matching tasks."
        lines = []
        for t in sel:
            assigned = f"→{t['assigned']}" if t.get("assigned") else ""
            needs = f" · needs:{t['needs']}" if t.get("needs") else ""
            sess = f" · sess:{t['session_id']}" if t.get("session_id") else ""
            lines.append(f"[{t['id']}] {t.get('status')} owner={t.get('owner') or '-'}{assigned} "
                         f"— {t.get('title')}{needs}{sess}")
        return "\n".join(lines)

    @mcp.tool
    def task_next(owner: str, caps: str = "") -> str:
        """Recommend the best open task(s) for `owner` to pick up next (then claim
        with task_claim). Ranking: tasks assigned to you → tasks whose `needs` match
        your capabilities → unassigned/unspecific. caps = extra capability tags
        (your registered agent capabilities are included automatically)."""
        items = [t for t in _read(TASKS_FILE) if t.get("status") == "open"]
        if not items:
            return "No open tasks."
        capset = _caps_set(caps) | _agent_caps(owner)

        def score(t):
            if t.get("assigned") == owner:
                return 0
            need = _caps_set(t.get("needs", ""))
            if need and need & capset:
                return 1
            if not need and not t.get("assigned"):
                return 2
            if not t.get("assigned"):
                return 3
            return 9  # assigned to someone else — not for you

        ranked = [t for t in sorted(items, key=lambda t: (score(t), t.get("ts", "")))
                  if score(t) < 9][:3]
        if not ranked:
            return f"No open tasks suitable for '{owner}' right now."
        out = ["Recommended next (claim with task_claim):"]
        for t in ranked:
            need = f" · needs:{t['needs']}" if t.get("needs") else ""
            sess = f" · sess:{t['session_id']}" if t.get("session_id") else ""
            out.append(f"[{t['id']}] {t.get('title')}{need}{sess}")
        return "\n".join(out)

    @mcp.tool
    def task_claim(task_id: str, owner: str) -> str:
        """Claim an open task for `owner` (sets status=claimed)."""
        items = _read(TASKS_FILE)
        for t in items:
            if t.get("id") == task_id:
                if t.get("status") == "done":
                    return f"Task {task_id} is already done."
                note = ""
                if t.get("assigned") and t["assigned"] != owner:
                    note = f" (note: it was assigned to '{t['assigned']}')"
                t["status"] = "claimed"
                t["owner"] = owner
                t["updated"] = _now()
                _write(TASKS_FILE, items)
                sess = f" Load context: session_load('{t['session_id']}')." if t.get("session_id") else ""
                return f"{owner} claimed task {task_id}.{note}{sess}"
        return f"No task {task_id}."

    @mcp.tool
    def task_handoff(task_id: str, to: str = "", note: str = "") -> str:
        """Hand a task to another agent (or release it: to=""). Sets the new owner,
        appends a note, and drops the recipient an inbox message with the linked
        session_id so they can pick up exactly where you left off."""
        items = _read(TASKS_FILE)
        for t in items:
            if t.get("id") == task_id:
                if to:
                    t["owner"] = to
                    t["assigned"] = to
                    t["status"] = "claimed"
                else:
                    t["owner"] = ""
                    t["status"] = "open"
                if note:
                    t.setdefault("notes", []).append({"ts": _now(), "note": note})
                t["updated"] = _now()
                _write(TASKS_FILE, items)
                if to:
                    inbox = _read(INBOX_FILE)
                    body = (f"Task {task_id} '{t.get('title')}' handed to you."
                            + (f" session_id={t['session_id']} (session_load it)."
                               if t.get("session_id") else "")
                            + (f" Note: {note}" if note else ""))
                    inbox.append({"id": _id(), "ts": _now(), "to": to, "from": "board",
                                  "subject": "task handoff", "body": body, "read": False})
                    _write(INBOX_FILE, inbox)
                return f"Handed task {task_id} to '{to}'." if to else f"Released task {task_id} back to open."
        return f"No task {task_id}."

    @mcp.tool
    def task_update(task_id: str, status: str = "", note: str = "") -> str:
        """Update a task's status (open/claimed/done) and/or append a progress note."""
        items = _read(TASKS_FILE)
        for t in items:
            if t.get("id") == task_id:
                if status:
                    t["status"] = status
                if note:
                    t.setdefault("notes", []).append({"ts": _now(), "note": note})
                t["updated"] = _now()
                _write(TASKS_FILE, items)
                return f"Updated task {task_id} (status={t.get('status')})."
        return f"No task {task_id}."

    @mcp.tool
    def task_delete(task_id: str) -> str:
        """Delete a task from the board by id."""
        items = _read(TASKS_FILE)
        kept = [t for t in items if t.get("id") != task_id]
        if len(kept) == len(items):
            return f"No task {task_id}."
        _write(TASKS_FILE, kept)
        return f"Deleted task {task_id}."

    # ── Agent registry ─────────────────────────────────────────────────
    @mcp.tool
    def agent_register(name: str, role: str = "", capabilities: str = "",
                       status: str = "online") -> str:
        """Register or refresh an agent (upsert by name); updates last_seen — this
        IS your presence heartbeat, so call it at the start of every session and
        whenever you resume. capabilities = comma/space tags used to route tasks
        (e.g. "mql5, build, vision"). status = a free-text note (e.g. "idle",
        "busy")."""
        items = _read(AGENTS_FILE)
        for a in items:
            if a.get("name") == name:
                a["role"] = role or a.get("role", "")
                a["capabilities"] = capabilities or a.get("capabilities", "")
                a["status"] = status or a.get("status", "")
                a["last_seen"] = _now()
                _write(AGENTS_FILE, items)
                return f"Updated agent '{name}' [{_presence(a)}]."
        items.append({"name": name, "role": role, "capabilities": capabilities,
                      "status": status, "last_seen": _now()})
        _write(AGENTS_FILE, items)
        return f"Registered agent '{name}' [online]."

    @mcp.tool
    def agent_list() -> str:
        """List registered agents with live presence (online/idle/away), online
        first — so you can see who's available to take work right now."""
        items = _read(AGENTS_FILE)
        if not items:
            return "No agents registered yet."
        rank = {"online": 0, "idle": 1, "away": 2}
        items.sort(key=lambda a: (rank.get(_presence(a), 3), a.get("name", "")))
        return "\n".join(
            f"- {a.get('name')} [{_presence(a)}] — {a.get('role', '')} — "
            f"{a.get('status', '')} — seen {a.get('last_seen', '')} — "
            f"caps: {a.get('capabilities', '')}"
            for a in items)

    @mcp.tool
    def agent_remove(name: str) -> str:
        """Remove an agent from the registry by name."""
        items = _read(AGENTS_FILE)
        kept = [a for a in items if a.get("name") != name]
        if len(kept) == len(items):
            return f"No agent '{name}'."
        _write(AGENTS_FILE, kept)
        return f"Removed agent '{name}'."
