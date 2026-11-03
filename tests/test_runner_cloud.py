"""A Claude cloud runner: AICortex wakes the person's Claude Code cloud task and sends
the due jobs along (runner_cloud.py, AICortex 2.3.0).

Before: Claude ran a job only through a setup-token on the Messages API, which
Anthropic refuses. Now AICortex calls the cloud task's API trigger; Claude runs the
jobs on the person's own plan. Proves:
- one wake-up carries the frame and exactly this runner's due jobs — nobody else's;
  long jobs are split so no wake-up exceeds Anthropic's limit;
- a successful wake-up records the run with the session link — only a claude.ai link;
- wake-ups keep two minutes apart (at most 30 an hour), honour Retry-After, and a
  failed one is neither retried (no duplicate sessions) nor recorded as a run;
- the token never shows in the state;
- a test wake-up sends a harmless check and no job;
- the ticker wakes Claude even while a model runner is still busy.
"""
import json
import sys
import threading
import time

import httpx
import pytest

from test_cov_cron import add, env, jobs, run  # noqa: F401 - env is a fixture

TOKEN = "sk-ant-oat01-test-token"
SESSION = "https://claude.ai/code/session_01Test"
REAL_CLIENT = httpx.Client


class Fire:
    """Stands in for Anthropic's /fire endpoint."""

    def __init__(self, status=200, body=None, headers=None):
        self.status, self.headers, self.requests = status, headers or {}, []
        self.body = body if body is not None else {
            "type": "routine_fire", "claude_code_session_id": "session_01Test",
            "claude_code_session_url": SESSION}

    def __call__(self, request):
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body, headers=self.headers)

    def texts(self):
        return [json.loads(r.content)["text"] for r in self.requests]


@pytest.fixture
def cloud(env, monkeypatch):
    runners = sys.modules["runners"]
    ok, msg, rec = runners.save({"kind": "claude-cloud", "trigger": "trig_0001AbC"},
                                by="alice", admin=False, key=TOKEN)
    assert ok, msg
    rc = sys.modules["runner_cloud"]
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(rc, "_clock", lambda: clock["t"])
    rc.reset()
    return rec, rc, clock


def _use(monkeypatch, fire):
    transport = httpx.MockTransport(fire)
    monkeypatch.setattr(sys.modules["runner_cloud"].httpx, "Client",
                        lambda **kw: REAL_CLIENT(transport=transport, **kw))
    return fire


def _state(rec):
    return sys.modules["runner_state"].get(rec["id"], True)


def test_a_wake_up_carries_exactly_this_runners_due_jobs(env, cloud, monkeypatch):
    rec, rc, _ = cloud
    other = sys.modules["runners"].save({"kind": "openrouter", "model": "m"}, by="alice",
                                        admin=False, key="sk-x")[2]
    add(env, who="alice", role="user", name="a", runner=rec["id"])
    add(env, who="alice", role="user", name="b", runner=other["id"])
    add(env, who="bob", role="user", name="c")
    fire = _use(monkeypatch, Fire())
    assert rc.wake(rec) == 1
    (req,) = fire.requests
    assert str(req.url) == "https://api.anthropic.com/v1/claude_code/routines/trig_0001AbC/fire"
    assert req.headers["authorization"] == f"Bearer {TOKEN}" and req.headers["anthropic-version"]
    (text,) = fire.texts()
    assert "AICortex wakes you" in text and "do a" in text
    assert "do b" not in text and "do c" not in text
    job = jobs(env)["a"]
    assert job["last_run"] and SESSION in job["last_result"]
    assert _state(rec)["state"] == "ran" and _state(rec)["link"] == SESSION
    assert TOKEN not in json.dumps(_state(rec))


def test_a_session_link_must_point_to_claude(env, cloud, monkeypatch):
    rec, rc, _ = cloud
    add(env, who="alice", role="user", name="a", runner=rec["id"])
    _use(monkeypatch, Fire(body={"claude_code_session_url": "https://evil.example.com/x"}))
    assert rc.wake(rec) == 1
    assert "evil" not in jobs(env)["a"]["last_result"] and _state(rec)["link"] == ""


