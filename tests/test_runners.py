"""Runners: who carries out scheduled jobs (runners.py, AICortex 2.3.0).

Before: one built-in runner, set up by an admin in runner_config.json, ran every job.
Now there is a registry of runners — each person may have their own (a Claude cloud
task to wake, a ChatGPT plan, an API key, or an OpenAI-compatible address such as a
LAN Ollama) — and a job is assigned to one of them. Proves:
- a 2.2 setup becomes the "Standard" runner on first read, without moving the key and
  without touching runner_config.json; a Claude subscription setup (setup-token, which
  Anthropic refuses) becomes a switched-off cloud runner;
- saving validates kind, model, address (through the SSRF guard) and trigger;
- a key is either picked from the vault — a person only their own entries, only admins
  shared ones, never the server's internal names — or typed in and kept under the
  runner's own internal name, never shown again;
- who may run whose job (may_run) and where an unassigned job goes (effective);
- only the owner edits a runner; admins may switch off or delete it; the standard
  runner can't be deleted; `runner_list` shows no key names.
"""
import asyncio
import json
import sys

import pytest
from fastmcp.exceptions import ToolError

KEY = "sk-or-test-0000"


@pytest.fixture
def reg(monkeypatch, request):
    monkeypatch.setenv("ALLOW_PLAINTEXT_VAULT", "1")
    monkeypatch.delenv("RUNNER_TOKEN", raising=False)
    monkeypatch.delenv("INTERNAL_ALLOW_CIDRS", raising=False)
    request.getfixturevalue("fresh_server")
    import runners
    return runners


def _vault():
    return sys.modules["secrets_store"]


def _legacy(reg, **cfg):
    reg.LEGACY_FILE.parent.mkdir(parents=True, exist_ok=True)
    reg.LEGACY_FILE.write_text(json.dumps(cfg))


def _save(reg, by="boss", admin=True, key="", **fields):
    fields.setdefault("kind", "openrouter")
    fields.setdefault("model", "vendor/model-a")
    return reg.save(fields, by=by, admin=admin, key=key)


def _call(tool, sub, role, **args):
    server, authz = sys.modules["server"], sys.modules["authz"]

    async def go():
        with authz.rest_identity(sub, role):
            try:
                res = await server.mcp.call_tool(tool, args)
                return "\n".join(getattr(c, "text", "") for c in res.content or [])
            except ToolError as exc:
                return f"DENIED: {exc}"
    return asyncio.run(go())


# ── a fresh install and the migration from 2.2 ───────────────────────────────
def test_a_fresh_install_has_no_runners_and_writes_nothing(reg):
    assert reg.load() == {"version": 1, "standard": "", "runners": []}
    assert not reg.RUNNERS_FILE.exists()


def test_the_2_2_setup_becomes_the_standard_runner(reg):
    _vault().vault_set("RUNNER_LLM_KEY", KEY)
    _legacy(reg, backend="openrouter", model="vendor/model-a", builtin=True, updated_by="boss")
    data = reg.load()
    (rec,) = data["runners"]
    assert data["standard"] == rec["id"] and rec["id"].startswith("r_")
    assert (rec["name"], rec["kind"], rec["model"], rec["owner"], rec["enabled"]) == (
        "Standard", "openrouter", "vendor/model-a", "boss", True)
    assert rec["key"] == {"name": "RUNNER_LLM_KEY", "shared": True}
    assert reg.resolve_key(rec) == KEY
    assert KEY not in reg.RUNNERS_FILE.read_text()
    assert reg.LEGACY_FILE.exists()                       # the way back to 2.2 stays open
    assert reg.load()["runners"] == [rec]                 # migrated once, not twice


@pytest.mark.parametrize("cfg, kind, enabled", [
    ({"backend": "openrouter", "model": "m", "builtin": False}, "openrouter", False),
    ({"backend": "anthropic", "builtin": True}, "anthropic", True),
    ({"backend": "claude-api", "builtin": True}, "anthropic", True),
    ({"backend": "chatgpt-abo", "builtin": True}, "chatgpt", True),
])
def test_an_older_setup_keeps_its_kind_and_switch(reg, cfg, kind, enabled):
    _legacy(reg, **cfg)
    data = reg.load()
    (rec,) = data["runners"]
    assert (rec["kind"], rec["enabled"], data["standard"]) == (kind, enabled, rec["id"])
    assert rec["key"] is None if kind == "chatgpt" else rec["key"]["name"] == "RUNNER_LLM_KEY"


def test_a_claude_subscription_setup_becomes_a_switched_off_cloud_runner(reg):
    _legacy(reg, backend="claude-abo", builtin=True, updated_by="boss")
    data = reg.load()
    (rec,) = data["runners"]
    assert (rec["kind"], rec["enabled"], rec["key"], data["standard"]) == ("claude-cloud", False, None, "")
    assert "cloud task" in rec["note"] and "RUNNER_LLM_KEY" in rec["note"]


