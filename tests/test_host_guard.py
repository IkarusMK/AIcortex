"""DNS rebinding is blocked; native clients connect under any host name.

DNS rebinding: a website points its own domain at the AICortex address, and the
visitor's browser then sends requests "from" that site. Found in the 2026-10
review: the Host/Origin guard was never installed (the old test exercised the
middleware class on its own), so a rebinding page got the local no-login admin of
the WebUI, could store secrets and mint agent keys, and in open mode reached every
tool; MCP_ALLOWED_HOSTS did nothing although the docs promised 421.

Rules now: a browser request (it carries Origin) must come from an IP address,
localhost, a name without a dot, a private-use name (.local, .lan, .home.arpa,
.internal), the BASE_URL host or a listed origin — else 403. The WebUI is
browser-only, so its Host must be one of those too — else 421. MCP_ALLOWED_HOSTS
makes the Host check strict for every request. Native clients send no Origin and
are unaffected (LAN IP, Docker name, VPN name).
"""
import httpx
import pytest

ACCEPT = "application/json, text/event-stream"
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}


def _post_mcp(base, host, origin=None):
    headers = {"Host": host, "Accept": ACCEPT}
    if origin:
        headers["Origin"] = origin
    return httpx.post(f"{base}/mcp", json=PING, headers=headers).status_code


def _ui(base, host, origin=None):
    headers = {"Host": host}
    if origin:
        headers["Origin"] = origin
    return httpx.get(f"{base}/ui/api/me", headers=headers)


@pytest.fixture
def srv(monkeypatch, request):
    monkeypatch.setenv("BASE_URL", "https://agent.example.net")
    return request.getfixturevalue("http_server")


def test_native_clients_pass_under_any_host_name(srv):
    for host in ("192.168.1.20:8787", "aicortex:8787", "nas.tail1234.ts.net",
                 "agent.example.net", "nas.example.org:8787"):
        assert _post_mcp(srv, host) not in (403, 421), host     # 401: no token, but let in


def test_browser_request_from_a_foreign_site_is_refused(srv):
    assert _post_mcp(srv, "evil.example.com:8787", "http://evil.example.com:8787") == 403
    assert _ui(srv, "evil.example.com:8787", "http://evil.example.com:8787").status_code == 403
    assert _post_mcp(srv, "localhost", "null") == 403


def test_browser_request_from_lan_or_base_url_passes(srv):
    assert _ui(srv, "192.168.1.20:8787", "http://192.168.1.20:8787").status_code == 200
    assert _post_mcp(srv, "agent.example.net", "https://agent.example.net") not in (403, 421)
    assert _ui(srv, "nas.local:8787", "http://nas.local:8787").status_code == 200


def test_webui_under_a_foreign_host_is_refused_even_without_origin(srv):
    assert _ui(srv, "evil.example.com:8787").status_code == 421
    assert _ui(srv, "nas:8787").status_code == 200


def test_local_no_login_admin_only_under_a_loopback_name(srv):
    port = srv.rsplit(":", 1)[1]
    assert _ui(srv, f"localhost:{port}").json().get("role") == "admin"
    assert _ui(srv, f"127.0.0.1:{port}").json().get("role") == "admin"
    assert not _ui(srv, "192.168.1.20:8787").json().get("authenticated")


def test_allowed_hosts_make_the_host_check_strict(monkeypatch, request):
    monkeypatch.setenv("BASE_URL", "https://agent.example.net")
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "nas.lan:8787")
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://chat.example.net")
    base = request.getfixturevalue("http_server")
    assert _post_mcp(base, "nas.lan:8787") not in (403, 421)
    assert _post_mcp(base, "agent.example.net") not in (403, 421)
    assert _post_mcp(base, "localhost") not in (403, 421)
    assert _post_mcp(base, "192.168.1.20:8787") == 421
    assert _post_mcp(base, "nas.lan:8787", "https://chat.example.net") not in (403, 421)


# ── Re-review (2026-10): header edge cases ──────────────────────────────────

def _policy(monkeypatch, **env):
    import importlib

    import hostguard
    for k in ("BASE_URL", "MCP_ALLOWED_HOSTS", "MCP_ALLOWED_ORIGINS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return importlib.reload(hostguard)


def test_idn_base_url_matches_the_punycode_browsers_send(monkeypatch):
    hg = _policy(monkeypatch, BASE_URL="https://bücher.example")
    p = hg.Policy()
    assert p.refuse("/ui", "xn--bcher-kva.example", None) == 0
    assert p.refuse("/mcp", "xn--bcher-kva.example", "https://xn--bcher-kva.example") == 0


def test_odd_headers_are_refused_not_trusted(monkeypatch):
    hg = _policy(monkeypatch)
    p = hg.Policy()
    assert p.refuse("/mcp", "localhost", "http://[") == 403          # malformed: no crash
    assert p.refuse("/mcp", "localhost", "NULL") == 403
    assert p.refuse("/mcp", "localhost", "http://127.0.0.1@evil.example.com") == 403
    assert p.refuse("/ui", "127.0.0.1:8787@evil.example.com", None) == 421
    assert not hg.is_loopback_name("127.0.0.1:8787@evil.example.com")
    assert not hg.is_loopback_name("[::1")
    assert hg.is_loopback_name("[::1]:8787") and hg.is_loopback_name("LOCALHOST.")


def test_wildcard_entries_in_allowed_hosts(monkeypatch):
    hg = _policy(monkeypatch, MCP_ALLOWED_HOSTS="*.example.net")
    p = hg.Policy()
    assert p.refuse("/mcp", "nas.example.net:8787", None) == 0
    assert p.refuse("/mcp", "example.net.evil.com", None) == 421


def test_duplicate_origin_headers_are_refused(srv):
    r = httpx.post(f"{srv}/mcp", json=PING, headers=[
        ("Host", "localhost"), ("Accept", ACCEPT),
        ("Origin", "http://evil.example.com"), ("Origin", "http://localhost")])
    assert r.status_code == 403
