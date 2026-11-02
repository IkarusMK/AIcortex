"""Playbooks (app/playbooks.py): definition checks, change rights, run state machine.

Proves the refusals around the approval rules: stages and plays are validated (types,
counts, ids, references, YAML aliases, size); a shared playbook is changed only by an
admin or its author; install never overwrites without force; a run takes submissions
only while active, decisions only when something waits, never a chat decision on a
WebUI stage, never a decision on someone else's run; identity lookups degrade safely.
"""
import json
import sys
from types import SimpleNamespace

import pytest

STAGE = {"id": "plan", "name": "Plan", "goal": "Capture intent", "artifact": "intent.md",
         "template": "# Intent", "approval": "webui", "approver": "Owner"}
STAGE2 = {"id": "build", "name": "Build", "goal": "Do it", "artifact": "plan.md", "approval": "webui"}


def _register(mod):
    tools = {}
    mod.register(SimpleNamespace(tool=lambda fn: tools.setdefault(fn.__name__, fn)))
    return tools


@pytest.fixture
def pb(monkeypatch, fresh_server):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    mod = sys.modules["playbooks"]
    return SimpleNamespace(mod=mod, t=_register(mod), authz=sys.modules["authz"])


def _boom(*_a, **_k):
    raise RuntimeError("lookup broke")


def _flow(pb, who="alice", role="user", name="Flow", stages=None, **kw):
    with pb.authz.rest_identity(who, role):
        text = pb.mod.save(name, name, "A flow", stages or [STAGE, STAGE2], via_webui=True, **kw)
    assert text.startswith("Saved"), text


def _run(pb, who="alice", name="flow", project="Garden"):
    with pb.authz.rest_identity(who, "user"):
        run, problem = pb.mod.start(name, project)
    assert run, problem
    return run["id"]


def _edit_run(pb, run_id, fn):
    path = pb.mod._run_path(run_id)
    data = json.loads(path.read_text())
    fn(data)
    path.write_text(json.dumps(data))


def test_lists_come_from_lists_json_or_yaml_and_nothing_else(pb):
    ok = pb.mod._as_list
    assert ok([1], "stages") == ([1], "") and ok(None, "stages") == ([], "") and ok("", "stages") == ([], "")
    assert ok('[{"a": 1}]', "stages") == ([{"a": 1}], "") and ok("- a\n- b", "stages") == (["a", "b"], "")
    assert ok(5, "stages") == (None, "stages must be a list")
    assert ok("x" * (pb.mod.MAX_INPUT + 1), "stages") == (None, "stages are too large")
    bad, why = ok("a: [unclosed", "stages")
    assert bad is None and why.startswith("stages must be a list (JSON or YAML):")
    assert ok("a: &x [1]\nb: *x", "stages")[1].endswith("YAML aliases are not allowed")


@pytest.mark.parametrize("stages, problem", [
    ([], "at least one stage"), ("", "at least one stage"), ("{}", "at least one stage"),
    ([STAGE] * 21, "at most 20 stages"),
    (["text"], "stage 1 is not an object"),
    ([dict(STAGE, approval="whenever")], "approval must be one of webui, chat, none"),
    ([STAGE, STAGE], "id 'plan' is missing or used twice"),
    ([{"id": "!!!"}], "id '' is missing or used twice"),
    (5, "stages must be a list"),
])
def test_stage_lists_are_refused_with_a_reason(pb, stages, problem):
    out, why = pb.mod._clean_stages(stages)
    assert out == [] and problem in why


def test_stages_are_normalized_and_bounded(pb):
    long = {"name": "N" * 200, "goal": "g" * 900, "template": "t" * 9000, "approval": " ChAt "}
    (a, b), why = pb.mod._clean_stages([long, {"id": "Final Review"}])
    assert why == "" and (a["id"], b["id"]) == ("n" * 60, "final-review")
    assert (len(a["name"]), len(a["goal"]), len(a["template"]), a["approval"]) == (80, 600, 8000, "chat")
    assert b["approval"] == "webui" and b["name"] == "final-review"


