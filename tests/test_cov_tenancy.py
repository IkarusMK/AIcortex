"""tenancy: non-admins are confined to their own data area, capabilities are default-deny.

Proves: one switch (AUTH_ENFORCE) turns areas on; admins and unresolved identities
are never confined; ``users/<slug>-<hash>`` areas are injective and traversal-safe;
a confined caller cannot name ``shared`` or another person's scope; services, skills
and device endpoints are DEFAULT-DENY under enforce and FAIL CLOSED (with an audit
line) on any error, but open in homelab mode; the act-as guard blocks privilege
escalation; policy.json is written atomically with a backup; and the admin-only
``tenancy_*`` tools manage the areas.
"""
import asyncio
import json
import re
from types import SimpleNamespace

import pytest
from fastmcp import Client


@pytest.fixture
def env(fresh_server, monkeypatch):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    import authz
    import tenancy

    def set_policy(obj):
        tenancy.AUTH_DIR.mkdir(parents=True, exist_ok=True)
        tenancy.POLICY_FILE.write_text(json.dumps(obj), encoding="utf-8")

    def audit():
        if not authz.AUDIT_FILE.exists():
            return []
        return [json.loads(x) for x in authz.AUDIT_FILE.read_text().splitlines() if x]

    def call(tool, **args):
        async def go():
            async with Client(fresh_server.mcp) as client:
                res = await client.call_tool(tool, args)
                return res.content[0].text
        return asyncio.run(go())

    return SimpleNamespace(t=tenancy, authz=authz, set_policy=set_policy, audit=audit,
                           call=call, off=lambda: monkeypatch.setenv("AUTH_ENFORCE", "0"),
                           patch=monkeypatch)


def _boom(*a, **k):
    raise RuntimeError("policy backend down")


# ── the switch ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    (None, True), ("", True), ("1", True), ("true", True), ("yes", True), ("garbage", True),
    ("0", False), ("false", False), ("No", False), ("OFF", False), (" off ", False),
])
def test_enforcement_is_on_unless_explicitly_disabled(env, value, expected):
    if value is None:
        env.patch.delenv("AUTH_ENFORCE", raising=False)
    else:
        env.patch.setenv("AUTH_ENFORCE", value)
    assert env.t.isolation_enabled() is expected


# ── area identities ─────────────────────────────────────────────────────────

def test_safe_segment_is_injective_and_traversal_proof(env):
    safe = env.t._safe
    assert safe("bob@x.com") != safe("bob.x.com")                # same slug, different hash
    assert safe("alice") == safe("alice")
    for raw in ("../../etc/passwd", "a/b\\c", "bob\x00", "Zoë 山田", "  ", "", None):
        assert re.fullmatch(r"[A-Za-z0-9_-]+", safe(raw)), raw
    assert safe("../../etc") .startswith("etc-")
    assert safe("") .startswith("user-")


def test_area_for_confines_a_plain_user_to_their_own_scope(env):
    area = env.t.area_for("alice", "user")
    assert area["confined"] is True
    assert area["memory_scope"] == area["vault_ns"] == f"users/{env.t._safe('alice')}"
    assert env.t.area_for("bob", "user")["memory_scope"] != area["memory_scope"]


@pytest.mark.parametrize("identity,role", [
    ("root", "admin"), ("", "user"), (None, "user"), ("unknown", "user")])
def test_admins_and_unresolved_identities_are_never_confined(env, identity, role):
    assert env.t.area_for(identity, role) == {"memory_scope": None, "vault_ns": None,
                                              "confined": False}


def test_homelab_mode_confines_nobody(env):
    env.off()
    assert env.t.area_for("alice", "user")["confined"] is False
    assert env.t.confine_memory_scope("alice", "user", "shared") == "shared"
    assert env.t.vault_namespace("alice", "user") == ""


def test_policy_can_widen_memory_and_vault_independently(env):
    env.set_policy({"users": {"alice": {"memory": " ALL ", "vault": "own"},
                              "bob": {"memory": "own", "vault": "all"}}})
    a, b = env.t.area_for("alice", "user"), env.t.area_for("bob", "user")
    assert a["memory_scope"] is None and a["vault_ns"].startswith("users/alice-")
    assert b["vault_ns"] is None and b["memory_scope"].startswith("users/bob-")


def test_corrupt_or_malformed_policy_falls_back_to_confinement(env):
    env.t.AUTH_DIR.mkdir(parents=True, exist_ok=True)
    env.t.POLICY_FILE.write_text("{ not json", encoding="utf-8")
    assert env.t.area_for("alice", "user")["confined"] is True
    env.set_policy({"users": ["alice"]})
    assert env.t.area_for("alice", "user")["memory_scope"].startswith("users/alice-")


# ── memory scope / vault namespace ──────────────────────────────────────────

