"""Per-user service/skill areas + cron act-as (task f821eab9).

Final model (briefing 02.07): areas ride on AUTH_ENFORCE; capabilities are
DEFAULT-DENY under enforce and FAIL-CLOSED; cron runs act-as as the job owner via
a capability token. Pure-function + integration tests (no MCP server).
"""
import json
import os
import sys
import tempfile
from pathlib import Path

# AUTH_STORE_DIR must be set BEFORE importing the modules (they compute POLICY_FILE
# at import time). One temp dir; we rewrite policy.json per scenario.
_DIR = tempfile.mkdtemp()
os.environ["AUTH_STORE_DIR"] = _DIR
os.environ["STORAGE_ENCRYPTION_KEY"] = "test-key-for-actas"
sys.path.insert(0, "/Users/steffenmac/Downloads/LLMConnector/app")

import tenancy   # noqa: E402
import authz     # noqa: E402
import actas     # noqa: E402

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        failures.append(name)


def set_policy(obj):
    Path(_DIR, "policy.json").write_text(json.dumps(obj), encoding="utf-8")


def enforce(on: bool):
    os.environ["AUTH_ENFORCE"] = "1" if on else "0"


ALICE, BOB = "alice@x.com", "bob@x.com"
POL = {"users": {
    ALICE: {"services": ["github", "Documents"], "skills": ["web-seite-lesen"]},
    BOB: {"services": "all"},          # services all, NO skills field
    "carol": {"services": "none"},
}, "roles": {"boss@x.com": "admin"}}
set_policy(POL)

# ── Homelab (AUTH_ENFORCE=0): no checks, everything allowed ──
enforce(False)
check("homelab: carol 'none' still allowed", tenancy.service_allowed("carol", "user", "github", "Dev"))
check("homelab: unknown user allowed", tenancy.service_allowed("nobody", "user", "x", "y"))
check("homelab: bob skills (no field) allowed", tenancy.skill_allowed(BOB, "user", "any", "z"))

# ── Enforce (AUTH_ENFORCE=1): default-deny + allow-lists ──
enforce(True)
check("enforce: admin all", tenancy.service_allowed("boss@x.com", "admin", "anything", "x"))
check("enforce: alice github by name", tenancy.service_allowed(ALICE, "user", "github", "Dev"))
check("enforce: alice paperless by CATEGORY", tenancy.service_allowed(ALICE, "user", "paperless", "Documents"))
check("enforce: alice crafty DENIED", not tenancy.service_allowed(ALICE, "user", "crafty", "Gaming"))
check("enforce: alice web skill allowed", tenancy.skill_allowed(ALICE, "user", "web-seite-lesen", "Web"))
check("enforce: alice other skill DENIED", not tenancy.skill_allowed(ALICE, "user", "mql5", "Programmierung"))
check("enforce: bob services all", tenancy.service_allowed(BOB, "user", "crafty", "Gaming"))
check("enforce: bob NO skills field → DEFAULT-DENY", not tenancy.skill_allowed(BOB, "user", "any", "z"))
check("enforce: carol none → deny", not tenancy.service_allowed("carol", "user", "github", "Dev"))
check("enforce: unknown user → DEFAULT-DENY", not tenancy.service_allowed("nobody", "user", "github", "Dev"))
check("enforce: empty identity → deny", not tenancy.service_allowed("", "user", "github", "Dev"))

# ── Fail-closed on corrupt policy (enforce) ──
Path(_DIR, "policy.json").write_text("{ this is not json", encoding="utf-8")
check("enforce: corrupt policy → non-admin DENY", not tenancy.service_allowed(ALICE, "user", "github", "Dev"))
check("enforce: corrupt policy → admin still all", tenancy.service_allowed("boss@x.com", "admin", "github", "Dev"))
enforce(False)
check("homelab: corrupt policy → allowed (no checks)", tenancy.service_allowed(ALICE, "user", "github", "Dev"))
set_policy(POL)
enforce(True)

# ── act_as_owner escalation guard ──
check("actas-owner: empty ok", tenancy.act_as_owner(BOB, "user", "") == (True, ""))
check("actas-owner: admin any", tenancy.act_as_owner("boss", "admin", ALICE) == (True, ALICE))
check("actas-owner: user self", tenancy.act_as_owner(BOB, "user", BOB) == (True, BOB))
check("actas-owner: user other refused", tenancy.act_as_owner(BOB, "user", ALICE)[0] is False)

# ── authz.effective_identity honors act-as, never defaults owner to admin ──
actas.end()
tok = actas.issue("job1", ALICE)
ok, _ = actas.begin(tok, "job1")
check("actas: begin ok", ok)
ident, role = authz.effective_identity()
check("effective: identity is owner", ident == ALICE)
check("effective: owner role is 'user' (NOT default-admin)", role == "user")
# integration: capability check now follows the owner's grants
check("act-as run: alice may use github", tenancy.caller_service_allowed("github", "Dev"))
check("act-as run: alice may NOT use crafty", not tenancy.caller_service_allowed("crafty", "Gaming"))
# owner that IS an admin in policy.roles resolves to admin
actas.end()
ok, _ = actas.begin(actas.issue("j2", "boss@x.com"), "j2")
_, boss_role = authz.effective_identity()
check("effective: policy-admin owner → admin role", boss_role == "admin")

# ── single-use: consume() then the same token can't begin again ──
actas.end()
t3 = actas.issue("j3", ALICE)
check("replay: first begin ok", actas.begin(t3, "j3")[0] is True)
actas.consume()
check("replay: binding cleared after consume", actas.current() is None)
check("replay: reused token refused", actas.begin(t3, "j3")[0] is False)
actas.end()
check("no binding: current() None", actas.current() is None)

print()
if failures:
    print(f"{len(failures)} FAILURES:", failures)
    sys.exit(1)
print("ALL TESTS PASSED")
