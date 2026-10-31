"""Scheduled jobs: when they run next, and schedules that can't run are refused.

Before: cron_add accepted any five words — "61 * * * *" or "0 0 30 2 *" were stored
and silently never ran — and editing a job reset its last run and creation date.
The next run is computed with the runner's own matcher (_is_due), so what the
WebUI shows is what the runner does.
"""
import asyncio
import sys
from datetime import datetime

import pytest
from fastmcp import Client


@pytest.fixture
def cron(request):
    request.getfixturevalue("fresh_server")
    return sys.modules["cron"]


@pytest.mark.parametrize("expr, after, expected", [
    ("30 6 * * *", "2026-10-31 07:00", "2026-11-01 06:30"),        # daily, tomorrow
    ("0 8 * * 1-5", "2026-10-30 09:00", "2026-11-02 08:00"),       # weekdays: Fri → Mon
    ("*/15 * * * *", "2026-10-31 10:07", "2026-10-31 10:15"),      # every quarter hour
    ("0 9 * * 7", "2026-10-31 10:00", "2026-11-01 09:00"),         # 7 = Sunday
    ("0 0 1 1 *", "2026-10-31 10:00", "2027-01-01 00:00"),         # once a year
])
def test_next_run(cron, expr, after, expected):
    nxt = cron.next_run(expr, datetime.strptime(after, "%Y-%m-%d %H:%M"))
    assert nxt.strftime("%Y-%m-%d %H:%M") == expected


@pytest.mark.parametrize("expr", ["61 * * * *", "* * *", "a b c d e", "0 0 30 2 *", "0 24 * * *",
                                  "*/0 * * * *", "5-1 * * * *"])
def test_schedules_that_cannot_run_are_explained(cron, expr):
    assert cron.schedule_problem(expr)


@pytest.mark.parametrize("expr", ["*/5 1-3,5 * * 1-5", "0 7 * * 0", "15 * * * *", "0 0 29 2 *"])
def test_valid_schedules_pass(cron, expr):
    assert cron.schedule_problem(expr) == ""


def _call(server, tool, args):
    async def go():
        async with Client(server.mcp) as client:
            return (await client.call_tool(tool, args, raise_on_error=False)).content[0].text
    return asyncio.run(go())


def test_cron_add_refuses_a_schedule_that_never_runs(request):
    server = request.getfixturevalue("fresh_server")
    text = _call(server, "cron_add", {"name": "never", "schedule": "0 0 30 2 *", "prompt": "x"})
    assert "Refused" in text and "never" in text.lower()
    assert sys.modules["cron"]._read() == []


def test_editing_a_job_keeps_its_history(request):
    server = request.getfixturevalue("fresh_server")
    cron = sys.modules["cron"]
    _call(server, "cron_add", {"name": "nightly", "schedule": "0 3 * * *", "prompt": "a"})
    jobs = cron._read()
    jobs[0].update(created="2026-01-01T00:00:00", last_run="2026-10-30T03:00", last_result="ok")
    cron._write(jobs)
    _call(server, "cron_add", {"name": "nightly", "schedule": "0 4 * * *", "prompt": "b"})
    job = cron._read()[0]
    assert job["schedule"] == "0 4 * * *" and job["prompt"] == "b"
    assert (job["created"], job["last_run"], job["last_result"]) == (
        "2026-01-01T00:00:00", "2026-10-30T03:00", "ok")
