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
import functools
import json
import os
import re
from datetime import datetime, time, timedelta
from pathlib import Path

import store

CRON_DIR = Path(os.environ.get("CRON_DIR", "/data/cron"))
CRON_FILE = CRON_DIR / "jobs.json"


def _read():
    # Missing file = no jobs; an unreadable one raises instead of reading as empty
    # (the next write would otherwise replace every job).
    return store.read_json(CRON_FILE, [])


def _write(data):
    store.write_json(CRON_FILE, data)


def _serialized(fn):
    """Run ``fn`` under the cron-store lock (atomic read-modify-write)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with store.locked(CRON_FILE):
            return fn(*args, **kwargs)
    return wrapper


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


# Four years and a day, so a leap-day schedule (29 Feb) still finds its next run.
_HORIZON_DAYS = 4 * 366
_PART = re.compile(r"^(\*|(\d+)(?:-(\d+))?)(?:/(\d+))?$")
_FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day of month", 1, 31), ("month", 1, 12),
           ("weekday", 0, 7))


def next_run(expr: str, after: datetime | None = None) -> datetime | None:
    """The first minute after `after` (default: now, server time) at which `expr`
    is due — checked with the runner's own matcher (_is_due) — or None if it never
    is within four years."""
    parts = (expr or "").split()
    if len(parts) != 5:
        return None
    minute, hour, dom, mon, dow = parts
    dow = dow.replace("7", "0")
    start = (after or datetime.now()).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for offset in range(_HORIZON_DAYS):
        day = (start + timedelta(days=offset)).date()
        if not (_field_match(dom, day.day, 1, 31) and _field_match(mon, day.month, 1, 12)
                and _field_match(dow, (day.weekday() + 1) % 7, 0, 6)):
            continue
        for h in range(24):
            if not _field_match(hour, h, 0, 23):
                continue
            for m in range(60):
                dt = datetime.combine(day, time(h, m))
                if dt >= start and _field_match(minute, m, 0, 59) and _is_due(expr, dt):
                    return dt
    return None


def schedule_problem(expr: str) -> str:
    """'' for a schedule the runner can run, else a short reason — five fields,
    numbers in range, and at least one real date (no 30 February)."""
    parts = (expr or "").split()
    if len(parts) != 5:
        return "a schedule has five fields: minute hour day-of-month month weekday"
    for value, (label, lo, hi) in zip(parts, _FIELDS):
        for part in value.split(","):
            m = _PART.match(part)
            if not m:
                return f"{label} '{value}' is not a cron field"
            a, b, step = m.group(2), m.group(3), m.group(4)
            nums = [int(x) for x in (a, b) if x is not None]
            if any(n < lo or n > hi for n in nums) or (len(nums) == 2 and nums[0] > nums[1]):
                return f"{label} '{value}' is outside {lo}-{hi}"
            if step is not None and int(step) < 1:
                return f"{label} '{value}' has a step of 0"
    if next_run(expr) is None:
        return "this schedule never runs (no date matches it)"
    return ""


def job_rows() -> list[dict]:
    """The caller's visible jobs with their next run (server time; '' when off)."""
    rows = []
    for j in visible_jobs():
        nxt = next_run(j.get("schedule", "")) if j.get("enabled") else None
        row = {k: j.get(k, "") for k in ("id", "name", "schedule", "prompt", "notify", "owner",
                                         "created", "last_run", "last_result")}
        rows.append(dict(row, enabled=bool(j.get("enabled")),
                         next_run=nxt.strftime("%Y-%m-%dT%H:%M") if nxt else ""))
    return rows


def _caller():
    """(identity, role, is_runner) for the REAL current caller (the token identity —
    NOT the act-as owner), for owner-scoping and runner-only gates. Fail-safe to
    ('unknown', 'user', False)."""
    try:
        import authz
        return authz.real_caller()
    except Exception:
        return "unknown", "user", False


def visible_jobs() -> list:
    """The jobs the real caller may see: an admin all of them, anyone else their own."""
    jobs = _read()
    caller, role, _ = _caller()
    return jobs if role == "admin" else [j for j in jobs if j.get("owner") == caller]


def _runner_or_admin():
    """(ok, identity) — is the real caller the NAS runner or an admin?"""
    ident, role, is_runner = _caller()
    return (is_runner or role == "admin"), ident


def _runner_plumbing_ok():
    """(ok, reason) for token-minting / act-as-starting tools (cron_due,
    act_as_begin). Requires the runner/admin AND that NO act-as run is currently
    bound — otherwise a job (whose tool calls share the runner's connection) could
    harvest other jobs' tokens or nest identities. Closes that escalation."""
    try:
        import actas
        if actas.current(_caller()[0]):
            return False, "not allowed while an act-as run is active"
    except Exception:
        pass
    ok, _ = _runner_or_admin()
    return ok, ("" if ok else "runner/admin only")


