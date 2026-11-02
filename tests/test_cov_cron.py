"""Scheduled jobs (app/cron.py): schedule parsing, ownership, and the runner hand-off.

Proves, with a frozen clock (Monday 2026-11-02 06:30):
- the cron matcher and the schedule check handle lists, ranges, steps, bad parts and
  impossible dates, and refuse over-long or crowded schedules;
- a user can schedule, list and delete only as themselves; an admin can act for anyone
  and keeps an edited job's history;
- cron_due hands the runner only enabled, due, not-yet-run jobs, gives owner jobs a
  token bound to that job and owner, WITHHOLDS them when no token can be minted, and
  refuses everyone but the runner or an admin - also while an act-as run is active;
- cron_mark_run / act_as_begin / act_as_end are runner-only and a used token is dead.
"""
import asyncio
import json
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastmcp.exceptions import ToolError

FROZEN = datetime(2026, 11, 2, 6, 30)            # a Monday, 06:30
STAMP = "2026-11-02T06:30"


# ── parsing (no server state needed) ─────────────────────────────────────────
@pytest.fixture
def cron(request):
    request.getfixturevalue("fresh_server")
    return sys.modules["cron"]


@pytest.mark.parametrize("expr, value, expected", [
    ("*", 5, True), ("", 5, True), ("5", 5, True), ("5", 6, False),
    ("1,5,10-12", 11, True), ("1,5,10-12", 7, False),
    ("10-20/5", 15, True), ("10-20/5", 16, False), ("10-20/5", 25, False),
    ("*/15", 45, True), ("*/15", 44, False), ("0-23/6", 18, True),
    ("*/0", 7, True),                      # a step of 0 is treated as 1
    ("5/10", 5, True), ("5/10", 15, False),
    ("*/x", 7, False), ("a-b", 3, False), ("1-x", 1, False), ("x", 3, False),
    ("3,x,4", 4, True),                    # a bad part is skipped, the others still count
])
def test_field_match(cron, expr, value, expected):
    assert cron._field_match(expr, value, 0, 59) is expected


@pytest.mark.parametrize("expr, due", [
    ("30 6 * * *", True), ("30 6 2 11 *", True), ("30 6 * * 1", True), ("30 6 * * 0", False),
    ("31 6 * * *", False), ("30 6 3 11 *", False), ("30 6 * 12 *", False),
    ("* * * *", False), ("* * * * * *", False), ("", False), (None, False),
])
def test_is_due_checks_every_field_and_the_field_count(cron, expr, due):
    assert cron._is_due(expr, FROZEN) is due


def test_sunday_is_0_and_7_and_monday_is_1(cron):
    sunday = datetime(2026, 11, 1, 9, 0)
    assert cron._is_due("0 9 * * 0", sunday) and cron._is_due("0 9 * * 7", sunday)
    assert cron._is_due("0 9 * * 1-7", sunday) and not cron._is_due("0 9 * * 1-5", sunday)
    assert cron._is_due("30 6 * * 1", FROZEN) and not cron._is_due("30 6 * * 7", FROZEN)


@pytest.mark.parametrize("expr", [
    "x" * 121, "* * * *", "* * * * * *", "", "61 * * * *", "* 24 * * *", "* * 0 * *",
    "* * 32 * *", "* * * 13 *", "0 0 30 2 *", "0 0 31 4 *",
    ",".join(str(i) for i in range(25)) + " * * * *",
])
def test_next_run_gives_up_on_impossible_or_oversized_schedules(cron, expr):
    assert cron.next_run(expr, FROZEN) is None


def test_next_run_is_strictly_after_ignores_seconds_and_finds_leap_days(cron):
    assert cron.next_run("30 6 * * *", FROZEN) == datetime(2026, 11, 3, 6, 30)
    assert cron.next_run("31 6 * * *", datetime(2026, 11, 2, 6, 30, 45)) == datetime(2026, 11, 2, 6, 31)
    assert cron.next_run("0 0 29 2 *", FROZEN) == datetime(2028, 2, 29, 0, 0)
    assert cron.next_run("0 0 * * 1", FROZEN) == datetime(2026, 11, 9, 0, 0)


@pytest.mark.parametrize("expr, reason", [
    ("x" * 121, "at most 120 characters"), ("* * * *", "five fields"), ("", "five fields"),
    (",".join(str(i) for i in range(25)) + " * * * *", "more than 24 entries"),
    ("a * * * *", "is not a cron field"), ("1-2-3 * * * *", "is not a cron field"),
    ("60 * * * *", "outside 0-59"), ("59-1 * * * *", "outside 0-59"), ("* 24 * * *", "outside 0-23"),
    ("* * 0 * *", "outside 1-31"), ("* * * 13 *", "outside 1-12"), ("* * * * 8", "outside 0-7"),
    ("*/0 * * * *", "step of 0"), ("0 0 30 2 *", "never runs"),
])
def test_schedule_problem_explains_each_kind_of_refusal(cron, expr, reason):
    assert reason in cron.schedule_problem(expr)


