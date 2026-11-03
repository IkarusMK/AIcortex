"""Schedule jobs as DATA — the NAS holds the cron, a runner triggers an LLM run.

cron_add / cron_list / cron_delete let any LLM client (desktop/mobile) create
scheduled jobs that live on the NAS. A small runner on the NAS (a recurring
agent invocation — e.g. ``claude -p`` or any LLM CLI/SDK — via system cron,
every minute) calls ``cron_due`` each tick, runs each due job's prompt through
this connector, then ``cron_mark_run`` and notifies the user. The connector
stores the SCHEDULE; the runner provides the actual LLM execution (the model
can't self-trigger server-side). A minute the runner missed is caught up for
``CATCH_UP_MINUTES``.

Schedules are standard 5-field cron expressions in the server's local time:
``minute hour day-of-month month day-of-week`` (``*`` , ``,`` lists, ``a-b``
ranges and ``*/step`` are supported; dow 0/7 = Sunday).
"""
import functools
import json
import os
import re
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import store

CRON_DIR = Path(os.environ.get("CRON_DIR", "/data/cron"))
CRON_FILE = CRON_DIR / "jobs.json"
# When the runner last asked for due jobs — without a runner nothing ever runs.
RUNNER_FILE = CRON_DIR / "runner.json"
RUNNER_STALE_MINUTES = 5
# How far back cron_due looks for a scheduled minute the runner missed (a slow run,
# a restart). Only the latest missed minute of a job is handed out, once.
CATCH_UP_MINUTES = 5
MAX_PROMPT = 20_000        # characters of a job's prompt


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
    dow_val = (dt.weekday() + 1) % 7  # Python Mon=0 → cron Sun=0
    return (_field_match(minute, dt.minute, 0, 59)
            and _field_match(hour, dt.hour, 0, 23)
            and _field_match(dom, dt.day, 1, 31)
            and _field_match(mon, dt.month, 1, 12)
            and _dow_match(dow, dow_val))


def _latest_slot(expr: str, now: datetime, created: str = "") -> str:
    """The latest minute stamp within the catch-up window (now included) at which
    ``expr`` was due, never one before the job was created — '' when there is none."""
    start = now.replace(second=0, microsecond=0)
    floor = (created or "")[:16]
    for back in range(CATCH_UP_MINUTES + 1):
        slot = start - timedelta(minutes=back)
        stamp = slot.strftime("%Y-%m-%dT%H:%M")
        if floor and stamp < floor:
            return ""
        if _is_due(expr, slot):
            return stamp
    return ""


def _dow_match(expr: str, dow_val: int) -> bool:
    """Weekday field: 0 and 7 both mean Sunday — also inside ranges such as 1-7."""
    return _field_match(expr, dow_val, 0, 7) or (dow_val == 0 and _field_match(expr, 7, 0, 7))


# Four years and a day, so a leap-day schedule (29 Feb) still finds its next run.
_HORIZON_DAYS = 4 * 366
# Real schedules are short. Longer ones only cost CPU on every save, preview and
# listing (a 180 KB schedule froze the server for seconds), so they are refused.
MAX_SCHEDULE = 120
MAX_PARTS = 24
_PART = re.compile(r"^(\*|(\d+)(?:-(\d+))?)(?:/(\d+))?$")
_FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day of month", 1, 31), ("month", 1, 12),
           ("weekday", 0, 7))


