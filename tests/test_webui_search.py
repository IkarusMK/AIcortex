"""WebUI tab "Search": find everything in the WebUI quickly from one field.

Before: skills, tools and secrets were only findable on their own pages (or through
the assistant). Now one admin view searches all three. It is admin-only like the vault
and the skills library, so it shows nobody more than they could already see. Secrets
are matched by NAME and description only — their values never leave the vault, not
even as a match. Tools are matched with the same vocabulary as integration_search, so
German words ("drucken") find the English tools.
"""
import sys
import time

import httpx
import pytest
from cryptography.fernet import Fernet

pytestmark = pytest.mark.oidc

SECRET_VALUE = "s3cr3t-value-0815"


@pytest.fixture
def ui(monkeypatch, request, fake_idp):
    monkeypatch.setenv("OIDC_CONFIG_URL", f"{fake_idp}/.well-known/openid-configuration")
    monkeypatch.setenv("OIDC_CLIENT_ID", "aicortex")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "upstream-secret")
    monkeypatch.setenv("JWT_SIGNING_KEY", "0" * 64)
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    base = request.getfixturevalue("http_server")
    webui = sys.modules["webui"]

    def client(sub="boss", role="admin", signed_in=True):
        cookies = {}
        if signed_in:
            cookies[webui.SESSION_COOKIE] = webui._sign({"sub": sub, "name": sub, "role": role, "claim_role": role,
                                                         "csrf": "tok", "exp": time.time() + 600})
        return httpx.Client(base_url=base, cookies=cookies, headers={"X-CSRF": "tok"})
    return client


def _skill(name, description, category="Testing", tags=""):
    d = sys.modules["skills"].SKILLS_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n"
                                f"category: {category}\ntags: {tags}\n---\n\nBody of {name}.\n",
                                encoding="utf-8")