def test_long_jobs_are_split_into_several_wake_ups(env, cloud, monkeypatch):
    rec, rc, _ = cloud
    for name in ("a", "b", "c", "d"):
        out = run(env, "alice", "user", "cron_add", name=name, schedule="* * * * *",
                  prompt=name * 19_000, runner=rec["id"])
        assert out.startswith("Scheduled")
    fire = _use(monkeypatch, Fire())
    assert rc.wake(rec) == 4
    texts = fire.texts()
    assert len(texts) == 2 and all(len(t) < rc.MAX_TEXT for t in texts)
    assert all(sum(n * 19_000 in t for t in texts) == 1 for n in "abcd")


def test_wake_ups_keep_apart_and_follow_retry_after(env, cloud, monkeypatch):
    rec, rc, clock = cloud
    add(env, who="alice", role="user", name="a", runner=rec["id"])
    fire = _use(monkeypatch, Fire())
    assert rc.wake(rec) == 1
    add(env, who="alice", role="user", name="b", runner=rec["id"])
    clock["t"] += 60
    assert rc.wake(rec) == 0 and "claimed" not in jobs(env)["b"]     # not claimed while waiting
    clock["t"] += 61
    assert rc.wake(rec) == 1 and len(fire.requests) == 2
    add(env, who="alice", role="user", name="c", runner=rec["id"])
    clock["t"] += 121
    _use(monkeypatch, Fire(status=429, body={"type": "error"}, headers={"retry-after": "600"}))
    assert rc.wake(rec) == 0 and _state(rec)["state"] == "error"
    add(env, who="alice", role="user", name="d", runner=rec["id"])
    clock["t"] += 300
    fire = _use(monkeypatch, Fire())
    assert rc.wake(rec) == 0 and fire.requests == []
    clock["t"] += 301
    assert rc.wake(rec) == 1


def test_a_failed_wake_up_is_neither_retried_nor_recorded(env, cloud, monkeypatch):
    rec, rc, clock = cloud
    add(env, who="alice", role="user", name="a", runner=rec["id"])
    _use(monkeypatch, Fire(status=401, body={"error": {"message": f"bad token {TOKEN}"}}))
    assert rc.wake(rec) == 0
    state = _state(rec)
    assert state["state"] == "error" and "401" in state["detail"] and TOKEN not in json.dumps(state)
    assert jobs(env)["a"]["last_run"] == ""
    clock["t"] += 121
    fire = _use(monkeypatch, Fire())
    assert rc.wake(rec) == 0 and fire.requests == []                # same minute: not again


def test_a_test_wake_up_sends_a_harmless_check(env, cloud, monkeypatch):
    rec, rc, _ = cloud
    add(env, who="alice", role="user", name="a", runner=rec["id"])
    fire = _use(monkeypatch, Fire())
    ok, msg = rc.test(rec)
    (text,) = fire.texts()
    assert ok and SESSION in msg and "test" in text.lower() and "do a" not in text
    assert "claimed" not in jobs(env)["a"]


def test_the_ticker_wakes_claude_while_a_model_runner_is_busy(env, cloud, monkeypatch):
    rec, rc, clock = cloud
    builtin = sys.modules["runner_builtin"]
    sys.modules["runners"].save({"kind": "openrouter", "model": "m"}, by="boss", admin=True, key="sk-x")
    gate, started = threading.Event(), []
    monkeypatch.setattr(builtin, "_work", lambda url: (started.append(1), gate.wait(5)))
    fire = _use(monkeypatch, Fire())
    builtin.tick("http://127.0.0.1:1/mcp")                         # the model run hangs
    add(env, who="alice", role="user", name="a", runner=rec["id"])
    clock["t"] += 121
    began = time.monotonic()
    builtin.tick("http://127.0.0.1:1/mcp")
    assert time.monotonic() - began < 2 and len(fire.requests) == 1
    assert started == [1]                                           # no second model run
    gate.set()
