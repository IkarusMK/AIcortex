"""WebUI management API (app/webui.py): who may call it, and the read-only views.

Proves for EVERY management route: no session -> 401, a user or viewer -> 403 (and the
refusal is audited), UI switched off -> 404, state-changing calls without the CSRF
token or from a foreign Origin -> 403 and change nothing, bodies that are not JSON
objects (or too large) -> 400. Also the static app (index, asset allow-list), the
overview counts, the service list (only constructed fields), the audit tail (newest
first, filtered, capped, bounded to the last 1 MB), skills and identities listings.
"""
import asyncio
import json
import sys
import time
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.oidc       # sign-in mode: sessions come from the signed cookie

GET, POST = "GET", "POST"
ROUTES = [
    (GET, "/ui/api/overview"), (GET, "/ui/api/secrets"), (POST, "/ui/api/secrets"),
    (POST, "/ui/api/secrets/delete"), (POST, "/ui/api/secrets/update"), (POST, "/ui/api/secrets/rekey"),
    (GET, "/ui/api/agentkeys"), (POST, "/ui/api/agentkeys"), (POST, "/ui/api/agentkeys/revoke"),
    (GET, "/ui/api/identities"), (GET, "/ui/api/services"), (GET, "/ui/api/audit"),
    (GET, "/ui/api/skills"), (GET, "/ui/api/skills/examples"), (POST, "/ui/api/skills/examples"),
    (GET, "/ui/api/skills/get"), (POST, "/ui/api/skills"), (POST, "/ui/api/skills/delete"),
    (GET, "/ui/api/users"), (POST, "/ui/api/users"), (POST, "/ui/api/users/delete"),
]
MUTATING = [r for r in ROUTES if r[0] == POST]
READS_BODY = [r for r in MUTATING if r[1] not in ("/ui/api/secrets/rekey", "/ui/api/skills/examples")]


class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def deco(fn):
            for m in methods:
                self.routes[(path, m)] = fn
            return fn
        return deco


class Ui:
    """Drives the registered handlers directly, as a browser session of a given role."""

    def __init__(self, webui):
        self.webui, self.mcp = webui, FakeMCP()
        webui.register(self.mcp)

    def raw(self, route, method=GET, *, who="boss", role="admin", csrf="tok", body=None,
            query=None, path=None, headers=None):
        hdr = dict(headers or {})
        if csrf is not None:
            hdr.setdefault("x-csrf", csrf)
        cookies = {} if who is None else {self.webui.SESSION_COOKIE: self.webui._sign(
            {"sub": who, "name": who, "role": role, "csrf": "tok", "exp": time.time() + 600})}
        raw = body if isinstance(body, bytes) else b"" if body is None else json.dumps(body).encode()
        req = SimpleNamespace(
            client=SimpleNamespace(host="192.0.2.10"), headers={k.lower(): v for k, v in hdr.items()},
            cookies=cookies, query_params=query or {}, path_params=path or {},
            base_url="http://cortex.example.com/", body=_const(raw))
        return asyncio.run(self.mcp.routes[(route, method)](req))

    def call(self, route, method=GET, **kw):
        resp = self.raw(route, method, **kw)
        return resp.status_code, (json.loads(resp.body) if resp.body else {})


def _const(raw):
    async def body():
        return raw
    return body


@pytest.fixture
def ui(monkeypatch, request, fake_idp):
    monkeypatch.setenv("OIDC_CONFIG_URL", f"{fake_idp}/.well-known/openid-configuration")
    monkeypatch.setenv("OIDC_CLIENT_ID", "aicortex")
    monkeypatch.setenv("JWT_SIGNING_KEY", "0" * 64)
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    monkeypatch.setenv("ALLOW_PLAINTEXT_VAULT", "1")
    monkeypatch.setenv("BASE_URL", "http://cortex.example.com")
    request.getfixturevalue("fresh_server")
    return Ui(sys.modules["webui"])


def _audit_lines():
    path = sys.modules["authz"].AUDIT_FILE
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


# ── the gate in front of every management route ─────────────────────────────
def test_anonymous_callers_get_401_on_every_route(ui):
    for method, route in ROUTES:
        assert ui.call(route, method, who=None)[0] == 401, (method, route)


@pytest.mark.parametrize("role", ["user", "viewer"])
def test_non_admins_get_403_on_every_route_and_each_refusal_is_audited(ui, role):
    for method, route in ROUTES:
        status, body = ui.call(route, method, who="bob", role=role, body={"name": "x", "value": "y"})
        assert status == 403 and body["detail"] == "admin role required", (method, route)
        last = _audit_lines()[-1]
        assert (last["identity"], last["role"], last["tool"], last["decision"]) == (
            "bob", role, "ui:access", "deny"), (method, route)
    assert not ui.webui.secrets_store.VAULT_FILE.exists()


