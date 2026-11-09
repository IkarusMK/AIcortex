"""WebUI tab "Search": find skills, tools and secrets quickly from one field.

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
            cookies[webui.SESSION_COOKIE] = webui._sign({"sub": sub, "name": sub, "role": role,
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


def test_a_too_short_query_returns_nothing(ui):
    c = ui()
    for q in ("", " ", "a"):
        assert _search(c, q) == {"query": q.strip(), "skills": [], "tools": [], "secrets": []}


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
