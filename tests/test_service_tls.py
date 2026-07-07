"""Per-service TLS options for call_service (v1.9.5).

Proves the #10 pattern (secure by default, operator opt-out) now also applies to
generic services: service_add persists tls_insecure/ca_bundle, cfgstore keeps them
across merge-updates, and call_service resolves httpx's verify= via
netguard.tls_verify — verify ON by default, ca_bundle wins over tls_insecure.
No MCP server and no network needed (httpx.request is captured).
"""
import contextlib
import importlib
import json
import tempfile
from pathlib import Path


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, fn):
        self.tools[fn.__name__] = fn
        return fn


def test_service_tls(monkeypatch):
    _D = Path(tempfile.mkdtemp())
    monkeypatch.setenv("SERVICES_DIR", str(_D / "services"))
    # services computes SERVICES_DIR at import time → reload after setting env.
    import netguard
    import services
    import tenancy
    importlib.reload(services)

    mcp = FakeMCP()
    services.register(mcp)
    service_add = mcp.tools["service_add"]
    service_list = mcp.tools["service_list"]
    call_service = mcp.tools["call_service"]

    # ── 1) tls_verify resolution precedence (netguard) ─────────────────────
    assert netguard.tls_verify({}) is True, "tls_verify: default is verify ON"
    assert netguard.tls_verify({"tls_insecure": True}) is False, "tls_verify: tls_insecure=True turns verification OFF"
    assert netguard.tls_verify({"tls_insecure": True, "ca_bundle": "/certs/ca.pem"}) == "/certs/ca.pem", "tls_verify: ca_bundle wins over tls_insecure"

    # ── 2) service_add persists the TLS fields ─────────────────────────────
    service_add("crafty-test", "https://192.0.2.10:1986", category="Gaming",
                description="self-signed panel", tls_insecure=True)
    cfg = json.loads((_D / "services" / "crafty-test.json").read_text())
    assert cfg.get("tls_insecure") is True, "service_add: tls_insecure persisted"
    assert cfg.get("ca_bundle") == "", "service_add: ca_bundle default empty"

    # ── 3) merge-update keeps the opt-out (write_merged semantics) ─────────
    service_add("crafty-test", "https://192.0.2.10:1986", description="renamed")
    cfg = json.loads((_D / "services" / "crafty-test.json").read_text())
    assert cfg.get("tls_insecure") is True, "merge-update without tls_insecure keeps the existing True"
    assert cfg.get("description") == "renamed", "merge-update applied the new description"

    # ── 4) service_list surfaces the insecure marker ───────────────────────
    monkeypatch.setattr(tenancy, "caller_service_allowed", lambda *a, **k: True)
    listing = service_list()
    assert "[TLS-INSECURE]" in listing, "service_list flags [TLS-INSECURE]"

    # ── 5) call_service passes verify= from the service config ─────────────
    captured = {}

    class FakeResp:
        status_code = 200
        text = "ok"

    def fake_request(method, url, **kw):
        captured.update(kw, method=method, url=url)
        return FakeResp()

    monkeypatch.setattr(services.httpx, "request", fake_request)
    monkeypatch.setattr(services.netguard, "check_url", lambda url: (True, "ok"))
    monkeypatch.setattr(services.netguard, "guard", lambda host: contextlib.nullcontext())

    out = call_service("crafty-test", path="/api/v2/servers")
    assert out.startswith("HTTP 200"), "call_service succeeds with stubbed transport"
    assert captured.get("verify") is False, "call_service: verify=False for tls_insecure service"

    service_add("pinned-test", "https://192.0.2.11", category="Gaming",
                ca_bundle="/data/certs/device.pem")
    call_service("pinned-test", path="/")
    assert captured.get("verify") == "/data/certs/device.pem", "call_service: verify=<ca_bundle path> when pinned"

    service_add("plain-test", "https://api.example.com", category="Web")
    call_service("plain-test", path="/")
    assert captured.get("verify") is True, "call_service: verify=True by default (secure by default)"