def test_everything_is_404_when_the_ui_is_switched_off(ui, monkeypatch):
    monkeypatch.setenv("UI_ENABLED", "off")
    for method, route in ROUTES:
        assert ui.call(route, method)[0] == 404, (method, route)
    assert ui.call("/ui/api/me")[0] == 404 and ui.call("/ui")[0] == 404


def test_mutations_without_the_right_csrf_header_are_refused_and_audited(ui):
    for method, route in MUTATING:
        for kw in ({"csrf": None}, {"csrf": "stale"}):
            status, body = ui.call(route, method, body={"name": "X", "value": "y"}, **kw)
            assert status == 403 and body["detail"] == "CSRF check failed", (method, route)
        assert _audit_lines()[-1]["tool"] == "ui:csrf", (method, route)
    assert not ui.webui.secrets_store.VAULT_FILE.exists()          # nothing was written


def test_mutations_from_a_foreign_origin_are_refused(ui):
    for method, route in MUTATING:
        status, _ = ui.call(route, method, body={"name": "X", "value": "y"},
                            headers={"Origin": "https://evil.example.net"})
        assert status == 403, (method, route)
    assert not ui.webui.secrets_store.VAULT_FILE.exists()
    own = ui.call("/ui/api/secrets", POST, body={"name": "X", "value": "y"},
                  headers={"Origin": "http://cortex.example.com"})
    assert own[0] == 200                                           # the same origin is fine


def test_bodies_that_are_not_a_json_object_are_400_on_every_route_that_reads_one(ui):
    for method, route in READS_BODY:
        for raw in (b"{not json", b"[1, 2]", b'"text"'):
            status, body = ui.call(route, method, body=raw)
            assert status == 400 and "invalid body" in json.dumps(body), (method, route, raw)


def test_an_oversized_body_is_refused_and_an_empty_one_means_no_fields(ui, monkeypatch):
    monkeypatch.setattr(ui.webui, "_MAX_BODY", 20)
    assert ui.call("/ui/api/secrets", POST, body=b'{"name": "A", "value": "' + b"x" * 40 + b'"}')[0] == 400
    monkeypatch.setattr(ui.webui, "_MAX_BODY", 1_000_000)
    status, body = ui.call("/ui/api/secrets", POST, body=b"  ")
    assert status == 400 and body["error"] == "name and value are required"


def test_a_plain_signed_in_person_reaches_their_own_area_but_not_management(ui, monkeypatch):
    assert ui.call("/ui/api/cron", who=None)[0] == 401
    assert ui.call("/ui/api/cron", who="bob", role="user")[0] == 200
    assert ui.call("/ui/api/secrets", who="bob", role="user")[0] == 403
    monkeypatch.setenv("UI_ENABLED", "0")
    assert ui.call("/ui/api/cron", who="bob", role="user")[0] == 404


# ── me, index, assets ───────────────────────────────────────────────────────
def test_me_reports_the_session_or_that_sign_in_is_needed(ui):
    status, body = ui.call("/ui/api/me", who=None)
    assert status == 200 and body["authenticated"] is False and body["oidc"] is True
    _, body = ui.call("/ui/api/me", who="alice", role="user")
    assert (body["authenticated"], body["sub"], body["role"], body["csrf"]) == (True, "alice", "user", "tok")


def test_index_and_assets_are_served_only_from_the_allow_list(ui, monkeypatch, tmp_path):
    page = ui.raw("/ui")
    assert page.status_code == 200 and "default-src 'none'" in page.headers["Content-Security-Policy"]
    assert ui.raw("/ui/assets/{name}", path={"name": "style.css"}).headers["content-type"].startswith("text/css")
    for name in ("../app.py", "secrets.enc", "index.html", ""):
        assert ui.call("/ui/assets/{name}", path={"name": name})[0] == 404
    monkeypatch.setattr(ui.webui, "UI_DIR", tmp_path / "empty")      # image without UI files
    assert ui.call("/ui")[0] == 500 and "assets missing" in ui.call("/ui")[1]["error"]
    assert ui.call("/ui/assets/{name}", path={"name": "app.js"})[0] == 404


