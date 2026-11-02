"""The multi-agent board (coordination.py): inbox, task lifecycle, agent registry.

Proves real behaviour through the in-memory MCP client: messages are addressed and
acknowledged, tasks are claimed under a lease and handed off, agents show presence,
and under AUTH_ENFORCE=1 each person sees only their own items while an admin sees
all. A corrupt board is refused instead of being silently replaced, and parallel
writers do not lose messages.
"""
import asyncio
import json
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastmcp import Client


def _call(server, tool, args=None):
    async def go():
        async with Client(server.mcp) as client:
            res = await client.call_tool(tool, args or {}, raise_on_error=False)
            return (res.content[0].text if res.content else ""), res.is_error
    return asyncio.run(go())


def _t(server, tool, args=None):
    return _call(server, tool, args)[0]


def _as(monkeypatch, person, agent="bot"):
    """Act as a non-admin person holding an agent key (role 'user')."""
    monkeypatch.setattr(sys.modules["authz"], "_identity", lambda: (person, False, {
        "aicortex_key": f"k-{person}", "key_scopes": ["all"], "agent": agent}))


def _as_admin(monkeypatch, person="root"):
    monkeypatch.setattr(sys.modules["authz"], "_identity", lambda: (person, False, {}))


@pytest.fixture
def board(monkeypatch, request):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    return request.getfixturevalue("fresh_server")


def _tasks():
    return json.loads(sys.modules["coordination"].TASKS_FILE.read_text(encoding="utf-8"))


def _put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _add(server, title, **kw):
    text = _t(server, "task_add", {"title": title, **kw})
    return text.split()[2].rstrip(":")


# ── inbox ────────────────────────────────────────────────────────────
def test_message_is_delivered_then_leaves_the_unread_list_when_acked(board):
    out, err = _call(board, "inbox_post", {"to": "hermes", "body": "build is green",
                                           "subject": "ci", "sender": "ci-bot"})
    assert not err and out.startswith("Posted message ") and out.endswith("to 'hermes'.")
    mid = out.split()[2]
    text = _t(board, "inbox_read", {"agent": "hermes"})
    assert f"[{mid}]" in text and "from ci-bot" in text and "ci: build is green" in text
    assert _t(board, "inbox_ack", {"message_id": mid}) == f"Marked {mid} read."
    assert _t(board, "inbox_read", {"agent": "hermes"}) == "No unread messages for 'hermes'."
    assert mid in _t(board, "inbox_read", {"agent": "hermes", "unread_only": False})


def test_broadcast_reaches_everyone_but_a_direct_message_only_its_addressee(board):
    _t(board, "inbox_post", {"to": "all", "body": "stand-up at ten"})
    _t(board, "inbox_post", {"to": "hermes", "body": "for hermes only"})
    assert "stand-up at ten" in _t(board, "inbox_read", {"agent": "other"})
    assert "for hermes only" not in _t(board, "inbox_read", {"agent": "other"})
    assert "for hermes only" in _t(board, "inbox_read", {"agent": "hermes"})


def test_read_returns_the_newest_messages_oldest_first_and_limit_floors_at_one(board):
    for n in (1, 2, 3):
        _t(board, "inbox_post", {"to": "hermes", "body": f"msg-{n}"})
    two = _t(board, "inbox_read", {"agent": "hermes", "limit": 2})
    assert "msg-1" not in two and two.index("msg-2") < two.index("msg-3")
    one = _t(board, "inbox_read", {"agent": "hermes", "limit": 0})
    assert "msg-3" in one and "msg-2" not in one


def test_empty_inbox_wording_and_unknown_message_ids_say_so(board):
    assert _t(board, "inbox_read", {"agent": "x", "unread_only": False}) == "No messages for 'x'."
    assert _t(board, "inbox_read", {"agent": "x"}) == "No unread messages for 'x'."
    assert _t(board, "inbox_ack", {"message_id": "nope"}) == "No message nope."
    assert _t(board, "inbox_delete", {"message_id": "nope"}) == "No message nope."