@pytest.mark.parametrize("plays, problem", [
    ("x" * 5, "plays must be a list"), ("a: [unclosed", "plays must be a list (JSON or YAML)"), ([{"title": "no id"}], "every play needs an id"),
    (["bare"], "every play needs an id"), ([{"id": "a", "requires": "b"}], "requires must be a list"),
    ([{"id": "a", "stage": "ghost"}], "names an unknown stage 'ghost'"),
    ([{"id": "a", "requires": ["ghost"]}], "requires an unknown play"),
    ([{"id": str(i)} for i in range(101)], "at most 100"),
])
def test_plays_are_refused_with_a_reason(pb, plays, problem):
    out, why = pb.mod._clean_plays(plays, {"plan"})
    assert out == [] and problem in why


def test_plays_may_reference_later_plays_and_are_slugged(pb):
    out, why = pb.mod._clean_plays(
        [{"id": "Second Play", "stage": "Plan", "requires": ["First Play"], "title": " T "},
         {"id": "First Play"}], {"plan"})
    assert why == "" and out[0]["requires"] == ["first-play"] and out[0]["stage"] == "plan"
    assert pb.mod._clean_plays(None, set()) == ([], "")


def test_save_reports_bad_names_stages_plays_and_oversize(pb, monkeypatch):
    for name in ("", "   ", "!!!", "Runs", None):
        assert pb.mod.save(name, "t", "d", [STAGE]).startswith("Refused: give the playbook a name")
    assert pb.mod.save("x", "t", "d", []).startswith("Refused: a playbook needs at least one stage")
    assert pb.mod.save("x", "t", "d", [STAGE], plays=[{"id": "p", "stage": "nope"}]).startswith(
        "Refused: play 'p' names an unknown stage")
    monkeypatch.setattr(pb.mod, "MAX_INPUT", 10)
    assert pb.mod.save("x", "t", "d", [STAGE]) == "Refused: the playbook is too large."


def test_a_playbook_that_does_not_read_back_is_removed(pb, monkeypatch):
    monkeypatch.setattr(pb.mod.store, "write_text_atomic",
                        lambda path, text: path.parent.mkdir(parents=True, exist_ok=True)
                        or path.write_text("---\ntitle: [unclosed\n---\n"))
    assert pb.mod.save("x", "t", "d", [STAGE]) == "Refused: the playbook could not be stored readably."
    assert not (pb.mod.PLAYBOOKS_DIR / "x" / "PLAYBOOK.md").exists()


def test_load_and_listing_skip_everything_that_is_not_a_playbook(pb):
    assert pb.mod.listing() == [] and pb.mod.load("") is None and pb.mod.load("runs") is None
    _flow(pb, name="Good", plays=[{"id": "p1"}])
    base = pb.mod.PLAYBOOKS_DIR
    (base / "nohead").mkdir()
    (base / "nohead" / "PLAYBOOK.md").write_text("just text")
    (base / "aslist").mkdir()
    (base / "aslist" / "PLAYBOOK.md").write_text("---\n- a\n- b\n---\n")
    (base / "empty").mkdir()
    (base / "stray.txt").write_text("x")
    (base / "runs").mkdir()
    assert [p["name"] for p in pb.mod.listing()] == ["good"]
    assert pb.mod.listing()[0]["stages"] == 2
    assert pb.mod.load("nohead") is None and pb.mod.load("aslist") is None and pb.mod.load("empty") is None


def test_only_admin_or_author_changes_or_deletes_a_shared_playbook(pb):
    _flow(pb, who="alice")
    with pb.authz.rest_identity("bob", "user"):
        assert pb.mod.save("Flow", "t", "d", [STAGE]).startswith("Refused: 'flow' is a shared playbook")
        assert pb.mod.delete("flow").startswith("Refused: 'flow' is a shared playbook")
    assert pb.mod.load("flow") is not None
    with pb.authz.rest_identity("alice", "user"):
        assert pb.mod.save("Flow", "t", "d", [STAGE]).startswith("Saved")
    (pb.mod.PLAYBOOKS_DIR / "flow" / "notes.txt").write_text("keep")        # the folder outlives its file
    with pb.authz.rest_identity("boss", "admin"):
        assert pb.mod.delete("flow") == "Deleted playbook 'flow'."
    assert pb.mod.load("flow") is None and (pb.mod.PLAYBOOKS_DIR / "flow" / "notes.txt").exists()