# ── overview, services, identities ──────────────────────────────────────────
def test_overview_counts_skills_secrets_registries_and_users(ui, tmp_path):
    import os
    webui = ui.webui
    skills = webui.skills.SKILLS_DIR
    for name, cat in (("a", "Home"), ("b", "Home"), ("c", "Mail")):
        (skills / name).mkdir(parents=True)
        (skills / name / "SKILL.md").write_text(
            f"---\nname: {name}\ncategory: {cat}\n---\nbody\n", encoding="utf-8")
    for env, fname in (("SERVICES_DIR", "ha.json"), ("FTP_DIR", "nas.json"), ("SSH_DIR", "box.json")):
        d = tmp_path / env.lower()
        d.mkdir(exist_ok=True)
        (d / fname).write_text("{}", encoding="utf-8")
    assert os.environ["SERVICES_DIR"] == str(tmp_path / "services_dir")
    ui.call("/ui/api/secrets", POST, body={"name": "HA_TOKEN", "value": "test-token"})
    ui.call("/ui/api/users", POST, body={"identity": "alice", "role": "user"})
    _, body = ui.call("/ui/api/overview")
    assert (body["skills"], body["categories"], body["secrets"]) == (3, 2, 1)
    assert (body["services"], body["devices"], body["users"]) == (1, 2, 1)
    assert body["enforce"] is True and body["version"]


def test_services_never_pass_raw_config_and_survive_unreadable_files(ui, tmp_path):
    ssh = tmp_path / "ssh_dir"
    ssh.mkdir()
    (ssh / "broken.json").write_text("{not json", encoding="utf-8")
    (ssh / "box.json").write_text(json.dumps(
        {"host": "box.example.net", "password_env": "BOX_PASSWORD", "private": "do-not-leak"}), encoding="utf-8")
    _, body = ui.call("/ui/api/services")
    rows = {r["name"]: r for r in body["services"]}
    assert rows["broken"] == {"kind": "ssh", "name": "broken", "target": "(unreadable)",
                              "description": "", "secret": ""}
    assert rows["box"]["target"] == "box.example.net" and rows["box"]["secret"] == "BOX_PASSWORD"
    assert "do-not-leak" not in json.dumps(body)


def test_identities_merge_policy_keys_and_the_caller(ui):
    ui.webui.tenancy._write_policy({"roles": {"bob": "user"}, "users": {"alice": {"memory": "own"}}})
    ui.call("/ui/api/agentkeys", POST, body={"identity": "carol", "name": "laptop"})
    _, body = ui.call("/ui/api/identities", who="boss")
    assert body["identities"] == ["alice", "bob", "boss", "carol"]


# ── audit tail ──────────────────────────────────────────────────────────────
def _write_audit(ui, lines):
    path = sys.modules["authz"].AUDIT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _rec(i, pad=0):
    return json.dumps({"ts": "2026-11-01T10:00:00+00:00", "identity": "alice", "role": "user",
                       "tool": f"tool_{i}", "decision": "allow", "reason": "ok" + "x" * pad})


def test_audit_is_empty_without_a_log_and_skips_damaged_lines(ui):
    assert ui.call("/ui/api/audit")[1] == {"entries": []}
    _write_audit(ui, [_rec(1), "{broken", "", _rec(2)])
    tools = [e["tool"] for e in ui.call("/ui/api/audit")[1]["entries"]]
    assert tools == ["tool_2", "tool_1"]


def test_audit_limit_is_clamped_and_garbage_falls_back_to_the_default(ui):
    _write_audit(ui, [_rec(i) for i in range(1100)])
    count = lambda limit: len(ui.call("/ui/api/audit", query={"limit": limit})[1]["entries"])
    assert (count("0"), count("-5"), count("3")) == (1, 1, 3)
    assert count("99999") == 1000 and count("abc") == 200
    assert len(ui.call("/ui/api/audit")[1]["entries"]) == 200


def test_audit_filter_is_case_insensitive(ui):
    _write_audit(ui, [_rec(1), _rec(2)])
    entries = ui.call("/ui/api/audit", query={"q": "TOOL_2"})[1]["entries"]
    assert [e["tool"] for e in entries] == ["tool_2"]


def test_audit_reads_only_the_last_megabyte_without_a_cut_line(ui):
    total, line = 4000, len(_rec(3999, pad=200)) + 1               # all lines the same length
    _write_audit(ui, [_rec(i, pad=200) for i in range(total)])     # a little over 1 MB
    assert sys.modules["authz"].AUDIT_FILE.stat().st_size > 1_000_000
    entries = ui.webui._audit_tail(100_000, "")
    assert entries[0]["tool"] == f"tool_{total - 1}"               # newest first
    assert 1_000_000 // line - 2 <= len(entries) < total           # the oldest part is not read
    assert all(e["tool"].startswith("tool_") for e in entries)     # the cut first line was dropped


# ── skills listing ──────────────────────────────────────────────────────────
def test_skill_listing_survives_an_unreadable_skill_file(ui):
    skills = ui.webui.skills.SKILLS_DIR
    (skills / "broken").mkdir(parents=True)
    (skills / "broken" / "SKILL.md").write_bytes(b"\xff\xfe not utf-8 \xff")
    _, body = ui.call("/ui/api/skills")
    assert [(s["name"], s["title"], s["description"]) for s in body["skills"]] == [("broken", "broken", "")]
