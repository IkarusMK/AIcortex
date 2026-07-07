"""Per-user service/skill areas + cron act-as (task f821eab9).

Final model (briefing 02.07): areas ride on AUTH_ENFORCE; capabilities are
DEFAULT-DENY under enforce and FAIL-CLOSED; cron runs act-as as the job owner via
a capability token. Pure-function + integration tests (no MCP server).
"""
import importlib
import json
import tempfile
from pathlib import Path


def test_per_user_areas_and_actas(monkeypatch):
    _DIR = tempfile.mkdtemp()
    monkeypatch.setenv("AUTH_STORE_DIR", _DIR)
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", "test-key-for-actas")
    # Modules compute POLICY_FILE at import time → reload after setting env.
    import tenancy
    import authz
    import actas
    importlib.reload(tenancy)
    importlib.reload(authz)
    importlib.reload(actas)

    def set_policy(obj):
        Path(_DIR, "policy.json").write_text(json.dumps(obj), encoding="utf-8")

    def enforce(on: bool):
        monkeypatch.setenv("AUTH_ENFORCE", "1" if on else "0")

    ALICE, BOB = "alice@x.com", "bob@x.com"
    POL = {"users": {
        ALICE: {"services": ["github", "Documents"], "skills": ["web-seite-lesen"]},
        BOB: {"services": "all"},          # services all, NO skills field
        "carol": {"services": "none"},
    }, "roles": {"boss@x.com": "admin"}}
    set_policy(POL)

    # ── Homelab (AUTH_ENFORCE=0): no checks, everything allowed ──
    enforce(False)
    assert tenancy.service_allowed("carol", "user", "github", "Dev"), "homelab: carol 'none' still allowed"
    assert tenancy.service_allowed("nobody", "user", "x", "y"), "homelab: unknown user allowed"
    assert tenancy.skill_allowed(BOB, "user", "any", "z"), "homelab: bob skills (no field) allowed"

    # ── Enforce (AUTH_ENFORCE=1): default-deny + allow-lists ──
    enforce(True)
    assert tenancy.service_allowed("boss@x.com", "admin", "anything", "x"), "enforce: admin all"
    assert tenancy.service_allowed(ALICE, "user", "github", "Dev"), "enforce: alice github by name"
    assert tenancy.service_allowed(ALICE, "user", "paperless", "Documents"), "enforce: alice paperless by CATEGORY"
    assert not tenancy.service_allowed(ALICE, "user", "crafty", "Gaming"), "enforce: alice crafty DENIED"
    assert tenancy.skill_allowed(ALICE, "user", "web-seite-lesen", "Web"), "enforce: alice web skill allowed"
    assert not tenancy.skill_allowed(ALICE, "user", "mql5", "Programmierung"), "enforce: alice other skill DENIED"
    assert tenancy.service_allowed(BOB, "user", "crafty", "Gaming"), "enforce: bob services all"
    assert not tenancy.skill_allowed(BOB, "user", "any", "z"), "enforce: bob NO skills field → DEFAULT-DENY"
    assert not tenancy.service_allowed("carol", "user", "github", "Dev"), "enforce: carol none → deny"
    assert not tenancy.service_allowed("nobody", "user", "github", "Dev"), "enforce: unknown user → DEFAULT-DENY"
    assert not tenancy.service_allowed("", "user", "github", "Dev"), "enforce: empty identity → deny"

    # ── Fail-closed on corrupt policy (enforce) ──
    Path(_DIR, "policy.json").write_text("{ this is not json", encoding="utf-8")
    assert not tenancy.service_allowed(ALICE, "user", "github", "Dev"), "enforce: corrupt policy → non-admin DENY"
    assert tenancy.service_allowed("boss@x.com", "admin", "github", "Dev"), "enforce: corrupt policy → admin still all"
    enforce(False)
    assert tenancy.service_allowed(ALICE, "user", "github", "Dev"), "homelab: corrupt policy → allowed (no checks)"
    set_policy(POL)
    enforce(True)

    # ── act_as_owner escalation guard ──
    assert tenancy.act_as_owner(BOB, "user", "") == (True, ""), "actas-owner: empty ok"
    assert tenancy.act_as_owner("boss", "admin", ALICE) == (True, ALICE), "actas-owner: admin any"
    assert tenancy.act_as_owner(BOB, "user", BOB) == (True, BOB), "actas-owner: user self"
    assert tenancy.act_as_owner(BOB, "user", ALICE)[0] is False, "actas-owner: user other refused"

    # ── authz.effective_identity honors act-as, never defaults owner to admin ──
    actas.end()
    tok = actas.issue("job1", ALICE)
    ok, _ = actas.begin(tok, "job1")
    assert ok, "actas: begin ok"
    ident, role = authz.effective_identity()
    assert ident == ALICE, "effective: identity is owner"
    assert role == "user", "effective: owner role is 'user' (NOT default-admin)"
    # integration: capability check now follows the owner's grants
    assert tenancy.caller_service_allowed("github", "Dev"), "act-as run: alice may use github"
    assert not tenancy.caller_service_allowed("crafty", "Gaming"), "act-as run: alice may NOT use crafty"
    # owner that IS an admin in policy.roles resolves to admin
    actas.end()
    ok, _ = actas.begin(actas.issue("j2", "boss@x.com"), "j2")
    _, boss_role = authz.effective_identity()
    assert boss_role == "admin", "effective: policy-admin owner → admin role"

    # ── single-use: consume() then the same token can't begin again ──
    actas.end()
    t3 = actas.issue("j3", ALICE)
    assert actas.begin(t3, "j3")[0] is True, "replay: first begin ok"
    actas.consume()
    assert actas.current() is None, "replay: binding cleared after consume"
    assert actas.begin(t3, "j3")[0] is False, "replay: reused token refused"
    actas.end()
    assert actas.current() is None, "no binding: current() None"

    # ── H1: per-user DEVICE/endpoint areas (caldav/imap/webdav/ssh/…) ──
    set_policy({"users": {
        "dave": {"caldav": ["nextcloud-cal"], "ssh": "all", "imap": "none"},
    }, "roles": {"boss@x.com": "admin"}})
    enforce(True)
    assert tenancy.endpoint_allowed("dave", "user", "caldav", "nextcloud-cal"), "H1: dave caldav granted endpoint"
    assert not tenancy.endpoint_allowed("dave", "user", "caldav", "work-cal"), "H1: dave caldav OTHER denied"
    assert tenancy.endpoint_allowed("dave", "user", "ssh", "any-host"), "H1: dave ssh=all"
    assert not tenancy.endpoint_allowed("dave", "user", "imap", "acct"), "H1: dave imap=none deny"
    assert not tenancy.endpoint_allowed("dave", "user", "webdav", "nc"), "H1: dave NO webdav grant → DEFAULT-DENY"
    assert tenancy.endpoint_allowed("boss@x.com", "admin", "caldav", "anything"), "H1: admin all endpoints"
    assert not tenancy.endpoint_allowed("nobody", "user", "caldav", "x"), "H1: unknown user default-deny"
    enforce(False)
    assert tenancy.endpoint_allowed("dave", "user", "imap", "acct"), "H1 homelab: all endpoints allowed"
