"""The Claude Code SessionStart hook loads the long-term memory into every session.

Found 2026-10: Claude Code loads its own memory and CLAUDE.md by itself, while
AICortex needed the model to remember a tool call — so it was rarely used, and a
missing connection went unnoticed. The hook fetches the cortex_start briefing over
REST at the start of a session (and after /clear or a compaction) and says so
loudly when AICortex can't be reached. It never prints the key.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "clients" / "claude-code" / "session_start.py"


def _run(env_extra, cwd, source="startup"):
    env = {k: v for k, v in os.environ.items() if not k.startswith("AICORTEX_")}
    env.update(env_extra)
    stdin = json.dumps({"session_id": "s1", "transcript_path": "/tmp/t.jsonl", "cwd": str(cwd),
                        "hook_event_name": "SessionStart", "source": source})
    p = subprocess.run([sys.executable, str(HOOK)], input=stdin, capture_output=True, text=True,
                       env=env, timeout=30)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout), p.stdout + p.stderr


@pytest.fixture
def cortex(request):
    base = request.getfixturevalue("http_server")
    key, _ = sys.modules["apikeys"].create("alice", name="claude-code", scopes="all")
    return base, key


def test_briefing_for_the_repository_is_loaded_with_the_memory_rules(cortex, tmp_path):
    base, key = cortex
    repo = tmp_path / "work" / "garden-app"
    (repo / ".git").mkdir(parents=True)
    (repo / ".aicortex-project").write_text("Garden\n", encoding="utf-8")
    out, raw = _run({"AICORTEX_URL": base, "AICORTEX_KEY": key}, repo / "src")
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "briefing for project 'Garden'" in ctx
    assert "Long-term" in ctx and "session_save" in ctx      # how memory is split
    assert key not in raw and len(ctx) <= 10_000


def test_project_defaults_to_the_repository_name(cortex, tmp_path):
    base, key = cortex
    repo = tmp_path / "my-app"
    (repo / ".git").mkdir(parents=True)
    out, _ = _run({"AICORTEX_URL": base, "AICORTEX_KEY": key}, repo)
    assert "briefing for project 'my-app'" in out["hookSpecificOutput"]["additionalContext"]


def test_outside_a_repository_no_folder_name_is_sent_as_project(cortex, tmp_path):
    base, key = cortex
    plain = tmp_path / "someone-home"
    plain.mkdir()
    out, _ = _run({"AICORTEX_URL": base, "AICORTEX_KEY": key}, plain)
    assert "for project" not in out["hookSpecificOutput"]["additionalContext"]


def test_key_can_come_from_a_command_like_the_keychain(cortex, tmp_path):
    base, key = cortex
    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    cmd = f"{sys.executable} -c \"print('{key}')\""
    out, raw = _run({"AICORTEX_URL": base, "AICORTEX_KEY_CMD": cmd}, repo)
    assert "briefing for project 'proj'" in out["hookSpecificOutput"]["additionalContext"]
    assert key not in raw


def test_unreachable_cortex_is_reported_not_hidden(tmp_path):
    out, raw = _run({"AICORTEX_URL": "http://127.0.0.1:9", "AICORTEX_KEY": "ak_x_secretvalue"}, tmp_path)
    assert "AICortex" in out["systemMessage"]
    assert "tell the user" in out["hookSpecificOutput"]["additionalContext"].lower()
    assert "secretvalue" not in raw


def test_missing_configuration_is_explained(tmp_path):
    out, _ = _run({}, tmp_path)
    assert "AICORTEX_URL" in out["systemMessage"]


def test_plain_http_to_a_public_host_is_refused(tmp_path):
    out, raw = _run({"AICORTEX_URL": "http://agent.example.com", "AICORTEX_KEY": "ak_x_secretvalue"},
                    tmp_path)
    assert "https" in out["systemMessage"] and "secretvalue" not in raw
