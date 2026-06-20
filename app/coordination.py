"""Multi-agent coordination layer — shared inbox, task board & agent registry.

The connector can't spawn agents (the model lives in the cloud), so this is the
shared substrate several Claude agents/devices use to coordinate:
  • an append-only INBOX (agent↔agent / agent↔user),
  • a claimable TASK board,
  • an AGENT registry.
All as data under COORD_DIR — no code per workflow, no redeploy. Memory scopes
remain the shared/per-agent knowledge layer; this adds messaging & task hand-off.
"""
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

COORD_DIR = Path(os.environ.get("COORD_DIR", "/data/coordination"))
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

    # ── Task board ─────────────────────────────────────────────────────
    @mcp.tool
    def task_add(title: str, detail: str = "", created_by: str = "") -> str:
        """Add a task to the shared board (status=open) for any agent to claim."""
        items = _read(TASKS_FILE)
        t = {"id": _id(), "ts": _now(), "title": title, "detail": detail,
             "status": "open", "owner": "", "created_by": created_by or "unknown",
             "updated": _now(), "notes": []}
        items.append(t)
        _write(TASKS_FILE, items)
        return f"Added task {t['id']}: {title}"

    @mcp.tool
    def task_list(status: str = "", owner: str = "") -> str:
        """List tasks, optionally filtered by status (open/claimed/done) or owner."""
        items = _read(TASKS_FILE)
        sel = [t for t in items
               if (not status or t.get("status") == status)
               and (not owner or t.get("owner") == owner)]
        if not sel:
            return "No matching tasks."
        return "\n".join(
            f"[{t['id']}] {t.get('status')} owner={t.get('owner') or '-'} — {t.get('title')}"
            for t in sel)

    @mcp.tool
    def task_claim(task_id: str, owner: str) -> str:
        """Claim an open task for `owner` (sets status=claimed)."""
        items = _read(TASKS_FILE)
        for t in items:
            if t.get("id") == task_id:
                if t.get("status") == "done":
                    return f"Task {task_id} is already done."
                t["status"] = "claimed"
                t["owner"] = owner
                t["updated"] = _now()
                _write(TASKS_FILE, items)
                return f"{owner} claimed task {task_id}."
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

    # ── Agent registry ─────────────────────────────────────────────────
    @mcp.tool
    def agent_register(name: str, role: str = "", capabilities: str = "") -> str:
        """Register or refresh an agent (upsert by name); updates last_seen."""
        items = _read(AGENTS_FILE)
        for a in items:
            if a.get("name") == name:
                a["role"] = role or a.get("role", "")
                a["capabilities"] = capabilities or a.get("capabilities", "")
                a["last_seen"] = _now()
                _write(AGENTS_FILE, items)
                return f"Updated agent '{name}'."
        items.append({"name": name, "role": role, "capabilities": capabilities,
                      "last_seen": _now()})
        _write(AGENTS_FILE, items)
        return f"Registered agent '{name}'."

    @mcp.tool
    def agent_list() -> str:
        """List registered agents (name — role — last seen — capabilities)."""
        items = _read(AGENTS_FILE)
        if not items:
            return "No agents registered yet."
        return "\n".join(
            f"- {a.get('name')} — {a.get('role', '')} — seen {a.get('last_seen', '')} — caps: {a.get('capabilities', '')}"
            for a in items)