# ── saving validates ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("fields, admin, why", [
    ({"kind": "claude-abo"}, True, "kind"),
    ({"kind": "openrouter", "model": ""}, True, "model"),
    ({"kind": "compatible", "base_url": "ftp://192.0.2.10/v1"}, True, "http"),
    ({"kind": "compatible", "base_url": "http://user:pw@192.0.2.10/v1"}, True, "user"),
    ({"kind": "compatible", "base_url": "http://192.0.2.10/v1?x=1"}, True, "query"),
    ({"kind": "claude-cloud", "trigger": "abc"}, True, "trig_"),
    ({"kind": "chatgpt"}, False, "admin"),
])
def test_saving_checks_kind_model_address_and_trigger(reg, fields, admin, why):
    ok, msg, rec = _save(reg, admin=admin, key=KEY, **fields)
    assert not ok and rec is None and why in msg
    assert reg.load()["runners"] == []


def test_there_is_one_chatgpt_runner_at_most(reg):
    assert _save(reg, kind="chatgpt", model="")[0]
    ok, msg, _ = _save(reg, kind="chatgpt", model="")
    assert not ok and "one" in msg


def test_a_cloud_trigger_is_taken_from_the_whole_address(reg):
    url = "https://api.anthropic.com/v1/claude_code/routines/trig_01AbCdEf/fire"
    ok, _, rec = _save(reg, kind="claude-cloud", model="", trigger=url, key="sk-ant-oat01-test")
    assert ok and rec["trigger"] == "trig_01AbCdEf"
    assert reg.fire_url(rec) == url


def test_an_ollama_address_needs_the_lan_allowed(reg, monkeypatch):
    fields = {"kind": "compatible", "model": "llama3.1", "base_url": "http://192.0.2.10:11434/v1/"}
    ok, msg, _ = _save(reg, **fields)
    assert not ok and "INTERNAL_ALLOW_CIDRS" in msg
    monkeypatch.setenv("INTERNAL_ALLOW_CIDRS", "192.0.2.0/24")
    ok, _, rec = _save(reg, **fields)
    assert ok and rec["base_url"] == "http://192.0.2.10:11434/v1" and rec["key"] is None


# ── keys: picked from the vault or typed in ──────────────────────────────────
def test_a_person_can_only_pick_their_own_vault_entries(reg):
    v = _vault()
    v.vault_set("OPENROUTER_API_KEY", "sk-shared")
    v.vault_set("MY_KEY", "sk-alice", owner="alice")
    v.vault_set("BOB_KEY", "sk-bob", owner="bob")
    v.vault_set("RUNNER_LLM_KEY", KEY)
    assert reg.usable_key_names("alice", admin=False) == [{"name": "MY_KEY", "shared": False}]
    names = reg.usable_key_names("boss", admin=True)
    assert {"name": "OPENROUTER_API_KEY", "shared": True} in names
    assert not {n["name"] for n in names} & {"RUNNER_LLM_KEY", "MY_KEY", "BOB_KEY"}
    for ref in ({"name": "OPENROUTER_API_KEY", "shared": True}, {"name": "BOB_KEY", "shared": False}):
        ok, _, _ = _save(reg, by="alice", admin=False, key_ref=ref)
        assert not ok
    ok, _, rec = _save(reg, by="alice", admin=False, key_ref={"name": "MY_KEY", "shared": False})
    assert ok and reg.resolve_key(rec) == "sk-alice"
    ok, _, rec = _save(reg, key_ref={"name": "OPENROUTER_API_KEY", "shared": True})
    assert ok and reg.resolve_key(rec) == "sk-shared"


@pytest.mark.parametrize("name", ["RUNNER_CHATGPT_SESSION", "STORAGE_ENCRYPTION_KEY", "RUNNER_LLM_KEY"])
def test_the_servers_own_secrets_are_never_picked(reg, name):
    _vault().vault_set(name, "x")
    ok, msg, _ = _save(reg, key_ref={"name": name, "shared": True})
    assert not ok and name in msg


def test_a_typed_key_is_kept_under_the_runners_own_name(reg):
    v = _vault()
    v.vault_set("MY_KEY", "sk-alice", owner="alice")
    ok, _, rec = _save(reg, by="alice", admin=False, key="sk-alice-typed")
    assert ok and rec["key"] == {"name": f"RUNNER_KEY_{rec['id']}", "shared": False}
    path = f"users/{sys.modules['tenancy']._safe('alice')}/RUNNER_KEY_{rec['id']}"
    assert v._read_all()[path] == "sk-alice-typed"
    assert v.get_secret(f"RUNNER_KEY_{rec['id']}") is None          # services never get it
    assert "sk-alice-typed" not in json.dumps(reg.public(rec)) + reg.RUNNERS_FILE.read_text()
    ok, _, picked = _save(reg, by="alice", admin=False, key_ref={"name": "MY_KEY", "shared": False})
    assert reg.delete(rec["id"], by="alice", admin=False)[0]
    assert reg.delete(picked["id"], by="alice", admin=False)[0]
    assert path not in v._read_all()                                 # its own key goes with it
    assert v._read_all()[f"users/{sys.modules['tenancy']._safe('alice')}/MY_KEY"] == "sk-alice"


