"""Authorization layer (app/authz.py): role decisions, identity fallbacks, the gate.

Proves the refusal and degradation paths: IdP group claims map to the highest valid
role but never beat an explicit policy entry; a per-agent key is never an accidental
admin; unknown roles and viewers are denied writes; every identity-resolution failure
degrades to the documented value (and leaves an audit trace instead of failing
silently); enforce_rest and the FastMCP middleware deny with a ToolError, stamp the
caller's identity over forged attribution, confine memory scope and device endpoints,
and filter tools/list by role and key scope.
"""
import asyncio
import json
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def az(monkeypatch, fresh_server):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    for var in ("AUTH_ROLE_CLAIM", "RUNNER_ROLE", "OIDC_DEFAULT_ROLE", "AUTH_AUDIT_ALL"):
        monkeypatch.delenv(var, raising=False)
    return sys.modules["authz"]


def _policy(az, obj):
    az.POLICY_FILE.parent.mkdir(parents=True, exist_ok=True)
    az.POLICY_FILE.write_text(json.dumps(obj), encoding="utf-8")


def _audit_lines(az):
    if not az.AUDIT_FILE.exists():
        return []
    return [json.loads(ln) for ln in az.AUDIT_FILE.read_text(encoding="utf-8").splitlines()]


def _boom(*_a, **_k):
    raise RuntimeError("lookup broke")


# ── claims and roles ─────────────────────────────────────────────────────────
def test_claims_prefer_the_forwarded_upstream_identity(az):
    assert az._claim_value("not-a-dict", "sub") is None
    assert az._claim_value({"sub": "top", "upstream_claims": {"sub": "idp"}}, "sub") == "idp"
    assert az._claim_value({"sub": "top", "upstream_claims": {"x": 1}}, "sub") == "top"
    assert az._claim_value({"sub": "top", "upstream_claims": "junk"}, "sub") == "top"


@pytest.mark.parametrize("claims, expected", [
    ({"groups": ["Viewer", "ADMIN", "user"]}, "admin"),                # highest wins, case-folded
    ({"upstream_claims": {"groups": "viewer, user"}}, "user"),         # comma string, upstream first
    ({"oc_groups": ["viewer"]}, "viewer"),                             # fallback claim name
    ({"groups": ["strangers", "", "  "]}, ""),                         # nothing usable
    ({"groups": None}, ""), ({}, ""), (None, ""),
])
def test_group_claims_map_to_the_highest_valid_role(az, claims, expected):
    assert az._role_from_claims(claims) == expected


def test_group_mapping_and_custom_claim_name_come_from_config(az, monkeypatch):
    _policy(az, {"groups": {"Staff": "user", "Wizards": "root"}})
    assert az._role_from_claims({"groups": ["staff"]}) == "user"
    assert az._role_from_claims({"groups": ["wizards"]}) == ""         # invalid target role ignored
    monkeypatch.setenv("AUTH_ROLE_CLAIM", "roles")
    assert az._role_from_claims({"roles": ["viewer"], "groups": ["admin"]}) == "viewer"
    monkeypatch.setenv("AUTH_ROLE_CLAIM", "oc_groups")
    assert az._role_from_claims({"oc_groups": "admin"}) == "admin"


def test_explicit_policy_beats_group_claims_and_junk_policy_is_ignored(az):
    _policy(az, {"roles": {"alice": "viewer", "carol": "god"}})
    admin_claims = {"groups": ["admin"]}
    assert az.role_for("alice", False, admin_claims) == "viewer"
    assert az.role_for("carol", False, admin_claims) == "admin"        # "god" is not a role
    assert az.role_for("dave", False, {"groups": ["viewer"]}) == "viewer"


def test_runner_and_default_roles_follow_the_secure_defaults(az, monkeypatch):
    assert az.role_for("runner", True) == "user"
    az.start_bootstrap_window()                     # a start with nobody in charge …
    assert az.role_for("operator", False) == "user"    # … an app or key never bootstraps,
    assert az.signin_role("operator") == "admin"       # … the first WebUI sign-in does
    monkeypatch.setenv("RUNNER_ROLE", "viewer")
    monkeypatch.setenv("OIDC_DEFAULT_ROLE", "user")
    assert az.role_for("runner", True) == "viewer" and az.role_for("operator", False) == "user"
    monkeypatch.setenv("RUNNER_ROLE", "root")
    monkeypatch.setenv("OIDC_DEFAULT_ROLE", "superuser")
    assert az.role_for("runner", True) == "user" and az.role_for("operator", False) == "user"
    monkeypatch.delenv("RUNNER_ROLE")
    monkeypatch.delenv("OIDC_DEFAULT_ROLE")
    _policy(az, {"runner": "viewer", "default": "viewer"})
    assert az.role_for("runner", True) == "viewer" and az.role_for("operator", False) == "viewer"


