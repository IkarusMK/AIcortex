"""Session handoff (app/sessions.py): checkpoints, rendering, retention and privacy.

Proves the paths the broader suite leaves out: a checkpoint needs a summary; a known
id is continued, a foreign one is refused; list/load/delete behave on empty, missing
and foreign sessions; stale sessions are pruned (and never when retention is 0 or the
timestamp is unreadable); a non-admin prunes only their own sessions and a broken
identity lookup prunes nothing; helper lookups fail open/closed as documented.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, fn):
        self.tools[fn.__name__] = fn
        return fn


@pytest.fixture
def env(monkeypatch, fresh_server):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    sessions = sys.modules["sessions"]
    monkeypatch.setattr(sessions, "RETENTION_DAYS", 90)
    fake = FakeMCP()
    sessions.register(fake)
    return SimpleNamespace(mod=sessions, t=fake.tools, authz=sys.modules["authz"])


def _stamp(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")


def _plant(sessions, sid, days_ago, **extra):
    """Write a session file whose last update lies `days_ago` days back."""
    sessions.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    data = {"id": sid, "title": sid, "created": _stamp(days_ago), "updated": _stamp(days_ago),
            "status": "open", "entries": [], "person": ""} | extra
    (sessions.SESSIONS_DIR / f"{sid}.json").write_text(json.dumps(data), encoding="utf-8")


def _boom(*_a, **_k):
    raise RuntimeError("lookup broke")


# ── saving ───────────────────────────────────────────────────────────────────
def test_a_checkpoint_without_summary_is_refused_and_writes_nothing(env):
    assert env.t["session_save"](summary="   ", title="Empty").startswith("Nothing to save")
    assert not list(env.mod.SESSIONS_DIR.glob("*.json"))


def test_a_new_session_is_named_after_its_title(env):
    text = env.t["session_save"](summary="Measured the hole", title="Fix Printer Queue!",
                                 model="claude", next_steps=" order a nozzle ")
    assert "'fix-printer-queue'" in text
    data = json.loads((env.mod.SESSIONS_DIR / "fix-printer-queue.json").read_text())
    assert data["title"] == "Fix Printer Queue!" and data["entries"][0]["next_steps"] == "order a nozzle"
    assert data["entries"][0]["model"] == "claude"


def test_without_title_or_id_the_session_gets_a_timestamp_id(env):
    text = env.t["session_save"](summary="quick note")
    assert "'session-" in text
    (only,) = list(env.mod.SESSIONS_DIR.glob("session-*.json"))
    assert json.loads(only.read_text())["entries"][0]["model"] == "unknown"


def test_a_known_id_is_continued_with_new_title_status_and_project(env):
    env.t["session_save"](summary="day one", session_id="trip", model="claude")
    env.t["session_save"](summary="day two", session_id="trip", title="Trip planning",
                          status="done", model="chatgpt", project="  Birdhouse ")
    data = json.loads((env.mod.SESSIONS_DIR / "trip.json").read_text())
    assert [e["model"] for e in data["entries"]] == ["claude", "chatgpt"]
    assert (data["title"], data["status"], data["project"]) == ("Trip planning", "done", "Birdhouse")


def test_a_hostile_session_id_stays_inside_the_sessions_dir(env):
    env.t["session_save"](summary="x", session_id="../../etc/passwd")
    (only,) = list(env.mod.SESSIONS_DIR.glob("*.json"))
    assert only.parent == env.mod.SESSIONS_DIR and only.stem == "etc-passwd"
    assert len(env.mod._slug("a" * 200)) == 60


def test_somebody_elses_session_cannot_be_extended(env):
    with env.authz.rest_identity("alice", "user"):
        env.t["session_save"](summary="alice private", session_id="s1")
    with env.authz.rest_identity("bob", "user"):
        text = env.t["session_save"](summary="bob injects", session_id="s1")
    assert text.startswith("Refused") and "belongs to someone else" in text
    data = json.loads((env.mod.SESSIONS_DIR / "s1.json").read_text())
    assert [e["summary"] for e in data["entries"]] == ["alice private"]
    assert data["person"] == "alice"


# ── listing, loading, rendering ──────────────────────────────────────────────
def test_list_and_load_say_so_when_there_are_no_sessions(env):
    assert env.t["session_list"]().startswith("No sessions yet")
    assert env.t["session_load"]().startswith("No sessions yet")
    assert env.t["session_load"]("nope") == "No session 'nope'. Use session_list."


def test_list_is_newest_first_and_load_renders_the_history(env):
    _plant(env.mod, "older", 3, title="Older work")
    env.t["session_save"](summary="Wired the sensor", next_steps="flash firmware",
                          title="Newer work", session_id="newer", model="claude")
    lines = env.t["session_list"]().splitlines()
    assert lines[0].startswith("- newer — Newer work") and "last: claude" in lines[0]
    assert lines[1].startswith("- older — Older work") and "last: ?" in lines[1]

    latest = env.t["session_load"]()                      # no id → where we left off
    assert latest.startswith("SESSION newer — Newer work")
    assert "Wired the sensor" in latest and "NEXT: flash firmware" in latest
    assert env.t["session_load"]("older").startswith("SESSION older — Older work")


def test_render_and_summary_line_tolerate_sparse_sessions(env):
    assert "(untitled)" in env.mod.summary_line({"id": "x"})
    text = env.mod.render({"id": "x", "entries": [{"model": "claude"}]})
    assert text.startswith("SESSION x — (untitled)") and "[open]" in text and "NEXT" not in text


def test_list_skips_unreadable_session_files(env):
    env.t["session_save"](summary="good", session_id="good")
    (env.mod.SESSIONS_DIR / "bad.json").write_text("{not json", encoding="utf-8")
    assert [s["id"] for s in env.mod._all()] == ["good"]
    assert env.t["session_list"]().count("\n") == 0


def test_recent_limits_and_orders_and_hides_foreign_sessions(env):
    _plant(env.mod, "a", 5, person="alice")
    _plant(env.mod, "b", 2, person="alice")
    _plant(env.mod, "c", 1, person="bob")
    with env.authz.rest_identity("alice", "user"):
        assert [s["id"] for s in env.mod.recent(5)] == ["b", "a"]
        assert [s["id"] for s in env.mod.recent(1)] == ["b"]
        assert env.mod.latest_open()["id"] == "b"
    with env.authz.rest_identity("boss", "admin"):
        assert env.mod.latest_open()["id"] == "c"


def test_latest_for_project_matches_a_normalized_name(env):
    _plant(env.mod, "old", 4, project="Birdhouse")
    _plant(env.mod, "new", 1, project="birdhouse ")
    _plant(env.mod, "other", 0, project="Garden")
    assert env.mod.latest_for_project("birdhouse")["id"] == "new"
    assert env.mod.latest_for_project("nothing") is None


# ── deleting ─────────────────────────────────────────────────────────────────
def test_delete_removes_own_session_and_reports_missing_ones(env):
    env.t["session_save"](summary="x", session_id="mine")
    assert env.t["session_delete"]("Mine") == "Deleted session 'mine'."
    assert not (env.mod.SESSIONS_DIR / "mine.json").exists()
    assert env.t["session_delete"]("mine") == "No session 'mine'."


def test_delete_does_not_confirm_or_remove_a_foreign_session(env):
    with env.authz.rest_identity("alice", "user"):
        env.t["session_save"](summary="x", session_id="s1")
    with env.authz.rest_identity("bob", "user"):
        assert env.t["session_delete"]("s1") == "No session 's1'."
    assert (env.mod.SESSIONS_DIR / "s1.json").exists()


# ── retention ────────────────────────────────────────────────────────────────
def test_saving_drops_sessions_older_than_the_retention_window(env):
    _plant(env.mod, "stale", 200)
    _plant(env.mod, "fresh", 10)
    text = env.t["session_save"](summary="today", session_id="today")
    assert "1 stale session(s) auto-removed" in text
    assert not (env.mod.SESSIONS_DIR / "stale.json").exists()
    assert (env.mod.SESSIONS_DIR / "fresh.json").exists()


def test_retention_zero_keeps_everything(env, monkeypatch):
    monkeypatch.setattr(env.mod, "RETENTION_DAYS", 0)
    _plant(env.mod, "ancient", 5000)
    assert env.mod._prune() == 0
    assert env.t["session_prune"]().startswith("Pruned 0 session(s)")
    assert (env.mod.SESSIONS_DIR / "ancient.json").exists()


def test_explicit_prune_uses_the_given_age_or_the_default(env):
    _plant(env.mod, "d10", 10)
    _plant(env.mod, "d100", 100)
    assert env.t["session_prune"](max_age_days=5) == "Pruned 2 session(s) older than 5 days."
    _plant(env.mod, "d100", 100)
    assert env.t["session_prune"]() == "Pruned 1 session(s) older than 90 days."


def test_unparsable_or_naive_timestamps_are_handled_without_data_loss(env):
    assert env.mod._age_days("garbage") == 0.0                    # never pruned
    naive = (datetime.now(timezone.utc) - timedelta(days=2)).replace(tzinfo=None).isoformat()
    assert 1.9 < env.mod._age_days(naive) < 2.1                   # read as UTC
    _plant(env.mod, "odd", 0, updated="not-a-date", created="")
    assert env.mod._prune(1) == 0 and (env.mod.SESSIONS_DIR / "odd.json").exists()


def test_a_session_that_cannot_be_unlinked_does_not_break_pruning(env, monkeypatch):
    _plant(env.mod, "stale", 200)

    class Stuck:
        def unlink(self):
            raise PermissionError("read-only")
    monkeypatch.setattr(env.mod, "_path", lambda sid: Stuck())
    assert env.mod._prune() == 0


def test_a_non_admin_prunes_only_their_own_sessions(env):
    _plant(env.mod, "alice-old", 200, person="alice")
    _plant(env.mod, "bob-old", 200, person="bob")
    with env.authz.rest_identity("alice", "user"):
        assert env.t["session_prune"]().startswith("Pruned 1 ")
    assert not (env.mod.SESSIONS_DIR / "alice-old.json").exists()
    assert (env.mod.SESSIONS_DIR / "bob-old.json").exists()
    with env.authz.rest_identity("boss", "admin"):
        assert env.t["session_prune"]().startswith("Pruned 1 ")


def test_prune_without_enforcement_covers_every_session(env, monkeypatch):
    monkeypatch.setenv("AUTH_ENFORCE", "0")
    _plant(env.mod, "bob-old", 200, person="bob")
    assert env.mod._own_only() is None
    assert env.t["session_prune"]().startswith("Pruned 1 ")


def test_prune_fails_closed_when_the_caller_cannot_be_resolved(env, monkeypatch):
    _plant(env.mod, "bob-old", 200, person="bob")
    monkeypatch.setattr(env.authz, "enabled", _boom)
    only = env.mod._own_only()
    assert only({"person": "bob"}) is False
    assert env.t["session_prune"]().startswith("Pruned 0 ")
    assert (env.mod.SESSIONS_DIR / "bob-old.json").exists()


# ── identity helpers ─────────────────────────────────────────────────────────
def test_person_is_empty_when_unknown_or_unresolvable(env, monkeypatch):
    assert env.mod._person() == ""                                # no token → "unknown"
    with env.authz.rest_identity("alice", "user"):
        assert env.mod._person() == "alice"
    monkeypatch.setattr(env.authz, "effective_identity", _boom)
    assert env.mod._person() == ""


def test_visibility_check_fails_open_when_authz_errors(env, monkeypatch):
    monkeypatch.setattr(env.authz, "sees", _boom)
    assert env.mod._sees({"person": "bob"}) is True
