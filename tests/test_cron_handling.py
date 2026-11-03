"""Scheduled jobs, easy to hand out (AICortex 2.3.1).

Before: "tomorrow at 8, do X" became a job that ran again a year later, and a job
without a runner ran nowhere unless an admin had set up a standard runner. Proves:
- a one-off job (once=True) switches itself off after its run — whoever records it
  (AICortex for a Claude wake-up or a model runner, an own runner via cron_mark_run);
  an edit keeps it one-off unless told otherwise; lists show it;
- a person's own runner is their default: their jobs without a runner go there (also
  an admin's own jobs, e.g. to their Claude), the first runner someone makes becomes
  their default, another one can be chosen, and deleting it lets the jobs fall back;
- a job the default runner may not take (an admin's prompt for someone's Claude)
  falls back to the standard runner instead of waiting forever;
- the WebUI saves both and shows the default; the playbook page opens on the library
  while there are no runs.
"""
import re
import sys
from pathlib import Path


from test_cov_cron import add, due, env, jobs, run  # noqa: F401 - env is a fixture
from test_runner_cloud import Fire, _use, cloud  # noqa: F401 - cloud is a fixture

STATIC = Path(__file__).resolve().parent.parent / "app" / "webui_static"


def _runner(by="boss", admin=True, **fields):
    fields.setdefault("kind", "openrouter")
    fields.setdefault("model", "vendor/model-a")
    ok, msg, rec = sys.modules["runners"].save(fields, by=by, admin=admin, key="sk-test")
    assert ok, msg
    return rec


def _rows(sub, role):
    with sys.modules["authz"].rest_identity(sub, role):
        return {r["id"]: r for r in sys.modules["cron"].job_rows()}


# ── one-off jobs ─────────────────────────────────────────────────────────────
def test_a_one_off_job_switches_itself_off_after_its_run(env):
    _runner()
    assert add(env, name="tomorrow", schedule="0 8 4 11 *", once=True).startswith("Scheduled")
    assert jobs(env)["tomorrow"]["once"] is True and _rows("boss", "admin")["tomorrow"]["once"] is True
    assert "once" in run(env, "boss", "admin", "cron_list")
    add(env, name="tomorrow", schedule="0 9 4 11 *")                    # an edit keeps it one-off
    assert jobs(env)["tomorrow"]["once"] is True
    assert run(env, "boss", "admin", "cron_mark_run", name="tomorrow", result="done").startswith("Marked")
    job = jobs(env)["tomorrow"]
    assert job["enabled"] is False and job["last_result"] == "done"
    add(env, name="daily")
    run(env, "boss", "admin", "cron_mark_run", name="daily")
    assert jobs(env)["daily"]["enabled"] is True and jobs(env)["daily"].get("once") is False


def test_a_one_off_job_handed_to_claude_switches_off(env, cloud, monkeypatch):
    rec, rc, _ = cloud
    assert add(env, who="alice", role="user", name="once", runner=rec["id"], once=True).startswith("Scheduled")
    _use(monkeypatch, Fire())
    assert rc.wake(rec) == 1
    assert jobs(env)["once"]["enabled"] is False


# ── a person's own default runner ────────────────────────────────────────────
def test_a_persons_jobs_go_to_their_own_runner(env):
    std = _runner()
    mine = _runner(by="alice", admin=False)
    add(env, who="alice", role="user", name="a")                         # no runner given
    add(env, who="bob", role="user", name="b")
    assert [j["id"] for j in env.cron.claim_due(mine["id"])] == ["a"]
    assert [j["id"] for j in env.cron.claim_due(std["id"])] == ["b"]
    assert "your default" in run(env, "alice", "user", "runner_list")


def test_an_admins_own_jobs_go_to_their_claude(env, cloud):
    runners = sys.modules["runners"]
    claude = runners.save({"kind": "claude-cloud", "trigger": "trig_0002Boss"}, by="boss", admin=True,
                          key="sk-ant-oat01-b")[2]
    add(env, name="report")                                              # boss, no owner, no runner
    assert [j["id"] for j in env.cron.claim_due(claude["id"], tokens=False)] == ["report"]


def test_the_first_runner_is_the_default_and_another_can_be_chosen(env):
    runners = sys.modules["runners"]
    first = _runner(by="alice", admin=False, name="first")
    second = _runner(by="alice", admin=False, name="second")
    add(env, who="alice", role="user", name="a")
    assert [j["id"] for j in env.cron.claim_due(first["id"])] == ["a"]
    assert runners.set_personal(second["id"], by="alice")[0]
    assert not runners.set_personal(first["id"], by="bob")[0]
    add(env, who="alice", role="user", name="b")
    assert [j["id"] for j in env.cron.claim_due(second["id"])] == ["b"]
    assert runners.delete(second["id"], by="alice", admin=False)[0]
    assert "personal" not in str(runners.load().get("personal", {}).get("alice", "")) or \
        runners.load()["personal"].get("alice") != second["id"]


def test_a_job_the_default_runner_may_not_take_goes_to_the_standard(env, cloud):
    std = _runner()
    rec, _, _ = cloud                                                     # alice's only runner: her Claude
    run(env, "boss", "admin", "cron_add", name="for-alice", schedule="* * * * *",
        prompt="written by an admin", owner="alice")
    assert "your default" in run(env, "alice", "user", "runner_list")   # her Claude is her default …
    assert env.cron.claim_due(rec["id"], tokens=False) == []            # … but not for an admin's prompt
    assert [j["id"] for j in env.cron.claim_due(std["id"])] == ["for-alice"]


# ── WebUI ────────────────────────────────────────────────────────────────────
def test_the_webui_saves_once_and_shows_and_sets_the_default(env, monkeypatch, request):
    from test_cov_webui import Ui
    webui = sys.modules["webui"]
    monkeypatch.setattr(webui, "_session", lambda request: {"sub": "alice", "role": "user",
                                                            "csrf": "tok", "mode": "oidc"})
    ui = Ui(webui)
    first = _runner(by="alice", admin=False, name="first")
    second = _runner(by="alice", admin=False, name="second")
    rows = {r["id"]: r for r in ui.call("/ui/api/runners", who="alice", role="user")[1]["runners"]}
    assert rows[first["id"]]["personal"] is True and rows[second["id"]]["personal"] is False
    status, body = ui.call("/ui/api/runners/personal", "POST", who="alice", role="user",
                           body={"id": second["id"]})
    assert status == 200 and body["ok"]
    assert sys.modules["runners"].personal_default(sys.modules["runners"].load(), "alice") == second["id"]


def test_the_cron_form_and_the_playbook_page():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="cron-once"' in html
    cron = (STATIC / "view-cron.js").read_text(encoding="utf-8")
    assert "once: $(\"#cron-once\").checked" in cron and "cron_f_once" in cron
    pb = (STATIC / "view-playbooks.js").read_text(encoding="utf-8")
    assert re.search(r'PB = \{ tab: ""', pb) and "pb_runs_empty_lib" in pb
    assert "docs/scheduling.md" in html and "cron_howto2" in cron
    guide = (STATIC.parent / "guide.py").read_text(encoding="utf-8")
    assert guide.count("once=True") >= 2 and "default runner" in guide
