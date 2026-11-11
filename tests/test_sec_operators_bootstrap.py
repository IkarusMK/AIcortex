"""Who becomes the operator, and that nobody can remove the last one.

Regression for the re-review finding "operator bootstrap race": on an install whose
policy names nobody (no users, no roles), ``migrate_operators`` records nobody and
the FIRST OIDC identity to sign in — at any time, months later — becomes admin for
good. Whoever the IdP lets in first (a family member, a self-registered account)
wins the management plane. Now:

- ``OPERATORS`` (comma-separated identities, env) are always operators;
- the first-come bootstrap only works within ``OPERATOR_BOOTSTRAP_MINUTES`` (default
  30, 0 = off) after a start with nobody in charge, only for a person signing in to
  the WebUI (``signin_role``), and is logged loudly — at startup and when it fires;
- the WebUI users endpoints and the tenancy tools refuse to remove the last
  operator, and every policy.json writer takes the policy lock the bootstrap and
  the upgrade migration take, so no update is lost (and an unreadable policy is
  never rewritten from scratch).

Invented identities only (alice, bob, boss, dave, kim).
"""
import asyncio
import json
import sys
import threading
import time
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.secure_default_role


def _policy(az):
    return json.loads(az.POLICY_FILE.read_text(encoding="utf-8"))


def _write(az, obj):
    az.POLICY_FILE.parent.mkdir(parents=True, exist_ok=True)
    az.POLICY_FILE.write_text(json.dumps(obj), encoding="utf-8")