def register(mcp):
    @mcp.tool
    @_serialized
    def cron_add(name: str, schedule: str, prompt: str, notify: str = "user",
                 enabled: bool = True, owner: str = "") -> str:
        """Create/update a scheduled job (stored as DATA on the NAS).
        schedule = 5-field cron (e.g. "30 6 * * *" = 06:30 daily, server local
        time). prompt = what the triggered LLM run should do. notify = inbox
        recipient for the result (default "user"). The NAS runner executes it.

        owner (act-as) = run this job in a user's area (their memory / vault /
        services / skills). A NON-ADMIN may schedule ONLY as themselves — their job
        is always tagged with their own identity and runs as them (no escalation). An
        ADMIN may set any owner, or leave it empty to run as the runner's default."""
        problem = schedule_problem(schedule)
        if problem:
            return (f"Refused: {problem}. A schedule is 'min hour dom mon dow', e.g. "
                    f"\"30 6 * * *\" = 06:30 daily, \"0 8 * * 1-5\" = 08:00 on weekdays.")
        caller, role, _ = _caller()
        req_owner = (owner or "").strip()
        if role == "admin":
            act_as = req_owner                      # admin: anyone, or "" = runner default
        else:
            if req_owner and req_owner != caller:
                return ("Refused: you can only schedule jobs as yourself — leave "
                        "owner empty (the job will run as you).")
            act_as = caller                          # non-admin: FORCED self act-as
        warn = ""
        if act_as:
            try:
                import actas
                if not actas.available():
                    warn = (" ⚠ act-as can't be secured (no STORAGE_ENCRYPTION_KEY) — "
                            "the runner will WITHHOLD this job until a signing key is set.")
            except Exception:
                pass
        jobs = _read()
        sid = _slug(name)
        taken = next((j for j in jobs if j.get("id") == sid), None)
        if taken and role != "admin" and taken.get("owner") != caller:
            return (f"Refused: a job named '{sid}' already belongs to someone else — "
                    "pick another name.")
        jobs = [j for j in jobs if j.get("id") != sid]
        history = taken or {}        # an edit keeps when the job was made and last ran
        jobs.append({"id": sid, "name": name, "schedule": schedule, "prompt": prompt,
                     "notify": notify or "user", "enabled": bool(enabled),
                     "owner": act_as,
                     "created": history.get("created")
                     or datetime.now().isoformat(timespec="seconds"),
                     "last_run": history.get("last_run", ""),
                     **({"last_result": history["last_result"]} if history.get("last_result") else {})})
        _write(jobs)
        as_note = f" as {act_as}" if act_as else ""
        return f"Scheduled job '{sid}' ({schedule}){as_note}.{warn}"

    @mcp.tool
    def cron_list() -> str:
        """List scheduled jobs (id — schedule — enabled — last run). A non-admin sees
        only their OWN jobs; an admin sees all."""
        jobs = visible_jobs()
        if not jobs:
            return "No scheduled jobs yet. Use cron_add."
        return "\n".join(
            f"- {j.get('id')} — {j.get('schedule')} — {'on' if j.get('enabled') else 'off'}"
            + (f" — as {j['owner']}" if j.get('owner') else "")
            + f" — last {j.get('last_run') or 'never'} — {j.get('name', '')}"
            for j in jobs)

    @mcp.tool
    @_serialized
    def cron_delete(name: str) -> str:
        """Delete a scheduled job by name/id. A non-admin may delete only their OWN
        jobs; an admin may delete any."""
        jobs = _read()
        sid = _slug(name)
        target = next((j for j in jobs if j.get("id") == sid), None)
        if not target:
            return f"No job '{name}'."
        caller, role, _ = _caller()
        if role != "admin" and target.get("owner") != caller:
            return "Refused: you can only delete your own jobs."
        _write([j for j in jobs if j.get("id") != sid])
        return f"Deleted job '{sid}'."

    @mcp.tool
    def act_as_begin(act_as_token: str, job_id: str = "") -> str:
        """For the NAS runner: switch the connector to run AS a job's owner, using the
        short-lived capability token from cron_due. Every following tool call in this
        run is scoped + gated as the owner (their memory / vault / services / skills)
        until act_as_end. Runner/admin only, and not while another act-as run is
        active; the token is validated server-side (signature, expiry, job match,
        single-use) BEFORE any identity switch."""
        ok, reason = _runner_plumbing_ok()
        if not ok:
            return f"Denied: act_as_begin is {reason}."
        import actas
        good, res = actas.begin(act_as_token, job_id or None, caller=_caller()[0])
        if not good:
            return f"Refused: {res}"
        return f"Acting as {res['sub']} for job {res['job']} (until token expiry)."

    @mcp.tool
    def act_as_end() -> str:
        """For the NAS runner: end the current act-as run and INVALIDATE its token
        (single-use), so the next job runs under the correct identity. Runner/admin;
        ends only the caller's own run."""
        ok, ident = _runner_or_admin()
        if not ok:
            return "Denied: only the NAS runner or an admin may end an act-as run."
        import actas
        actas.consume(ident)
        return "act-as ended (token invalidated)."

    @mcp.tool
    def cron_due() -> str:
        """For the NAS runner: return enabled jobs due THIS minute that haven't run
        yet this minute, as JSON [{id, prompt, notify, owner, act_as_token}]. Run
        each, then call cron_mark_run(id).

        act-as: when a job has an `owner`, `act_as_token` is a SHORT-LIVED capability
        token the runner must pass back on the execution call so the connector runs
        the job in that owner's area. The runner holds no standing authority — only
        this per-job, time-boxed token. Fail-closed: if a token can't be minted (no
        signing key), the job is WITHHELD rather than run without confinement.
        Runner/admin only, and not during an active act-as run (so a running job
        can't harvest other jobs' tokens)."""
        ok, reason = _runner_plumbing_ok()
        if not ok:
            return f"Denied: cron_due is {reason} (it mints act-as capability tokens)."
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
    @_serialized
    def cron_mark_run(name: str, result: str = "") -> str:
        """For the NAS runner: mark a job as run this minute (prevents double
        execution); optionally store a short last result. Runner/admin only."""
        ok, _ = _runner_or_admin()
        if not ok:
            return "Denied: cron_mark_run is for the NAS runner."
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