def _search(c, q, **extra):
    r = c.get("/ui/api/search", params={"q": q, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_search_is_admin_only(ui):
    assert ui(signed_in=False).get("/ui/api/search", params={"q": "print"}).status_code == 401
    assert ui("alice", "user").get("/ui/api/search", params={"q": "print"}).status_code == 403


def test_finds_skills_by_name_and_by_description(ui):
    _skill("docker-compose-hardening", "Hardens compose stacks for the NAS", category="Security")
    _skill("pdf-merge", "Merge several PDF files into one", category="Documents", tags="pdf, merge")
    c = ui()
    hits = _search(c, "compose")["skills"]
    assert hits[0]["name"] == "docker-compose-hardening" and hits[0]["category"] == "Security"
    assert "Hardens compose stacks" in hits[0]["summary"]
    assert [h["name"] for h in _search(c, "merge")["skills"]] == ["pdf-merge"]
    assert _search(c, "nothing-like-this")["skills"] == []


def test_finds_tools_also_with_german_words(ui):
    c = ui()
    tools = {t["name"]: t for t in _search(c, "drucken")["tools"]}
    assert "print_document" in tools
    assert tools["print_document"]["signature"].startswith("print_document(printer*")
    assert tools["print_document"]["summary"]
    assert "scan_document" in {t["name"] for t in _search(c, "scan")["tools"]}


def test_finds_secrets_by_name_and_never_returns_a_value(ui):
    store = sys.modules["secrets_store"]
    store.vault_set("PAPERLESS_TOKEN", SECRET_VALUE, description="API token for Paperless")
    store.vault_set("MAIL_PASSWORD", "other-value", owner="alice", description="Alice's mail")
    c = ui()
    r = c.get("/ui/api/search", params={"q": "paperless"})
    assert SECRET_VALUE not in r.text
    hits = r.json()["secrets"]
    assert [(h["name"], h["shared"]) for h in hits] == [("PAPERLESS_TOKEN", True)]
    assert hits[0]["description"] == "API token for Paperless"
    assert set(hits[0]) == {"name", "owner_label", "shared", "description"}
    private = _search(c, "mail_password")["secrets"]
    assert [(h["name"], h["owner_label"], h["shared"]) for h in private] == [("MAIL_PASSWORD", "alice", False)]
    assert _search(c, "s3cr3t")["secrets"] == []          # a value is never searchable


GROUPS = ("skills", "tools", "secrets", "services", "cron", "runners", "playbooks", "runs",
          "notes", "clients", "users")


def test_a_too_short_query_returns_nothing(ui):
    c = ui()
    for q in ("", " ", "a"):
        assert _search(c, q) == {"query": q.strip(), **{g: [] for g in GROUPS}}


def test_results_are_capped_per_group(ui):
    for i in range(30):
        _skill(f"note-skill-{i:02d}", "A note skill")
    c = ui()
    assert len(_search(c, "note")["skills"]) == 20               # default
    assert len(_search(c, "note", limit=5)["skills"]) == 5
    assert len(_search(c, "note", limit=999)["skills"]) == 30    # capped at 50, only 30 exist
    assert len(_search(c, "note", limit="x")["skills"]) == 20    # nonsense → default


def test_the_search_tab_is_wired_into_the_ui(ui):
    c = ui()
    page = c.get("/ui").text
    assert '<a href="#search" data-nav="search" data-admin>' in page
    assert 'id="view-search"' in page and "/ui/assets/view-search.js?v=" in page
    js = c.get("/ui/assets/view-search.js")
    assert js.status_code == 200 and "VIEWS.search" in js.text and 'ADMIN_VIEWS.add("search")' in js.text
    assert "innerHTML" not in js.text      # results are rendered as text, never as markup


# ── Everything the WebUI has, not only skills, tools and secrets ─────────────
# Each area is filled through its own WebUI endpoint (as a person would), then found
# by the search. Keys and secrets that come back once on creation must never show up.

def _ok(r):
    assert r.status_code == 200, r.text
    return r.json()


def test_finds_services_and_devices_by_name_description_and_secret_name(ui):
    c = ui()
    _ok(c.post("/ui/api/registry/services", json={
        "name": "Recipe Box", "base_url": "https://recipes.example.com", "category": "Household",
        "token_env": "RECIPE_TOKEN", "description": "Recipes and shopping lists"}))
    for q in ("recipe", "shopping", "RECIPE_TOKEN"):
        hits = _search(c, q)["services"]
        assert [(h["kind"], h["name"]) for h in hits] == [("services", "recipe-box")], q
    assert set(hits[0]) == {"kind", "name", "target", "description", "secret"}
    assert hits[0]["target"] == "https://recipes.example.com" and hits[0]["secret"] == "RECIPE_TOKEN"


def test_finds_cron_jobs_by_name_and_prompt(ui):
    c = ui()
    _ok(c.post("/ui/api/cron", json={"name": "Morning digest", "schedule": "0 7 * * *",
                                     "prompt": "Summarise the weather and the calendar"}))
    by_name = _search(c, "digest")["cron"]
    assert [h["name"] for h in by_name] == ["Morning digest"]
    assert by_name[0]["schedule"] == "0 7 * * *" and "weather" in by_name[0]["summary"]
    assert [h["name"] for h in _search(c, "calendar")["cron"]] == ["Morning digest"]


def test_finds_runners_but_never_their_key(ui):
    c = ui()
    key = "sk-test-runner-key-4711"
    _ok(c.post("/ui/api/runners", json={"name": "Night shift", "kind": "anthropic", "key": key}))
    r = c.get("/ui/api/search", params={"q": "night"})
    assert key not in r.text
    hits = r.json()["runners"]
    assert [(h["name"], h["kind"]) for h in hits] == [("Night shift", "anthropic")]
    assert set(hits[0]) == {"id", "name", "kind", "owner", "enabled"}


def test_finds_playbooks_and_their_runs(ui):
    c = ui()
    _ok(c.post("/ui/api/playbooks", json={"name": "Release flow", "title": "Release flow",
                                          "description": "Ship a new version step by step",
                                          "stages": [{"id": "plan", "name": "Plan", "goal": "g",
                                                      "artifact": "plan.md", "approval": "webui"}]}))
    _ok(c.post("/ui/api/playbooks/runs/start", json={"playbook": "release-flow", "project": "Garden"}))
    found = _search(c, "release")
    assert [h["name"] for h in found["playbooks"]] == ["release-flow"]
    assert "step by step" in found["playbooks"][0]["summary"]
    runs = _search(c, "garden")["runs"]
    assert [(h["playbook_title"], h["project"], h["status"]) for h in runs] == [("Release flow", "Garden", "active")]


def test_finds_notes_by_title_and_by_text(ui):
    c = ui()
    _ok(c.post("/ui/api/memory", json={"title": "Nozzle size", "content": "The printer uses a 0.4 mm nozzle.",
                                       "type": "reference", "project": "Workshop", "tags": "printer"}))
    assert [h["title"] for h in _search(c, "nozzle")["notes"]] == ["Nozzle size"]
    hit = _search(c, "0.4 mm")["notes"][0]
    assert hit["title"] == "Nozzle size" and hit["project"] == "Workshop" and "0.4 mm" in hit["summary"]


def test_finds_clients_but_never_their_key(ui):
    c = ui()
    created = _ok(c.post("/ui/api/agentkeys", json={"identity": "carol", "name": "kitchen tablet"}))
    secret = created["key"]
    assert secret.startswith("ak_")
    r = c.get("/ui/api/search", params={"q": "kitchen"})
    assert secret not in r.text
    hits = r.json()["clients"]
    assert [(h["name"], h["owner"]) for h in hits] == [("kitchen tablet", "carol")]
    assert set(hits[0]) == {"keyid", "name", "owner", "status"}


def test_finds_users_by_identity_and_note(ui):
    c = ui()
    _ok(c.post("/ui/api/users", json={"identity": "grandma", "role": "viewer", "note": "reads the recipes"}))
    assert [(h["identity"], h["role"]) for h in _search(c, "grandma")["users"]] == [("grandma", "viewer")]
    assert [h["identity"] for h in _search(c, "recipes")["users"]] == ["grandma"]


def test_the_view_shows_every_group_and_links_to_its_tab(ui):
    js = ui().get("/ui/assets/view-search.js").text
    for group, tab in (("services", "#services"), ("cron", "#cron"), ("runners", "#runners"),
                       ("playbooks", "#playbooks"), ("runs", "#playbooks"), ("notes", "#memory"),
                       ("clients", "#clients"), ("users", "#users"), ("secrets", "#vault")):
        assert f'"{group}"' in js and f'"{tab}"' in js, group


def test_edge_cases_snippet_key_status_and_an_unreadable_note(fresh_server, monkeypatch):
    ws = sys.modules["webui_search"]
    assert ws._snippet("First line\nsecond line", "zzz", {"yyy": 2}) == "First line"   # no match → first line
    assert ws._snippet("", "x", {}) == ""
    assert ws._key_status({"disabled": True}) == "disabled"
    assert ws._key_status({"expires": time.time() - 60}) == "expired"
    assert ws._key_status({"expires": time.time() + 60}) == ws._key_status({}) == "active"
    shared = sys.modules["memory"].scope_path("shared")
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "good.md").write_text("---\ntitle: Good one\n---\n\nBody\n", encoding="utf-8")
    (shared / "broken.md").write_text("---\ntitle: Broken\n---\n", encoding="utf-8")
    real = sys.modules["memory"].read_meta

    def read_meta(p):
        if p.name == "broken.md":
            raise OSError("unreadable")
        return real(p)
    monkeypatch.setattr(sys.modules["memory"], "read_meta", read_meta)
    assert [r["title"] for r in ws._note_rows()] == ["Good one"]
