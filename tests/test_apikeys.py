"""REST API keys + REST authz gate (v1.9.0).

Pure-function + integration tests (no MCP server). Verifies: key mint/verify,
constant-time hash, expiry/disabled, scope allow-list + hard denylist, rate limit,
full CRUD (revoke=delete); and the authz side: API-key role never defaults to admin,
the request-scoped identity contextvar, and enforce_rest mirrors the middleware gate.
"""
import importlib
import json
import tempfile
import time
from pathlib import Path

import pytest


def test_apikeys_and_rest_authz(monkeypatch):
    _DIR = tempfile.mkdtemp()
    monkeypatch.setenv("AUTH_STORE_DIR", _DIR)
    monkeypatch.setenv("APIKEY_DIR", str(Path(_DIR) / "apikeys"))
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", "test-key")
    # Modules compute dirs/policy path at import time → reload after setting env,
    # dependencies first (tenancy → authz → apikeys).
    import tenancy
    import authz
    import apikeys
    importlib.reload(tenancy)
    importlib.reload(authz)
    importlib.reload(apikeys)

    def _write_policy(obj):
        Path(_DIR, "policy.json").write_text(json.dumps(obj), encoding="utf-8")

    # ── Key mint / verify ───────────────────────────────────────────────────
    full, pub = apikeys.create("alice", name="n8n", scopes="memory, fs_read")
    assert full.startswith("ak_") and full.count("_") >= 2, "create returns ak_ key"
    assert "secret_sha256" not in pub, "public record has no secret hash"
    assert (apikeys.verify(full) or {}).get("sub") == "alice", "verify accepts the real key"
    assert apikeys.verify(full[:-3] + "xyz") is None, "verify rejects a tampered key"
    assert apikeys.verify("ak_deadbeefdead_" + "A" * 40) is None, "verify rejects unknown keyid"
    assert apikeys.verify("not-a-key") is None and apikeys.verify("") is None, "verify rejects garbage"

    # ── empty scopes refused (default-deny) ─────────────────────────────────
    with pytest.raises(ValueError):
        apikeys.create("bob", scopes="")

    # ── expiry & disabled ───────────────────────────────────────────────────
    full_exp, pub_exp = apikeys.create("carol", scopes="all", ttl_days=1)
    p = apikeys.APIKEY_DIR / f"{pub_exp['keyid']}.json"
    rec = json.loads(p.read_text())
    rec["expires"] = int(time.time()) - 10          # force expired
    p.write_text(json.dumps(rec))
    assert apikeys.verify(full_exp) is None, "verify rejects expired key"
    rec["expires"] = 0
    rec["disabled"] = True
    p.write_text(json.dumps(rec))
    assert apikeys.verify(full_exp) is None, "verify rejects disabled key"

    # ── scope allow-list + hard denylist ────────────────────────────────────
    assert apikeys.scope_allows(["memory_read"], "memory_read"), "scope: exact name"
    assert apikeys.scope_allows(["fs_*"], "fs_write"), "scope: prefix glob"
    assert apikeys.scope_allows("skills", "skill_load"), "scope: alias expands"
    assert apikeys.scope_allows(["all"], "ping"), "scope: 'all' allows normal tool"
    assert not apikeys.scope_allows(["memory_read"], "fs_write"), "scope: default-deny miss"
    assert not apikeys.scope_allows(["all"], "secret_set"), "hard-deny: secret_set even with 'all'"
    assert apikeys.hard_denied("apikey_create"), "hard-deny: apikey_create"
    assert apikeys.hard_denied("tenancy_set"), "hard-deny: tenancy_ prefix"
    assert not apikeys.hard_denied("memory_read"), "hard-deny: normal tool not denied"

    # ── list + revoke (full CRUD) ───────────────────────────────────────────
    names = {k["sub"] for k in apikeys.list_keys()}
    assert "alice" in names, "list_keys shows minted keys"
    assert all("secret_sha256" not in k for k in apikeys.list_keys()), "list_keys never leaks hash"
    alice_kid = json.loads((apikeys.APIKEY_DIR / f"{pub['keyid']}.json").read_text())["keyid"]
    assert apikeys.revoke(alice_kid) is True, "revoke deletes an existing key"
    assert apikeys.verify(full) is None, "verify fails after revoke"
    assert apikeys.revoke("ffffffffffff") is False, "revoke unknown keyid → False"

    # ── rate limit ──────────────────────────────────────────────────────────
    ok_all = all(apikeys.rate_ok("kx", limit=3)[0] for _ in range(3))
    blocked, retry = apikeys.rate_ok("kx", limit=3)
    assert ok_all, "rate: first N allowed"
    assert (not blocked) and retry >= 1, "rate: N+1 blocked with retry"
    assert apikeys.rate_ok("ky", limit=0)[0] is True, "rate: limit=0 disables"

    # ── authz: API-key role never defaults to admin ─────────────────────────
    _write_policy({"roles": {"adminguy": "admin", "peeker": "viewer"}})
    assert authz.role_for_apikey("nobody") == "user", "role_for_apikey unknown → user (never admin)"
    assert authz.role_for_apikey("adminguy") == "admin", "role_for_apikey honors policy admin"
    assert authz.role_for_apikey("peeker") == "viewer", "role_for_apikey honors policy viewer"

    # ── authz: request-scoped identity contextvar ───────────────────────────
    with authz.rest_identity("dana", "user"):
        ident, role = authz.effective_identity()
    assert (ident, role) == ("dana", "user"), "rest_identity sets effective identity"
    # and it resets on exit (falls through to real caller / unknown without fastmcp)
    ident2, _ = authz.effective_identity()
    assert ident2 != "dana", "rest_identity resets after the block"

    # ── authz: enforce_rest mirrors the middleware gate ─────────────────────
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    _write_policy({"users": {"eve": {"memory": "own"}}})  # eve is a confined non-admin

    ok_admin, _ = authz.enforce_rest("root", "admin", "service_add", {"name": "x"})
    assert ok_admin, "enforce_rest: admin may call admin tool"
    ok_user, reason = authz.enforce_rest("eve", "user", "service_add", {"name": "x"})
    assert not ok_user, "enforce_rest: user denied admin tool"
    ok_norm, _ = authz.enforce_rest("eve", "user", "ping", {})
    assert ok_norm, "enforce_rest: user allowed normal tool"

    # memory scope confinement mutates args for a confined user
    margs = {"scope": "shared", "title": "t", "content": "c", "type": "user"}
    authz.enforce_rest("eve", "user", "memory_write", margs)
    assert margs["scope"].startswith("users/"), "enforce_rest: confines memory scope"

    # device-endpoint area: no caldav grant → deny under enforce
    ok_ep, reason_ep = authz.enforce_rest("eve", "user", "caldav_add_event",
                                          {"endpoint": "nc", "calendar": "c",
                                           "summary": "s", "start": "x", "end": "y"})
    assert (not ok_ep) and "not in your allowed set" in reason_ep, "enforce_rest: endpoint default-deny"

    # attribution stamping (can't be forged from the body)
    iargs = {"to": "user", "body": "hi", "sender": "SPOOFED"}
    authz.enforce_rest("eve", "user", "inbox_post", iargs)
    assert iargs["sender"] == "eve", "enforce_rest: stamps real sender"

    # homelab mode: enforce_rest is a no-op pass-through
    monkeypatch.setenv("AUTH_ENFORCE", "0")
    ok_homelab, _ = authz.enforce_rest("eve", "user", "service_add", {"name": "x"})
    assert ok_homelab, "enforce_rest: homelab allows (no checks)"
