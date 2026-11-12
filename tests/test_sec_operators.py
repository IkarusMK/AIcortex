"""Only operators are admin by default — not every identity the IdP knows.

Regression for the audit finding "every OIDC identity is admin": ``role_for`` fell
back to OIDC_DEFAULT_ROLE → policy "default" → "admin", so anyone the IdP let sign
in (a family member, a test account, a self-registered user) got the whole
management plane. Now:

- precedence for an interactive person: explicit ``roles`` → IdP claim →
  OIDC_DEFAULT_ROLE → policy ``default`` → listed in ``operators`` → admin, else
  "user";
- bootstrap: with no operator and no explicit admin, the FIRST person to sign in to
  the WebUI (within the bootstrap window after the start) is recorded as operator (so
  a fresh install doesn't lock its owner out) — ``signin_role``, never ``role_for``;
- upgrade: once, at startup, the identities that are admin today (listed in policy
  ``users``/``roles`` without an explicit non-admin role) become operators — today's
  admins stay admin, only NEW identities start as "user" (the WebUI's admins from the
  audit log too: test_sec_operators_restore.py);
- agent keys and cron act-as never take this path: an operator's key and jobs stay
  "user" unless an explicit role says otherwise.
"""
import json
import sys
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.secure_default_role


@pytest.fixture
def az(monkeypatch, fresh_server):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    for var in ("AUTH_ROLE_CLAIM", "RUNNER_ROLE", "OPERATORS", "OPERATOR_BOOTSTRAP_MINUTES"):
        monkeypatch.delenv(var, raising=False)
    az = sys.modules["authz"]
    # As a start with sign-in configured does: nobody is in charge yet, so the
    # first-come bootstrap window opens (test_sec_operators_bootstrap.py covers it).
    az.start_bootstrap_window()
    return az


def _write(az, obj):
    az.POLICY_FILE.parent.mkdir(parents=True, exist_ok=True)
    az.POLICY_FILE.write_text(json.dumps(obj), encoding="utf-8")


def _read(az):
    return json.loads(az.POLICY_FILE.read_text(encoding="utf-8"))


# ── the default for a new identity ───────────────────────────────────────────
def test_a_new_identity_is_a_user_once_an_operator_exists(az):
    _write(az, {"operators": ["boss"]})
    assert az.role_for("boss", False) == "admin"
    assert az.role_for("stranger", False) == "user"
    assert az.session_role("stranger") == "user"
    assert _read(az) == {"operators": ["boss"]}                     # nothing recorded


def test_the_precedence_explicit_claim_env_policy_operators(az, monkeypatch):
    _write(az, {"operators": ["boss", "vera"], "roles": {"vera": "viewer"}})
    assert az.role_for("vera", False) == "viewer"                   # explicit beats operator
    assert az.role_for("boss", False, {"groups": ["user"]}) == "user"   # claim beats operator
    assert az.session_role("boss", "viewer") == "viewer"
    _write(az, {"operators": ["boss"], "default": "viewer"})
    assert az.role_for("boss", False) == "viewer"                   # policy default beats it
    monkeypatch.setenv("OIDC_DEFAULT_ROLE", "admin")
    assert az.role_for("stranger", False) == "admin"                # the documented opt-in
    monkeypatch.setenv("OIDC_DEFAULT_ROLE", "superuser")
    assert az.role_for("stranger", False) == "user"                 # junk is never admin


def test_junk_in_operators_is_ignored(az):
    _write(az, {"operators": "boss", "roles": {"x": "admin"}})       # not a list
    assert az.role_for("boss", False) == "user"
    _write(az, {"operators": [None, "", 7, " boss "], "roles": {"x": "admin"}})
    assert az.role_for("boss", False) == "admin"
    assert az.role_for("7", False) == "user"                        # only strings count


# ── bootstrap ────────────────────────────────────────────────────────────────
def test_the_first_person_signing_in_to_the_webui_becomes_the_operator(az):
    assert az.signin_role("dave") == "admin"
    assert _read(az)["operators"] == ["dave"]
    assert az.signin_role("erin") == "user"
    assert az.role_for("dave", False) == "admin"
    audit = az.AUDIT_FILE.read_text(encoding="utf-8")
    assert "operator" in audit and "dave" in audit


def test_bootstrap_keeps_the_rest_of_the_policy(az):
    _write(az, {"roles": {"kim": "user"}, "users": {"kim": {"memory": "own"}},
                "groups": {"staff": "user"}})
    assert az.signin_role("dave") == "admin"
    pol = _read(az)
    assert pol["operators"] == ["dave"] and pol["roles"] == {"kim": "user"}
    assert pol["users"] == {"kim": {"memory": "own"}} and pol["groups"] == {"staff": "user"}


@pytest.mark.parametrize("policy", [
    {"roles": {"boss": "admin"}},                                      # an admin exists
    {"operators": ["boss"]},                                           # an operator exists
    {"default": "user"},                                               # the default is decided
])
def test_no_bootstrap_when_someone_is_already_in_charge(az, policy):
    _write(az, policy)
    assert az.signin_role("dave") == "user"
    assert _read(az) == policy