def test_an_agent_key_is_never_an_accidental_admin(az):
    key_claims = {"aicortex_key": "k1", "groups": ["admin"]}
    assert az.role_for("alice", False, key_claims) == "user"           # group claim ignored
    _policy(az, {"roles": {"alice": "admin"}})
    assert az.role_for("alice", False, key_claims) == "admin"          # only explicit policy


@pytest.mark.parametrize("role, tool, allowed", [
    ("admin", "secret_set", True), ("viewer", "memory_read", True), ("viewer", "memory_write", False),
    ("user", "memory_write", True), ("user", "secret_set", False), ("user", "apikey_create", False),
    ("intruder", "ping", False), ("", "ping", False), (None, "ping", False),
])
def test_decide_is_deny_by_default_for_unknown_roles(az, role, tool, allowed):
    ok, reason = az.decide(role, tool)
    assert ok is allowed and reason


# ── audit and identity resolution ────────────────────────────────────────────
def test_audit_appends_jsonl_and_never_breaks_a_call(az, monkeypatch, tmp_path):
    az.audit("", "user", "ping", "deny", "because")
    az.audit("alice", "admin", "ping", "allow", "ok")
    first, second = _audit_lines(az)
    assert first["identity"] == "unknown" and second["decision"] == "allow"
    blocker = tmp_path / "plain-file"
    blocker.write_text("x")
    monkeypatch.setattr(az, "AUTH_DIR", blocker)                       # mkdir under a file fails
    assert az.audit("alice", "user", "ping", "deny", "x") is None


def test_token_identity_prefers_the_verifiers_own_client_id(az, monkeypatch):
    import fastmcp.server.dependencies as deps
    tok = SimpleNamespace(client_id='{"qualified": 1}', original_client_id="runner",
                          claims={"sub": "alice"})
    monkeypatch.setattr(deps, "get_access_token", lambda: tok)
    assert az._identity() == ("alice", True, {"sub": "alice"})
    monkeypatch.setattr(deps, "get_access_token", _boom)
    assert az._identity() == ("unknown", False, {})


def test_effective_identity_degrades_step_by_step(az, monkeypatch):
    import actas
    monkeypatch.setattr(az, "_identity", lambda: ("bob", False, {}))
    _policy(az, {"roles": {"bob": "viewer", "owner1": "viewer"}})
    assert az.effective_identity() == ("bob", "viewer")
    monkeypatch.setattr(actas, "current", lambda caller="": "owner1")   # a job acts as its owner
    assert az.effective_identity() == ("owner1", "viewer")
    monkeypatch.setattr(actas, "current", lambda caller="": "stranger")
    assert az.effective_identity() == ("stranger", "user")             # never the default admin
    monkeypatch.setattr(actas, "current", _boom)                        # binding lookup breaks
    assert az.effective_identity() == ("bob", "viewer")
    monkeypatch.setattr(az, "role_for", _boom)
    assert az.effective_identity() == ("unknown", "user")
    monkeypatch.setattr(az, "_identity", _boom)
    assert az.effective_identity() == ("unknown", "user")
    monkeypatch.setattr(az, "_REST_IDENTITY", SimpleNamespace(get=_boom))
    assert az.effective_identity() == ("unknown", "user")              # still no exception


def test_real_caller_ignores_actas_and_labels_name_the_agent(az, monkeypatch):
    monkeypatch.setattr(az, "_identity", lambda: ("runner", True, {"agent": "hermes"}))
    assert az.real_caller() == ("runner", "user", True)
    assert az.caller_label() == "runner/hermes"
    with az.rest_identity("alice", "viewer"):
        assert az.real_caller() == ("alice", "viewer", False)


def test_sees_depends_on_enforcement_role_and_ownership(az, monkeypatch):
    with az.rest_identity("alice", "user"):
        assert az.sees("alice") and az.sees("") and not az.sees("bob")
    with az.rest_identity("boss", "admin"):
        assert az.sees("bob")
    monkeypatch.setenv("AUTH_ENFORCE", "0")
    with az.rest_identity("alice", "user"):
        assert az.sees("bob")


