"""REST API keys + REST authz gate (v1.9.0).

Pure-function + integration tests (no MCP server). Verifies: key mint/verify,
constant-time hash, expiry/disabled, scope allow-list + hard denylist, rate limit,
full CRUD (revoke=delete); and the authz side: API-key role never defaults to admin,
the request-scoped identity contextvar, and enforce_rest mirrors the middleware gate.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# Env must be set BEFORE import (modules compute dirs/policy path at import time).
_DIR = tempfile.mkdtemp()
os.environ["AUTH_STORE_DIR"] = _DIR
os.environ["APIKEY_DIR"] = str(Path(_DIR) / "apikeys")
os.environ["STORAGE_ENCRYPTION_KEY"] = "test-key"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import apikeys  # noqa: E402
import authz    # noqa: E402
import tenancy  # noqa: E402

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        failures.append(name)


def _write_policy(obj):
    Path(_DIR, "policy.json").write_text(json.dumps(obj), encoding="utf-8")


# ── Key mint / verify ───────────────────────────────────────────────────────
full, pub = apikeys.create("alice", name="n8n", scopes="memory, fs_read")
check("create returns ak_ key", full.startswith("ak_") and full.count("_") >= 2)
check("public record has no secret hash", "secret_sha256" not in pub)
check("verify accepts the real key", (apikeys.verify(full) or {}).get("sub") == "alice")
check("verify rejects a tampered key", apikeys.verify(full[:-3] + "xyz") is None)
check("verify rejects unknown keyid", apikeys.verify("ak_deadbeefdead_" + "A" * 40) is None)
check("verify rejects garbage", apikeys.verify("not-a-key") is None and apikeys.verify("") is None)

# ── empty scopes refused (default-deny) ─────────────────────────────────────
try:
    apikeys.create("bob", scopes="")
    check("create refuses empty scopes", False)
except ValueError:
    check("create refuses empty scopes", True)

# ── expiry & disabled ───────────────────────────────────────────────────────
full_exp, pub_exp = apikeys.create("carol", scopes="all", ttl_days=1)
p = apikeys.APIKEY_DIR / f"{pub_exp['keyid']}.json"
rec = json.loads(p.read_text())
rec["expires"] = int(time.time()) - 10          # force expired
p.write_text(json.dumps(rec))
check("verify rejects expired key", apikeys.verify(full_exp) is None)
rec["expires"] = 0
rec["disabled"] = True
p.write_text(json.dumps(rec))
check("verify rejects disabled key", apikeys.verify(full_exp) is None)

# ── scope allow-list + hard denylist ────────────────────────────────────────
check("scope: exact name", apikeys.scope_allows(["memory_read"], "memory_read"))
check("scope: prefix glob", apikeys.scope_allows(["fs_*"], "fs_write"))
check("scope: alias expands", apikeys.scope_allows("skills", "skill_load"))
check("scope: 'all' allows normal tool", apikeys.scope_allows(["all"], "ping"))
check("scope: default-deny miss", not apikeys.scope_allows(["memory_read"], "fs_write"))
check("hard-deny: secret_set even with 'all'", not apikeys.scope_allows(["all"], "secret_set"))
check("hard-deny: apikey_create", apikeys.hard_denied("apikey_create"))
check("hard-deny: tenancy_ prefix", apikeys.hard_denied("tenancy_set"))
check("hard-deny: normal tool not denied", not apikeys.hard_denied("memory_read"))

# ── list + revoke (full CRUD) ───────────────────────────────────────────────
names = {k["sub"] for k in apikeys.list_keys()}
check("list_keys shows minted keys", "alice" in names)
check("list_keys never leaks hash", all("secret_sha256" not in k for k in apikeys.list_keys()))
alice_kid = json.loads((apikeys.APIKEY_DIR / f"{pub['keyid']}.json").read_text())["keyid"]
check("revoke deletes an existing key", apikeys.revoke(alice_kid) is True)
check("verify fails after revoke", apikeys.verify(full) is None)
check("revoke unknown keyid → False", apikeys.revoke("ffffffffffff") is False)

# ── rate limit ──────────────────────────────────────────────────────────────
ok_all = all(apikeys.rate_ok("kx", limit=3)[0] for _ in range(3))
blocked, retry = apikeys.rate_ok("kx", limit=3)
check("rate: first N allowed", ok_all)
check("rate: N+1 blocked with retry", (not blocked) and retry >= 1)
check("rate: limit=0 disables", apikeys.rate_ok("ky", limit=0)[0] is True)

# ── authz: API-key role never defaults to admin ─────────────────────────────
_write_policy({"roles": {"adminguy": "admin", "peeker": "viewer"}})
check("role_for_apikey unknown → user (never admin)", authz.role_for_apikey("nobody") == "user")
check("role_for_apikey honors policy admin", authz.role_for_apikey("adminguy") == "admin")
check("role_for_apikey honors policy viewer", authz.role_for_apikey("peeker") == "viewer")

# ── authz: request-scoped identity contextvar ───────────────────────────────
with authz.rest_identity("dana", "user"):
    ident, role = authz.effective_identity()
check("rest_identity sets effective identity", (ident, role) == ("dana", "user"))
# and it resets on exit (falls through to real caller / unknown without fastmcp)
ident2, _ = authz.effective_identity()
check("rest_identity resets after the block", ident2 != "dana")

# ── authz: enforce_rest mirrors the middleware gate ─────────────────────────
os.environ["AUTH_ENFORCE"] = "1"
_write_policy({"users": {"eve": {"memory": "own"}}})  # eve is a confined non-admin

ok_admin, _ = authz.enforce_rest("root", "admin", "service_add", {"name": "x"})
check("enforce_rest: admin may call admin tool", ok_admin)
ok_user, reason = authz.enforce_rest("eve", "user", "service_add", {"name": "x"})
check("enforce_rest: user denied admin tool", not ok_user)
ok_norm, _ = authz.enforce_rest("eve", "user", "ping", {})
check("enforce_rest: user allowed normal tool", ok_norm)

# memory scope confinement mutates args for a confined user
margs = {"scope": "shared", "title": "t", "content": "c", "type": "user"}
authz.enforce_rest("eve", "user", "memory_write", margs)
check("enforce_rest: confines memory scope", margs["scope"].startswith("users/"))

# device-endpoint area: no caldav grant → deny under enforce
ok_ep, reason_ep = authz.enforce_rest("eve", "user", "caldav_add_event",
                                      {"endpoint": "nc", "calendar": "c",
                                       "summary": "s", "start": "x", "end": "y"})
check("enforce_rest: endpoint default-deny", (not ok_ep) and "not in your allowed set" in reason_ep)

# attribution stamping (can't be forged from the body)
iargs = {"to": "user", "body": "hi", "sender": "SPOOFED"}
authz.enforce_rest("eve", "user", "inbox_post", iargs)
check("enforce_rest: stamps real sender", iargs["sender"] == "eve")

# homelab mode: enforce_rest is a no-op pass-through
os.environ["AUTH_ENFORCE"] = "0"
ok_homelab, _ = authz.enforce_rest("eve", "user", "service_add", {"name": "x"})
check("enforce_rest: homelab allows (no checks)", ok_homelab)
os.environ["AUTH_ENFORCE"] = "1"

print()
if failures:
    print(f"{len(failures)} FAILED:", ", ".join(failures))
    sys.exit(1)
print("ALL TESTS PASSED")
