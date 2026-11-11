"""The starter skills reach a fresh installation.

Before: eight example skills lay in examples/skills, but the image only carried app/,
so an installation from the prebuilt image could not get them without copying files
by hand — a new library started empty.
"""
import sys
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.oidc       # keep the WebUI's sign-in mode for the ui fixture

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def ui(monkeypatch, request, fake_idp):
    monkeypatch.setenv("OIDC_CONFIG_URL", f"{fake_idp}/.well-known/openid-configuration")
    monkeypatch.setenv("OIDC_CLIENT_ID", "aicortex")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "upstream-secret")
    monkeypatch.setenv("JWT_SIGNING_KEY", "0" * 64)
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    base = request.getfixturevalue("http_server")
    webui = sys.modules["webui"]

    def client(sub, role):
        cookie = webui._sign({"sub": sub, "name": sub, "role": role, "claim_role": role, "csrf": "tok",
                              "exp": time.time() + 600})
        return httpx.Client(base_url=base, cookies={webui.SESSION_COOKIE: cookie},
                            headers={"X-CSRF": "tok"}, timeout=30)
    return client


def test_the_image_ships_the_starter_skills():
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY examples/skills/ ./skill_examples/" in dockerfile


def test_bundled_examples_are_listed(ui):
    examples = {e["name"]: e for e in sys.modules["skills"].examples()}
    assert "home-assistant-rest" in examples
    assert examples["home-assistant-rest"]["category"] == "Home Automation"
    assert not any(e["installed"] for e in examples.values())


def test_an_admin_adds_them_once_and_edits_survive(ui):
    boss = ui("boss", "admin")
    listed = boss.get("/ui/api/skills/examples").json()["examples"]
    assert len(listed) >= 7 and not any(e["installed"] for e in listed)

    r = boss.post("/ui/api/skills/examples", json={})
    assert r.status_code == 200 and r.json()["ok"]
    assert "home-assistant-rest" in r.json()["added"]
    skills = sys.modules["skills"]
    page = skills.SKILLS_DIR / "home-assistant-rest" / "SKILL.md"
    assert page.is_file()
    # searched as the admin: with sign-in configured a caller without an identity
    # sees no skills at all (authz: an unresolved identity is never the operator)
    with sys.modules["authz"].rest_identity("boss", "admin"):
        found = skills.search("home assistant")
    assert any(n == "home-assistant-rest" for _, n, _ in found)

    page.write_text(page.read_text(encoding="utf-8") + "\nLocal note.\n", encoding="utf-8")
    again = boss.post("/ui/api/skills/examples", json={}).json()
    assert again["added"] == [] and "home-assistant-rest" in again["skipped"]
    assert "Local note." in page.read_text(encoding="utf-8")
    assert all(e["installed"] for e in boss.get("/ui/api/skills/examples").json()["examples"])


def test_only_an_admin_adds_starter_skills(ui):
    bob = ui("bob", "user")
    assert bob.post("/ui/api/skills/examples", json={}).status_code == 403
    assert not list(sys.modules["skills"].SKILLS_DIR.glob("*/SKILL.md"))