@pytest.mark.parametrize("expr", ["*/5 8-18 * * 1-5", "0 0 29 2 *", "0 9 * * 7", "1,2,3 * * * *"])
def test_schedule_problem_accepts_real_schedules(cron, expr):
    assert cron.schedule_problem(expr) == ""


# ── fixtures for the tool-level tests ────────────────────────────────────────
@pytest.fixture
def env(monkeypatch, request):
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("RUNNER_TOKEN", raising=False)
    server = request.getfixturevalue("fresh_server")
    cron = sys.modules["cron"]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return FROZEN if tz is None else FROZEN.replace(tzinfo=tz)

    monkeypatch.setattr(cron, "datetime", Clock)
    import actas
    return SimpleNamespace(server=server, cron=cron, actas=actas, mp=monkeypatch)


def run(env, sub, role, tool, **args):
    authz = sys.modules["authz"]

    async def go():
        with authz.rest_identity(sub, role):
            try:
                res = await env.server.mcp.call_tool(tool, args)
                return "\n".join(getattr(c, "text", "") for c in res.content or [])
            except ToolError as exc:
                return f"DENIED: {exc}"
    return asyncio.run(go())


def add(env, who="boss", role="admin", name="report", schedule="* * * * *", **kw):
    return run(env, who, role, "cron_add", name=name, schedule=schedule, prompt=f"do {name}", **kw)


def jobs(env):
    return {j["id"]: j for j in env.cron._read()}


def due(env, who="boss", role="admin"):
    text = run(env, who, role, "cron_due")
    return json.loads(text) if text.startswith("[") else text


# ── add / list / delete respect ownership ───────────────────────────────────
def test_a_user_can_schedule_only_as_themselves(env):
    assert "as alice" in add(env, "alice", "user", name="mine")
    assert jobs(env)["mine"]["owner"] == "alice"
    assert "as alice" in add(env, "alice", "user", name="mine2", owner="alice")
    text = add(env, "alice", "user", name="theirs", owner="bob")
    assert text.startswith("Refused: you can only schedule jobs as yourself") and "theirs" not in jobs(env)


def test_an_admin_schedules_for_anyone_or_for_the_runner_default(env):
    assert "as carol" in add(env, name="a", owner=" carol ")
    assert "as " not in add(env, name="b")
    assert (jobs(env)["a"]["owner"], jobs(env)["b"]["owner"]) == ("carol", "")


def test_a_user_cannot_take_over_someone_elses_job_name_but_an_admin_keeps_history(env):
    add(env, "alice", "user", name="report")
    text = add(env, "bob", "user", name="report", schedule="0 1 * * *")
    assert "already belongs to someone else" in text and jobs(env)["report"]["owner"] == "alice"
    run(env, "boss", "admin", "cron_mark_run", name="report", result="all fine")
    stored = env.cron._read()
    stored[0]["created"] = "2026-01-01T00:00:00"
    env.cron._write(stored)
    add(env, name="report", schedule="0 2 * * *", owner="alice")
    job = jobs(env)["report"]
    assert (job["schedule"], job["created"], job["last_run"], job["last_result"]) == (
        "0 2 * * *", "2026-01-01T00:00:00", STAMP, "all fine")


def test_a_bad_schedule_is_refused_and_nothing_is_stored(env):
    text = add(env, schedule="0 0 30 2 *")
    assert text.startswith("Refused: this schedule never runs") and env.cron._read() == []


def test_owner_jobs_warn_when_act_as_cannot_be_secured(env):
    env.mp.delenv("STORAGE_ENCRYPTION_KEY")
    assert "WITHHOLD" in add(env, name="a", owner="carol")
    assert "WITHHOLD" not in add(env, name="b")                      # no owner, nothing to secure
    env.mp.setattr(env.actas, "available", lambda: 1 / 0)             # a broken check never blocks
    assert add(env, name="c", owner="carol").startswith("Scheduled job 'c'")


