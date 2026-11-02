"""Services with a user name and password (HTTP Basic auth).

Before: a Basic-auth API (AdGuard Home, routers, many NAS apps) needed the vault
secret to hold base64("user:password") built by hand. Now service_add takes the
`username`, the vault secret holds just the password, and the connector builds
the header. A pre-built secret without a username still works as before.
"""
import base64

from test_cov_services import _raw, svc, wire  # noqa: F401 - svc and wire are fixtures


def _basic(value: str) -> str:
    return "Basic " + base64.b64encode(value.encode()).decode()


def test_username_and_password_secret_make_the_basic_header(svc, wire, monkeypatch):
    monkeypatch.setenv("SVC_TOKEN", "pa:ss wörd")
    out = svc.t["service_add"]("dns", "https://dns.example.com", token_env="SVC_TOKEN",
                               auth_scheme="Basic", username="alice", category="Network")
    assert out.startswith("Registered service 'dns'")
    svc.t["call_service"]("dns", path="/control/status")
    assert wire["headers"] == {"Authorization": _basic("alice:pa:ss wörd")}


def test_a_prebuilt_basic_secret_without_username_still_works(svc, wire, monkeypatch):
    prebuilt = base64.b64encode(b"alice:secret").decode()
    monkeypatch.setenv("SVC_TOKEN", prebuilt)
    _raw(svc, "old", token_env="SVC_TOKEN", auth_scheme="Basic")
    svc.t["call_service"]("old")
    assert wire["headers"] == {"Authorization": f"Basic {prebuilt}"}


def test_the_username_shows_in_the_list_but_never_the_password(svc, monkeypatch):
    monkeypatch.setenv("SVC_TOKEN", "hunter2")
    svc.t["service_add"]("dns", "https://dns.example.com", token_env="SVC_TOKEN",
                         auth_scheme="Basic", username="alice", category="Network")
    listing = svc.t["service_list"]()
    assert "dns" in listing and "hunter2" not in listing
    assert svc.mod._load("dns")["username"] == "alice"


def test_an_update_without_username_keeps_it(svc, wire, monkeypatch):
    monkeypatch.setenv("SVC_TOKEN", "pw")
    svc.t["service_add"]("dns", "https://dns.example.com", token_env="SVC_TOKEN",
                         auth_scheme="Basic", username="alice", category="Network")
    sys_cfgstore = __import__("cfgstore")
    with sys_cfgstore.given({"name", "base_url", "description"}):
        svc.t["service_add"]("dns", "https://dns.example.com", description="DNS filter")
    svc.t["call_service"]("dns")
    assert wire["headers"] == {"Authorization": _basic("alice:pw")}