@pytest.mark.parametrize("requested", ["shared", "", "users/other-0123456789", "../shared", "global"])
def test_confined_caller_cannot_choose_another_memory_scope(env, requested):
    own = f"users/{env.t._safe('alice')}"
    assert env.t.confine_memory_scope("alice", "user", requested) == own


def test_admin_and_widened_users_keep_their_requested_scope(env):
    env.set_policy({"users": {"carol": {"memory": "all"}}})
    assert env.t.confine_memory_scope("root", "admin", "projects") == "projects"
    assert env.t.confine_memory_scope("root", "admin", "") == "shared"
    assert env.t.confine_memory_scope("carol", "user", "projects") == "projects"


def test_private_data_resolution_failure_fails_open_by_design(env):
    env.patch.setattr(env.t, "area_for", _boom)
    assert env.t.confine_memory_scope("alice", "user", "projects") == "projects"
    assert env.t.confine_memory_scope("alice", "user", "") == "shared"
    assert env.t.vault_namespace("alice", "user") == ""


def test_vault_namespace_per_person(env):
    assert env.t.vault_namespace("alice", "user") == f"users/{env.t._safe('alice')}"
    assert env.t.vault_namespace("root", "admin") == ""


def test_rest_gate_rewrites_scope_for_a_user_but_not_for_an_admin(env):
    args = {"scope": "users/someone-else-0123456789", "key": "k"}
    assert env.authz.enforce_rest("alice", "user", "memory_read", args)[0]
    assert args["scope"] == f"users/{env.t._safe('alice')}"
    admin_args = {"scope": "projects"}
    assert env.authz.enforce_rest("root", "admin", "memory_read", admin_args)[0]
    assert admin_args["scope"] == "projects"


# ── identity resolution ─────────────────────────────────────────────────────

def test_current_identity_follows_the_request_scoped_binding(env):
    with env.authz.rest_identity("alice", "user"):
        assert env.t.current_identity() == ("alice", "user")


def test_current_identity_fails_safe_to_none(env):
    env.patch.setattr(env.authz, "effective_identity", _boom)
    assert env.t.current_identity() == (None, None)


# ── service / skill / endpoint capabilities (default-deny) ─────────────────

@pytest.mark.parametrize("spec,expected", [
    ("all", "all"), ("", "all"), ("  ALL ", "all"), ("none", frozenset()), (" None ", frozenset()),
    ("github, Docs  mail", frozenset({"github", "docs", "mail"})),
    (["GitHub", " ", "Docs"], frozenset({"github", "docs"})),
    (("x", "Y"), frozenset({"x", "y"})), ([], frozenset()),
    (7, frozenset()), ({"a": 1}, frozenset()), (None, frozenset()),
])
def test_access_spec_shapes(env, spec, expected):
    env.set_policy({"users": {"alice": {"services": spec}}})
    assert env.t._access_spec("alice", "user", "services") == expected


def test_access_spec_defaults_deny_when_nothing_is_assigned(env):
    env.set_policy({"users": {"alice": {"skills": "all"}, "mallory": "all"}})
    deny = frozenset()
    assert env.t._access_spec("alice", "user", "services") == deny      # no key
    assert env.t._access_spec("nobody", "user", "services") == deny     # unknown user
    assert env.t._access_spec("mallory", "user", "services") == deny    # entry not a dict
    assert env.t._access_spec("root", "admin", "services") == "all"
    env.set_policy({"users": "oops"})
    assert env.t._access_spec("alice", "user", "services") == deny
    env.off()
    assert env.t._access_spec("nobody", "user", "services") == "all"


def test_service_and_skill_match_by_name_or_category_case_insensitively(env):
    env.set_policy({"users": {"alice": {"services": ["GitHub", "Documents"], "skills": "none"}}})
    t = env.t
    assert t.service_allowed("alice", "user", "github", "Dev")
    assert t.service_allowed("alice", "user", " GITHUB ")
    assert t.service_allowed("alice", "user", "paperless", "documents")     # by category
    assert not t.service_allowed("alice", "user", "crafty", "Gaming")
    assert not t.service_allowed("alice", "user", "", "")                   # blanks never match
    assert not t.skill_allowed("alice", "user", "web-read", "Web")          # "none"
    assert t.service_allowed("root", "admin", "anything")
    assert not t.service_allowed("nobody", "user", "github")


@pytest.mark.parametrize("fn", ["service_allowed", "skill_allowed"])
def test_capability_check_errors_fail_closed_and_are_audited(env, fn):
    env.patch.setattr(env.t, "_access_spec", _boom)
    assert getattr(env.t, fn)("alice", "user", "github") is False
    rec = env.audit()[-1]
    assert rec["decision"] == "deny" and rec["identity"] == "alice"
    assert "area-check failed" in rec["reason"] and "policy backend down" in rec["reason"]


