"""The first operator over an app — only when the IdP names the person.

Found by the upgrade check 2.5.0 → 2.7.0: a single-user install that was only ever
used through a connected app (never the WebUI) has no policy entries and no WebUI
sign-in in its audit log, so the upgrade found nobody to keep as admin — and since
the bootstrap only fired for a WebUI sign-in, the person who owns the install came
back as "user", their notes confined to a personal area.

The review behind "WebUI only" worried about an APP becoming the operator: an IdP
that doesn't forward the person's 'sub' leaves the app's client_id as the identity.
So the bootstrap may fire over /mcp only when the token carries the person's own
'sub' (the identity IS that sub) — never for a bare client_id, an agent key or a
runner, and only inside the start window, like the WebUI sign-in.
"""
import json
import sys

import pytest

pytestmark = pytest.mark.secure_default_role


@pytest.fixture
def az(monkeypatch, fresh_server):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    for var in ("AUTH_ROLE_CLAIM", "RUNNER_ROLE", "OPERATORS", "OPERATOR_BOOTSTRAP_MINUTES"):
        monkeypatch.delenv(var, raising=False)
    az = sys.modules["authz"]
    az.start_bootstrap_window()
    return az


def _operators(az):
    if not az.POLICY_FILE.exists():
        return []
    return json.loads(az.POLICY_FILE.read_text(encoding="utf-8")).get("operators", [])


def test_a_person_named_by_the_idp_becomes_the_operator_over_an_app(az):
    claims = {"upstream_claims": {"sub": "alice-sub"}}
    assert az.role_for("alice-sub", False, claims) == "admin"
    assert _operators(az) == ["alice-sub"]
    assert az.role_for("bob-sub", False, {"upstream_claims": {"sub": "bob-sub"}}) == "user"


def test_a_bare_client_id_never_becomes_the_operator(az):
    assert az.role_for("app-client-7", False, {"client_id": "app-client-7"}) == "user"
    assert az.role_for("app-client-7", False) == "user"
    # the token names another person than the identity: not that person's own call
    assert az.role_for("app-client-7", False, {"upstream_claims": {"sub": "alice-sub"}}) == "user"
    assert _operators(az) == []


def test_keys_runners_and_claim_roles_never_bootstrap_over_an_app(az):
    assert az.role_for("alice-sub", False, {"aicortex_key": "k1", "sub": "alice-sub"}) == "user"
    assert az.role_for("runner", True, {"sub": "runner"}) == "user"
    assert az.role_for("alice-sub", False, {"sub": "alice-sub", "groups": ["user"]}) == "user"
    assert _operators(az) == []


def test_outside_the_window_an_app_call_stays_user(az, monkeypatch):
    monkeypatch.setenv("OPERATOR_BOOTSTRAP_MINUTES", "0")
    az.start_bootstrap_window()
    assert az.role_for("alice-sub", False, {"upstream_claims": {"sub": "alice-sub"}}) == "user"
    assert _operators(az) == []