def test_list_shows_each_person_their_own_jobs_and_the_admin_all(env):
    assert run(env, "alice", "user", "cron_list") == "No scheduled jobs yet. Use cron_add."
    add(env, "alice", "user", name="mine")
    add(env, name="theirs", owner="carol", enabled=False)
    mine = run(env, "alice", "user", "cron_list")
    assert "mine" in mine and "theirs" not in mine and "— on — as alice — last never — mine" in mine
    everything = run(env, "boss", "admin", "cron_list")
    assert "theirs" in everything and "— off — as carol" in everything
    run(env, "boss", "admin", "cron_mark_run", name="mine")
    assert f"last {STAMP}" in run(env, "boss", "admin", "cron_list")


def test_job_rows_carry_the_next_run_only_for_enabled_jobs(env):
    add(env, name="on", schedule="45 6 * * *")
    add(env, name="off", schedule="45 6 * * *", enabled=False)
    with sys.modules["authz"].rest_identity("boss", "admin"):
        rows = {r["id"]: r for r in env.cron.job_rows()}
    assert (rows["on"]["next_run"], rows["on"]["enabled"]) == ("2026-11-02T06:45", True)
    assert (rows["off"]["next_run"], rows["off"]["enabled"]) == ("", False)


def test_delete_respects_ownership_and_slugs_the_name(env):
    add(env, "alice", "user", name="Daily Report")
    assert "your own jobs" in run(env, "bob", "user", "cron_delete", name="daily-report")
    assert "daily-report" in jobs(env)
    assert run(env, "bob", "user", "cron_delete", name="nothing") == "No job 'nothing'."
    assert run(env, "alice", "user", "cron_delete", name="Daily Report") == "Deleted job 'daily-report'."
    add(env, "alice", "user", name="again")
    assert run(env, "boss", "admin", "cron_delete", name="again") == "Deleted job 'again'." and not jobs(env)


def test_a_failing_identity_lookup_falls_back_to_an_unknown_user(env):
    add(env, name="orphan")
    env.mp.setattr(sys.modules["authz"], "real_caller", lambda: 1 / 0)
    assert env.cron._caller() == ("unknown", "user", False)
    assert env.cron.visible_jobs() == []                              # no admin rights by accident


# ── cron_due: who gets what ─────────────────────────────────────────────────
def _seed(env):
    add(env, name="plain")
    add(env, name="mine", owner="alice")
    add(env, name="off", enabled=False)
    add(env, name="later", schedule="45 6 * * *")
    add(env, name="ran")
    run(env, "boss", "admin", "cron_mark_run", name="ran")


def test_cron_due_returns_only_enabled_due_and_not_yet_run_jobs(env):
    _seed(env)
    items = due(env)
    assert [i["id"] for i in items] == ["plain", "mine"]
    assert set(items[0]) == {"id", "prompt", "notify", "owner", "act_as_token"}
    assert (items[0]["owner"], items[0]["act_as_token"], items[0]["notify"]) == ("", "", "user")


def test_an_owner_job_gets_a_token_bound_to_that_job_and_owner(env):
    _seed(env)
    mine = [i for i in due(env) if i["id"] == "mine"][0]
    ok, claims = env.actas.verify(mine["act_as_token"], expected_job="mine")
    assert ok and claims["sub"] == "alice"
    assert env.actas.verify(mine["act_as_token"], expected_job="plain") == (False, "job mismatch")


def test_an_owner_job_is_withheld_when_no_token_can_be_minted(env):
    _seed(env)
    env.mp.delenv("STORAGE_ENCRYPTION_KEY")
    assert [i["id"] for i in due(env)] == ["plain"]


def test_only_the_runner_or_an_admin_may_ask_for_due_jobs(env):
    _seed(env)
    text = due(env, "bob", "user")
    assert text == "Denied: cron_due is runner/admin only (it mints act-as capability tokens)."
    assert not env.cron.RUNNER_FILE.exists()                         # a refusal is not a heartbeat
    env.mp.setattr(env.cron, "_caller", lambda: ("runner", "user", True))
    assert [i["id"] for i in due(env, "runner", "user")] == ["plain", "mine"]
    assert env.cron.RUNNER_FILE.exists()


def test_a_failing_heartbeat_write_does_not_stop_the_runner(env):
    _seed(env)
    real = env.cron.store.write_json

    def flaky(path, data):
        if path == env.cron.RUNNER_FILE:
            raise OSError("read-only")
        return real(path, data)
    env.mp.setattr(env.cron.store, "write_json", flaky)
    assert [i["id"] for i in due(env)] == ["plain", "mine"]


