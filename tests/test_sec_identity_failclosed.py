"""An identity that can't be resolved is never an admin once sign-in is configured.

Regression: ``authz._identity()`` degraded to "unknown" on any error and
``role_for("unknown")`` fell through to the default admin; ``effective_identity()``
degraded to ("unknown", "user"); ``tenancy.area_for()`` treated an empty/unknown
identity as unrestricted. With OIDC configured every /mcp caller carries a token, so
an unresolved identity is a fault, not a person: it now gets NO role (every tool
denied, with an audit line) and is confined to a bucket of its own.

The no-OIDC local-operator mode — "unknown" is the person at this machine — and
AUTH_ENFORCE=0 behave exactly as before.
"""
import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

UNRESOLVED = ("unknown", "")


def _boom(*_a, **_k):
    raise RuntimeError("token lookup broke")


@pytest.fixture
def mods(monkeypatch, fresh_server):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    return SimpleNamespace(authz=sys.modules["authz"], tenancy=sys.modules["tenancy"])


@pytest.fixture
def strict(monkeypatch, mods):
    """Sign-in configured (read at call time; the server itself was built without it)."""
    monkeypatch.setenv("OIDC_CONFIG_URL", "https://idp.example.com/.well-known/openid-configuration")
    monkeypatch.setenv("OIDC_CLIENT_ID", "aicortex")
    return mods


def _audit(az):
    if not az.AUDIT_FILE.exists():
        return []
    return [json.loads(ln) for ln in az.AUDIT_FILE.read_text(encoding="utf-8").splitlines()]


def _call(mw, tool, args=None):
    ctx = SimpleNamespace(message=SimpleNamespace(name=tool, arguments=args or {}))

    async def nxt(_context):
        return "reached-tool"
    return asyncio.run(mw.on_call_tool(ctx, nxt))


# ── sign-in configured: no role, denied, audited ─────────────────────────────
@pytest.mark.parametrize("ident", UNRESOLVED)
def test_an_unresolved_identity_has_no_role(strict, ident):
    az = strict.authz
    assert az.auth_configured() is True
    assert az.role_for(ident, False) == ""
    assert az.role_for(ident, False, {"groups": ["admin"]}) == ""      # no claim can lift it
    assert az.session_role(ident, "admin") == ""
    ok, reason = az.decide("", "ping")
    assert not ok and "identity" in reason


def test_the_gate_denies_and_audits_an_unresolved_caller(strict, monkeypatch):
    az = strict.authz
    from fastmcp.exceptions import ToolError
    monkeypatch.setattr(az, "_identity", lambda: ("unknown", False, {}))
    mw = az.build_middleware()
    for tool in ("ping", "memory_read", "secret_set"):
        with pytest.raises(ToolError, match="Denied by policy"):
            _call(mw, tool)
    lines = [r for r in _audit(az) if r["identity"] == "unknown"]
    assert [r["decision"] for r in lines] == ["deny", "deny", "deny"]

    async def nxt(_context):
        return [SimpleNamespace(name="ping"), SimpleNamespace(name="secret_set")]
    assert asyncio.run(mw.on_list_tools(None, nxt)) == []


def test_a_broken_identity_lookup_degrades_to_no_role(strict, monkeypatch):
    az = strict.authz
    monkeypatch.setattr(az, "_identity", _boom)
    assert az.effective_identity() == ("unknown", "")
    monkeypatch.setattr(az, "_identity", lambda: ("bob", False, {}))
    monkeypatch.setattr(az, "role_for", _boom)
    assert az.effective_identity() == ("unknown", "")


@pytest.mark.parametrize("ident", UNRESOLVED)
@pytest.mark.parametrize("role", ["", "user", "admin"])
def test_tenancy_confines_an_unresolved_identity(strict, ident, role):
    t = strict.tenancy
    area = t.area_for(ident, role)
    assert area["confined"] is True
    assert area["memory_scope"].startswith("users/") and area["vault_ns"].startswith("users/")
    assert t.confine_memory_scope(ident, role, "shared") != "shared"
    assert t.vault_namespace(ident, role) != ""
    assert not t.service_allowed(ident, role, "github")
    assert not t.skill_allowed(ident, role, "printer-setup")
    assert not t.endpoint_allowed(ident, role, "ssh", "box.example.com")


def test_an_admin_entry_named_unknown_does_not_open_it_up(strict):
    strict.tenancy._write_policy({"roles": {"unknown": "admin"},
                                  "users": {"unknown": {"memory": "all", "services": "all"}}})
    assert strict.authz.role_for("unknown", False) == ""
    assert strict.tenancy.area_for("unknown", "admin")["memory_scope"] is not None
    assert not strict.tenancy.service_allowed("unknown", "admin", "github")


# ── unchanged: local operator, enforcement off, resolved identities ──────────
@pytest.mark.secure_default_role
def test_without_sign_in_unknown_is_still_the_local_operator(mods, monkeypatch):
    az, t = mods.authz, mods.tenancy
    assert az.auth_configured() is False
    assert az.role_for("unknown", False) == "admin"
    assert t.area_for("unknown", "admin") == {"memory_scope": None, "vault_ns": None,
                                              "confined": False}
    assert t.confine_memory_scope("unknown", "admin", "shared") == "shared"
    monkeypatch.setattr(az, "_identity", _boom)
    assert az.effective_identity() == ("unknown", "user")


def test_with_enforcement_off_nothing_changes(strict, monkeypatch):
    monkeypatch.setenv("AUTH_ENFORCE", "0")
    az, t = strict.authz, strict.tenancy
    assert az.role_for("unknown", False) == "admin"
    assert t.area_for("unknown", "")["confined"] is False
    monkeypatch.setattr(az, "_identity", _boom)
    assert az.effective_identity() == ("unknown", "user")


def test_a_resolved_person_is_unaffected(strict):
    az, t = strict.authz, strict.tenancy
    assert az.role_for("alice", False, {"groups": ["viewer"]}) == "viewer"
    assert az.role_for("runner", True) == "user"
    assert t.area_for("boss", "admin")["confined"] is False
    assert t.area_for("alice", "user")["memory_scope"].startswith("users/alice-")