def test_delete_removes_one_message_and_purge_drops_only_read_ones(board):
    keep = _t(board, "inbox_post", {"to": "a", "body": "unread one"}).split()[2]
    gone = _t(board, "inbox_post", {"to": "a", "body": "to delete"}).split()[2]
    done = _t(board, "inbox_post", {"to": "a", "body": "handled"}).split()[2]
    assert _t(board, "inbox_delete", {"message_id": gone}) == f"Deleted message {gone}."
    _t(board, "inbox_ack", {"message_id": done})
    assert _t(board, "inbox_delete", {"purge_read": True}) == "Purged 1 read message(s)."
    left = _t(board, "inbox_read", {"agent": "a", "unread_only": False})
    assert keep in left and gone not in left and done not in left


def test_people_cannot_read_ack_or_delete_each_others_messages(monkeypatch, board):
    _as(monkeypatch, "alice")
    mid = _t(board, "inbox_post", {"to": "hermes", "body": "alice private"}).split()[2]
    _t(board, "inbox_ack", {"message_id": mid})
    _as(monkeypatch, "bob")
    assert "alice private" not in _t(board, "inbox_read", {"agent": "hermes", "unread_only": False})
    assert _t(board, "inbox_ack", {"message_id": mid}) == f"No message {mid}."
    assert _t(board, "inbox_delete", {"message_id": mid}) == f"No message {mid}."
    assert _t(board, "inbox_delete", {"purge_read": True}) == "Purged 0 read message(s)."
    _as(monkeypatch, "alice")
    assert "alice private" in _t(board, "inbox_read", {"agent": "hermes", "unread_only": False})
    _as_admin(monkeypatch)
    assert "alice private" in _t(board, "inbox_read", {"agent": "hermes", "unread_only": False})


def test_sender_is_stamped_from_the_token_not_taken_from_the_arguments(monkeypatch, board):
    _as(monkeypatch, "alice", agent="alice-bot")
    _t(board, "inbox_post", {"to": "hermes", "body": "hi", "sender": "bob/forged"})
    _as_admin(monkeypatch)
    text = _t(board, "inbox_read", {"agent": "hermes"})
    assert "from alice/alice-bot" in text and "forged" not in text


def test_an_unowned_event_such_as_a_webhook_is_visible_to_every_person(monkeypatch, board):
    mid = sys.modules["coordination"].post_inbox("alice", "hook payload", sender="webhook")
    _as(monkeypatch, "bob")
    assert f"[{mid}]" in _t(board, "inbox_read", {"agent": "alice"})


# ── tasks ────────────────────────────────────────────────────────────
def test_task_add_reports_routing_and_task_list_shows_it(board):
    out = _t(board, "task_add", {"title": "Slice bracket", "for_agent": "hermes",
                                 "needs": "cad", "session_id": "s-1"})
    assert out.endswith("Slice bracket (→ hermes, needs: cad)")
    line = _t(board, "task_list")
    assert "open owner=-→hermes — Slice bracket · needs:cad · sess:s-1" in line
    assert _t(board, "task_add", {"title": "Plain"}).endswith("Plain")


def test_task_list_filters_by_status_and_owner(board):
    a, b = _add(board, "first"), _add(board, "second")
    _t(board, "task_claim", {"task_id": a, "owner": "hermes"})
    assert _t(board, "task_list", {"status": "claimed"}).count("\n") == 0
    assert "first" in _t(board, "task_list", {"owner": "hermes"})
    assert "second" in _t(board, "task_list", {"status": "open"})
    assert _t(board, "task_list", {"status": "done"}) == "No matching tasks."
    assert b in _t(board, "task_list")


def test_created_by_is_stamped_from_the_token(monkeypatch, board):
    _as(monkeypatch, "alice", agent="a-bot")
    _t(board, "task_add", {"title": "T", "created_by": "bob"})
    assert _tasks()[0]["created_by"] == "alice/a-bot" and _tasks()[0]["person"] == "alice"


