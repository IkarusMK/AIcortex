"""The upgrade keeps the people who were admin in the WebUI admin.

Regression for a field report: a single-user install updated from 2.5.x to 2.6.x and
its owner was no longer admin. Two causes:

1. ``migrate_operators`` only listed the identities named in policy.json ("users",
   "roles"). A single-user install names nobody there, so nobody was migrated and the
   first-come bootstrap stayed open.
2. That bootstrap fired for ANY interactive identity, MCP calls of a connected app
   included. When the IdP doesn't forward the person's ``sub`` (stock OIDCProxy), the
   identity is the app's client_id — so an app, not the person, could become the
   operator, or take the slot by calling first after a restart.

Now:

- the upgrade also restores the people who were admin in the WebUI before: their
  sign-ins in the audit log (and its rotated files) with role admin make them
  operators — once (a marker in policy.json), unioned with existing operators (never
  removing anyone), not for an explicit non-admin role, not for someone an admin
  removed or demoted later, and not when a default role is configured. The start
  says so in one line.

Invented identities only (alice, bob, boss, carol, dave, erin, kim, mallory).
"""
import json
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.secure_default_role, pytest.mark.oidc]

RESTORED_LINE = "auth: restored {n} admin(s) from earlier WebUI sign-ins as operators"


@pytest.fixture
def oidc_env(monkeypatch, fake_idp):
    monkeypatch.setenv("OIDC_CONFIG_URL", f"{fake_idp}/.well-known/openid-configuration")
    monkeypatch.setenv("OIDC_CLIENT_ID", "aicortex")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "upstream-secret")
    monkeypatch.setenv("JWT_SIGNING_KEY", "0" * 64)
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    for var in ("OPERATORS", "OPERATOR_BOOTSTRAP_MINUTES", "AUTH_ROLE_CLAIM", "AUDIT_KEEP",
                "AUDIT_MAX_BYTES"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def _auth_dir(tmp_path) -> Path:
    d = tmp_path / "auth_store_dir"                  # where fresh_server points AUTH_STORE_DIR
    d.mkdir(exist_ok=True)
    return d


def _rec(identity, role="admin", tool="ui:login", decision="allow", reason=None):
    """One audit line exactly as authz.audit writes it (webui._audit for a sign-in)."""
    reason = f"browser login, role={role}" if reason is None else reason
    return json.dumps({"ts": "2026-03-01T10:00:00+00:00", "identity": identity, "role": role,
                       "tool": tool, "decision": decision, "reason": reason},
                      ensure_ascii=False)


def _tool_call(identity):
    return _rec(identity, "admin", "secret_set", "deny", "admin-only tool")


def _seed(tmp_path, files=None, policy=None):
    d = _auth_dir(tmp_path)
    for name, lines in (files or {}).items():
        data = b"".join((ln if isinstance(ln, bytes) else ln.encode("utf-8")) + b"\n"
                        for ln in lines)
        (d / name).write_bytes(data)
    if policy is not None:
        (d / "policy.json").write_text(json.dumps(policy), encoding="utf-8")
    return d


def _start(request, capsys):
    """Start the server the way production does (sign-in configured) and return the
    authz module plus what it printed."""
    capsys.readouterr()
    request.getfixturevalue("fresh_server")
    return sys.modules["authz"], capsys.readouterr().out


def _policy(az):
    return json.loads(az.POLICY_FILE.read_text(encoding="utf-8"))


# ── the upgrade restores the WebUI's admins ──────────────────────────────────
def test_a_single_user_install_keeps_its_admin_after_the_upgrade(oidc_env, request, capsys,
                                                                tmp_path):
    _seed(tmp_path, {"audit.log": [_tool_call("alice"), _rec("alice"), _rec("alice"),
                                   _rec("bob", role="user")]})
    az, out = _start(request, capsys)
    pol = _policy(az)
    assert pol["operators"] == ["alice"]                       # bob was never admin
    assert pol.get("operators_migrated")                       # recorded: runs once
    assert az.session_role("alice") == "admin"
    assert az.session_role("bob") == "user"
    assert RESTORED_LINE.format(n=1) in out
    assert "30 minutes" not in out                             # someone is in charge again


def test_rotated_audit_files_are_read(oidc_env, request, capsys, tmp_path):
    _seed(tmp_path, {
        "audit.log": [_rec("carol", role="user"), _tool_call("carol")],
        "audit.log.1": [_rec("bob")],
        "audit.log.3": [_rec("alice"), _tool_call("alice")],
        "audit.log.old": [_rec("mallory")],                    # not a generation of the log
    })
    az, out = _start(request, capsys)
    assert _policy(az)["operators"] == ["alice", "bob"]
    assert RESTORED_LINE.format(n=2) in out


def test_an_explicit_non_admin_role_is_not_promoted(oidc_env, request, capsys, tmp_path):
    _seed(tmp_path, {"audit.log": [_rec("bob"), _rec("kim"), _rec("alice")]},
          policy={"roles": {"bob": "viewer", "kim": "user"}})
    az, _ = _start(request, capsys)
    pol = _policy(az)
    assert pol["operators"] == ["alice"]
    assert pol["roles"] == {"bob": "viewer", "kim": "user"}
    assert az.session_role("bob") == "viewer" and az.session_role("kim") == "user"


def test_existing_operators_are_kept_and_unioned_once(oidc_env, request, capsys, tmp_path):
    # What 2.6.x left behind: an app took the operator slot, the person lost admin.
    _seed(tmp_path, {"audit.log": [_rec("alice"), _rec("bob", role="user"),
                                   _rec("alice", role="user")]},
          policy={"operators": ["app-client-7"], "users": {"bob": {"memory": "own"}}})
    az, out = _start(request, capsys)
    pol = _policy(az)
    assert pol["operators"] == ["app-client-7", "alice"]       # nobody removed, alice back
    assert pol["users"] == {"bob": {"memory": "own"}}          # listed, never admin: stays user
    assert az.session_role("alice") == "admin" and az.session_role("bob") == "user"
    assert RESTORED_LINE.format(n=1) in out
    # A later start does not run it again — not even for new admin sign-ins or new entries.
    with open(az.AUDIT_FILE, "a", encoding="utf-8") as f:
        f.write(_rec("carol") + "\n")
    az.POLICY_FILE.write_text(json.dumps({**pol, "users": {**pol["users"], "zoe": {}}}),
                              encoding="utf-8")
    assert az.migrate_operators() is None
    assert _policy(az)["operators"] == ["app-client-7", "alice"]


def test_a_configured_default_role_decides_instead(oidc_env, request, capsys, tmp_path):
    oidc_env.setenv("OIDC_DEFAULT_ROLE", "user")
    _seed(tmp_path, {"audit.log": [_rec("alice")]}, policy={"users": {"kim": {}}})
    az, out = _start(request, capsys)
    assert _policy(az) == {"users": {"kim": {}}}               # untouched, no marker
    assert az.session_role("alice") == "user"
    assert "restored" not in out
    oidc_env.setenv("OIDC_DEFAULT_ROLE", "")                   # … the default is dropped
    az.POLICY_FILE.write_text(json.dumps({"users": {"kim": {}}, "default": "viewer"}),
                              encoding="utf-8")
    assert az.migrate_operators() is None                      # policy "default" decides too
    az.POLICY_FILE.write_text(json.dumps({"users": {"kim": {}}}), encoding="utf-8")
    result = az.migrate_operators()                            # nobody decides: it runs
    assert result == {"kept": ["kim"], "restored": ["alice"]}
    assert _policy(az)["operators"] == ["alice", "kim"]


def test_garbled_audit_lines_are_ignored(oidc_env, request, capsys, tmp_path):
    lines = [
        b"\xff\xfe\x00\x01 not text at all",
        b"{torn",
        b"[]", b"null", b"42", b'"ui:login"',
        json.dumps({"tool": "ui:login"}).encode(),                        # fields missing
        json.dumps({"identity": 7, "role": "admin", "tool": "ui:login", "decision": "allow",
                    "reason": "browser login, role=admin"}).encode(),      # not a string
        _rec("").encode(), _rec("  ").encode(), _rec("unknown").encode(), _rec("?").encode(),
        _rec("evil\nline").encode(),                                      # control character
        _rec("x" * 600).encode(),                                         # absurdly long
        _rec("mallory", decision="deny").encode(),
        _rec("mallory", tool="ui:access").encode(),
        _rec("mallory", reason="browser login, role=user").encode(),      # role fields disagree
        _rec("mallory", role="user", reason="browser login, role=admin").encode(),
        _rec("mallory", reason="browser login, role=administrator").encode(),
        (_rec("mallory")[:-1] + ', "pad": "' + "y" * 200_000 + '"}').encode(),  # over-long line
        _rec("alice").encode(),
        _rec("kim")[:40].encode(),                                        # torn at the end
    ]
    d = _seed(tmp_path, {"audit.log": lines})
    (d / "audit.log.2").mkdir()                                           # not a file
    az, out = _start(request, capsys)
    assert _policy(az)["operators"] == ["alice"]
    assert RESTORED_LINE.format(n=1) in out


def test_someone_removed_or_demoted_later_is_not_restored(oidc_env, request, capsys, tmp_path):
    _seed(tmp_path, {
        "audit.log.1": [
            _rec("alice"), _rec("bob"), _rec("carol"), _rec("dave"), _rec("erin"),
            _rec("boss", "admin", "ui:user_delete", "allow", "identity=bob"),
            _rec("boss", "admin", "ui:user_set", "allow", "identity=carol role=default"),
            _rec("boss", "admin", "ui:user_set", "allow", "identity=dave role=-"),  # areas only
            _rec("boss", "admin", "ui:user_delete", "allow", "identity=erin"),
            _rec("boss", "admin", "ui:user_delete", "deny", "identity=alice: Refused"),
        ],
        "audit.log": [_rec("erin")],                           # signed in as admin again later
    })
    az, _ = _start(request, capsys)
    assert _policy(az)["operators"] == ["alice", "dave", "erin"]


def test_an_unreadable_policy_is_left_alone_and_nothing_raises(oidc_env, request, capsys,
                                                               tmp_path):
    d = _seed(tmp_path, {"audit.log": [_rec("alice")]})
    (d / "policy.json").write_text("{torn", encoding="utf-8")
    az, _ = _start(request, capsys)
    assert az.POLICY_FILE.read_text(encoding="utf-8") == "{torn"
    assert az.migrate_operators() is None
    assert "operators not migrated" in az.AUDIT_FILE.read_text(encoding="utf-8")
    az.POLICY_FILE.write_text("{}", encoding="utf-8")          # repaired: it runs now
    assert az.migrate_operators() == {"kept": [], "restored": ["alice"]}