def test_the_author_is_recognized_through_the_agent_suffix(pb, monkeypatch):
    pb.authz.POLICY_FILE.parent.mkdir(parents=True, exist_ok=True)
    pb.authz.POLICY_FILE.write_text(json.dumps({"roles": {"alice": "user"}}))
    monkeypatch.setattr(pb.authz, "_identity", lambda: ("alice", False, {"agent": "hermes"}))
    assert pb.mod.save("Flow", "t", "d", [STAGE]).startswith("Saved")
    assert pb.mod.load("flow")["author"] == "alice/hermes"
    monkeypatch.setattr(pb.authz, "_identity", lambda: ("alice", False, {}))
    assert pb.mod.save("Flow", "t", "d", [STAGE]).startswith("Saved")        # same person, other client


def test_an_unreadable_playbook_is_left_to_admins_and_delete_says_unknown(pb):
    folder = pb.mod.PLAYBOOKS_DIR / "broken"
    folder.mkdir(parents=True)
    (folder / "PLAYBOOK.md").write_text("garbage")
    with pb.authz.rest_identity("alice", "user"):
        assert pb.mod.save("Broken", "t", "d", [STAGE]).startswith("Refused: 'broken' is a shared")
    with pb.authz.rest_identity("boss", "admin"):
        assert pb.mod.save("Broken", "t", "d", [STAGE]).startswith("Saved")
    assert pb.mod.delete("ghost") == "No playbook 'ghost'."


def test_without_enforcement_anyone_may_change_and_a_broken_check_refuses(pb, monkeypatch):
    _flow(pb, who="alice")
    with monkeypatch.context() as m:
        m.setenv("AUTH_ENFORCE", "0")
        with pb.authz.rest_identity("bob", "user"):
            assert pb.mod.save("Flow", "t", "d", [STAGE]).startswith("Saved")
    with monkeypatch.context() as m:
        m.setattr(pb.authz, "enabled", _boom)
        assert pb.mod.save("Flow", "t", "d", [STAGE]) == "Refused: permissions could not be checked."
        assert pb.mod.delete("flow") == "Refused: permissions could not be checked."


def test_the_bundled_example_is_never_installed_over_an_existing_copy(pb):
    assert pb.mod.install_example("nope") == "No bundled template 'nope'."
    with pb.authz.rest_identity("boss", "admin"):
        assert pb.mod.install_example().startswith("Saved playbook 'ai-native-sdlc'")
        assert pb.mod.install_example().startswith("Refused: 'ai-native-sdlc' exists already")
        assert pb.mod.install_example(force=True).startswith("Saved")
    with pb.authz.rest_identity("bob", "user"):
        assert "shared playbook" in pb.mod.install_example(force=True)


def test_only_an_interactive_person_counts_and_identity_lookups_fail_safe(pb, monkeypatch):
    import actas
    person = lambda: ("alice", False, {})                                   # noqa: E731
    monkeypatch.setattr(pb.authz, "_identity", person)
    monkeypatch.setattr(actas, "current", lambda caller="": None)
    assert pb.mod._interactive_person() is True
    with pb.authz.rest_identity("alice", "user"):
        assert pb.mod._interactive_person() is False                        # REST caller
    for ident in (("alice", True, {}), ("alice", False, {"aicortex_key": "k"}),
                  ("unknown", False, {}), ("", False, {})):
        monkeypatch.setattr(pb.authz, "_identity", lambda ident=ident: ident)
        assert pb.mod._interactive_person() is False
    monkeypatch.setattr(pb.authz, "_identity", person)
    monkeypatch.setattr(actas, "current", lambda caller="": "owner")        # a scheduled job
    assert pb.mod._interactive_person() is False
    monkeypatch.setattr(actas, "current", _boom)
    assert pb.mod._interactive_person() is False
    monkeypatch.setattr(pb.authz, "effective_identity", _boom)
    assert pb.mod._caller() == ("unknown", "user")
    monkeypatch.setattr(pb.authz, "caller_label", _boom)
    assert pb.mod._label() == "unknown"
    monkeypatch.setattr(pb.authz, "sees", _boom)
    assert pb.mod._sees("alice") is False


