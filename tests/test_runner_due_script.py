"""runner/due.mjs asks the connector for due jobs without any LLM.

The Claude Code runner used to start the model every minute just so the model
could call cron_due — even when nothing was due. due.mjs makes that one call over
MCP with the RUNNER_TOKEN, and run-once.sh only starts the agent when it returns
jobs. Needs Node (skipped without it).
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "runner" / "due.mjs"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


@pytest.fixture
def connector(monkeypatch, request):
    monkeypatch.setenv("RUNNER_TOKEN", "runner-secret-123")
    return request.getfixturevalue("http_server")


def _due(url, token="runner-secret-123"):
    env = dict(os.environ, CONNECTOR_URL=f"{url}/mcp", RUNNER_TOKEN=token)
    return subprocess.run([NODE, str(SCRIPT)], env=env, capture_output=True,
                          text=True, timeout=60)


def test_nothing_due_prints_an_empty_list(connector):
    res = _due(connector)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "[]"


def test_a_due_job_is_returned_as_json(connector):
    cron = sys.modules["cron"]
    cron._write([{"id": "report", "name": "report", "schedule": "* * * * *",
                  "prompt": "do report", "notify": "user", "enabled": True,
                  "owner": "", "created": "2026-01-01T00:00:00", "last_run": ""}])
    res = _due(connector)
    assert res.returncode == 0, res.stderr
    assert [j["id"] for j in json.loads(res.stdout)] == ["report"]


def test_a_wrong_token_fails_without_output(connector):
    res = _due(connector, token="nope")
    assert res.returncode == 1 and res.stdout == ""
    assert "cron_due failed: HTTP 401" in res.stderr