def test_a_claim_holds_against_other_agents_until_the_lease_ends(board):
    tid = _add(board, "Weld frame", session_id="s-9")
    ok = _t(board, "task_claim", {"task_id": tid, "owner": "hermes"})
    assert ok == f"hermes claimed task {tid}. Load context: session_load('s-9')."
    refused = _t(board, "task_claim", {"task_id": tid, "owner": "ada"})
    assert "already claimed by 'hermes'" in refused and "lease ends in" in refused
    assert _tasks()[0]["owner"] == "hermes"
    assert "claimed task" in _t(board, "task_claim", {"task_id": tid, "owner": "hermes"})
    tasks = _tasks()
    tasks[0]["lease_until"] = _ago(30)
    _put(sys.modules["coordination"].TASKS_FILE, tasks)
    assert "ada claimed task" in _t(board, "task_claim", {"task_id": tid, "owner": "ada"})
    assert _tasks()[0]["owner"] == "ada"


def test_claim_notes_assignment_and_rejects_unknown_or_finished_tasks(board):
    tid = _add(board, "Assigned", for_agent="hermes")
    assert "(note: it was assigned to 'hermes')" in _t(board, "task_claim", {"task_id": tid, "owner": "ada"})
    assert _t(board, "task_claim", {"task_id": "zzz", "owner": "ada"}) == "No task zzz."
    _t(board, "task_update", {"task_id": tid, "status": "done"})
    assert _t(board, "task_claim", {"task_id": tid, "owner": "ada"}) == f"Task {tid} is already done."


def test_task_next_ranks_assigned_then_matching_then_unspecific(board):
    _add(board, "needs cad", needs="cad, print")
    _add(board, "anything")
    _add(board, "mine", for_agent="hermes")
    _add(board, "others", for_agent="ada")
    plain = _t(board, "task_next", {"owner": "hermes"})
    assert plain.splitlines()[0].startswith("Recommended next")
    order = [plain.index(t) for t in ("mine", "anything", "needs cad")]
    assert order == sorted(order) and "others" not in plain
    capped = _t(board, "task_next", {"owner": "hermes", "caps": "CAD"})
    assert capped.index("needs cad") < capped.index("anything")


def test_task_next_uses_the_registered_capabilities_and_caps_at_three(board):
    _t(board, "agent_register", {"name": "hermes", "capabilities": "cad,print"})
    for n in range(5):
        _add(board, f"job-{n}", needs="print")
    out = _t(board, "task_next", {"owner": "hermes"}).splitlines()
    assert len(out) == 4 and all("needs:print" in ln for ln in out[1:])


def test_task_next_with_nothing_suitable_says_so(board):
    assert _t(board, "task_next", {"owner": "x"}) == "No open tasks."
    _add(board, "private", for_agent="ada")
    assert _t(board, "task_next", {"owner": "x"}) == "No open tasks suitable for 'x' right now."


def test_handoff_gives_the_recipient_ownership_and_an_inbox_message(board):
    tid = _add(board, "Calibrate", session_id="s-3")
    out = _t(board, "task_handoff", {"task_id": tid, "to": "ada", "note": "bed is level"})
    assert out == f"Handed task {tid} to 'ada'."
    t = _tasks()[0]
    assert (t["owner"], t["assigned"], t["status"]) == ("ada", "ada", "claimed")
    assert t["lease_until"] and t["notes"][0]["note"] == "bed is level"
    msg = _t(board, "inbox_read", {"agent": "ada"})
    assert "from board" in msg and "session_id=s-3" in msg and "Note: bed is level" in msg
    assert _t(board, "task_handoff", {"task_id": tid}) == f"Released task {tid} back to open."
    t = _tasks()[0]
    assert (t["owner"], t["status"]) == ("", "open") and "lease_until" not in t
    assert _t(board, "task_handoff", {"task_id": "zzz", "to": "ada"}) == "No task zzz."


def test_update_changes_status_drops_the_lease_and_appends_notes(board):
    tid = _add(board, "Paint")
    _t(board, "task_claim", {"task_id": tid, "owner": "ada"})
    assert _t(board, "task_update", {"task_id": tid, "note": "first coat"}) == \
        f"Updated task {tid} (status=claimed)."
    assert "lease_until" in _tasks()[0]
    _t(board, "task_update", {"task_id": tid, "status": "done", "note": "dry"})
    t = _tasks()[0]
    assert t["status"] == "done" and "lease_until" not in t
    assert [n["note"] for n in t["notes"]] == ["first coat", "dry"]
    assert _t(board, "task_update", {"task_id": "zzz", "status": "done"}) == "No task zzz."