@pytest.fixture
def oidc_env(monkeypatch, fake_idp):
    monkeypatch.setenv("OIDC_CONFIG_URL", f"{fake_idp}/.well-known/openid-configuration")
    monkeypatch.setenv("OIDC_CLIENT_ID", "aicortex")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "upstream-secret")
    monkeypatch.setenv("JWT_SIGNING_KEY", "0" * 64)
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    for var in ("OPERATORS", "OPERATOR_BOOTSTRAP_MINUTES", "AUTH_ROLE_CLAIM"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def _start(request, capsys):
    """Start the server the way production does (sign-in configured) and return the
    authz module plus what it printed."""
    capsys.readouterr()
    request.getfixturevalue("fresh_server")
    return sys.modules["authz"], capsys.readouterr().out


# ── OPERATORS ────────────────────────────────────────────────────────────────
@pytest.mark.oidc
def test_operators_from_the_environment_are_always_admin(oidc_env, request, capsys):
    oidc_env.setenv("OPERATORS", " alice, bob ,")
    az, out = _start(request, capsys)
    assert "alice" in out and "bob" in out                       # named at startup
    assert az.role_for("bob", False) == "admin"
    assert az.role_for("alice", False) == "admin"
    assert az.signin_role("dave") == "user"                      # nobody bootstraps
    assert "operators" not in _policy(az)                        # nothing was recorded
    _write(az, {"operators": ["carol"]})                         # the policy names others …
    assert az.role_for("alice", False) == "admin"                # … OPERATORS still count
    oidc_env.setenv("OIDC_DEFAULT_ROLE", "viewer")               # a default for strangers …
    assert az.session_role("alice") == "admin"                   # … not for named operators
    assert az.session_role("dave") == "viewer"
    _write(az, {"roles": {"alice": "viewer"}})
    assert az.session_role("alice") == "viewer"                  # an explicit role still wins


# ── the bootstrap window ─────────────────────────────────────────────────────
@pytest.mark.oidc
def test_the_first_sign_in_wins_only_within_the_window(oidc_env, request, capsys):
    az, out = _start(request, capsys)
    assert "OPERATORS" in out and "30 minutes" in out            # loud at startup
    real = time.monotonic
    later = real() + 31 * 60
    oidc_env.setattr(az, "_clock", lambda: later, raising=False)
    assert az.signin_role("dave") == "user"                      # too late: no admin by luck
    assert "operators" not in _policy(az)
    assert az.default_role() == "user"


@pytest.mark.oidc
def test_inside_the_window_the_bootstrap_fires_and_says_so(oidc_env, request, capsys):
    oidc_env.setenv("OPERATOR_BOOTSTRAP_MINUTES", "5")
    az, out = _start(request, capsys)
    assert "5 minutes" in out
    assert az.default_role() == "admin"
    assert az.signin_role("dave") == "admin"
    assert "dave" in capsys.readouterr().out                     # loud when it fires
    assert _policy(az)["operators"] == ["dave"]
    assert az.signin_role("erin") == "user"


@pytest.mark.oidc
def test_zero_minutes_switches_the_bootstrap_off(oidc_env, request, capsys):
    oidc_env.setenv("OPERATOR_BOOTSTRAP_MINUTES", "0")
    az, out = _start(request, capsys)
    assert "OPERATORS" in out
    assert az.signin_role("dave") == "user"
    assert "operators" not in _policy(az)


@pytest.mark.oidc
def test_no_window_when_someone_is_in_charge_at_startup(oidc_env, request, capsys, tmp_path):
    auth_dir = tmp_path / "auth_store_dir"                       # where fresh_server points it
    auth_dir.mkdir()
    (auth_dir / "policy.json").write_text(json.dumps({"operators": ["boss"]}), encoding="utf-8")
    az, out = _start(request, capsys)
    assert "30 minutes" not in out
    (auth_dir / "policy.json").write_text(json.dumps({}), encoding="utf-8")   # emptied later
    assert az.signin_role("dave") == "user"                      # the window never opened


# ── never the last operator ──────────────────────────────────────────────────
class _FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def deco(fn):
            for m in methods:
                self.routes[(path, m)] = fn
            return fn
        return deco


@pytest.fixture
def ui(oidc_env, request):
    oidc_env.setenv("BASE_URL", "http://cortex.example.com")
    request.getfixturevalue("fresh_server")
    webui, mcp = sys.modules["webui"], _FakeMCP()
    webui.register(mcp)

    def call(route, body):
        cookie = webui._sign({"sub": "boss", "name": "boss", "role": "admin",
                              "claim_role": "admin", "csrf": "tok", "exp": time.time() + 600})

        async def raw():
            return json.dumps(body).encode()
        req = SimpleNamespace(client=SimpleNamespace(host="192.0.2.10"), headers={"x-csrf": "tok"},
                              cookies={webui.SESSION_COOKIE: cookie}, query_params={},
                              path_params={}, base_url="http://cortex.example.com/", body=raw)
        resp = asyncio.run(mcp.routes[(route, "POST")](req))
        return resp.status_code, json.loads(resp.body)
    return SimpleNamespace(call=call, az=sys.modules["authz"], tenancy=sys.modules["tenancy"])


@pytest.mark.oidc
def test_the_webui_never_removes_the_last_operator(ui):
    _write(ui.az, {"operators": ["alice"], "users": {"alice": {"memory": "all"}}})
    status, body = ui.call("/ui/api/users/delete", {"identity": "alice"})
    assert status == 409 and "last" in body["message"]
    status, body = ui.call("/ui/api/users", {"identity": "alice", "role": "viewer"})
    assert status == 409 and "last" in body["message"]
    assert _policy(ui.az) == {"operators": ["alice"], "users": {"alice": {"memory": "all"}}}
    # with a second person in charge, either change is fine
    assert ui.call("/ui/api/users", {"identity": "bob", "role": "admin"})[0] == 200
    assert ui.call("/ui/api/users/delete", {"identity": "alice"})[0] == 200
    pol = _policy(ui.az)
    assert pol["operators"] == [] and pol["roles"] == {"bob": "admin"}
    status, body = ui.call("/ui/api/users", {"identity": "bob", "role": "default"})
    assert status == 409 and "last" in body["message"]


@pytest.mark.oidc
def test_a_policy_writer_never_rewrites_an_unreadable_policy(ui):
    ui.az.POLICY_FILE.parent.mkdir(parents=True, exist_ok=True)
    ui.az.POLICY_FILE.write_text("{torn", encoding="utf-8")
    tools = {}
    ui.tenancy.register(SimpleNamespace(tool=lambda fn: tools.setdefault(fn.__name__, fn)))
    assert tools["tenancy_set"]("kim", memory="own").startswith("Refused")
    assert tools["tenancy_unset"]("kim").startswith("Refused")
    assert ui.call("/ui/api/users", {"identity": "kim", "role": "user"})[0] == 409
    assert ui.call("/ui/api/users/delete", {"identity": "kim"})[0] == 409
    assert ui.az.POLICY_FILE.read_text(encoding="utf-8") == "{torn"


def _concurrent_with_the_lock(az, write):
    """Hold the policy lock (as the operator bootstrap does), let `write` run in another
    thread meanwhile, record an operator under the lock, then release. A writer that
    doesn't take the lock reads the policy before the operator is recorded and then
    overwrites it."""
    import store
    tenancy = sys.modules["tenancy"]
    done = threading.Event()
    with store.locked(az.POLICY_FILE):
        worker = threading.Thread(target=lambda: (write(), done.set()), daemon=True)
        worker.start()
        time.sleep(0.3)
        assert not done.is_set()                                 # it waits for the lock
        tenancy._write_policy({"operators": ["boss"]})
    worker.join(timeout=10)
    assert done.is_set()
    return _policy(az)


@pytest.mark.oidc
def test_tenancy_writes_take_the_policy_lock(ui):
    tools = {}
    ui.tenancy.register(SimpleNamespace(tool=lambda fn: tools.setdefault(fn.__name__, fn)))
    pol = _concurrent_with_the_lock(ui.az, lambda: tools["tenancy_set"]("kim", memory="own"))
    assert pol["operators"] == ["boss"] and pol["users"]["kim"]["memory"] == "own"


@pytest.mark.oidc
def test_webui_user_writes_take_the_policy_lock(ui):
    pol = _concurrent_with_the_lock(
        ui.az, lambda: ui.call("/ui/api/users", {"identity": "kim", "role": "viewer"}))
    assert pol["operators"] == ["boss"] and pol["roles"] == {"kim": "viewer"}
