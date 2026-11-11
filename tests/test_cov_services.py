"""Service registry and caller (app/services.py): registration rules, listing, call refusals.

Proves the refusal and error paths of the allow-listed service caller: a new service
needs a category; the catalog groups by category, tolerates unreadable configs and hides
what a person's area does not grant; call_service refuses unknown services, areas not
granted, ingest-only services, odd methods, absolute URLs and internal addresses before
any request is made; the token is injected server-side (Bearer, custom header, never a
server setting); a failing transport becomes a message, long bodies are paged. No real
network: httpx.request is replaced.
"""
import contextlib
import json
import sys
from types import SimpleNamespace

import pytest


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, fn):
        self.tools[fn.__name__] = fn
        return fn


class FakeResponse:
    def __init__(self, status_code=200, text="ok"):
        self.status_code, self.text = status_code, text


@pytest.fixture
def svc(monkeypatch, fresh_server):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    monkeypatch.delenv("INTERNAL_ALLOW_CIDRS", raising=False)
    monkeypatch.delenv("SVC_TOKEN", raising=False)
    # The test token may go to the test services' hosts — bound by a person, as
    # only a person binds a secret (2.6.0); the value comes from the environment.
    sys.modules["secrets_store"].vault_set_hosts(
        "SVC_TOKEN", "", ["ha.example.com", "dns.example.com", "old.example.com"], by="test")
    mod = sys.modules["services"]
    fake = FakeMCP()
    mod.register(fake)
    return SimpleNamespace(mod=mod, t=fake.tools, authz=sys.modules["authz"],
                           tenancy=sys.modules["tenancy"])


@pytest.fixture
def wire(svc, monkeypatch):
    """Let requests through the network guard and capture what would be sent."""
    sent = {"response": FakeResponse()}

    def fake_request(method, url, **kw):
        sent.update(kw, method=method, url=url)
        if isinstance(sent["response"], Exception):
            raise sent["response"]
        return sent["response"]
    monkeypatch.setattr(svc.mod.httpx, "request", fake_request)
    monkeypatch.setattr(svc.mod.netguard, "check_url", lambda url: (True, "ok"))
    monkeypatch.setattr(svc.mod.netguard, "guard", lambda host: contextlib.nullcontext())
    return sent


def _raw(svc, name, **cfg):
    svc.mod.SERVICES_DIR.mkdir(parents=True, exist_ok=True)
    (svc.mod.SERVICES_DIR / f"{name}.json").write_text(
        json.dumps({"name": name, "base_url": f"https://{name}.example.com"} | cfg), encoding="utf-8")


def _grant(svc, **area):
    svc.tenancy.AUTH_DIR.mkdir(parents=True, exist_ok=True)
    svc.tenancy.POLICY_FILE.write_text(json.dumps({"users": {"alice": area}}), encoding="utf-8")


# ── registering ──────────────────────────────────────────────────────────────
def test_a_new_service_must_have_a_category_but_an_update_may_omit_it(svc):
    refused = svc.t["service_add"]("Home Assistant", "https://ha.example.com/")
    assert refused.startswith("Refused: a new service MUST have a category")
    assert not svc.mod.SERVICES_DIR.exists() or not list(svc.mod.SERVICES_DIR.glob("*.json"))
    assert svc.t["service_add"]("Home Assistant", "https://ha.example.com/", category=" Smart Home ") == (
        "Registered service 'home-assistant'.")
    assert svc.t["service_add"]("Home Assistant", "https://ha2.example.com", description="moved").startswith("Registered")
    cfg = json.loads((svc.mod.SERVICES_DIR / "home-assistant.json").read_text())
    assert (cfg["category"], cfg["base_url"], cfg["description"]) == ("Smart Home", "https://ha2.example.com", "moved")


