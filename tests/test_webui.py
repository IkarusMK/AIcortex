"""Tests for the admin WebUI (webui.py).

Focus: the security envelope — session signing, admin gating, CSRF, and the
"names only, never values" vault contract. Handlers are exercised directly via
a FakeMCP that captures the custom routes; no HTTP server, no IdP.
"""
import asyncio
import importlib
import json
import time


class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def deco(fn):
            for m in methods:
                self.routes[(path, m)] = fn
            return fn
        return deco


class Req:
    """Minimal request double for the handler signatures webui uses."""

    def __init__(self, headers=None, cookies=None, query=None, path=None,
                 body=b"", base_url="http://localhost:8787/"):
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.cookies = cookies or {}
        self.query_params = query or {}
        self.path_params = path or {}
        self._body = body
        self.base_url = base_url

    async def body(self):
        return self._body


def _webui(monkeypatch, tmp_path, oidc=False):
    """Fresh module stack against tmp dirs. oidc=False → open mode (localhost)."""
    monkeypatch.setenv("VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setenv("AUTH_STORE_DIR", str(tmp_path / "auth"))
    monkeypatch.setenv("ALLOW_PLAINTEXT_VAULT", "1")  # no Fernet key in tests
    monkeypatch.setenv("JWT_SIGNING_KEY", "test-signing-key")
    if oidc:
        monkeypatch.setenv("OIDC_CONFIG_URL", "https://idp.example/.well-known/openid-configuration")
        monkeypatch.setenv("OIDC_CLIENT_ID", "aicortex")
    else:
        monkeypatch.delenv("OIDC_CONFIG_URL", raising=False)
        monkeypatch.delenv("OIDC_CLIENT_ID", raising=False)
    import authz, tenancy, secrets_store, skills, webui
    for mod in (authz, tenancy, secrets_store, skills, webui):
        importlib.reload(mod)
    mcp = FakeMCP()
    webui.register(mcp)
    return webui, mcp


def _call(mcp, route, method="GET", **kw):
    resp = asyncio.run(mcp.routes[(route, method)](Req(**kw)))
    payload = json.loads(resp.body) if resp.body else {}
    return resp.status_code, payload


def _admin_cookie(webui):
    return {"aicortex_ui": webui._sign(
        {"sub": "boss", "role": "admin", "csrf": "tok", "exp": time.time() + 300})}


# ── session signing ──────────────────────────────────────────────────────────

def test_sign_verify_roundtrip_and_tamper(monkeypatch, tmp_path):
    webui, _ = _webui(monkeypatch, tmp_path)
    tok = webui._sign({"sub": "a", "exp": time.time() + 60})
    assert webui._verify(tok)["sub"] == "a"
    body, mac = tok.split(".", 1)
    assert webui._verify(body + "x." + mac) is None      # tampered payload
    assert webui._verify(body + "." + mac[:-2] + "zz") is None  # tampered mac


def test_expired_session_rejected(monkeypatch, tmp_path):
    webui, _ = _webui(monkeypatch, tmp_path)
    tok = webui._sign({"sub": "a", "exp": time.time() - 1})
    assert webui._verify(tok) is None


# ── auth gating ──────────────────────────────────────────────────────────────

def test_open_mode_is_local_admin(monkeypatch, tmp_path):
    """No OIDC configured → server binds localhost-only and the UI treats the
    local operator as admin (same trust model as the MCP endpoint)."""
    _, mcp = _webui(monkeypatch, tmp_path)
    status, me = _call(mcp, "/ui/api/me")
    assert status == 200 and me["authenticated"] and me["role"] == "admin"


def test_oidc_mode_requires_session(monkeypatch, tmp_path):
    _, mcp = _webui(monkeypatch, tmp_path, oidc=True)
    status, me = _call(mcp, "/ui/api/me")
    assert status == 200 and me["authenticated"] is False
    status, _ = _call(mcp, "/ui/api/secrets")
    assert status == 401


def test_non_admin_session_is_forbidden(monkeypatch, tmp_path):
    webui, mcp = _webui(monkeypatch, tmp_path, oidc=True)
    cookie = {"aicortex_ui": webui._sign(
        {"sub": "kid", "role": "user", "csrf": "c", "exp": time.time() + 300})}
    status, body = _call(mcp, "/ui/api/secrets", cookies=cookie)
    assert status == 403 and "admin" in body["detail"]


def test_admin_session_passes(monkeypatch, tmp_path):
    webui, mcp = _webui(monkeypatch, tmp_path, oidc=True)
    status, body = _call(mcp, "/ui/api/secrets", cookies=_admin_cookie(webui))
    assert status == 200 and body == {"secrets": []}


# ── CSRF ─────────────────────────────────────────────────────────────────────

def test_mutation_without_csrf_header_is_blocked(monkeypatch, tmp_path):
    webui, mcp = _webui(monkeypatch, tmp_path, oidc=True)
    status, body = _call(
        mcp, "/ui/api/secrets", "POST", cookies=_admin_cookie(webui),
        body=json.dumps({"name": "X", "value": "y"}).encode())
    assert status == 403 and "CSRF" in body["detail"]


def test_mutation_with_foreign_origin_is_blocked(monkeypatch, tmp_path):
    webui, mcp = _webui(monkeypatch, tmp_path, oidc=True)
    status, _ = _call(
        mcp, "/ui/api/secrets", "POST", cookies=_admin_cookie(webui),
        headers={"X-CSRF": "tok", "Origin": "https://evil.example"},
        body=json.dumps({"name": "X", "value": "y"}).encode())
    assert status == 403


# ── vault: names only, values write-only ─────────────────────────────────────

def test_secret_set_list_delete_never_returns_value(monkeypatch, tmp_path):
    webui, mcp = _webui(monkeypatch, tmp_path)
    hdr = {"X-CSRF": "local"}
    status, body = _call(
        mcp, "/ui/api/secrets", "POST", headers=hdr,
        body=json.dumps({"name": "API_KEY", "value": "SUPERSECRET"}).encode())
    assert status == 200 and body["ok"]
    assert "SUPERSECRET" not in json.dumps(body)
    status, body = _call(mcp, "/ui/api/secrets")
    assert status == 200
    assert body["secrets"] == [{"name": "API_KEY", "owner": ""}]
    assert "SUPERSECRET" not in json.dumps(body)
    status, body = _call(
        mcp, "/ui/api/secrets/delete", "POST", headers=hdr,
        body=json.dumps({"name": "API_KEY"}).encode())
    assert status == 200 and body["ok"]
    status, body = _call(mcp, "/ui/api/secrets")
    assert body["secrets"] == []


# ── skills ───────────────────────────────────────────────────────────────────

def test_skill_save_requires_category(monkeypatch, tmp_path):
    _, mcp = _webui(monkeypatch, tmp_path)
    hdr = {"X-CSRF": "local"}
    status, body = _call(
        mcp, "/ui/api/skills", "POST", headers=hdr,
        body=json.dumps({"name": "Test", "description": "d",
                         "instructions": "x"}).encode())
    assert status == 400 and "category" in body["error"]


def test_skill_roundtrip(monkeypatch, tmp_path):
    _, mcp = _webui(monkeypatch, tmp_path)
    hdr = {"X-CSRF": "local"}
    status, body = _call(
        mcp, "/ui/api/skills", "POST", headers=hdr,
        body=json.dumps({"name": "Drucker Setup", "description": "IPP einrichten",
                         "category": "devops", "tags": "print",
                         "instructions": "# Schritte"}).encode())
    assert status == 200 and body["ok"]
    status, body = _call(mcp, "/ui/api/skills")
    assert [s["name"] for s in body["skills"]] == ["drucker-setup"]
    status, body = _call(mcp, "/ui/api/skills/get",
                         query={"name": "drucker-setup"})
    assert status == 200 and body["category"] == "devops"
    assert body["instructions"].startswith("# Schritte")
    status, body = _call(mcp, "/ui/api/skills/delete", "POST", headers=hdr,
                         body=json.dumps({"name": "drucker-setup"}).encode())
    assert status == 200 and body["ok"]


# ── users (roles + areas in policy.json) ─────────────────────────────────────

def test_user_set_and_delete_updates_policy(monkeypatch, tmp_path):
    _, mcp = _webui(monkeypatch, tmp_path)
    hdr = {"X-CSRF": "local"}
    status, body = _call(
        mcp, "/ui/api/users", "POST", headers=hdr,
        body=json.dumps({"identity": "anna", "role": "viewer", "memory": "own",
                         "skills": "kitchen", "devices": {"ssh": "all"},
                         "note": "Gast"}).encode())
    assert status == 200 and body["ok"]
    pol = json.loads((tmp_path / "auth" / "policy.json").read_text())
    assert pol["roles"]["anna"] == "viewer"
    assert pol["users"]["anna"]["skills"] == ["kitchen"]
    assert pol["users"]["anna"]["ssh"] == "all"
    status, body = _call(mcp, "/ui/api/users")
    assert body["users"][0]["identity"] == "anna"
    status, body = _call(mcp, "/ui/api/users/delete", "POST", headers=hdr,
                         body=json.dumps({"identity": "anna"}).encode())
    assert status == 200 and body["ok"]
    pol = json.loads((tmp_path / "auth" / "policy.json").read_text())
    assert "anna" not in pol.get("roles", {}) and "anna" not in pol.get("users", {})


def test_user_rejects_bad_role_and_device_kind(monkeypatch, tmp_path):
    _, mcp = _webui(monkeypatch, tmp_path)
    hdr = {"X-CSRF": "local"}
    status, _ = _call(mcp, "/ui/api/users", "POST", headers=hdr,
                      body=json.dumps({"identity": "a", "role": "god"}).encode())
    assert status == 400
    status, _ = _call(mcp, "/ui/api/users", "POST", headers=hdr,
                      body=json.dumps({"identity": "a",
                                       "devices": {"warp": "all"}}).encode())
    assert status == 400


# ── services & devices (read-only) ──────────────────────────────────────────

def test_services_list_never_leaks_config_fields(monkeypatch, tmp_path):
    """Only constructed fields (kind/name/target/description/secret NAME) may
    leave the endpoint — an unexpected config key must never pass through."""
    monkeypatch.setenv("SERVICES_DIR", str(tmp_path / "services"))
    monkeypatch.setenv("FTP_DIR", str(tmp_path / "ftp"))
    _, mcp = _webui(monkeypatch, tmp_path)
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "github.json").write_text(json.dumps({
        "name": "github", "base_url": "https://api.github.com",
        "token_env": "GITHUB_TOKEN", "description": "GitHub API",
        "api_key_plaintext": "LEAKME-NOT",  # hostile/unknown field
    }))
    (tmp_path / "ftp").mkdir()
    (tmp_path / "ftp" / "p1s.json").write_text(json.dumps({
        "name": "p1s", "host": "192.168.178.77", "port": 990,
        "password_env": "BAMBU_ACCESS_CODE", "description": "Drucker-SD",
    }))
    status, body = _call(mcp, "/ui/api/services")
    assert status == 200
    dump = json.dumps(body)
    assert "LEAKME-NOT" not in dump and "api_key_plaintext" not in dump
    rows = {r["name"]: r for r in body["services"]}
    assert rows["github"]["target"] == "https://api.github.com"
    assert rows["github"]["secret"] == "GITHUB_TOKEN"      # ref NAME is fine
    assert rows["p1s"]["kind"] == "ftp"
    assert rows["p1s"]["target"] == "192.168.178.77:990"