def test_key_scopes_gate_only_agent_keys(az, monkeypatch):
    monkeypatch.setattr(az, "_identity", lambda: ("alice", False, {}))
    assert az.key_allows("secret_set") is True                         # not a key → not this gate
    key = {"aicortex_key": "k", "key_scopes": ["memory_read", "secret_set"]}
    monkeypatch.setattr(az, "_identity", lambda: ("alice", False, key))
    assert az.key_allows("memory_read") and not az.key_allows("fs_read")
    assert not az.key_allows("secret_set")                             # hard deny beats the scope


# ── the REST gate ────────────────────────────────────────────────────────────
def test_enforce_rest_is_a_noop_without_enforcement_or_tool(az, monkeypatch):
    assert az.enforce_rest("alice", "user", "", {}) == (True, "enforce off")
    monkeypatch.setenv("AUTH_ENFORCE", "0")
    assert az.enforce_rest("alice", "viewer", "memory_write", {}) == (True, "enforce off")
    assert _audit_lines(az) == []


def test_enforce_rest_denies_and_audits_and_optionally_audits_allows(az, monkeypatch):
    ok, reason = az.enforce_rest("vera", "viewer", "memory_write", {})
    assert not ok and "read-only" in reason
    assert [(r["decision"], r["tool"]) for r in _audit_lines(az)] == [("deny", "memory_write")]
    assert az.enforce_rest("vera", "viewer", "memory_read", {"scope": "shared"})[0]
    assert len(_audit_lines(az)) == 1                                   # allows are quiet …
    monkeypatch.setenv("AUTH_AUDIT_ALL", "yes")
    az.enforce_rest("vera", "viewer", "memory_read", {"scope": "shared"})
    assert _audit_lines(az)[-1]["decision"] == "allow"                  # … unless AUTH_AUDIT_ALL


def test_enforce_rest_stamps_the_real_identity_over_forged_attribution(az):
    task, post = {"created_by": "forged"}, {"sender": "forged"}
    assert az.enforce_rest("alice", "user", "task_add", task)[0] and task["created_by"] == "alice"
    assert az.enforce_rest("alice", "user", "inbox_post", post)[0] and post["sender"] == "alice"
    anonymous = {"created_by": "kept"}
    az.enforce_rest("unknown", "user", "task_add", anonymous)
    assert anonymous["created_by"] == "kept"                            # nobody to stamp
    assert az.enforce_rest("alice", "user", "ping", None) == (True, "ok")


def test_enforce_rest_confines_scope_and_checks_device_areas(az):
    own = {"scope": "shared"}
    az.enforce_rest("alice", "user", "memory_read", own)
    assert own["scope"].startswith("users/alice-")
    admin = {"scope": "shared"}
    az.enforce_rest("boss", "admin", "memory_read", admin)
    assert admin["scope"] == "shared"
    denied = az.enforce_rest("alice", "user", "ssh_run", {"host": "box.example.com"})
    assert not denied[0] and "ssh endpoint 'box.example.com' is not in your allowed set" in denied[1]
    _policy(az, {"users": {"alice": {"ssh": ["box.example.com"]}}})
    assert az.enforce_rest("alice", "user", "ssh_run", {"host": "Box.Example.com"})[0]
    assert any(r["decision"] == "deny" and "ssh endpoint" in r["reason"] for r in _audit_lines(az))


def test_enforce_rest_fails_open_but_leaves_a_trace(az, monkeypatch):
    import tenancy
    monkeypatch.setattr(tenancy, "confine_memory_scope", _boom)
    assert az.enforce_rest("alice", "user", "memory_read", {"scope": "x"}) == (True, "ok")
    (trace,) = _audit_lines(az)
    assert trace["decision"] == "fail-open" and "RuntimeError" in trace["reason"]


# ── the FastMCP middleware ───────────────────────────────────────────────────
def _ctx(tool, args=None):
    return SimpleNamespace(message=SimpleNamespace(name=tool, arguments=args))


def _call(mw, ctx):
    async def nxt(_context):
        return "reached-tool"
    return asyncio.run(mw.on_call_tool(ctx, nxt))


def _tool_error():
    from fastmcp.exceptions import ToolError
    return ToolError


def test_middleware_refuses_a_forbidden_tool_before_it_runs(az):
    mw = az.build_middleware()
    with az.rest_identity("alice", "user"), pytest.raises(_tool_error(), match="Denied by policy"):
        _call(mw, _ctx("service_add", {}))
    with az.rest_identity("vera", "viewer"), pytest.raises(_tool_error(), match="read-only"):
        _call(mw, _ctx("memory_write", {}))
    with az.rest_identity("boss", "admin"):
        assert _call(mw, _ctx("service_add", {})) == "reached-tool"
    assert [r["decision"] for r in _audit_lines(az)] == ["deny", "deny"]