def test_delete_removes_a_task_and_reports_unknown_ids(board):
    tid = _add(board, "Temp")
    assert _t(board, "task_delete", {"task_id": tid}) == f"Deleted task {tid}."
    assert _t(board, "task_delete", {"task_id": tid}) == f"No task {tid}."
    assert _t(board, "task_list") == "No matching tasks."


def test_people_see_and_change_only_their_own_tasks_an_admin_sees_all(monkeypatch, board):
    _as(monkeypatch, "alice")
    a = _add(board, "Alice task")
    _as(monkeypatch, "bob")
    _add(board, "Bob task")
    listing = _t(board, "task_list")
    assert "Bob task" in listing and "Alice task" not in listing
    for tool, args in (("task_claim", {"owner": "bob"}), ("task_update", {"status": "done"}),
                       ("task_handoff", {"to": "bob"}), ("task_delete", {})):
        assert _t(board, tool, {"task_id": a, **args}) == f"No task {a}."
    assert "Alice task" not in _t(board, "task_next", {"owner": "bob"})
    _as_admin(monkeypatch)
    both = _t(board, "task_list")
    assert "Alice task" in both and "Bob task" in both
    assert _t(board, "task_update", {"task_id": a, "status": "done"}).endswith("(status=done).")


def test_without_enforcement_every_person_sees_everything(monkeypatch, request):
    monkeypatch.setenv("AUTH_ENFORCE", "0")
    server = request.getfixturevalue("fresh_server")
    _as(monkeypatch, "alice")
    _add(server, "Shared by alice")
    _as(monkeypatch, "bob")
    assert "Shared by alice" in _t(server, "task_list")


def test_a_handoff_message_is_not_readable_by_another_person(monkeypatch, board):
    _as(monkeypatch, "alice")
    tid = _add(board, "Alice confidential plan")
    _t(board, "task_handoff", {"task_id": tid, "to": "hermes", "note": "secret detail"})
    _as(monkeypatch, "bob")
    assert "confidential" not in _t(board, "inbox_read", {"agent": "hermes"})


def test_an_unknown_task_status_is_refused_and_changes_nothing(board):
    tid = _add(board, "Status check")
    out = _t(board, "task_update", {"task_id": tid, "status": "banana", "note": "ignored"})
    assert out == "Refused: status must be one of open, claimed, blocked, done."
    assert _tasks()[0]["status"] == "open" and _tasks()[0]["notes"] == []
    assert _t(board, "task_update", {"task_id": tid, "status": "blocked"}).endswith("(status=blocked).")


# ── agents ───────────────────────────────────────────────────────────
def test_register_creates_then_refreshes_the_same_agent(board):
    assert _t(board, "agent_register", {"name": "hermes", "role": "cad", "capabilities": "cad"}) == \
        "Registered agent 'hermes' [online]."
    assert _t(board, "agent_register", {"name": "hermes", "status": "busy"}) == \
        "Updated agent 'hermes' [online]."
    listing = _t(board, "agent_list")
    assert listing.count("hermes") == 1 and "— cad — busy —" in listing and "caps: cad" in listing


def test_agent_list_orders_by_presence_and_empty_registry_says_so(board):
    assert _t(board, "agent_list") == "No agents registered yet."
    coord = sys.modules["coordination"]
    _put(coord.AGENTS_FILE, [
        {"name": "far", "last_seen": _ago(86400)}, {"name": "mid", "last_seen": _ago(900)},
        {"name": "near", "last_seen": _ago(5)}, {"name": "never", "last_seen": ""}])
    lines = _t(board, "agent_list").splitlines()
    assert [(ln.split()[1], ln.split()[2]) for ln in lines] == [
        ("near", "[online]"), ("mid", "[idle]"), ("far", "[away]"), ("never", "[away]")]
    assert [r.split()[1] for r in coord.agent_rows()] == ["near", "mid", "far", "never"]


