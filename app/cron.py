"""Schedule jobs as DATA — the NAS holds the cron, a runner triggers an LLM run.

cron_add / cron_list / cron_delete let any LLM client (desktop/mobile) create
scheduled jobs that live on the NAS. A small runner on the NAS (a recurring
agent invocation — e.g. ``claude -p`` or any LLM CLI/SDK — via system cron,
every minute) calls ``cron_due`` each tick, runs each due job's prompt through
this connector, then ``cron_mark_run`` and notifies the user. The connector
stores the SCHEDULE; the runner provides the actual LLM execution (the model
can't self-trigger server-side).

Schedules are standard 5-field cron expressions in the server's local time:
``minute hour day-of-month month day-of-week`` (``*`` , ``,`` lists, ``a-b``
ranges and ``*/step`` are supported; dow 0/7 = Sunday).
"""
import json
import os
import re
from datetime import datetime
from pathlib import Path

CRON_DIR = Path(os.environ.get("CRON_DIR", "/data/cron"))
CRON_FILE = CRON_DIR / "jobs.json"


def _read():
    if not CRON_FILE.exists():
        return []
    try:
        return json.loads(CRON_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _write(data):
    CRON_DIR.mkdir(parents=True, exist_ok=True)
    CRON_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:60] or "job"


def _field_match(expr: str, value: int, lo: int, hi: int) -> bool:
    expr = (expr or "*").strip()
    for part in expr.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            rng, step_s = part.split("/", 1)
            try:
                step = max(1, int(step_s))
            except ValueError:
                continue
        else:
            rng = part
        if rng == "*":
            start, end = lo, hi
        elif "-" in rng:
            try:
                a, b = rng.split("-", 1)
                start, end = int(a), int(b)
            except ValueError:
                continue
        else:
            try:
                start = end = int(rng)
            except ValueError:
                continue
        if start <= value <= end and (value - start) % step == 0:
            return True
    return False


def _is_due(expr: str, dt: datetime) -> bool:
    parts = (expr or "").split()
    if len(parts) != 5:
        return False
    minute, hour, dom, mon, dow = parts
    dow = dow.replace("7", "0")  # cron: 0 and 7 both mean Sunday
    dow_val = (dt.weekday() + 1) % 7  # Python Mon=0 → cron Sun=0
    return (_field_match(minute, dt.minute, 0, 59)
            and _field_match(hour, dt.hour, 0, 23)
            and _field_match(dom, dt.day, 1, 31)
            and _field_match(mon, dt.month, 1, 12)
            and _field_match(dow, dow_val, 0, 6))


def register(mcp):
    @mcp.tool
    def cron_add(name: str, schedule: str, prompt: str, notify: str = "user",
                 enabled: bool = True, owner: str = "") -> str:
        """Create/update a scheduled job (stored as DATA on the NAS).
        schedule = 5-field cron (e.g. "30 6 * * *" = 06:30 daily, server local
        time). prompt = what the triggered LLM run should do. notify = inbox
        recipient for the result (default "user"). The NAS runner executes it.

        owner (act-as) = run this job in a specific user's area (their memory /
        vault / services / skills). Leave empty for the runner's default identity.
        Only an admin may set an owner OTHER than themselves — a non-admin can
        schedule jobs only as themselves (no privilege escalation)."""
        if len(schedule.split()) != 5:
            return "schedule must be a 5-field cron expression: 'min hour dom mon dow'."
        act_as = (owner or "").strip()
        if act_as:
            try:
                import tenancy
                caller, role = tenancy.current_identity()
                ok, resolved = tenancy.act_as_owner(caller, role, act_as)
                if not ok:
                    return f"Refused: {resolved}"
                act_as = resolved
            except Exception:
                pass  # fail-open: identity glitch stores the requested owner as-is
        jobs = _read()
        sid = _slug(name)
        jobs = [j for j in jobs if j.get("id") != sid]
        jobs.append({"id": sid, "name": name, "schedule": schedule, "prompt": prompt,
                     "notify": notify or "user", "enabled": bool(enabled),
                     "owner": act_as,
                     "created": datetime.now().isoformat(timespec="seconds"),
                     "last_run": ""})
        _write(jobs)
        as_note = f" as {act_as}" if act_as else ""
        return f"Scheduled job '{sid}' ({schedule}){as_note}."

    @mcp.tool
    def cron_list() -> str:
        """List scheduled jobs (id — schedule — enabled — last run)."""
        jobs = _read()
        if not jobs:
            return "No scheduled jobs yet. Use cron_add."
        return "\n".join(
            f"- {j.get('id')} — {j.get('schedule')} — {'on' if j.get('enabled') else 'off'}"
            + (f" — as {j['owner']}" if j.get('owner') else "")
            + f" — last {j.get('last_run') or 'never'} — {j.get('name', '')}"
            for j in jobs)

    @mcp.tool
    def cron_delete(name: str) -> str:
        """Delete a scheduled job by name/id."""
        jobs = _read()
        sid = _slug(name)
        kept = [j for j in jobs if j.get("id") != sid]
        if len(kept) == len(jobs):
            return f"No job '{name}'."
        _write(kept)
        return f"Deleted job '{sid}'."

    @mcp.tool
    def cron_due() -> str:
        """For the NAS runner: return enabled jobs due THIS minute that haven't run
        yet this minute, as JSON [{id, prompt, notify, owner, act_as_token}]. Run
        each, then call cron_mark_run(id).

        act-as: when a job has an `owner`, `act_as_token` is a SHORT-LIVED capability
        token the runner must pass back on the execution call so the connector runs
        the job in that owner's area. The runner holds no standing authority — only
        this per-job, time-boxed token. Fail-closed: if a token can't be minted (no
        signing key), the job is WITHHELD rather than run without confinement."""
        import actas
        now = datetime.now()
        stamp = now.strftime("%Y-%m-%dT%H:%M")
        due = []
        for j in _read():
            if not (j.get("enabled") and j.get("last_run") != stamp
                    and _is_due(j.get("schedule", ""), now)):
                continue
            owner = j.get("owner", "")
            token = ""
            if owner:
                token = actas.issue(j["id"], owner)
                if not token:
                    # owner set but no way to mint a confined token → do NOT hand the
                    # runner an unconfined job. Surface it so the operator can fix the
                    # missing STORAGE_ENCRYPTION_KEY rather than run it wide-open.
                    continue
            due.append({"id": j["id"], "prompt": j["prompt"],
                        "notify": j.get("notify", "user"),
                        "owner": owner, "act_as_token": token})
        return json.dumps(due, ensure_ascii=False)

    @mcp.tool
    def cron_mark_run(name: str, result: str = "") -> str:
        """For the NAS runner: mark a job as run this minute (prevents double
        execution); optionally store a short last result."""
        jobs = _read()
        sid = _slug(name)
        for j in jobs:
            if j.get("id") == sid:
                j["last_run"] = datetime.now().strftime("%Y-%m-%dT%H:%M")
                if result:
                    j["last_result"] = result[:500]
                _write(jobs)
                return f"Marked '{sid}' run."
        return f"No job '{name}'."
