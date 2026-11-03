"""Scheduled jobs go to a runner (cron.py with runners.py, AICortex 2.3.0).

Before: every due job went to whichever runner asked `cron_due`. Now a job names its
runner (`cron_add(runner=…)`); without one it goes to the standard runner, and only
when there is none (or it is off) to an external runner — as in 2.2. Proves:
- a job keeps its runner and who wrote its prompt; an edit without `runner` keeps it;
- a job may only go to a runner it may use — never into someone else's Claude, and an
  admin's prompt never into a person's cloud runner; `external` is for admins;
- the built-in ticker claims exactly its runner's jobs, once per minute, with act-as
  tokens for jobs that run in an owner's area; a job no longer allowed is skipped;
- `cron_due` (an external runner) gets only the jobs meant for it;
- the owner of a cloud runner may mark its jobs run (the Claude session reports back).
"""
import sys

import pytest

from test_cov_cron import add, due, env, jobs, run  # noqa: F401 - env is a fixture


def _runner(by="boss", admin=True, **fields):
    fields.setdefault("kind", "openrouter")
    fields.setdefault("model", "vendor/model-a")
    ok, msg, rec = sys.modules["runners"].save(fields, by=by, admin=admin, key="sk-test")
    assert ok, msg
    return rec


def _cloud(by):
    return _runner(by=by, admin=by == "boss", kind="claude-cloud", model="", trigger="trig_0001")


def _rows(sub, role):
    with sys.modules["authz"].rest_identity(sub, role):
        return {r["id"]: r for r in sys.modules["cron"].job_rows()}


def _as_runner(env):
    env.mp.setattr(env.cron, "_caller", lambda: ("runner", "user", True))


@pytest.fixture
def setup(env):
    std = _runner()                                       # boss's first: the standard
    mine = _runner(by="alice", admin=False, name="Alice OpenRouter")
    return std, mine


def test_a_job_remembers_its_runner_and_who_wrote_it(env, setup):
    std, mine = setup
    assert add(env, who="alice", role="user", name="a", runner=mine["id"]).startswith("Scheduled")
    job = jobs(env)["a"]
    assert (job["runner"], job["updated_by"], job["owner"]) == (mine["id"], "alice", "alice")
    assert _rows("alice", "user")["a"]["runner"] == mine["id"]
    assert mine["id"] in run(env, "alice", "user", "cron_list")


def test_an_edit_keeps_the_runner_unless_told(env, setup):
    std, mine = setup
    add(env, who="alice", role="user", name="a", runner=mine["id"])
    add(env, who="alice", role="user", name="a", enabled=False)
    assert jobs(env)["a"]["runner"] == mine["id"]
    add(env, who="alice", role="user", name="a", runner="")
    assert jobs(env)["a"]["runner"] == ""


def test_a_job_only_goes_to_a_runner_it_may_use(env, setup):
    std, mine = setup
    out = add(env, who="bob", role="user", name="b", runner=mine["id"])
    assert out.startswith("Refused") and f"choose one of {std['id']} (runner_list)" in out
    assert add(env, who="bob", role="user", name="b", runner="r_nope").startswith("Refused")
    assert add(env, who="alice", role="user", name="c", runner="external").startswith("Refused")
    assert add(env, name="d", runner="external").startswith("Scheduled")
    assert set(jobs(env)) == {"d"}


def test_an_admins_prompt_never_runs_in_a_persons_claude(env, setup):
    cloud = _cloud("alice")
    assert add(env, who="alice", role="user", name="a", runner=cloud["id"]).startswith("Scheduled")
    out = run(env, "boss", "admin", "cron_add", name="a", schedule="* * * * *",
              prompt="something else", owner="alice")
    assert out.startswith("Refused")
    assert jobs(env)["a"]["prompt"] == "do a"
    # switching it off leaves the prompt — and who wrote it — as it was
    assert add(env, name="a", owner="alice", enabled=False).startswith("Scheduled")
    assert jobs(env)["a"]["updated_by"] == "alice"
    assert add(env, who="boss", name="b", runner=cloud["id"]).startswith("Refused")


def test_the_ticker_claims_its_runners_jobs_once(env, setup):
    std, mine = setup
    add(env, who="alice", role="user", name="a", runner=mine["id"])
    add(env, name="b")                                               # unassigned → standard
    add(env, who="bob", role="user", name="c")                       # bob's, unassigned → standard
    got = env.cron.claim_due(mine["id"])
    assert [j["id"] for j in got] == ["a"] and got[0]["name"] == "a" and got[0]["act_as_token"]
    assert jobs(env)["a"]["claimed_by"] == mine["id"]
    assert env.cron.claim_due(mine["id"]) == []                      # once per minute
    assert "claimed" not in jobs(env)["b"]
    assert sorted(j["id"] for j in env.cron.claim_due(std["id"])) == ["b", "c"]


def test_a_cloud_runner_gets_its_jobs_without_act_as_tokens(env, setup):
    cloud = _cloud("alice")
    add(env, who="alice", role="user", name="a", runner=cloud["id"])
    (job,) = env.cron.claim_due(cloud["id"], tokens=False)
    assert (job["id"], job["prompt"], job["act_as_token"]) == ("a", "do a", "")


def test_a_job_no_longer_allowed_is_not_handed_out(env, setup):
    cloud = _cloud("alice")
    add(env, who="alice", role="user", name="a", runner=cloud["id"])
    env.cron._write([dict(j, updated_by="boss") for j in env.cron._read()])   # edited by hand
    assert env.cron.claim_due(cloud["id"], tokens=False) == []


def test_an_external_runner_gets_only_the_jobs_meant_for_it(env, setup):
    std, _ = setup
    add(env, name="x")
    add(env, name="y", runner="external")
    _as_runner(env)
    (item,) = due(env)
    assert item["id"] == "y" and set(item) == {"id", "prompt", "notify", "owner", "act_as_token"}
    sys.modules["runners"].set_enabled(std["id"], False, by="boss", admin=True)
    assert [j["id"] for j in due(env)] == ["x"]                      # standard off: as in 2.2


def test_the_owner_of_a_cloud_runner_marks_its_jobs_run(env, setup):
    std, mine = setup
    cloud = _cloud("alice")
    add(env, who="alice", role="user", name="a", runner=cloud["id"])
    add(env, who="alice", role="user", name="b", runner=mine["id"])
    assert run(env, "alice", "user", "cron_mark_run", name="a", result="done").startswith("Marked")
    assert jobs(env)["a"]["last_result"] == "done"
    assert run(env, "alice", "user", "cron_mark_run", name="b").startswith("Denied")
    assert run(env, "bob", "user", "cron_mark_run", name="a").startswith("Denied")