def test_run_lookup_ignores_bad_ids_and_corrupt_files(pb):
    _flow(pb)
    rid = _run(pb)
    assert pb.mod.get_run("../x") is None and pb.mod.get_run("") is None and pb.mod.get_run("abcdef12") is None
    pb.mod._run_path(rid).write_text("{corrupt")
    assert pb.mod.get_run(rid) is None and pb.mod.runs_for(status="all") == []


def test_start_refuses_unknown_playbooks_and_too_many_runs(pb, monkeypatch):
    with pb.authz.rest_identity("alice", "user"):
        assert pb.mod.start("ghost", "p") == (None, "No playbook 'ghost'. playbook_list shows the library.")
    _flow(pb)
    monkeypatch.setattr(pb.mod, "MAX_ACTIVE_RUNS", 2)
    _run(pb), _run(pb)
    with pb.authz.rest_identity("alice", "user"):
        run, why = pb.mod.start("flow", "p")
    assert run is None and why.startswith("Refused: 2 active runs already")
    assert _run(pb, who="bob")                                              # other people unaffected


def test_runs_are_filtered_by_person_project_and_status_newest_first(pb):
    _flow(pb)
    a1, a2, b1 = _run(pb, project="Garden Shed"), _run(pb, project="Roof"), _run(pb, who="bob", project="Roof")
    _edit_run(pb, a1, lambda r: r.update(created="2020-01-01T00:00:00+00:00"))
    with pb.authz.rest_identity("alice", "user"):
        assert [r["id"] for r in pb.mod.runs_for()] == [a2, a1]
        assert [r["id"] for r in pb.mod.runs_for(project="garden-shed")] == [a1]
        assert pb.mod.runs_for(person="bob") == []                          # not hers to see
        assert pb.mod.cancel(a2)[1] == ""
        assert [r["id"] for r in pb.mod.runs_for()] == [a1]
        assert [r["id"] for r in pb.mod.runs_for(status="cancelled")] == [a2]
        assert len(pb.mod.runs_for(status="all")) == 2
    with pb.authz.rest_identity("boss", "admin"):
        assert [r["id"] for r in pb.mod.runs_for(person="bob")] == [b1]
        assert len(pb.mod.runs_for(status="all")) == 3


def test_submissions_are_validated_bounded_and_versioned(pb):
    _flow(pb)
    rid = _run(pb)
    assert pb.mod.submit(rid, "x" * (pb.mod.MAX_ARTIFACT + 1))[1].startswith("Refused: the artifact is too long")
    assert pb.mod.submit(rid, note="only a note")[1].startswith("Refused: submit the artifact text or a reference")
    run, why = pb.mod.submit(rid, ref_url="https://git.example.com/pr/1", note="n" * 600, ref_commit="a" * 100)
    v = run["stages"][0]["versions"][0]
    assert why == "" and (v["n"], len(v["note"]), len(v["ref"]["commit"])) == (1, 500, 64)
    assert pb.mod.submit(rid, "second")[0]["stages"][0]["versions"][-1]["n"] == 2
    assert pb.mod.submit("zzzzzzzz", "x")[1] == "No run 'zzzzzzzz'."
    assert pb.mod.submit("../etc", "x") == (None, "No run '../etc'.")
    _edit_run(pb, rid, lambda r: r["stages"][0].update(status="approved"))   # hand-edited state
    assert pb.mod.submit(rid, "x")[1] == "Refused: this stage is already approved."
    _flow(pb, name="Auto", stages=[dict(STAGE, approval="none"), STAGE2])
    auto, _ = pb.mod.submit(_run(pb, name="auto"), "x")                      # nobody to wait for
    assert auto["current"] == 1 and auto["stages"][0]["decision"]["via"] == "none"