@pytest.mark.parametrize("fn", ["service_allowed", "skill_allowed"])
def test_capability_check_errors_do_not_lock_anyone_out_in_homelab(env, fn):
    env.patch.setattr(env.t, "_access_spec", _boom)
    env.off()
    assert getattr(env.t, fn)("alice", "user", "github") is True
    assert env.audit() == []


def test_a_failing_audit_never_turns_a_deny_into_an_allow_or_a_crash(env):
    env.patch.setattr(env.t, "_access_spec", _boom)
    env.patch.setattr(env.authz, "audit", _boom)
    assert env.t.service_allowed("alice", "user", "github") is False


def test_caller_checks_use_the_effective_identity(env):
    env.set_policy({"users": {"alice": {"services": ["github"], "skills": ["web"],
                                        "caldav": ["family-cal"]}}})
    t = env.t
    with env.authz.rest_identity("alice", "user"):
        assert t.caller_service_allowed("github") and not t.caller_service_allowed("crafty")
        assert t.caller_skill_allowed("web") and not t.caller_skill_allowed("mql")
        assert t.caller_endpoint_allowed("caldav", "Family-Cal")
        assert not t.caller_endpoint_allowed("caldav", "work-cal")
        assert not t.caller_endpoint_allowed("ssh", "box")
    with env.authz.rest_identity("bob", "user"):
        assert not t.caller_service_allowed("github")
    with env.authz.rest_identity("root", "admin"):
        assert t.caller_service_allowed("anything") and t.caller_endpoint_allowed("ssh", "box")


@pytest.mark.parametrize("fn,args,kind", [
    ("caller_service_allowed", ("github",), "services"),
    ("caller_skill_allowed", ("web",), "skills"),
    ("caller_endpoint_allowed", ("ssh", "box"), "ssh"),
])
def test_caller_checks_fail_closed_when_identity_cannot_be_resolved(env, fn, args, kind):
    env.patch.setattr(env.t, "current_identity", _boom)
    assert getattr(env.t, fn)(*args) is False
    rec = env.audit()[-1]
    assert rec["decision"] == "deny" and rec["tool"].startswith(f"{kind}:")
    env.off()
    assert getattr(env.t, fn)(*args) is True


def test_endpoint_access_is_default_deny_per_kind(env):
    env.set_policy({"users": {"alice": {"ssh": ["Box1"], "imap": "all", "ftp": "none"}}})
    t = env.t
    assert t.endpoint_allowed("alice", "user", "ssh", "box1")
    assert not t.endpoint_allowed("alice", "user", "ssh", "box2")
    assert t.endpoint_allowed("alice", "user", "imap", "anything")
    assert not t.endpoint_allowed("alice", "user", "ftp", "x")
    assert not t.endpoint_allowed("alice", "user", "mqtt", "x")             # no grant
    assert t.endpoint_allowed("root", "admin", "mqtt", "x")
    env.off()
    assert t.endpoint_allowed("alice", "user", "mqtt", "x")


def test_rest_gate_denies_ungranted_device_endpoints(env):
    env.set_policy({"users": {"alice": {"ssh": ["box1"]}}})
    ok, _ = env.authz.enforce_rest("alice", "user", "ssh_run", {"host": "box1", "command": "ls"})
    assert ok
    ok, reason = env.authz.enforce_rest("alice", "user", "ssh_run", {"host": "box2", "command": "ls"})
    assert not ok and "not in your allowed set" in reason


# ── cron act-as guard ───────────────────────────────────────────────────────

@pytest.mark.parametrize("caller,role,requested,expected", [
    ("alice", "user", "", (True, "")), ("alice", "user", "   ", (True, "")),
    ("alice", "user", None, (True, "")),
    ("alice", "user", "alice", (True, "alice")), ("alice", "user", " alice ", (True, "alice")),
    ("root", "admin", "bob", (True, "bob")),
])
def test_act_as_owner_allowed_cases(env, caller, role, requested, expected):
    assert env.t.act_as_owner(caller, role, requested) == expected


@pytest.mark.parametrize("caller,role", [("alice", "user"), ("", "user"), (None, "viewer"), ("alice", "")])
def test_non_admin_cannot_schedule_as_another_person(env, caller, role):
    ok, reason = env.t.act_as_owner(caller, role, "root")
    assert ok is False and "only an admin" in reason


# ── policy.json persistence ─────────────────────────────────────────────────

def test_write_policy_is_atomic_and_keeps_one_backup_generation(env):
    env.t._write_policy({"users": {"a": {}}})
    assert not env.t.POLICY_FILE.with_suffix(".json.bak").exists()           # nothing to back up yet
    env.t._write_policy({"users": {"b": {}}})
    assert json.loads(env.t.POLICY_FILE.read_text()) == {"users": {"b": {}}}
    assert json.loads(env.t._POLICY_BAK.read_text()) == {"users": {"a": {}}}
    assert not (env.t.AUTH_DIR / "policy.json.tmp").exists()