def test_audit_endpoint_tails_newest_first_with_filter(monkeypatch, tmp_path):
    _, mcp = _webui(monkeypatch, tmp_path)
    import authz
    authz.audit("steffen", "admin", "ftp_upload", "allow", "ok")
    authz.audit("gast", "viewer", "secret_set", "deny", "admin-only tool")
    authz.audit("steffen", "admin", "ui:secret_set", "allow", "name=X owner=shared")
    status, body = _call(mcp, "/ui/api/audit", query={"limit": "10"})
    assert status == 200 and len(body["entries"]) == 3
    assert body["entries"][0]["tool"] == "ui:secret_set"   # newest first
    status, body = _call(mcp, "/ui/api/audit", query={"q": "deny", "limit": "10"})
    assert [e["identity"] for e in body["entries"]] == ["gast"]


# ── static assets ────────────────────────────────────────────────────────────

def test_asset_allowlist_blocks_unknown_files(monkeypatch, tmp_path):
    _, mcp = _webui(monkeypatch, tmp_path)
    status, _ = _call(mcp, "/ui/assets/{name}", path={"name": "secrets.enc"})
    assert status == 404
    resp = asyncio.run(mcp.routes[("/ui/assets/{name}", "GET")](
        Req(path={"name": "app.js"})))
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]


def test_index_injects_version_cachebuster(monkeypatch, tmp_path):
    import version
    _, mcp = _webui(monkeypatch, tmp_path)
    resp = asyncio.run(mcp.routes[("/ui", "GET")](Req()))
    html = resp.body.decode("utf-8")
    assert "{{v}}" not in html
    assert f"?v={version.__version__}" in html
    assert "Content-Security-Policy" in resp.headers