def test_decisions_need_something_waiting_and_the_right_channel(pb):
    _flow(pb, stages=[dict(STAGE, approval="chat"), STAGE2])
    rid = _run(pb)
    assert pb.mod.decide(rid, "maybe")[1] == "Refused: decision must be 'approve' or 'reject'."
    assert pb.mod.decide(rid, "approve")[1] == "Refused: nothing is waiting for a decision in this stage."
    pb.mod.submit(rid, "draft")
    run, why = pb.mod.decide(rid, "reject", by="alice", via="chat", note="n" * 2000)
    assert why == "" and run["stages"][0]["status"] == "rejected" and len(run["stages"][0]["decision"]["note"]) == 1000
    pb.mod.submit(rid, "better")
    assert pb.mod.decide(rid, "approve", by="alice", via="chat")[0]["current"] == 1
    pb.mod.submit(rid, "plan")
    refused = pb.mod.decide(rid, "approve", by="alice", via="chat")[1]
    assert "approved by a person in the AICortex WebUI" in refused
    assert pb.mod.get_run(rid)["stages"][1]["status"] == "submitted"
    assert pb.mod.decide(rid, "approve", by="alice")[0]["status"] == "done"
    assert pb.mod.decide(rid, "approve")[1] == "Refused: the run is done."


def test_cancelled_runs_take_no_more_work_and_foreign_runs_look_missing(pb):
    _flow(pb)
    rid = _run(pb)
    with pb.authz.rest_identity("bob", "user"):
        assert pb.mod.cancel(rid)[1] == f"No run '{rid}'."
        assert pb.mod.decide(rid, "approve")[1] == f"No run '{rid}'."
    with pb.authz.rest_identity("alice", "user"):
        assert pb.mod.cancel(rid)[0]["status"] == "cancelled"
        assert pb.mod.cancel(rid)[1] == "Refused: the run is cancelled."
        assert pb.mod.submit(rid, "x")[1] == "Refused: the run is cancelled."


def test_next_step_and_describe_tell_what_is_due(pb):
    _flow(pb, stages=[dict(STAGE, approval="chat"), dict(STAGE2, artifact="", approval="webui")])
    rid = _run(pb)
    run = pb.mod.get_run(rid)
    assert pb.mod.next_step(run) == "stage 1/2 Plan: write intent.md and playbook_submit it"
    text = pb.mod.describe(run)
    assert "Goal: Capture intent" in text and "Approver: Owner" in text and "Template for intent.md:\n# Intent" in text
    assert "Goal:" not in pb.mod.describe(run, full=False)
    pb.mod.submit(rid, "x")
    run = pb.mod.get_run(rid)
    assert pb.mod.next_step(run) == "stage 1/2 Plan: intent.md waits for approval in the chat or the WebUI"
    described = pb.mod.describe(run)
    assert "Artifacts so far:\n  - Plan: v1 submitted" in described and "Template" not in described
    pb.mod.decide(rid, "reject", by="alice", via="chat", note="more detail")
    assert pb.mod.next_step(pb.mod.get_run(rid)).endswith("rejected — more detail; rework and playbook_submit again")
    pb.mod.submit(rid, "y")
    pb.mod.decide(rid, "approve", by="alice", via="chat")
    run = pb.mod.get_run(rid)
    assert pb.mod.next_step(run) == "stage 2/2 Build: write the result and playbook_submit it"
    pb.mod.submit(rid, "z")
    assert pb.mod.next_step(pb.mod.get_run(rid)).endswith("result waits for approval in the WebUI")
    pb.mod.decide(rid, "reject", by="alice")
    assert pb.mod.next_step(pb.mod.get_run(rid)).endswith("rejected; rework and playbook_submit again")
    assert pb.mod.next_step({"status": "done"}) == "done"