def test_leaving_the_key_empty_keeps_it(reg):
    ok, _, rec = _save(reg, key=KEY)
    ok, msg, again = _save(reg, id=rec["id"], name="Renamed")
    assert ok and again["key"] == rec["key"] and again["name"] == "Renamed"
    assert reg.resolve_key(again) == KEY
    ok, msg, _ = _save(reg)
    assert not ok and "key" in msg                                   # a new one needs a key


# ── the first admin runner is the standard ───────────────────────────────────
def test_the_first_runner_an_admin_makes_is_the_standard(reg):
    _, _, cloud = _save(reg, kind="claude-cloud", model="", trigger="trig_0001", key="sk-ant-oat01-t")
    _, _, mine = _save(reg, by="alice", admin=False, key=KEY)
    assert reg.load()["standard"] == ""                              # neither a cloud nor a user's
    _, _, first = _save(reg, key=KEY)
    _, _, second = _save(reg, key=KEY)
    assert reg.load()["standard"] == first["id"]
    assert reg.set_standard(second["id"], by="boss", admin=True)[0]
    assert not reg.set_standard(cloud["id"], by="boss", admin=True)[0]
    assert not reg.set_standard(mine["id"], by="boss", admin=True)[0]
    assert reg.load()["standard"] == second["id"]


# ── who may run whose job, and where an unassigned job goes ──────────────────
def test_may_run(reg):
    cloud = {"id": "r_c", "kind": "claude-cloud", "owner": "alice"}
    own = {"id": "r_o", "kind": "openai", "owner": "alice"}
    std = {"id": "r_s", "kind": "openrouter", "owner": "boss"}
    m = reg.may_run
    assert m(cloud, "r_s", job_owner="alice", job_by="alice")
    assert not m(cloud, "r_s", job_owner="alice", job_by="boss")     # an admin's prompt in alice's Claude
    assert not m(cloud, "r_s", job_owner="bob", job_by="alice")
    assert m(std, "r_s", job_owner="bob", job_by="bob")              # the standard runs anyone's
    assert m(own, "r_s", job_owner="alice", job_by="boss")
    assert m(own, "r_s", job_owner="", job_by="alice")
    assert not m(own, "r_s", job_owner="bob", job_by="bob")
    assert not m(own, "r_s", job_owner="", job_by="boss")


def test_effective_runner_of_a_job(reg):
    data = {"standard": "r_s", "runners": [{"id": "r_s", "enabled": True},
                                           {"id": "r_o", "enabled": False}]}
    e = reg.effective
    assert e("", data) == "r_s" and e("r_gone", data) == "r_s"
    assert e("r_o", data) == "r_o"                                  # assigned but off: paused
    assert e("external", data) == "external"
    off = dict(data, runners=[{"id": "r_s", "enabled": False}])
    assert e("", off) == "external" and e("", {"standard": "", "runners": []}) == "external"


# ── rights ───────────────────────────────────────────────────────────────────
def test_only_the_owner_edits_a_runner(reg):
    _save(reg, key=KEY)                                              # boss's standard
    ok, _, rec = _save(reg, by="alice", admin=False, key=KEY)
    assert not _save(reg, by="bob", admin=False, id=rec["id"], name="mine now")[0]
    assert not _save(reg, by="boss", admin=True, id=rec["id"], model="other/model")[0]
    assert reg.set_enabled(rec["id"], False, by="boss", admin=True)[0]
    assert not reg.set_enabled(rec["id"], True, by="bob", admin=False)[0]
    assert reg.get(rec["id"])["enabled"] is False
    assert not reg.delete(rec["id"], by="bob", admin=False)[0]
    assert reg.delete(rec["id"], by="boss", admin=True)[0]
    assert not reg.delete(reg.load()["standard"], by="boss", admin=True)[0]


def test_runner_list_shows_each_person_their_runners_and_no_key_names(reg):
    _, _, std = _save(reg, key=KEY)
    _, _, alices = _save(reg, by="alice", admin=False, name="Alice OpenRouter", key=KEY)
    _, _, bobs = _save(reg, by="bob", admin=False, name="Bob OpenRouter", key=KEY)
    out = _call("runner_list", "alice", "user")
    assert alices["id"] in out and std["id"] in out and bobs["id"] not in out
    assert "RUNNER_KEY_" not in out and KEY not in out
    everything = _call("runner_list", "boss", "admin")
    assert bobs["id"] in everything and "standard" in everything


def test_a_cloud_runner_needs_a_sign_in_of_ones_own(reg):
    ok, msg, _ = reg.save({"kind": "claude-cloud", "trigger": "trig_0001"}, by="local-operator",
                          admin=True, key="sk-ant-oat01-t", cloud_ok=False)
    assert not ok and "OIDC" in msg


def test_the_guide_tells_models_how_to_pick_a_runner(reg):
    guide = sys.modules["guide"] if "guide" in sys.modules else __import__("guide")
    text = guide.GUIDE
    assert text.count("runner_list") >= 2 and "cron_add(name, schedule, prompt[, owner, runner])" in text
    assert "setup-token" not in text and len(guide.INSTRUCTIONS) <= 2000