@pytest.mark.parametrize("content, last_seen, ok", [
    (None, "", False), ("{broken", "", False), ('{"last_seen": "garbage"}', "garbage", False),
    ('{"last_seen": 12345}', "12345", False),
    ('{"last_seen": "2026-11-02T06:30:00"}', "2026-11-02T06:30:00", False),        # no time zone
    ('{"last_seen": "2026-11-02T06:28:00+00:00"}', "2026-11-02T06:28:00+00:00", True),
    ('{"last_seen": "2026-11-02T06:20:00+00:00"}', "2026-11-02T06:20:00+00:00", False),
])
def test_runner_status_reads_odd_heartbeat_files_safely(env, content, last_seen, ok):
    if content is not None:
        env.cron.CRON_DIR.mkdir(parents=True, exist_ok=True)
        env.cron.RUNNER_FILE.write_text(content, encoding="utf-8")
    assert env.cron.runner_status() == {"last_seen": last_seen, "ok": ok}


# ── act-as lifecycle ────────────────────────────────────────────────────────
def _token(env, job="mine"):
    _seed(env)
    return [i for i in due(env) if i["id"] == job][0]["act_as_token"]


def test_act_as_run_blocks_token_harvesting_nesting_and_replay(env):
    token = _token(env)
    assert run(env, "boss", "admin", "act_as_begin", act_as_token=token, job_id="mine") == \
        "Acting as alice for job mine (until token expiry)."
    assert "not allowed while an act-as run is active" in due(env)
    assert "not allowed while an act-as run is active" in run(
        env, "boss", "admin", "act_as_begin", act_as_token=token, job_id="mine")
    assert run(env, "boss", "admin", "act_as_end") == "act-as ended (token invalidated)."
    assert isinstance(due(env), list)                                 # free again
    assert run(env, "boss", "admin", "act_as_begin", act_as_token=token) == \
        "Refused: token already used (replay)"


def test_act_as_begin_refuses_users_wrong_jobs_and_bad_tokens(env):
    token = _token(env)
    assert run(env, "bob", "user", "act_as_begin", act_as_token=token) == \
        "Denied: act_as_begin is runner/admin only."
    assert run(env, "boss", "admin", "act_as_begin", act_as_token=token, job_id="plain") == \
        "Refused: job mismatch"
    assert run(env, "boss", "admin", "act_as_begin", act_as_token="junk") == "Refused: malformed token"
    body, sig = token.split(".")
    assert run(env, "boss", "admin", "act_as_begin", act_as_token=f"{body}.{sig[::-1]}") == \
        "Refused: bad signature"
    env.mp.delenv("STORAGE_ENCRYPTION_KEY")
    assert "unavailable" in run(env, "boss", "admin", "act_as_begin", act_as_token=token)


def test_only_the_runner_or_an_admin_ends_an_act_as_run(env):
    assert run(env, "bob", "user", "act_as_end").startswith("Denied: only the NAS runner or an admin")
    assert run(env, "boss", "admin", "act_as_end") == "act-as ended (token invalidated)."


# ── cron_mark_run ───────────────────────────────────────────────────────────
def test_marking_a_run_is_runner_or_admin_only_and_stops_double_execution(env):
    _seed(env)
    assert run(env, "alice", "user", "cron_mark_run", name="mine") == \
        "Denied: cron_mark_run is for the NAS runner."               # not even for the job's owner
    assert jobs(env)["mine"]["last_run"] == ""
    assert run(env, "boss", "admin", "cron_mark_run", name="mine") == "Marked 'mine' run."
    assert jobs(env)["mine"]["last_run"] == STAMP and jobs(env)["plain"]["last_run"] == ""
    assert [i["id"] for i in due(env)] == ["plain"]                   # not handed out twice


def test_mark_run_stores_a_trimmed_result_keeps_the_old_one_and_reports_unknown_jobs(env):
    add(env, name="Nightly Backup")
    assert run(env, "boss", "admin", "cron_mark_run", name="Nightly Backup", result="x" * 900) == \
        "Marked 'nightly-backup' run."
    assert jobs(env)["nightly-backup"]["last_result"] == "x" * 500
    run(env, "boss", "admin", "cron_mark_run", name="nightly-backup")
    assert jobs(env)["nightly-backup"]["last_result"] == "x" * 500
    assert run(env, "boss", "admin", "cron_mark_run", name="ghost") == "No job 'ghost'."
    env.mp.setattr(env.cron, "_caller", lambda: ("runner", "user", True))
    assert run(env, "runner", "user", "cron_mark_run", name="nightly-backup", result="ok") == \
        "Marked 'nightly-backup' run."


def test_a_broken_act_as_check_still_leaves_the_runner_or_admin_gate_in_force(env):
    _seed(env)
    env.mp.setattr(env.actas, "current", lambda caller="": 1 / 0)
    assert isinstance(due(env), list)                                 # admin: still served
    assert due(env, "bob", "user").startswith("Denied: cron_due is runner/admin only")
