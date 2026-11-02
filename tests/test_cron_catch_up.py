"""cron_due catches up a minute the runner missed.

The runner asks for due jobs about once a minute, but a slow run or a restart can
skip a whole minute. cron_due used to hand out only jobs due in the current minute,
so a job whose minute was skipped never ran. Now it looks back a few minutes
(CATCH_UP_MINUTES) and hands out the latest missed slot of a job once - never a
slot from before the job existed and never one older than the window.

Clock frozen at Monday 2026-11-02 06:30 (see test_cov_cron).
"""
from test_cov_cron import add, due, env, jobs, run  # noqa: F401 - env is a fixture


def _created(env, job_id, when):
    data = env.cron._read()
    for j in data:
        if j["id"] == job_id:
            j["created"] = when
    env.cron._write(data)


def _ids(env):
    return [i["id"] for i in due(env)]


def test_a_job_whose_minute_was_missed_is_handed_out_late_and_only_once(env):
    add(env, name="late", schedule="28 6 * * *")
    _created(env, "late", "2026-11-02T06:00:00")
    assert _ids(env) == ["late"]
    run(env, "boss", "admin", "cron_mark_run", name="late")
    assert _ids(env) == []


def test_a_job_that_ran_on_an_earlier_day_is_still_caught_up(env):
    add(env, name="daily", schedule="28 6 * * *")
    _created(env, "daily", "2026-10-01T09:00:00")
    data = env.cron._read()
    data[0]["last_run"] = "2026-11-01T06:28"
    env.cron._write(data)
    assert _ids(env) == ["daily"]


def test_the_window_reaches_back_catch_up_minutes_and_no_further(env):
    edge = 30 - env.cron.CATCH_UP_MINUTES
    add(env, name="edge", schedule=f"{edge} 6 * * *")
    add(env, name="older", schedule=f"{edge - 1} 6 * * *")
    for job_id in ("edge", "older"):
        _created(env, job_id, "2026-11-02T05:00:00")
    assert _ids(env) == ["edge"]


def test_no_catch_up_for_a_minute_before_the_job_existed(env):
    add(env, name="same-minute", schedule="28 6 * * *")
    add(env, name="newer", schedule="28 6 * * *")
    _created(env, "same-minute", "2026-11-02T06:28:30")
    _created(env, "newer", "2026-11-02T06:29:00")
    assert _ids(env) == ["same-minute"]


def test_several_missed_slots_in_the_window_run_once(env):
    add(env, name="twice", schedule="27,29 6 * * *")
    _created(env, "twice", "2026-11-02T06:00:00")
    assert _ids(env) == ["twice"]
    assert jobs(env)["twice"]["last_run"] == ""