def test_write_policy_survives_a_failing_backup(env):
    import shutil
    env.t._write_policy({"v": 1})
    env.patch.setattr(shutil, "copy2", _boom)
    env.t._write_policy({"v": 2})
    assert json.loads(env.t.POLICY_FILE.read_text()) == {"v": 2}


def test_formatters(env):
    t = env.t
    assert t._fmt_access(["a", " ", "b "]) == "a,b" and t._fmt_access([]) == "none"
    assert t._fmt_access(None) == "all" and t._fmt_access("  ") == "all" and t._fmt_access("none") == "none"
    assert t._norm_access(" ALL ") == "all" and t._norm_access("None") == "none"
    assert t._norm_access("a, b;c  d") == ["a", "b;c", "d"] and t._norm_access("") == []


# ── admin tools ─────────────────────────────────────────────────────────────

def test_tenancy_tools_are_admin_only(env):
    for tool in ("tenancy_set", "tenancy_unset", "tenancy_show", "tenancy_list", "tenancy_status"):
        assert env.authz.decide("user", tool)[0] is False, tool
        assert env.authz.decide("viewer", tool)[0] is False, tool
        assert env.authz.decide("admin", tool)[0] is True, tool


def test_status_list_show_on_an_empty_policy(env):
    status = env.call("tenancy_status")
    assert "isolation: ON" in status and "Configured users: 0" in status
    assert env.call("tenancy_list").startswith("No per-user areas configured")
    assert "No per-user area for 'ghost'" in env.call("tenancy_show", identity=" ghost ")


def test_set_creates_and_describes_an_area_and_keeps_unspecified_fields(env):
    out = env.call("tenancy_set", identity=" alice ", services="GitHub, Docs", skills="none",
                   grant="caldav=family-cal; ssh=all\nimap=none; junk-without-equals", note="Alice")
    assert out.startswith("Set area for 'alice'.")
    assert "memory=own" in out and "services=github,docs" in out and "skills=none" in out
    assert "caldav=family-cal" in out and "ssh=all" in out and "imap=none" in out and "note: Alice" in out
    env.call("tenancy_set", identity="alice", memory="ALL")                 # others untouched
    entry = json.loads(env.t.POLICY_FILE.read_text())["users"]["alice"]
    assert entry["memory"] == "all" and entry["services"] == ["github", "docs"]
    assert entry["skills"] == "none" and entry["caldav"] == ["family-cal"] and entry["note"] == "Alice"
    assert env.t.area_for("alice", "user")["memory_scope"] is None          # now effective


def test_set_warns_when_enforcement_is_off(env):
    env.off()
    assert "AUTH_ENFORCE is OFF" in env.call("tenancy_set", identity="bob")
    assert "OFF" in env.call("tenancy_status") and "homelab mode" in env.call("tenancy_status")


@pytest.mark.parametrize("kwargs,message", [
    ({"identity": "  "}, "identity is required"),
    ({"identity": "bob", "memory": "everything"}, "memory must be one of"),
    ({"identity": "bob", "vault": "root"}, "vault must be one of"),
    ({"identity": "bob", "memory": "all", "grant": "rm=all"}, "unknown grant kind 'rm'"),
])
def test_set_refuses_bad_input_without_touching_the_policy(env, kwargs, message):
    env.set_policy({"users": {"carol": {"memory": "own"}}})
    before = env.t.POLICY_FILE.read_text()
    assert message in env.call("tenancy_set", **kwargs)
    assert env.t.POLICY_FILE.read_text() == before


def test_status_list_and_show_report_all_users_sorted(env):
    env.call("tenancy_set", identity="zed", memory="all")
    env.call("tenancy_set", identity="amy", vault="all", services="none")
    assert "Configured users: 2" in env.call("tenancy_status")
    listing = env.call("tenancy_list").splitlines()
    assert [ln.split(":")[0] for ln in listing] == ["- amy", "- zed"]
    assert "shared + all (unconfined)" in env.call("tenancy_show", identity="zed")
    assert "memory scope: users/amy-" in env.call("tenancy_show", identity="amy")


def test_unset_removes_the_user_and_restores_confinement(env):
    env.call("tenancy_set", identity="alice", memory="all")
    assert env.t.area_for("alice", "user")["memory_scope"] is None
    assert env.call("tenancy_unset", identity="alice").startswith("Removed area for 'alice'")
    assert env.t.area_for("alice", "user")["confined"] is True
    assert "nothing to remove" in env.call("tenancy_unset", identity="alice")
    env.set_policy({"users": "broken"})
    assert "nothing to remove" in env.call("tenancy_unset", identity="alice")
