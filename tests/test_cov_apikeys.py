"""API keys (app/apikeys.py): scopes, expiry, verification refusals and the admin tools.

Proves what the existing end-to-end test skips: scope specs in every accepted shape
(alias, glob, string, blanks, 'all'), the hard denylist beating any scope, a key that
is malformed / tampered / expired / disabled / stored as junk never verifies (and
verify never raises), listings tolerate corrupt files and never reveal the secret,
the sliding-window rate limit really slides, and apikey_create / _list / _revoke
return the documented messages and stay admin-only.
"""
import asyncio
import json
import sys
import time
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, fn):
        self.tools[fn.__name__] = fn
        return fn


@pytest.fixture
def keys(monkeypatch, fresh_server):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    apikeys = sys.modules["apikeys"]
    fake = FakeMCP()
    apikeys.register(fake)
    return SimpleNamespace(mod=apikeys, t=fake.tools, server=fresh_server)


def _record_path(keys, pub):
    return keys.mod.APIKEY_DIR / f"{pub['keyid']}.json"


# ── scopes ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("spec, expected", [
    ("memory", ["memory_read", "memory_list", "memory_search", "memory_write",
                "memory_note", "memory_candidates"]),
    ("fs_read, fs_read  fs_write", ["fs_read", "fs_write"]),          # string, deduped
    (["Skills", " ", "ping"], ["skill_*", "ping"]),                    # alias case-insensitive
    (["memory", "memory"], ["memory_read", "memory_list", "memory_search", "memory_write",
                            "memory_note", "memory_candidates"]),
    (["fs_read", "ALL", "ping"], ["all"]),                             # 'all' swallows the rest
    (None, []), ("", []), ([" ", ""], []),
])
def test_scope_specs_are_normalized(keys, spec, expected):
    assert keys.mod._expand_scopes(spec) == expected


def test_scope_matching_is_exact_or_prefix_and_default_deny(keys):
    allows = keys.mod.scope_allows
    assert allows("tasks", "task_add") and allows("tasks", "inbox_read") and allows("tasks", "agent_list")
    assert allows(["fs_read"], "fs_read") and not allows(["fs_read"], "fs_read_all")
    assert allows(["fs_*"], "fs_") and not allows(["fs_*"], "fx_write")
    assert not allows([], "ping") and not allows("", "ping")
    assert allows("all", "ping") and allows(["Memory", "ALL"], "anything_else")


@pytest.mark.parametrize("tool", ["secret_set", "secret_delete", "apikey_create", "apikey_list",
                                  "apikey_revoke", "tenancy_set", "tenancy_anything"])
def test_hard_denylist_beats_every_scope(keys, tool):
    assert keys.mod.hard_denied(tool)
    assert not keys.mod.scope_allows("all", tool)
    assert not keys.mod.scope_allows([tool, "tenancy_*"], tool)


# ── minting ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("sub", ["", "   ", None])
def test_a_key_needs_an_identity(keys, sub):
    with pytest.raises(ValueError, match="sub"):
        keys.mod.create(sub, scopes="all")


def test_a_key_needs_scopes_and_nothing_is_written_without_them(keys):
    with pytest.raises(ValueError, match="scopes is required"):
        keys.mod.create("alice", scopes=" , ")
    assert not keys.mod.APIKEY_DIR.exists() or not list(keys.mod.APIKEY_DIR.glob("*.json"))


def test_created_record_stores_only_the_hash_and_a_normalized_profile(keys):
    full, pub = keys.mod.create("alice", name=" n8n ", scopes="memory", ttl_days=2,
                                created_by=" boss ", profile="VOLL")
    stored = json.loads(_record_path(keys, pub).read_text())
    secret = full.split("_", 2)[2]
    assert secret not in json.dumps(stored) and stored["secret_sha256"] == keys.mod._hash(secret)
    assert "secret_sha256" not in pub
    assert (pub["name"], pub["created_by"], pub["profile"]) == ("n8n", "boss", "full")
    assert abs(pub["expires"] - (time.time() + 2 * 86400)) < 5
    assert keys.mod.create("alice", scopes="all", profile="bogus")[1]["profile"] == "core"
    assert keys.mod.create("alice", scopes="all", ttl_days=0)[1]["expires"] == 0
    assert keys.mod.create("alice", scopes="all", ttl_days=-3)[1]["expires"] == 0