def test_list_search_and_load_tools(pb):
    t = pb.t
    assert t["playbook_list"]().startswith("No playbooks yet")
    assert t["playbook_search"]("anything") == "No playbook matches 'anything'."
    _flow(pb, name="Garden Build", category="Dev", guidance="Keep it small.", source="https://docs.example.com/x",
          plays=[{"id": "p1", "stage": "plan", "title": "First"}, {"id": "p2", "stage": "build", "title": "Second", "requires": ["p1"]}])
    _flow(pb, name="Kitchen", category="Home", stages=[{"id": "x"}])
    assert t["playbook_list"](category="dev").splitlines() == ["- garden-build — Garden Build (2 stages, Dev): A flow"]
    assert t["playbook_list"](category="none").startswith("No playbooks yet")
    assert t["playbook_search"]("garden").startswith("- garden-build") and "kitchen" not in t["playbook_search"]("garden")
    assert t["playbook_search"]("small").startswith("- garden-build")      # a guidance hit counts
    assert t["playbook_search"]("a b") == "No playbook matches 'a b'."     # short words are ignored
    assert t["playbook_load"]("nope") == "No playbook 'nope'."
    text = t["playbook_load"]("garden-build")
    for part in ("# Garden Build (garden-build)", "Source: https://docs.example.com/x", "## Stage 1: Plan → intent.md",
                 "Approval: webui by Owner", "Template:\n# Intent", "- p1 (plan): First — no prerequisites",
                 "- p2 (build): Second — needs p1", "## Guidance\nKeep it small."):
        assert part in text
    assert "→ (no artifact)" in t["playbook_load"]("kitchen") and "## Plays" not in t["playbook_load"]("kitchen")


def test_write_and_delete_tools_keep_the_approval_rules(pb):
    t = pb.t
    with pb.authz.rest_identity("alice", "user"):
        saved = t["playbook_write"]("Via Tool", "T", "D", json.dumps([STAGE]), plays=json.dumps([{"id": "p"}]))
        assert saved == "Saved playbook 'via-tool' (1 stages)."
        loosened = t["playbook_write"]("Via Tool", "T", "D", [dict(STAGE, approval="none")])
        assert loosened.startswith("Refused: stage 'Plan' would need less than its 'webui' approval")
        assert t["playbook_delete"]("via-tool") == "Deleted playbook 'via-tool'."
        assert t["playbook_delete"]("via-tool") == "No playbook 'via-tool'."


def test_run_tools_report_problems_and_progress(pb):
    t = pb.t
    _flow(pb)
    with pb.authz.rest_identity("alice", "user"):
        assert t["playbook_start"]("ghost", "p").startswith("No playbook 'ghost'")
        started = t["playbook_start"]("flow", "Garden", "Watering")
        rid = pb.mod.runs_for()[0]["id"]
        assert started.startswith("Started.\nRun ") and "Watering" in started
        assert t["playbook_status"](rid).startswith(f"Run {rid}")
        assert t["playbook_status"]("deadbeef") == "No run 'deadbeef'."
        assert t["playbook_status"](project="garden").count("Run ") == 1
        assert t["playbook_status"](project="other") == "No active playbook runs."
        assert t["playbook_runs"]().startswith(f"- {rid} — Watering (flow, Garden): stage 1/2 Plan")
        assert t["playbook_runs"](status="done") == "No matching runs."
        assert t["playbook_submit"](rid).startswith("Refused: submit the artifact")
        assert t["playbook_submit"](rid, artifact="draft").startswith("Submitted.\nRun")
        assert t["playbook_cancel"]("deadbeef") == "No run 'deadbeef'."
        assert t["playbook_cancel"](rid) == f"Cancelled run {rid}."


def test_recorded_approvals_come_only_from_an_interactive_person(pb, monkeypatch):
    _flow(pb, stages=[dict(STAGE, approval="chat"), STAGE2])
    rid = _run(pb)
    pb.mod.submit(rid, "x")
    assert pb.t["playbook_record_approval"](rid, "approve").startswith("Refused: only the person themselves")
    assert pb.mod.get_run(rid)["stages"][0]["status"] == "submitted"
    monkeypatch.setattr(pb.mod, "_interactive_person", lambda: True)
    with pb.authz.rest_identity("alice", "user"):
        assert pb.t["playbook_record_approval"](rid, "approve", note="ok").startswith("Recorded.\nRun")
        assert pb.t["playbook_record_approval"](rid, "approve").startswith("Refused: nothing is waiting")
    assert pb.mod.get_run(rid)["stages"][0]["decision"]["via"] == "chat"
