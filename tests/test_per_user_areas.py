"""Unit tests for per-user service/skill areas + cron act-as (task f821eab9).

Pure-function tests for tenancy.py — no MCP server needed. Drives policy.json via
a temp AUTH_STORE_DIR and TENANCY_ISOLATE, then checks the access resolvers and the
act-as escalation guard.
"""
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

APP = "/Users/steffenmac/Downloads/LLMConnector/app"
sys.path.insert(0, APP)

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        failures.append(name)


def load_tenancy(policy: dict, isolate=True):
    """Reload tenancy with a fresh temp policy so each scenario is isolated."""
    d = tempfile.mkdtemp()
    Path(d, "policy.json").write_text(json.dumps(policy), encoding="utf-8")
    os.environ["AUTH_STORE_DIR"] = d
    os.environ["TENANCY_ISOLATE"] = "1" if isolate else "0"
    import tenancy
    importlib.reload(tenancy)
    return tenancy


ALICE = "alice@x.com"
BOB = "bob@x.com"

# ── Scenario A: isolation OFF → everything allowed (homelab, backward compat) ──
t = load_tenancy({"users": {ALICE: {"services": "none", "skills": "none"}}}, isolate=False)
check("iso-off: service allowed despite 'none'", t.service_allowed(ALICE, "user", "github", "Dev"))
check("iso-off: skill allowed despite 'none'", t.skill_allowed(ALICE, "user", "web-seite-lesen", "Web"))

# ── Scenario B: isolation ON, user with an allow-list ──
pol = {"users": {
    ALICE: {"services": ["github", "Documents"], "skills": ["web-seite-lesen"]},
    BOB: {"services": "all"},          # explicit all
    "carol": {"services": "none"},     # locked out
}}
t = load_tenancy(pol, isolate=True)

# admin is never confined
check("admin: all services", t.service_allowed(ALICE, "admin", "anything", "x"))

# alice: name in list
check("alice: github by name", t.service_allowed(ALICE, "user", "github", "Dev"))
# alice: category in list (paperless is category 'Documents')
check("alice: paperless by CATEGORY", t.service_allowed(ALICE, "user", "paperless", "Documents"))
# alice: not listed → denied
check("alice: crafty denied", not t.service_allowed(ALICE, "user", "crafty", "Gaming"))
# alice skills
check("alice: web skill allowed", t.skill_allowed(ALICE, "user", "web-seite-lesen", "Web"))
check("alice: other skill denied", not t.skill_allowed(ALICE, "user", "mql5-mastery", "Programmierung"))
# alice has no 'skills'? she does. But she has no explicit 'services' default → she does. Check default:
check("alice: skills default is her list (candlestick denied)",
      not t.skill_allowed(ALICE, "user", "candlestick", "Trading"))

# bob: explicit 'all'
check("bob: all services", t.service_allowed(BOB, "user", "crafty", "Gaming"))
# bob: skills field ABSENT → default 'all'
check("bob: skills default all", t.skill_allowed(BOB, "user", "anything", "x"))

# carol: 'none' → nothing
check("carol: service none", not t.service_allowed("carol", "user", "github", "Dev"))

# unknown user (no entry) → default 'all' for capabilities
check("unknown user: default all services", t.service_allowed("nobody", "user", "github", "Dev"))

# unresolved identity → fail-open all
check("unresolved: fail-open all", t.service_allowed("", "user", "github", "Dev"))
check("unknown identity token: fail-open all", t.service_allowed("unknown", "user", "github", "Dev"))

# ── Scenario C: act-as escalation guard ──
check("actas: empty owner ok (no act-as)", t.act_as_owner(BOB, "user", "") == (True, ""))
check("actas: admin may set any owner", t.act_as_owner("admin-sub", "admin", ALICE) == (True, ALICE))
check("actas: user as SELF ok", t.act_as_owner(BOB, "user", BOB) == (True, BOB))
ok, reason = t.act_as_owner(BOB, "user", ALICE)
check("actas: user as OTHER refused", ok is False and "admin" in reason)

# ── Scenario D: string allow-list ("github, Documents") parses like a list ──
t2 = load_tenancy({"users": {ALICE: {"services": "github, Documents"}}}, isolate=True)
check("str-list: github allowed", t2.service_allowed(ALICE, "user", "github", "Dev"))
check("str-list: crafty denied", not t2.service_allowed(ALICE, "user", "crafty", "Gaming"))

# ── Scenario E: _fmt_access rendering ──
check("fmt: list", t.__dict__["_fmt_access"](["github", "Documents"]) == "github,Documents")
check("fmt: all", t.__dict__["_fmt_access"]("all") == "all")
check("fmt: empty list → none", t.__dict__["_fmt_access"]([]) == "none")

print()
if failures:
    print(f"{len(failures)} FAILURES:", failures)
    sys.exit(1)
print("ALL TESTS PASSED")