# ── verification ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", [None, "", 42, "plain", "ak_", "ak_onlykeyid", "ak__secret",
                                 "ak_abc_", "ak_deadbeefdead_" + "A" * 43])
def test_malformed_or_unknown_keys_never_verify(keys, bad):
    assert keys.mod.verify(bad) is None


def test_a_valid_key_verifies_and_a_wrong_secret_does_not(keys):
    full, pub = keys.mod.create("alice", scopes="all")
    assert keys.mod.verify(full)["sub"] == "alice"
    keyid_part = full.rsplit("_", 1)[0]
    assert keys.mod.verify(keyid_part + "_" + "B" * 43) is None


def test_expired_and_disabled_keys_are_rejected(keys):
    full, pub = keys.mod.create("alice", scopes="all", ttl_days=1)
    path = _record_path(keys, pub)
    rec = json.loads(path.read_text())
    assert keys.mod.verify(full) is not None
    path.write_text(json.dumps(rec | {"expires": int(time.time()) - 1}))
    assert keys.mod.verify(full) is None
    path.write_text(json.dumps(rec | {"expires": 0, "disabled": True}))
    assert keys.mod.verify(full) is None


@pytest.mark.parametrize("content", ["[1, 2]", "null", "{not json", '"just a string"'])
def test_a_record_that_is_not_an_object_never_verifies(keys, content):
    full, pub = keys.mod.create("alice", scopes="all")
    _record_path(keys, pub).write_text(content)
    assert keys.mod.verify(full) is None


def test_a_crafted_keyid_cannot_leave_the_key_directory(keys):
    outside = keys.mod.APIKEY_DIR.parent / "outside.json"
    outside.write_text(json.dumps({"sub": "mallory", "secret_sha256": keys.mod._hash("x")}))
    assert keys.mod.verify("ak_../outside_x") is None
    assert keys.mod._keyid_path("../../etc").parent == keys.mod.APIKEY_DIR
    assert keys.mod.revoke("../outside") is False and outside.exists()


# ── listing and revoking ─────────────────────────────────────────────────────
def test_listing_without_a_key_directory_is_empty(keys):
    assert not keys.mod.APIKEY_DIR.exists()
    assert keys.mod.list_keys() == []


def test_listing_skips_corrupt_records_and_hides_hashes(keys):
    keys.mod.create("alice", scopes="all")
    (keys.mod.APIKEY_DIR / "ffffffffffff.json").write_text("{corrupt")
    listed = keys.mod.list_keys()
    assert [k["sub"] for k in listed] == ["alice"]
    assert "secret_sha256" not in listed[0]


def test_revoke_deletes_the_record_once(keys):
    full, pub = keys.mod.create("alice", scopes="all")
    assert keys.mod.revoke(pub["keyid"].upper()) is True
    assert keys.mod.verify(full) is None
    assert keys.mod.revoke(pub["keyid"]) is False and keys.mod.revoke("") is False


