"""runner/run-once.sh: the Claude Code runner starts the agent only when a job is due.

Before: every tick ran `claude -p` so the model could ask cron_due itself. Now, with
RUNNER_TOKEN set, due.mjs asks first; an idle tick ends without the agent, a due
job starts it with the jobs in the prompt, and a failing check is reported. Setups
without RUNNER_TOKEN keep the old way. (`node` and the agent are stand-ins here.)
"""
import json
import os
import subprocess
from pathlib import Path

RUN_ONCE = Path(__file__).resolve().parents[1] / "runner" / "run-once.sh"


def _tick(tmp_path, due_output, due_status=0, token="ak_runner_test"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    node = bin_dir / "node"
    node.write_text(f"#!/bin/sh\nprintf '%s\\n' '{due_output}'\nexit {due_status}\n")
    agent = bin_dir / "agent"
    record = tmp_path / "agent-args.json"
    agent.write_text("#!/usr/bin/env python3\nimport json, sys\n"
                     f"json.dump(sys.argv[1:], open({str(record)!r}, 'w'))\n")
    for f in (node, agent):
        f.chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", RUNNER_CMD=str(agent),
               HOME=str(tmp_path))
    if token:
        env["RUNNER_TOKEN"] = token
    else:
        env.pop("RUNNER_TOKEN", None)
    res = subprocess.run(["sh", str(RUN_ONCE)], env=env, capture_output=True, text=True, timeout=30)
    args = json.loads(record.read_text()) if record.exists() else None
    return res, args


def test_an_idle_tick_does_not_start_the_agent(tmp_path):
    res, args = _tick(tmp_path, "[]")
    assert res.returncode == 0 and args is None


def test_a_due_job_starts_the_agent_with_the_jobs_in_the_prompt(tmp_path):
    res, args = _tick(tmp_path, '[{"id":"report","prompt":"do report"}]')
    assert res.returncode == 0, res.stderr
    assert args is not None and '"id":"report"' in args[-1] and "do not call it" in args[-1]


def test_a_failed_check_is_reported_and_starts_nothing(tmp_path):
    res, args = _tick(tmp_path, "Denied: cron_due is runner/admin only")
    assert res.returncode == 1 and args is None and "cron_due: Denied" in res.stderr
    res, args = _tick(tmp_path, "", due_status=1)
    assert res.returncode != 0 and args is None


def test_without_a_runner_token_the_agent_asks_cron_due_itself(tmp_path):
    res, args = _tick(tmp_path, "[]", token="")
    assert res.returncode == 0 and args is not None and "Due jobs" not in args[-1]
