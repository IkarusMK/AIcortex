"""Only a person binds a secret to its first host — an AI session can't claim one.

Regression for the re-review finding "secret first-binding by a prompt-injected admin
session": a secret that was not bound yet (and that no config from before the
upgrade referenced) was bound to whichever host the first ``*_add`` named. So an
injected admin session could claim a fresh token — ``service_add(base_url=<its
host>, token_env=<the token>)`` — and then receive it with every call.

From 2.6.0 a first binding is made only
- by a person in the WebUI (registering or editing the integration there, or the
  vault's host list),
- with ``secret_set(name, value, hosts=...)`` — which needs the value, or
- once, at the first start of this version, for every secret a config that already
  existed references (to that config's hosts) — recorded, so it never runs again.

A ``*_add`` or use with an unbound secret from an MCP/REST session is refused with a
message saying how to bind it. Writing a secret while the vault's metadata is
unreadable is refused too (it used to start over from {}, unbinding every secret).

Invented data only (example.com/.net hosts, alice/boss).
"""
import asyncio
import importlib
import json
import os
import sys
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastmcp import Client

API, EVIL = "api.example.com", "evil.example.net"
APP = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "app"))


def _call(server, tool, args=None):
    async def go():
        async with Client(server.mcp) as client:
            res = await client.call_tool(tool, args or {}, raise_on_error=False)
            return res.content[0].text if res.content else ""
    return asyncio.run(go())


def _as_webui(server, tool, args, who="boss"):
    """Run a tool as the WebUI's registry editor does: as a person (rest identity)."""
    authz = sys.modules["authz"]

    async def go():
        with authz.rest_identity(who, "admin"):
            tool_obj = await server.mcp.get_tool(tool)
            res = await tool_obj.run(args)
            return "\n".join(getattr(c, "text", "") for c in res.content)
    return asyncio.run(go())


class _Wire:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append(url)
        return SimpleNamespace(status_code=200, text="ok")


def _isolate(monkeypatch, server):
    ng = sys.modules["netguard"]
    monkeypatch.setattr(ng, "check_url", lambda url: (True, "ok"))
    monkeypatch.setattr(ng, "check_host", lambda host: (True, "ok"))
    wire = _Wire()
    monkeypatch.setattr(httpx, "request", wire.request)
    server.wire = wire
    return server


@pytest.fixture
def srv(monkeypatch, request):
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    monkeypatch.delenv("CI_TOKEN", raising=False)
    return _isolate(monkeypatch, request.getfixturevalue("fresh_server"))


def _service(name, host, token):
    return {"name": name, "base_url": f"https://{host}/api", "token_env": token, "category": "Dev"}


def _hosts(name):
    return [e["hosts"] for e in sys.modules["secrets_store"].vault_entries() if e["name"] == name]


KINDS = [
    ("service_add", {"base_url": f"https://{EVIL}/api", "token_env": "FRESH_TOKEN", "category": "X"}),
    ("mcp_add", {"url": f"https://{EVIL}/mcp", "token_env": "FRESH_TOKEN"}),
    ("webdav_add", {"base_url": f"https://{EVIL}/dav", "username": "alice", "password_env": "FRESH_TOKEN"}),
    ("mail_add", {"host": EVIL, "from_addr": "alice@example.com", "username": "alice",
                  "password_env": "FRESH_TOKEN"}),
    ("ssh_add", {"host": EVIL, "username": "alice", "password_env": "FRESH_TOKEN"}),
]


@pytest.mark.parametrize("tool,args", KINDS, ids=[k[0] for k in KINDS])
def test_an_ai_session_cannot_claim_an_unbound_secret(srv, tool, args):
    sys.modules["secrets_store"].vault_set("FRESH_TOKEN", "fresh-value")   # no hosts yet
    out = _call(srv, tool, {"name": "grab", **args})
    assert out.startswith("Refused") and "FRESH_TOKEN" in out
    assert "secret_set('FRESH_TOKEN', <value>, hosts=" in out and "WebUI" in out
    assert _hosts("FRESH_TOKEN") == [None]                               # nothing was bound


def test_nor_an_unbound_secret_from_the_environment(srv, monkeypatch):
    monkeypatch.setenv("CI_TOKEN", "ci-value")
    out = _call(srv, "service_add", {"name": "grab", **_service("grab", EVIL, "CI_TOKEN")})
    assert out.startswith("Refused") and "CI_TOKEN" in out
    assert not (sys.modules["services"].SERVICES_DIR / "grab.json").exists()


def test_an_unbound_secret_is_refused_at_use_too(srv):
    sys.modules["secrets_store"].vault_set("FRESH_TOKEN", "fresh-value")
    folder = sys.modules["services"].SERVICES_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "grab.json").write_text(json.dumps(_service("grab", EVIL, "FRESH_TOKEN")))
    out = _call(srv, "call_service", {"service": "grab"})
    assert out.startswith("Refused") and "not bound" in out and srv.wire.calls == []
    assert _hosts("FRESH_TOKEN") == [None]