# ── rate limit ───────────────────────────────────────────────────────────────
def test_rate_limit_window_slides_and_reports_retry_after(keys, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(keys.mod, "time", SimpleNamespace(time=lambda: clock[0]))
    assert all(keys.mod.rate_ok("k", limit=2)[0] for _ in range(2))
    clock[0] = 1010.0
    ok, retry = keys.mod.rate_ok("k", limit=2)
    assert not ok and retry == 51                      # oldest hit leaves the window at +60 s
    assert keys.mod.rate_ok("other", limit=2)[0]       # per-key, not global
    clock[0] = 1061.0
    assert keys.mod.rate_ok("k", limit=2) == (True, 0)  # old hits dropped from the window


def test_rate_limit_default_and_disabled_modes(keys, monkeypatch):
    monkeypatch.setattr(keys.mod, "_RATE_PER_MIN", 1)
    assert keys.mod.rate_ok("a")[0] and not keys.mod.rate_ok("a")[0]
    monkeypatch.setattr(keys.mod, "_RATE_PER_MIN", 0)
    assert all(keys.mod.rate_ok("a") == (True, 0) for _ in range(5))


# ── the admin tools ──────────────────────────────────────────────────────────
def test_apikey_create_shows_the_key_once_with_scopes_and_expiry(keys):
    text = keys.t["apikey_create"]("alice", name="hermes", scopes="memory, ping", ttl_days=1)
    (rec,) = keys.mod.list_keys()
    assert f"keyid {rec['keyid']}" in text and "SHOWN ONCE" in text and "memory_read" in text
    assert f"apikey_revoke('{rec['keyid']}')" in text
    assert "expires: never" not in text and "expires: 20" in text
    full = next(w for w in text.split() if w.startswith("ak_") and w.count("_") >= 2)
    assert keys.mod.verify(full)["sub"] == "alice"
    assert "expires: never" in keys.t["apikey_create"]("bob", scopes="all")


def test_apikey_create_refuses_instead_of_raising(keys, monkeypatch):
    assert keys.t["apikey_create"]("", scopes="all").startswith("Refused: sub")
    assert keys.t["apikey_create"]("alice", scopes="").startswith("Refused: scopes is required")

    def broken(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(keys.mod, "create", broken)
    assert keys.t["apikey_create"]("alice", scopes="all") == "Could not create API key: disk full"


def test_apikey_create_records_who_created_it(keys):
    authz = sys.modules["authz"]
    with authz.rest_identity("boss", "admin"):
        keys.t["apikey_create"]("alice", scopes="all")
    assert keys.mod.list_keys()[0]["created_by"] == "boss"


def test_apikey_list_reports_state_without_secrets(keys):
    assert keys.t["apikey_list"]().startswith("No API keys yet")
    _, live = keys.mod.create("alice", name="hermes", scopes="memory_read")
    _, old = keys.mod.create("bob", scopes="all", ttl_days=1)
    _, off = keys.mod.create("carol", scopes="fs_*")
    old_path, off_path = _record_path(keys, old), _record_path(keys, off)
    old_path.write_text(json.dumps(json.loads(old_path.read_text()) | {"expires": int(time.time()) - 5}))
    off_path.write_text(json.dumps(json.loads(off_path.read_text()) | {"disabled": True}))
    text = keys.t["apikey_list"]()
    by_sub = {line.split(" — ")[1]: line for line in text.splitlines()}
    assert "'hermes'" in by_sub["alice"] and "active, expires: never" in by_sub["alice"]
    assert "EXPIRED" in by_sub["bob"] and "disabled" in by_sub["carol"]
    assert "secret" not in text.lower() and f"ak_{live['keyid']}…" in text


def test_apikey_revoke_accepts_id_prefixed_id_or_a_pasted_key(keys):
    full1, p1 = keys.mod.create("alice", scopes="all")
    full2, p2 = keys.mod.create("bob", scopes="all")
    full3, p3 = keys.mod.create("carol", scopes="all")
    assert keys.t["apikey_revoke"](p1["keyid"]).startswith(f"Revoked API key '{p1['keyid']}'")
    assert keys.t["apikey_revoke"](f"ak_{p2['keyid']}").startswith("Revoked")
    assert keys.t["apikey_revoke"](f"  {full3}  ").startswith("Revoked")
    assert all(keys.mod.verify(f) is None for f in (full1, full2, full3))
    assert keys.t["apikey_revoke"](p1["keyid"]) == f"No API key with keyid '{p1['keyid']}'."


def test_the_key_tools_are_denied_to_a_non_admin_through_the_server(keys):
    authz = sys.modules["authz"]

    async def go():
        with authz.rest_identity("alice", "user"):
            try:
                await keys.server.mcp.call_tool("apikey_create", {"identity": "alice", "scopes": "all"})
            except ToolError as exc:
                return str(exc)
    assert "Denied by policy" in asyncio.run(go())
    assert not keys.mod.APIKEY_DIR.exists() or not list(keys.mod.APIKEY_DIR.glob("*.json"))