def next_run(expr: str, after: datetime | None = None) -> datetime | None:
    """The first minute after `after` (default: now, server time) at which `expr`
    is due — checked with the runner's own matcher (_is_due) — or None if it never
    is within four years."""
    parts = (expr or "").split()
    if len(parts) != 5 or len(expr) > MAX_SCHEDULE or any(p.count(",") >= MAX_PARTS for p in parts):
        return None
    minute, hour, dom, mon, dow = parts
    # Each field once into the values it allows; the days are then cheap to scan.
    minutes = sorted(v for v in range(60) if _field_match(minute, v, 0, 59))
    hours = sorted(v for v in range(24) if _field_match(hour, v, 0, 23))
    doms = {v for v in range(1, 32) if _field_match(dom, v, 1, 31)}
    mons = {v for v in range(1, 13) if _field_match(mon, v, 1, 12)}
    dows = {v for v in range(7) if _dow_match(dow, v)}
    if not (minutes and hours and doms and mons and dows):
        return None
    start = (after or datetime.now()).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for offset in range(_HORIZON_DAYS):
        day = (start + timedelta(days=offset)).date()
        if day.day not in doms or day.month not in mons or (day.weekday() + 1) % 7 not in dows:
            continue
        for h in hours:
            for m in minutes:
                dt = datetime.combine(day, time(h, m))
                if dt >= start and _is_due(expr, dt):
                    return dt
    return None


