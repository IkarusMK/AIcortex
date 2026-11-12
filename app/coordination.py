"""Multi-agent coordination layer — shared inbox, task board & agent registry.

The connector can't spawn agents (the model lives in the cloud or runs locally),
so this is the shared substrate several LLM agents/devices use to coordinate:
  • an append-only INBOX (agent↔agent / agent↔user),
  • a claimable TASK board,
  • an AGENT registry.
All as data under COORD_DIR — no code per workflow, no redeploy. Memory scopes
remain the shared/per-agent knowledge layer; this adds messaging & task hand-off.
"""
import functools
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from pydantic import Field

import store
import settings

COORD_DIR = Path(os.environ.get("COORD_DIR", "/data/coordination"))

# Presence thresholds: an agent is "online" if seen within ONLINE_SECS, "idle"
# within IDLE_SECS, else "away". last_seen is refreshed by agent_register.
ONLINE_SECS = settings.knob("AGENT_ONLINE_SECS", settings.AGENT_ONLINE_SECS, int)    # 5 min
IDLE_SECS = settings.knob("AGENT_IDLE_SECS", settings.AGENT_IDLE_SECS, int)       # 30 min
_TASK_STATUS = ("open", "claimed", "blocked", "done")
INBOX_FILE = COORD_DIR / "inbox.json"
TASKS_FILE = COORD_DIR / "tasks.json"
AGENTS_FILE = COORD_DIR / "agents.json"


def _read(path: Path):
    # Missing file = empty board; an unreadable one raises instead of reading as
    # empty (the next write would otherwise replace the whole board).
    return store.read_json(path, [])


def _write(path: Path, data) -> None:
    store.write_json(path, data)