def test_middleware_stamps_identity_and_confines_memory_scope(az):
    mw = az.build_middleware()
    args = {"sender": "forged", "created_by": "forged", "scope": "shared"}
    with az.rest_identity("alice", "user"):
        _call(mw, _ctx("inbox_post", args))
        _call(mw, _ctx("task_add", args))
        _call(mw, _ctx("memory_read", args))
    assert args["sender"] == "alice" and args["created_by"] == "alice"
    assert args["scope"].startswith("users/alice-")


def test_middleware_denies_unassigned_device_endpoints(az):
    mw = az.build_middleware()
    with az.rest_identity("alice", "user"), pytest.raises(_tool_error(), match="ssh endpoint 'box.example.com'"):
        _call(mw, _ctx("ssh_run", {"host": "box.example.com"}))
    _policy(az, {"users": {"alice": {"ssh": "all"}}})
    with az.rest_identity("alice", "user"):
        assert _call(mw, _ctx("ssh_run", {"host": "box.example.com"})) == "reached-tool"


def test_middleware_fails_open_with_a_trace_when_identity_breaks(az, monkeypatch):
    mw = az.build_middleware()
    with monkeypatch.context() as m:
        m.setattr(az, "effective_identity", _boom)
        assert _call(mw, _ctx("service_add", {})) == "reached-tool"
    assert _audit_lines(az)[-1]["decision"] == "fail-open"
    import tenancy
    monkeypatch.setattr(tenancy, "confine_memory_scope", _boom)         # scope glitch is swallowed
    with az.rest_identity("alice", "user"):
        assert _call(mw, _ctx("memory_read", {"scope": "x"})) == "reached-tool"


def test_agent_key_scopes_apply_even_without_enforcement(az, monkeypatch):
    monkeypatch.setenv("AUTH_ENFORCE", "0")
    key = {"aicortex_key": "k", "key_scopes": ["memory_read"]}
    monkeypatch.setattr(az, "_identity", lambda: ("alice", False, key))
    mw = az.build_middleware()
    assert _call(mw, _ctx("memory_read", {})) == "reached-tool"
    with pytest.raises(_tool_error(), match="outside the key's scopes"):
        _call(mw, _ctx("fs_read", {}))


def test_tools_list_is_filtered_by_role_and_key_scope(az, monkeypatch):
    mw = az.build_middleware()
    tools = [SimpleNamespace(name=n) for n in ("ping", "memory_read", "memory_write", "secret_set")]

    def listed():
        async def nxt(_context):
            return tools
        return [t.name for t in asyncio.run(mw.on_list_tools(None, nxt))]

    with az.rest_identity("vera", "viewer"):
        assert listed() == ["ping", "memory_read"]
    with az.rest_identity("alice", "user"):
        assert listed() == ["ping", "memory_read", "memory_write"]
    with az.rest_identity("boss", "admin"):
        assert len(listed()) == 4
    monkeypatch.setenv("AUTH_ENFORCE", "0")
    with az.rest_identity("vera", "viewer"):
        assert len(listed()) == 4
    key = {"aicortex_key": "k", "key_scopes": ["ping"]}
    monkeypatch.setattr(az, "_identity", lambda: ("alice", False, key))
    assert listed() == ["ping"]


def test_tools_list_falls_back_to_the_full_list_with_a_trace(az, monkeypatch):
    mw = az.build_middleware()
    tools = [SimpleNamespace(name="secret_set")]

    async def nxt(_context):
        return tools
    monkeypatch.setattr(az, "key_allows", _boom)
    assert asyncio.run(mw.on_list_tools(None, nxt)) == tools
    assert _audit_lines(az)[-1]["tool"] == "tools/list"


def test_build_middleware_degrades_when_fastmcp_pieces_are_missing(az, monkeypatch):
    import fastmcp.server.middleware as fm

    class Broken:
        def __init__(self):
            raise RuntimeError("cannot construct")
    with monkeypatch.context() as m:
        m.setattr(fm, "Middleware", Broken)
        assert az.build_middleware() is None
    with monkeypatch.context() as m:
        m.setitem(sys.modules, "fastmcp.server.middleware", None)       # import fails
        assert az.build_middleware() is None
    with monkeypatch.context() as m:
        m.setitem(sys.modules, "fastmcp.exceptions", None)              # ToolError → RuntimeError
        mw = az.build_middleware()
    with az.rest_identity("alice", "user"), pytest.raises(RuntimeError, match="Denied by policy"):
        _call(mw, _ctx("service_add", {}))
