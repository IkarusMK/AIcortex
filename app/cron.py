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


def _caller():
    """(identity, role, is_runner) for the REAL current caller (the token identity —
    NOT the act-as owner), for owner-scoping and runner-only gates. Fail-safe to
    ('unknown', 'user', False)."""
    try:
        import authz
        ident, is_runner, claims = authz._identity()
        return ident, authz.role_for(ident, is_runner, claims), is_runner
    except Exception:
        return "unknown", "user", False


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
        if actas.current():
            return False, "not allowed while an act-as run is active"
    except Exception:
        pass
    ok, _ = _runner_or_admin()
    return ok, ("" if ok else "runner/admin only")


def register(mcp):
    @mcp.tool
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
        if len(schedule.split()) != 5:
            return "schedule must be a 5-field cron expression: 'min hour dom mon dow'."
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
        jobs = [j for j in jobs if j.get("id") != sid]
        jobs.append({"id": sid, "name": name, "schedule": schedule, "prompt": prompt,
                     "notify": notify or "user", "enabled": bool(enabled),
                     "owner": act_as,
                     "created": datetime.now().isoformat(timespec="seconds"),
                     "last_run": ""})
        _write(jobs)
        as_note = f" as {act_as}" if act_as else ""
        return f"Scheduled job '{sid}' ({schedule}){as_note}.{warn}"

    @mcp.tool
    def cron_list() -> str:
        """List scheduled jobs (id — schedule — enabled — last run). A non-admin sees
        only their OWN jobs; an admin sees all."""
        jobs = _read()
        caller, role, _ = _caller()
        if role != "admin":
            jobs = [j for j in jobs if j.get("owner") == caller]
        if not jobs:
            return "No scheduled jobs yet. Use cron_add."
        return "\n".join(
            f"- {j.get('id')} — {j.get('schedule')} — {'on' if j.get('enabled') else 'off'}"
            + (f" — as {j['owner']}" if j.get('owner') else "")
            + f" — last {j.get('last_run') or 'never'} — {j.get('name', '')}"
            for j in jobs)

    @mcp.tool
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
        good, res = actas.begin(act_as_token, job_id or None)
        if not good:
            return f"Refused: {res}"
        return f"Acting as {res['sub']} for job {res['job']} (until token expiry)."

    @mcp.tool
    def act_as_end() -> str:
        """For the NAS runner: end the current act-as run and INVALIDATE its token
        (single-use), so the next job runs under the correct identity. Runner/admin."""
        ok, _ = _runner_or_admin()
        if not ok:
            return "Denied: only the NAS runner or an admin may end an act-as run."
        import actas
        actas.consume()
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
