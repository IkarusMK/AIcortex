"""Approvals: a tool that changes data waits until its person says yes in the WebUI.

With ``approve_writes`` on (mcp_access, the default), a call through an app, a runner
or an agent key to a tool the upstream server does not mark readOnlyHint does not run.
It is kept here as an approval — server, tool, arguments, the channel that asked —
for TTL seconds, at most MAX_PENDING per person, and one channel asks for at most
CHANNEL_MAX of them per CHANNEL_WINDOW seconds (so one app can't fill every slot). The
person sees it in the WebUI and approves or rejects it there (CSRF-checked, their own
approvals only; no tool can).
On approval AICortex itself runs exactly that call, once, with the person's sign-in,
and keeps the answer for RESULT_TTL seconds; the AI fetches it once with
``mcp_call(..., approval_id=…)``. A rejected or expired approval never runs, a decided
one can't be decided again, a fetched answer is gone.

Each person's approvals are one file, encrypted with the vault key (an answer can hold
the upstream server's data); without STORAGE_ENCRYPTION_KEY nothing is kept — no
sign-in can be stored without it either.
"""
import json
import secrets
import time

import store

TTL = 600                  # seconds an approval waits for the person
RESULT_TTL = 3600          # seconds an answer waits to be fetched
MAX_PENDING = 20           # approvals waiting per person
CHANNEL_MAX = 5            # new approvals one channel (app, runner, key) may ask for …
CHANNEL_WINDOW = 600       # … per this many seconds, whatever became of them
MAX_ARGS = 16 * 1024       # bytes of a call's arguments (JSON)
MAX_RESULT = 64 * 1024     # characters of an answer kept
_KEEP = 86400              # seconds a decided approval is remembered at all


def _clock() -> float:
    return time.time()


class Refused(Exception):
    """An approval that can't be created, decided or fetched — the message says why."""


def _file(person: str):
    import mcp_gateway
    import tenancy
    return mcp_gateway.MCP_DIR / "_access" / f"approvals-{tenancy._safe(person)}.enc"


def _cipher():
    import secrets_store
    f = secrets_store._fernet()
    if f is None:
        raise Refused("Approvals need STORAGE_ENCRYPTION_KEY (like every stored sign-in).")
    return f


def _read(person: str) -> list:
    path = _file(person)
    if not path.exists():
        return []
    try:
        rows = json.loads(_cipher().decrypt(path.read_bytes()))
    except Refused:
        raise
    except Exception:
        return []            # unreadable (another key): nothing can be approved from it
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _write(person: str, rows: list) -> None:
    path = _file(person)
    path.parent.mkdir(parents=True, exist_ok=True)
    store.write_bytes_atomic(path, _cipher().encrypt(json.dumps(rows).encode()))


def _tidy(rows: list, now: float) -> list:
    """Expire what waited too long, forget answers nobody fetched and old decisions."""
    out = []
    for r in rows:
        if r.get("state") == "pending" and r.get("expires", 0) < now:
            r = {**r, "state": "expired"}
        if r.get("state") == "done" and r.get("result_until", 0) < now:
            r = {**{k: v for k, v in r.items() if k != "result"}, "state": "unfetched"}
        if r.get("created", 0) + _KEEP >= now:
            out.append(r)
    return out


def _change(person: str, fn):
    """Run fn(rows, now) → (new rows, answer) on `person`'s approvals, under a lock."""
    path = _file(person)
    path.parent.mkdir(parents=True, exist_ok=True)
    with store.locked(path):
        now = _clock()
        rows, answer = fn(_tidy(_read(person), now), now)
        _write(person, rows)
        return answer


def create(person: str, server: str, tool: str, args, channel: str) -> str:
    """Keep a held call; returns its approval id."""
    args = args if isinstance(args, dict) else {}
    if len(json.dumps(args)) > MAX_ARGS:
        raise Refused(f"Refused: the arguments are larger than {MAX_ARGS} bytes.")

    def add(rows, now):
        recent = [r for r in rows if r.get("channel") == channel
                  and r.get("created", 0) > now - CHANNEL_WINDOW]
        if len(recent) >= CHANNEL_MAX:
            raise Refused(f"Refused: {channel} already asked for {len(recent)} approvals in the "
                          f"last {CHANNEL_WINDOW // 60} minutes — at most {CHANNEL_MAX} per app, "
                          f"runner or key. Try again later; {person} decides the waiting "
                          f"ones in the AICortex WebUI (Services → Approvals).")
        waiting = [r for r in rows if r.get("state") == "pending"]
        if len(waiting) >= MAX_PENDING:
            raise Refused(f"Refused: {len(waiting)} approvals are already waiting for "
                          f"{person} — they decide in the WebUI first.")
        aid = secrets.token_urlsafe(12)
        return rows + [{"id": aid, "server": server, "tool": tool, "args": args,
                        "channel": channel, "created": now, "expires": now + TTL,
                        "state": "pending"}], aid
    return _change(person, add)


def get(person: str, aid: str) -> dict:
    """`person`'s approval `aid` while it still waits for a decision (else Refused)."""
    row = next((r for r in _tidy(_read(person), _clock()) if r.get("id") == aid), None)
    if row is None:
        raise Refused("No such approval.")
    if row.get("state") != "pending":
        raise Refused(f"This approval is no longer waiting ({row.get('state')}).")
    return row


def waiting(person: str) -> list:
    """`person`'s approvals that wait for a decision (oldest first)."""
    now = _clock()
    return [r for r in _tidy(_read(person), now) if r.get("state") == "pending"]


def claim(person: str, aid: str, approve: bool) -> dict:
    """Decide `person`'s approval `aid`: approved → its record, state "running" (so a
    second decision finds nothing to do); rejected → state "rejected"."""
    def decide(rows, now):
        row = next((r for r in rows if r.get("id") == aid), None)
        if row is None:
            raise Refused("No such approval.")
        if row.get("state") != "pending":
            raise Refused(f"This approval is no longer waiting ({row.get('state')}).")
        new = {**row, "state": "running" if approve else "rejected", "decided": now}
        return [new if r is row else r for r in rows], new
    return _change(person, decide)


def finish(person: str, aid: str, result: str) -> None:
    """Keep the answer of an approved call until it is fetched."""
    def done(rows, now):
        return [{**r, "state": "done", "result": result[:MAX_RESULT],
                 "result_until": now + RESULT_TTL} if r.get("id") == aid else r
                for r in rows], None
    _change(person, done)


def fetch(person: str, aid: str, server: str, tool: str) -> str:
    """The answer of `person`'s approved call `aid` — once; else where it stands."""
    def take(rows, now):
        row = next((r for r in rows if r.get("id") == aid
                    and r.get("server") == server and r.get("tool") == tool), None)
        if row is None:
            return rows, f"No approval '{aid}' for '{tool}' on '{server}'."
        state = row.get("state")
        if state == "done":
            gone = {**{k: v for k, v in row.items() if k != "result"}, "state": "fetched"}
            return [gone if r is row else r for r in rows], row.get("result", "")
        return rows, {
            "pending": f"Still waiting for {person}'s approval in the AICortex WebUI "
                       f"(until {time.strftime('%H:%M', time.localtime(row['expires']))}).",
            "running": "Approved — the call is running; ask again in a moment.",
            "rejected": f"{person} rejected this call; it was not run.",
            "expired": "The approval expired before anyone decided; the call was not run.",
            "fetched": "The answer was already fetched.",
            "unfetched": "The answer was kept for an hour and is gone now.",
        }.get(state, "Unknown state.")
    return _change(person, take)