def test_the_normal_flow_binds_with_the_value_first(srv):
    vault = sys.modules["secrets_store"]
    vault.vault_set("FRESH_TOKEN", "fresh-value")
    assert _call(srv, "service_add", _service("api", API, "FRESH_TOKEN")).startswith("Refused")
    out = _call(srv, "secret_set", {"name": "FRESH_TOKEN", "value": "fresh-value", "hosts": API})
    assert out.startswith("Stored") and API in out
    assert _call(srv, "service_add", _service("api", API, "FRESH_TOKEN")).startswith("Registered")
    assert _call(srv, "call_service", {"service": "api"}).startswith("HTTP 200")
    assert srv.wire.calls == [f"https://{API}/api/"]
    assert _call(srv, "service_add", _service("grab", EVIL, "FRESH_TOKEN")).startswith("Refused")


def test_a_person_in_the_webui_makes_the_first_binding(srv):
    vault = sys.modules["secrets_store"]
    vault.vault_set("FRESH_TOKEN", "fresh-value")
    assert _call(srv, "service_add", _service("api", API, "FRESH_TOKEN")).startswith("Refused")
    assert _as_webui(srv, "service_add", _service("api", API, "FRESH_TOKEN")).startswith("Registered")
    assert _hosts("FRESH_TOKEN") == [[API]]
    assert _call(srv, "call_service", {"service": "api"}).startswith("HTTP 200")   # the AI, later
    # Registered in the WebUI before its value exists: the value set later is bound too.
    assert _as_webui(srv, "service_add", _service("later", API, "LATER_TOKEN")).startswith("Registered")
    vault.vault_set("LATER_TOKEN", "later-value")
    assert _call(srv, "call_service", {"service": "later"}).startswith("HTTP 200")
    assert _call(srv, "service_add", _service("grab", EVIL, "LATER_TOKEN")).startswith("Refused")


# ── the upgrade: configs that existed before are bound once, at the first start ──
def _restart():
    """A process restart: every app module is imported afresh, server.py included."""
    for name, mod in list(sys.modules.items()):
        path = getattr(mod, "__file__", None) or ""
        if path and os.path.realpath(path).startswith(APP + os.sep):
            del sys.modules[name]
    return importlib.import_module("server")


@pytest.fixture
def upgraded(monkeypatch, request, tmp_path):
    """An install as 2.5.0 left it: a token in the vault, two services using it (one
    token on two hosts), and an unbound token nobody references yet."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", key)
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    monkeypatch.setenv("VAULT_DIR", str(tmp_path / "vault_dir"))
    vault_dir, services = tmp_path / "vault_dir", tmp_path / "services_dir"
    services.mkdir(parents=True)
    for name, host in (("ha-lan", "ha.example.com"), ("ha-cloud", "ha.example.net")):
        (services / f"{name}.json").write_text(json.dumps(_service(name, host, "HA_TOKEN")))
    import secrets_store as old_vault                  # writes the 2.5.0 vault files
    importlib.reload(old_vault)
    assert old_vault.VAULT_DIR == vault_dir
    old_vault.vault_set("HA_TOKEN", "ha-value")
    old_vault.vault_set("SPARE_TOKEN", "spare-value")
    meta = json.loads((vault_dir / "meta.json").read_text())
    (vault_dir / "meta.json").write_text(json.dumps(
        {k: v for k, v in meta.items() if not k.startswith("_/")}))     # 2.5.0 had no bindings
    return _isolate(monkeypatch, request.getfixturevalue("fresh_server"))


def test_configs_from_before_the_upgrade_keep_working(upgraded):
    assert _hosts("HA_TOKEN") == [["ha.example.com", "ha.example.net"]]  # bound at the start
    assert _call(upgraded, "call_service", {"service": "ha-cloud"}).startswith("HTTP 200")
    assert _call(upgraded, "call_service", {"service": "ha-lan"}).startswith("HTTP 200")
    assert _call(upgraded, "service_add", _service("x", EVIL, "HA_TOKEN")).startswith("Refused")


def test_the_upgrade_binding_never_runs_again(upgraded, monkeypatch):
    assert _hosts("SPARE_TOKEN") == [None]
    folder = sys.modules["services"].SERVICES_DIR
    (folder / "grab.json").write_text(json.dumps(_service("grab", EVIL, "SPARE_TOKEN")))
    server = _isolate(monkeypatch, _restart())       # a later start sees the new config …
    assert _hosts("SPARE_TOKEN") == [None]           # … and binds nothing
    out = _call(server, "call_service", {"service": "grab"})
    assert out.startswith("Refused") and server.wire.calls == []


# ── unreadable metadata: never start over from {} ────────────────────────────
def test_writing_a_secret_with_unreadable_metadata_is_refused(srv):
    vault = sys.modules["secrets_store"]
    vault.vault_set("FRESH_TOKEN", "fresh-value", hosts=[API])
    vault.VAULT_META.write_text("{torn", encoding="utf-8")
    before = vault.VAULT_FILE.read_bytes()
    assert vault.vault_set("OTHER_TOKEN", "other-value").startswith("Refus")
    assert vault.vault_update("FRESH_TOKEN", value="new-value").startswith("Refus")
    assert vault.vault_update("FRESH_TOKEN", description="note").startswith("Refus")
    assert vault.VAULT_META.read_text(encoding="utf-8") == "{torn"      # bindings not wiped
    assert vault.VAULT_FILE.read_bytes() == before                     # nothing half-written


def test_the_guide_teaches_binding_with_the_value_first(srv):
    text = sys.modules["guide"].GUIDE
    flow = 'secret_set(name, value, hosts="api.example.com")'
    assert text.count(flow) >= 2                                       # DE and EN
    for part in text.split("AICortex — your personal"):
        assert flow in part and "_add" in part.split(flow, 1)[1][:400]