def test_no_bootstrap_for_a_claim_role_a_key_a_runner_or_an_unreadable_policy(az):
    assert az.signin_role("dave", {"groups": ["user"]}) == "user"
    assert az.signin_role("dave", {"aicortex_key": "k1"}) == "user"
    assert az.role_for("runner", True) == "user"
    assert not az.POLICY_FILE.exists()
    _write(az, {})
    az.POLICY_FILE.write_text("{torn", encoding="utf-8")            # never overwrite a broken policy
    assert az.signin_role("dave") == "user"
    assert az.POLICY_FILE.read_text(encoding="utf-8") == "{torn"


# ── operators keep their privilege only interactively ────────────────────────
def test_an_operators_agent_key_and_jobs_stay_user(az):
    _write(az, {"operators": ["alice"]})
    assert az.role_for("alice", False) == "admin"
    assert az.role_for_apikey("alice") == "user"
    assert az._actas_role("alice") == "user"
    assert az.role_for("alice", False, {"aicortex_key": "k1", "sub": "alice"}) == "user"
    _write(az, {"operators": ["alice"], "roles": {"alice": "admin"}})
    assert az.role_for_apikey("alice") == "admin"                   # only an explicit role lifts it


# ── upgrade migration ────────────────────────────────────────────────────────
def test_the_upgrade_turns_todays_admins_into_operators(az):
    _write(az, {"users": {"alice": {"memory": "all"}, "bob": {"memory": "own"}},
                "roles": {"bob": "user", "carol": "admin", "vera": "viewer", "odd": "god"}})
    assert az.migrate_operators() == {"kept": ["alice", "carol", "odd"], "restored": []}
    pol = _read(az)
    assert pol["operators"] == ["alice", "carol", "odd"]
    assert pol["operators_migrated"] == az.OPERATORS_MIGRATION
    assert pol["roles"]["bob"] == "user" and pol["users"]["bob"] == {"memory": "own"}
    assert az.role_for("alice", False) == "admin" and az.role_for("bob", False) == "user"
    assert az.role_for("newcomer", False) == "user"
    assert az.migrate_operators() is None                            # once


@pytest.mark.parametrize("policy, env", [
    ({"users": {"alice": {}}, "default": "admin"}, None),             # default kept by policy
    ({"users": {"alice": {}}}, "admin"),                              # … or by the env
])
def test_the_upgrade_leaves_a_decided_policy_alone(az, monkeypatch, policy, env):
    if env:
        monkeypatch.setenv("OIDC_DEFAULT_ROLE", env)
    _write(az, policy)
    assert az.migrate_operators() is None
    assert _read(az) == policy


def test_once_operators_exist_the_people_listed_are_not_promoted(az):
    _write(az, {"users": {"alice": {}}, "operators": []})             # already decided
    assert az.migrate_operators() == {"kept": [], "restored": []}
    assert _read(az) == {"users": {"alice": {}}, "operators": [],
                         "operators_migrated": az.OPERATORS_MIGRATION}
    assert az.session_role("alice") == "user"


def test_the_upgrade_only_records_that_it_ran_on_a_fresh_install(az):
    assert az.migrate_operators() == {"kept": [], "restored": []}
    assert _read(az) == {"operators_migrated": az.OPERATORS_MIGRATION}  # bootstrap handles it
    assert az.migrate_operators() is None


@pytest.mark.oidc
def test_the_server_migrates_at_startup(monkeypatch, request, tmp_path, fake_idp):
    auth_dir = tmp_path / "auth_store_dir"                          # where fresh_server points it
    auth_dir.mkdir()
    (auth_dir / "policy.json").write_text(json.dumps(
        {"users": {"alice": {"memory": "own"}}, "roles": {"bob": "user"}}), encoding="utf-8")
    monkeypatch.setenv("OIDC_CONFIG_URL", f"{fake_idp}/.well-known/openid-configuration")
    monkeypatch.setenv("OIDC_CLIENT_ID", "aicortex")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "upstream-secret")
    monkeypatch.setenv("JWT_SIGNING_KEY", "0" * 64)
    request.getfixturevalue("fresh_server")
    pol = json.loads((auth_dir / "policy.json").read_text(encoding="utf-8"))
    assert pol["operators"] == ["alice"]


# ── what the WebUI shows ─────────────────────────────────────────────────────
@pytest.fixture
def users_view(az):
    webui = sys.modules["webui"]
    return SimpleNamespace(rows=lambda: webui._user_rows())


def test_the_users_view_reports_the_real_default_and_marks_operators(az, users_view, monkeypatch):
    assert users_view.rows()["default_role"] == "admin"             # the next sign-in bootstraps
    _write(az, {"operators": ["boss"], "users": {"kim": {"memory": "own"}},
                "roles": {"vera": "viewer"}})
    view = users_view.rows()
    assert view["default_role"] == "user"
    rows = {u["identity"]: u for u in view["users"]}
    assert set(rows) == {"boss", "kim", "vera"}
    assert rows["boss"]["operator"] is True and rows["boss"]["effective_role"] == "admin"
    assert rows["kim"]["operator"] is False and rows["kim"]["effective_role"] == "user"
    assert rows["vera"]["role"] == "viewer" and rows["vera"]["effective_role"] == "viewer"
    monkeypatch.setenv("OIDC_DEFAULT_ROLE", "viewer")
    assert users_view.rows()["default_role"] == "viewer"