def test_agent_names_belong_to_their_person(monkeypatch, board):
    _as(monkeypatch, "alice")
    _t(board, "agent_register", {"name": "hermes", "role": "mine"})
    _as(monkeypatch, "bob")
    assert _t(board, "agent_register", {"name": "hermes", "role": "evil"}).startswith(
        "Refused: agent name 'hermes' belongs to alice.")
    assert _t(board, "agent_remove", {"name": "hermes"}) == "Refused: agent 'hermes' belongs to alice."
    assert "hermes" not in _t(board, "agent_list")
    _as_admin(monkeypatch)
    assert "— mine —" in _t(board, "agent_list")
    assert _t(board, "agent_remove", {"name": "hermes"}) == "Removed agent 'hermes'."
    assert _t(board, "agent_remove", {"name": "hermes"}) == "No agent 'hermes'."


# ── helpers used by the catalog / cortex_start ───────────────────────
def test_presence_thresholds_and_unparseable_timestamps(board):
    c = sys.modules["coordination"]
    assert c._presence({"last_seen": _ago(c.ONLINE_SECS - 30)}) == "online"
    assert c._presence({"last_seen": _ago(c.ONLINE_SECS + 30)}) == "idle"
    assert c._presence({"last_seen": _ago(c.IDLE_SECS + 30)}) == "away"
    naive = (datetime.now(timezone.utc) - timedelta(seconds=10)).replace(tzinfo=None).isoformat()
    assert c._presence({"last_seen": naive}) == "online"
    assert c._presence({"last_seen": "garbage"}) == c._presence({}) == "away"


def test_lease_is_at_least_one_minute_and_defaults_to_an_hour(board):
    c = sys.modules["coordination"]
    mins = lambda v: round(-c._age_secs(c._lease_until(v)) / 60)  # noqa: E731
    assert (mins(5), mins(-9), mins(0)) == (5, 1, 60)


def test_capability_tags_split_on_commas_and_whitespace(board):
    c = sys.modules["coordination"]
    assert c._caps_set("CAD, print  mql5") == {"cad", "print", "mql5"}
    assert c._caps_set(["Cad", "print"]) == {"cad", "print"}
    assert c._caps_set(None) == set()


def test_open_tasks_and_board_summary_for_the_briefing(board):
    c = sys.modules["coordination"]
    assert c.board_overview() == [] and c.board_counts() == {}
    a = _add(board, "Bird house", project="Birdhouse")
    _add(board, "Bakery", project="bakery")
    _add(board, "No project")
    _t(board, "task_update", {"task_id": a, "status": "done"})
    assert [t["title"] for t in c.open_tasks("  BIRDHOUSE ")] == []
    assert [t["title"] for t in c.open_tasks("bakery")] == ["Bakery"]
    assert len(c.open_tasks()) == 2
    assert c.board_counts() == {"done": 1, "open": 2}
    view = c.board_overview(limit=1)
    assert view[0] == "  1 done · 2 open" and len(view) == 2 and "owner=-" in view[1]


def test_identity_failures_fail_open_for_views_and_unowned_for_stamps(monkeypatch, board):
    c = sys.modules["coordination"]
    monkeypatch.setattr(sys.modules["authz"], "effective_identity",
                        lambda: (_ for _ in ()).throw(RuntimeError("no identity")))
    assert c._person() == "" and c._sees({"person": "someone"}) is True


# ── robustness ───────────────────────────────────────────────────────
def test_a_corrupt_board_is_refused_and_left_untouched(board):
    path = sys.modules["coordination"].TASKS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    text, err = _call(board, "task_add", {"title": "would overwrite"})
    assert err and "unreadable" in text
    assert path.read_text(encoding="utf-8") == "{not json"


def test_parallel_posts_lose_no_message(board):
    c = sys.modules["coordination"]
    threads = [threading.Thread(target=c.post_inbox, args=("hermes", f"m{n}")) for n in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stored = json.loads(c.INBOX_FILE.read_text(encoding="utf-8"))
    assert sorted(m["body"] for m in stored) == sorted(f"m{n}" for n in range(16))
    assert len({m["id"] for m in stored}) == 16