def schedule_problem(expr: str) -> str:
    """'' for a schedule the runner can run, else a short reason — five fields,
    numbers in range, and at least one real date (no 30 February)."""
    if len(expr or "") > MAX_SCHEDULE:
        return f"a schedule has at most {MAX_SCHEDULE} characters"
    parts = (expr or "").split()
    if len(parts) != 5:
        return "a schedule has five fields: minute hour day-of-month month weekday"
    for value, (label, lo, hi) in zip(parts, _FIELDS):
        if value.count(",") >= MAX_PARTS:
            return f"{label} has more than {MAX_PARTS} entries"
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
                                         "runner", "created", "last_run", "last_result")}
        rows.append(dict(row, enabled=bool(j.get("enabled")), once=bool(j.get("once")),
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


def _runner_problem(runner: str, owner: str, by: str, role: str) -> str:
    """"" when a job of `owner`, prompt written by `by`, may go to `runner`
    ("" = the standard runner, "external" = an own program that fetches its jobs)."""
    import runners
    if not runner:
        return ""
    if runner == runners.EXTERNAL:
        return "" if role == "admin" else "Refused: only an admin may hand a job to an external runner."
    data = runners.load()
    rec = next((r for r in data["runners"] if r["id"] == runner), None)
    if rec and runners.may_run(rec, data["standard"], owner, by):
        return ""
    usable = [r["id"] for r in data["runners"] if runners.may_run(r, data["standard"], owner, by)]
    return (f"Refused: this job may not go to runner '{runner}'"
            + (f" — choose one of {', '.join(usable)} (runner_list)." if usable
               else " — set up a runner in the WebUI (tab \"Runner\")."))


def _author(taken, text: tuple, caller: str, owner: str, role: str) -> str:
    """Who wrote a job's text (name, prompt, recipient) — a Claude cloud runner runs only
    its owner's own. An edit that changes none of them keeps the author; a job from
    before 2.3 (no author) is taken on only by its owner (an admin for ownerless ones)."""
    if not taken or tuple(taken.get(k, "") for k in ("name", "prompt", "notify")) != text:
        return caller
    if taken.get("updated_by"):
        return taken["updated_by"]
    return caller if caller == owner or (not owner and role == "admin") else ""


def _key_problem(runner: str) -> str:
    """An agent key can't write a job for a Claude cloud runner: the job would run with
    the person's full rights and all their claude.ai connectors."""
    import authz
    import runners
    rec = runners.get(runner) if runner else None
    if rec and rec.get("kind") == runners.CLOUD and authz.is_agent_key():
        return ("Refused: a job for a Claude runner is written by the person themselves "
                "(WebUI or their own app), not with an agent key.")
    return ""


def _claim(pick, tokens: bool, rid: str = "") -> list:
    """Hand out the due jobs `pick` accepts — each once per scheduled minute (its slot).
    With `tokens`, a job with an owner gets a short-lived act-as token and is withheld
    when none can be minted. Caller holds the cron lock."""
    import actas
    now = datetime.now()
    due, out = [], []
    for j in _read():
        slot = _latest_slot(j.get("schedule", ""), now, j.get("created", ""))
        ready = (j.get("enabled") and slot and (j.get("last_run") or "") < slot
                 and j.get("claimed") != slot and pick(j))
        owner = j.get("owner", "")
        token = actas.issue(j["id"], owner) if ready and owner and tokens else ""
        if not ready or (owner and tokens and not token):
            out.append(j)
            continue
        due.append({"id": j["id"], "name": j.get("name") or j["id"], "prompt": j["prompt"],
                    "notify": j.get("notify", "user"), "owner": owner, "act_as_token": token})
        out.append(dict(j, claimed=slot, **({"claimed_by": rid} if rid else {})))
    if due:
        _write(out)
    return due


@_serialized
def claim_due(rid: str, tokens: bool = True) -> list:
    """For the built-in ticker: claim the due jobs of runner `rid` — those assigned to
    it, or unassigned when it is the standard runner, and that it may still run.
    A cloud runner passes tokens=False: its Claude session runs as the owner."""
    import runners
    data = runners.load()
    rec = next((r for r in data["runners"] if r["id"] == rid), None)
    if rec is None:
        return []
    return _claim(lambda j: runners.route(j, data) == rid
                  and runners.may_run(rec, data["standard"], j.get("owner", ""),
                                      j.get("updated_by", "")), tokens, rid)


@_serialized
def mark_run(name: str, result: str = "") -> bool:
    """Mark a job run this minute, with an optional short result → False if unknown."""
    jobs = _read()
    sid = _slug(name)
    if not any(j.get("id") == sid for j in jobs):
        return False
    stamp = datetime.now().strftime("%Y-%m-%dT%H:%M")
    extra = {"last_result": result[:500]} if result else {}
    _write([dict(j, last_run=stamp, **extra, **({"enabled": False} if j.get("once") else {}))
            if j.get("id") == sid else j for j in jobs])
    return True


def _own_cloud_job(job: dict, ident: str) -> bool:
    """Is `job` assigned to a cloud runner of `ident`? (Its Claude session reports back.)"""
    import runners
    rec = runners.get(job.get("runner") or "")
    return bool(ident) and bool(rec) and rec.get("kind") == runners.CLOUD and rec.get("owner") == ident


def _note_runner() -> None:
    try:
        store.write_json(RUNNER_FILE, {"last_seen": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    except OSError:
        pass


def runner_status() -> dict:
    """{last_seen, ok}: when the runner last asked for due jobs, and whether that
    was within RUNNER_STALE_MINUTES (it asks every minute)."""
    try:
        seen = str(store.read_json(RUNNER_FILE, {}).get("last_seen", ""))
    except Exception:
        seen = ""
    try:
        ok = bool(seen) and (datetime.now(timezone.utc) - datetime.fromisoformat(seen)
                             <= timedelta(minutes=RUNNER_STALE_MINUTES))
    except (TypeError, ValueError):
        ok = False
    return {"last_seen": seen, "ok": ok}


def _binding(ident: str) -> str:
    """The key the caller's act-as run is bound to (identity + credential, authz)."""
    try:
        import authz
        return authz.binding_key(ident)
    except Exception:
        return ident


def _runner_plumbing_ok():
    """(ok, reason) for token-minting / act-as-starting tools (cron_due,
    act_as_begin). Requires the runner/admin AND that NO act-as run is currently
    bound — otherwise a job (whose tool calls share the runner's connection) could
    harvest other jobs' tokens or nest identities. Closes that escalation."""
    try:
        import actas
        if actas.current(_binding(_caller()[0])):
            return False, "not allowed while an act-as run is active"
    except Exception:
        pass
    ok, _ = _runner_or_admin()
    return ok, ("" if ok else "runner/admin only")


def register(mcp):
    @mcp.tool
    @_serialized
    def cron_add(name: str, schedule: str, prompt: str, notify: str = "user",
                 enabled: bool = True, owner: str = "", runner: str | None = None,
                 once: bool | None = None) -> str:
        """Create/update a scheduled job (stored as DATA on the NAS).
        schedule = 5-field cron (e.g. "30 6 * * *" = 06:30 daily, server local
        time). prompt = what the triggered LLM run should do. notify = inbox
        recipient for the result (default "user").

        runner = which runner carries it out (an id from runner_list); leave it out to
        keep the job's runner — a new job then goes to the person's own default runner.

        once = True for a one-off job ("tomorrow at 8"): it switches itself off after
        its run, so a date schedule like "0 8 4 11 *" doesn't come back next year.

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
        history = taken or {}        # an edit keeps when the job was made and last ran
        if len(prompt or "") > MAX_PROMPT:
            return f"Refused: the prompt is too long — at most {MAX_PROMPT:,} characters."
        by = _author(taken, (name, prompt, notify or "user"), caller, act_as, role)
        runner = history.get("runner", "") if runner is None else str(runner).strip()
        problem = _runner_problem(runner, act_as, by, role) or _key_problem(runner)
        if problem:
            return problem
        jobs = [j for j in jobs if j.get("id") != sid]
        kept = {k: history[k] for k in ("last_result", "claimed", "claimed_by") if history.get(k)}
        jobs.append({"id": sid, "name": name, "schedule": schedule, "prompt": prompt,
                     "notify": notify or "user", "enabled": bool(enabled),
                     "owner": act_as, "runner": runner, "updated_by": by,
                     "once": bool(history.get("once")) if once is None else bool(once),
                     "created": history.get("created")
                     or datetime.now().isoformat(timespec="seconds"),
                     "last_run": history.get("last_run", ""), **kept})
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
            + (f" — runner {j['runner']}" if j.get('runner') else "")
            + (" — once" if j.get("once") else "")
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
        good, res = actas.begin(act_as_token, job_id or None, caller=_binding(_caller()[0]))
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
        actas.consume(_binding(ident))
        return "act-as ended (token invalidated)."

    @mcp.tool
    @_serialized
    def cron_due() -> str:
        """For the NAS runner: return enabled jobs that are due now, or were due in
        the last few minutes (a missed tick), and have not run since, as JSON
        [{id, prompt, notify, owner, act_as_token}]. Run each, then call
        cron_mark_run(id).

        act-as: when a job has an `owner`, `act_as_token` is a SHORT-LIVED capability
        token the runner must pass back on the execution call so the connector runs
        the job in that owner's area. The runner holds no standing authority — only
        this per-job, time-boxed token. Fail-closed: if a token can't be minted (no
        signing key), the job is WITHHELD rather than run without confinement.
        Runner/admin only, and not during an active act-as run (so a running job
        can't harvest other jobs' tokens). A job is handed out once per slot, so two
        runners never run it twice."""
        ok, reason = _runner_plumbing_ok()
        if not ok:
            return f"Denied: cron_due is {reason} (it mints act-as capability tokens)."
        _note_runner()
        import runners
        data = runners.load()
        # Only the jobs meant for an external runner: those assigned to it, and the
        # unassigned ones while no standard runner is on (as up to 2.2). A job with an
        # owner whose act-as token can't be minted is withheld, never run unconfined.
        due = _claim(lambda j: runners.route(j, data) == runners.EXTERNAL, tokens=True)
        return json.dumps([{k: v for k, v in d.items() if k != "name"} for d in due],
                          ensure_ascii=False)

    @mcp.tool
    @_serialized
    def cron_mark_run(name: str, result: str = "") -> str:
        """For the runner: mark a job as run this minute (prevents double execution);
        optionally store a short last result. Runner/admin only — and the owner of a
        Claude cloud runner for that runner's jobs (the Claude session reports back)."""
        ok, ident = _runner_or_admin()
        sid = _slug(name)
        if not ok and not any(j.get("id") == sid and _own_cloud_job(j, ident) for j in _read()):
            return "Denied: cron_mark_run is for the NAS runner."
        return f"Marked '{sid}' run." if mark_run(name, result) else f"No job '{name}'."