def test_registering_hints_at_a_missing_token_secret_only_when_it_is_missing(svc, monkeypatch):
    text = svc.t["service_add"]("ha", "https://ha.example.com", token_env="SVC_TOKEN", category="Dev")
    assert "secret_set('SVC_TOKEN'" in text
    monkeypatch.setenv("SVC_TOKEN", "test-token")
    assert svc.t["service_add"]("ha", "https://ha.example.com", token_env="SVC_TOKEN") == "Registered service 'ha'."


def test_a_storage_failure_is_reported_not_raised(svc, monkeypatch):
    def broken(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(svc.mod.cfgstore, "write_merged", broken)
    assert svc.t["service_add"]("ha", "https://ha.example.com", category="Dev") == (
        "Could not register service: disk full")


def test_delete_removes_a_service_or_says_it_is_unknown(svc):
    svc.t["service_add"]("ha", "https://ha.example.com", category="Dev")
    assert svc.t["service_delete"]("HA") == "Deleted service 'ha'."
    assert svc.t["service_delete"]("ha") == "No service 'ha'."
    assert svc.mod._slug("") == "service" and svc.mod._load("ha") is None


def test_an_unparsable_config_loads_as_none(svc):
    svc.mod.SERVICES_DIR.mkdir(parents=True)
    (svc.mod.SERVICES_DIR / "bad.json").write_text("{broken")
    assert svc.mod._load("bad") is None


# ── listing ──────────────────────────────────────────────────────────────────
def test_list_reports_an_empty_registry(svc):
    assert svc.t["service_list"]() == "No services configured yet."
    svc.mod.SERVICES_DIR.mkdir(parents=True)
    assert svc.t["service_list"]() == "No services configured yet. Use service_add."


def test_list_without_categories_is_flat_and_marks_unreadable_configs(svc):
    _raw(svc, "alpha", description="first")
    svc.mod.SERVICES_DIR.joinpath("zeta.json").write_text("{broken")
    assert svc.t["service_list"]().splitlines() == [
        "- alpha — https://alpha.example.com — first", "- zeta — (unreadable config)"]


def test_list_groups_by_category_with_lock_markers_and_uncategorized_last(svc):
    _raw(svc, "ha", category="Smart Home", write_only=True)
    _raw(svc, "docs", category="documents", tls_insecure=True)
    _raw(svc, "misc")
    lines = svc.t["service_list"]().splitlines()
    assert [ln for ln in lines if ln.startswith("[")] == ["[documents]", "[Smart Home]", "[Uncategorized]"]
    assert any("[INGEST-ONLY / write_only]" in ln for ln in lines)
    assert any("[TLS-INSECURE]" in ln for ln in lines)


def test_list_shows_a_person_only_the_services_their_area_grants(svc):
    _raw(svc, "ha", category="Smart Home")
    _raw(svc, "docs", category="Documents")
    with svc.authz.rest_identity("alice", "user"):
        assert svc.t["service_list"]().startswith("No services available to you")
    _grant(svc, services=["ha"])
    with svc.authz.rest_identity("alice", "user"):
        assert svc.t["service_list"]().splitlines() == ["[Smart Home]", "  - ha — https://ha.example.com — "]
    _grant(svc, services=["documents"])                                  # a category grants its services
    with svc.authz.rest_identity("alice", "user"):
        assert "docs" in svc.t["service_list"]() and "ha" not in svc.t["service_list"]()


# ── calling: refusals before any request ─────────────────────────────────────
def test_calls_to_unknown_or_ungranted_services_never_reach_the_network(svc, wire):
    _raw(svc, "ha", category="Smart Home")
    assert svc.t["call_service"]("nope") == "Unknown service 'nope'. Use service_list / service_add."
    with svc.authz.rest_identity("alice", "user"):
        assert svc.t["call_service"]("ha").startswith("Denied: service 'ha' is not in your allowed set")
    assert "url" not in wire


def test_an_ingest_only_service_cannot_be_read(svc, wire):
    _raw(svc, "scanner", write_only=True)
    assert svc.t["call_service"]("scanner").startswith("Refused: service 'scanner' is INGEST-ONLY")
    assert "url" not in wire


@pytest.mark.parametrize("path", ["http://evil.example.net/x", "//evil.example.net/x", "ftp://x/y"])
def test_absolute_urls_are_not_valid_paths(svc, wire, path):
    _raw(svc, "ha")
    assert svc.t["call_service"]("ha", path=path).startswith("Invalid path")
    assert "url" not in wire


def test_only_known_http_methods_are_allowed(svc, wire):
    _raw(svc, "ha")
    assert svc.t["call_service"]("ha", method="TRACE") == "Method 'TRACE' not allowed."
    assert svc.t["call_service"]("ha", method="get").startswith("HTTP 200")
    assert wire["method"] == "GET"


def test_internal_addresses_are_blocked_by_the_network_policy(svc, monkeypatch):
    _raw(svc, "lan", base_url="https://192.0.2.10")
    monkeypatch.setattr(svc.mod.httpx, "request", lambda *a, **k: pytest.fail("request sent"))
    text = svc.t["call_service"]("lan", path="/api")
    assert text.startswith("Blocked by network policy") and "192.0.2.10" in text


# ── calling: token handling and transport ────────────────────────────────────
def test_the_token_is_injected_as_bearer_or_as_a_raw_custom_header(svc, wire, monkeypatch):
    monkeypatch.setenv("SVC_TOKEN", "test-token")
    _raw(svc, "ha", token_env="SVC_TOKEN")
    svc.t["call_service"]("ha", path="/api/states", method="post", json_body={"a": 1}, params={"q": "x"})
    assert wire["headers"] == {"Authorization": "Bearer test-token"}
    assert (wire["method"], wire["url"], wire["json"], wire["params"]) == (
        "POST", "https://ha.example.com/api/states", {"a": 1}, {"q": "x"})
    assert wire["verify"] is True and wire["timeout"] == 30
    # same host: the token is bound to ha.example.com
    _raw(svc, "plain", base_url="https://ha.example.com", token_env="SVC_TOKEN", auth_scheme="",
         auth_header="X-Api-Key")
    svc.t["call_service"]("plain")
    assert wire["headers"] == {"X-Api-Key": "test-token"}


def test_a_missing_secret_stops_the_call_before_any_request(svc, wire):
    _raw(svc, "ha", token_env="SVC_TOKEN")
    text = svc.t["call_service"]("ha")
    assert text.startswith("Service 'ha' needs secret 'SVC_TOKEN'") and "url" not in wire


def test_a_service_cannot_be_pointed_at_the_servers_own_settings(svc, wire, monkeypatch):
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", "server-internal-key")
    _raw(svc, "evil", token_env="STORAGE_ENCRYPTION_KEY")
    assert svc.t["call_service"]("evil").startswith("Service 'evil' needs secret")
    assert "url" not in wire


def test_transport_errors_become_a_message(svc, wire):
    _raw(svc, "ha")
    wire["response"] = ConnectionError("connection refused")
    assert svc.t["call_service"]("ha") == "Request failed: connection refused"


def test_long_responses_come_in_pieces_with_the_next_offset(svc, wire):
    _raw(svc, "ha")
    wire["response"] = FakeResponse(200, "x" * 1200 + "END")
    first = svc.t["call_service"]("ha", max_chars=500)
    assert first.startswith("HTTP 200\n") and "703 more characters — call again with offset=500" in first
    last = svc.t["call_service"]("ha", offset=1000, max_chars=500)
    assert last.endswith("END") and "more characters" not in last


def test_paging_clamps_its_arguments(svc):
    text = "y" * 50_000
    assert len(svc.mod._page(text, offset=-5, max_chars=1)) < 700          # at least 500 chars
    assert "offset=40000" in svc.mod._page(text, max_chars=10**9)         # at most 40 000 chars
    assert svc.mod._page("short", offset=None, max_chars=None) == "short"