def _serialized(fn):
    """Run ``fn`` under the board lock so its read-modify-write can't interleave
    with another agent's (tool calls run in parallel threads)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with store.locked(COORD_DIR / "board"):
            return fn(*args, **kwargs)
    return wrapper


def _person() -> str:
    """The person behind the current call ('' when it can't be resolved)."""
    try:
        import authz
        ident = authz.effective_identity()[0]
        return "" if ident == "unknown" else ident
    except Exception:
        return ""


def _sees(item: dict) -> bool:
    """Per-person views: with enforcement on, a non-admin sees only their own
    tasks, messages and agents (plus unowned ones, e.g. webhook events)."""
    try:
        import authz
        return authz.sees(item.get("person", ""))
    except Exception:
        return True


def _mine(items: list) -> list:
    return [x for x in items if _sees(x)]


def _id() -> str:
    return uuid.uuid4().hex[:8]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _lease_until(minutes: int) -> str:
    """ISO timestamp `minutes` from now (at least one minute)."""
    until = datetime.now(timezone.utc).timestamp() + max(1, int(minutes or 60)) * 60
    return datetime.fromtimestamp(until, timezone.utc).isoformat(timespec="seconds")


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


@_serialized
def post_inbox(to: str, body: str, subject: str = "", sender: str = "") -> str:
    """Append a message to the shared inbox and return its id. Module-level so
    non-tool callers (e.g. the inbound webhook receiver) can deposit events too."""
    items = _read(INBOX_FILE)
    msg = {"id": _id(), "ts": _now(), "to": to, "from": sender or "unknown",
           "subject": subject, "body": body, "read": False, "person": _person()}
    items.append(msg)
    _write(INBOX_FILE, items)
    return msg["id"]


def _caps_set(s) -> set:
    if isinstance(s, (list, tuple)):
        s = " ".join(s)
    return {c for c in re.split(r"[,\s]+", (s or "").lower()) if c}


def _agent_caps(name: str) -> set:
    for a in _mine(_read(AGENTS_FILE)):
        if a.get("name") == name:
            return _caps_set(a.get("capabilities", ""))
    return set()


# ── Public helpers for bootstrap (live team/board snapshot) ─────────────────
def agent_rows() -> list[str]:
    """The caller's registered agents with presence, online first (for the catalog)."""
    items = _mine(_read(AGENTS_FILE))
    rank = {"online": 0, "idle": 1, "away": 2}
    items.sort(key=lambda a: (rank.get(_presence(a), 3), a.get("name", "")))
    out = []
    for a in items:
        caps = f" · caps: {a['capabilities']}" if a.get("capabilities") else ""
        out.append(f"  - {a.get('name', '?')} [{_presence(a)}] — {a.get('role', '')}{caps}")
    return out


def open_tasks(project: str = "") -> list:
    """Tasks not done yet, optionally only those of `project` (normalized match)."""
    import memory
    want = memory.project_key(project)
    return [t for t in _mine(_read(TASKS_FILE)) if t.get("status") != "done"
            and (not want or memory.project_key(t.get("project", "")) == want)]


def board_counts() -> dict:
    counts: dict = {}
    for t in _mine(_read(TASKS_FILE)):
        s = t.get("status", "open")
        counts[s] = counts.get(s, 0) + 1
    return counts


def board_overview(limit: int = 5) -> list[str]:
    """Compact task-board snapshot (summary + active tasks) for bootstrap."""
    tasks = _mine(_read(TASKS_FILE))
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
        mid = post_inbox(to, body, subject, sender)
        return f"Posted message {mid} to '{to}'."

    @mcp.tool
    def inbox_read(agent: str, unread_only: bool = True, limit: int = 20) -> str:
        """Read messages addressed to `agent` (also matches 'all'), newest last.
        Marks nothing read — call inbox_ack(id) when handled."""
        items = _read(INBOX_FILE)
        sel = [m for m in _mine(items) if m.get("to") in (agent, "all")
               and (not unread_only or not m.get("read"))][-max(1, limit):]
        if not sel:
            return f"No {'unread ' if unread_only else ''}messages for '{agent}'."
        return "\n".join(
            f"[{m['id']}] {m['ts']} from {m.get('from')} — {m.get('subject', '')}: {m.get('body', '')}"
            for m in sel)

    @mcp.tool
    @_serialized
    def inbox_ack(message_id: str) -> str:
        """Mark a message as read/handled by id."""
        items = _read(INBOX_FILE)
        for m in items:
            if m.get("id") == message_id and _sees(m):
                m["read"] = True
                _write(INBOX_FILE, items)
                return f"Marked {message_id} read."
        return f"No message {message_id}."

    @mcp.tool
    @_serialized
    def inbox_delete(message_id: str = "", purge_read: bool = False) -> str:
        """Delete one message by id, or purge all read messages (purge_read=True)."""
        items = _read(INBOX_FILE)
        if purge_read:
            kept = [m for m in items if not (m.get("read") and _sees(m))]
            _write(INBOX_FILE, kept)
            return f"Purged {len(items) - len(kept)} read message(s)."
        kept = [m for m in items if not (m.get("id") == message_id and _sees(m))]
        if len(kept) == len(items):
            return f"No message {message_id}."
        _write(INBOX_FILE, kept)
        return f"Deleted message {message_id}."

    # ── Task board ─────────────────────────────────────────────────────
    @mcp.tool
    @_serialized
    def task_add(title: str, detail: str = "", created_by: str = "",
                 needs: str = "", for_agent: str = "", session_id: str = "",
                 project: str = "") -> str:
        """Add a task to the shared board (status=open) for any agent to claim.
        needs = capability tag(s) an agent should have (e.g. "mql5, build"); used by
        task_next to route work. for_agent = assign it to a specific agent by name.
        session_id = link to a work session (sessions.py) so whoever picks it up can
        session_load the full context — the basis for clean handoffs.
        project = the project the task belongs to (shown by cortex_start)."""
        items = _read(TASKS_FILE)
        t = {"id": _id(), "ts": _now(), "title": title, "detail": detail,
             "status": "open", "owner": "", "created_by": created_by or "unknown",
             "needs": needs, "assigned": for_agent, "session_id": session_id,
             "project": (project or "").strip(), "person": _person(),
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
    def task_list(status: Annotated[str, Field(description="open, claimed, blocked or done; "
                                                           "empty = all")] = "",
                  owner: Annotated[str, Field(description="Agent holding the task, as after "
                                                          "owner= in the list; empty = any")] = "") -> str:
        """List the tasks on the shared agent board — work handed between agents —
        oldest first, every match (id, status, owner, title). Check it before adding or
        claiming one (task_add / task_claim via integration_call)."""
        items = _mine(_read(TASKS_FILE))
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
        items = [t for t in _mine(_read(TASKS_FILE)) if t.get("status") == "open"]
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
    @_serialized
    def task_claim(task_id: str, owner: str, lease_minutes: int = 60) -> str:
        """Claim an open task for `owner` (sets status=claimed). A claim holds for
        `lease_minutes`; until then other agents can't take the task over (the same
        owner may re-claim to extend it). After the lease runs out the task is free
        again, so a crashed agent can't block it forever."""
        items = _read(TASKS_FILE)
        for t in items:
            if t.get("id") == task_id and _sees(t):
                if t.get("status") == "done":
                    return f"Task {task_id} is already done."
                holder = t.get("owner")
                lease_left = -_age_secs(t.get("lease_until", "")) if t.get("lease_until") else 0
                if t.get("status") == "claimed" and holder and holder != owner and lease_left > 0:
                    return (f"Task {task_id} is already claimed by '{holder}' "
                            f"(lease ends in {int(lease_left // 60) + 1} min). "
                            f"Pick another with task_next, or ask {holder} via inbox_post.")
                note = ""
                if t.get("assigned") and t["assigned"] != owner:
                    note = f" (note: it was assigned to '{t['assigned']}')"
                t["status"] = "claimed"
                t["owner"] = owner
                t["lease_until"] = _lease_until(lease_minutes)
                t["updated"] = _now()
                _write(TASKS_FILE, items)
                sess = f" Load context: session_load('{t['session_id']}')." if t.get("session_id") else ""
                return f"{owner} claimed task {task_id}.{note}{sess}"
        return f"No task {task_id}."

    @mcp.tool
    @_serialized
    def task_handoff(task_id: str, to: str = "", note: str = "") -> str:
        """Hand a task to another agent (or release it: to=""). Sets the new owner,
        appends a note, and drops the recipient an inbox message with the linked
        session_id so they can pick up exactly where you left off."""
        items = _read(TASKS_FILE)
        for t in items:
            if t.get("id") == task_id and _sees(t):
                if to:
                    t["owner"] = to
                    t["assigned"] = to
                    t["status"] = "claimed"
                    t["lease_until"] = _lease_until(60)      # recipient gets a fresh lease
                else:
                    t["owner"] = ""
                    t["status"] = "open"
                    t.pop("lease_until", None)
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
                    # The message belongs to the task's person, like the task itself —
                    # without it, every person could read the handoff note.
                    inbox.append({"id": _id(), "ts": _now(), "to": to, "from": "board",
                                  "subject": "task handoff", "body": body, "read": False,
                                  "person": t.get("person") or _person()})
                    _write(INBOX_FILE, inbox)
                return f"Handed task {task_id} to '{to}'." if to else f"Released task {task_id} back to open."
        return f"No task {task_id}."

    @mcp.tool
    @_serialized
    def task_update(task_id: str, status: str = "", note: str = "") -> str:
        """Update a task's status (open/claimed/blocked/done) and/or append a progress note."""
        if status and status not in _TASK_STATUS:
            return f"Refused: status must be one of {', '.join(_TASK_STATUS)}."
        items = _read(TASKS_FILE)
        for t in items:
            if t.get("id") == task_id and _sees(t):
                if status:
                    t["status"] = status
                    if status != "claimed":
                        t.pop("lease_until", None)
                if note:
                    t.setdefault("notes", []).append({"ts": _now(), "note": note})
                t["updated"] = _now()
                _write(TASKS_FILE, items)
                return f"Updated task {task_id} (status={t.get('status')})."
        return f"No task {task_id}."

    @mcp.tool
    @_serialized
    def task_delete(task_id: str) -> str:
        """Delete a task from the board by id."""
        items = _read(TASKS_FILE)
        kept = [t for t in items if not (t.get("id") == task_id and _sees(t))]
        if len(kept) == len(items):
            return f"No task {task_id}."
        _write(TASKS_FILE, kept)
        return f"Deleted task {task_id}."

    # ── Agent registry ─────────────────────────────────────────────────
    @mcp.tool
    @_serialized
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
                if not _sees(a):
                    return (f"Refused: agent name '{name}' belongs to {a.get('person')}. "
                            f"Register under another name.")
                a["role"] = role or a.get("role", "")
                a["capabilities"] = capabilities or a.get("capabilities", "")
                a["status"] = status or a.get("status", "")
                a["last_seen"] = _now()
                _write(AGENTS_FILE, items)
                return f"Updated agent '{name}' [{_presence(a)}]."
        items.append({"name": name, "role": role, "capabilities": capabilities,
                      "status": status, "last_seen": _now(), "person": _person()})
        _write(AGENTS_FILE, items)
        return f"Registered agent '{name}' [online]."

    @mcp.tool
    def agent_list() -> str:
        """List registered agents with live presence (online/idle/away), online
        first — so you can see who's available to take work right now. You see your
        own agents (an admin: everyone's)."""
        items = _mine(_read(AGENTS_FILE))
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
    @_serialized
    def agent_remove(name: str) -> str:
        """Remove one of your agents from the registry by name."""
        items = _read(AGENTS_FILE)
        for a in items:
            if a.get("name") == name and not _sees(a):
                return f"Refused: agent '{name}' belongs to {a.get('person')}."
        kept = [a for a in items if a.get("name") != name]
        if len(kept) == len(items):
            return f"No agent '{name}'."
        _write(AGENTS_FILE, kept)
        return f"Removed agent '{name}'."
