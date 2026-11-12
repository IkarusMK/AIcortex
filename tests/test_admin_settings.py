"""Settings that moved from .env into the WebUI (2.7.1): the mail recipient
allow-list, the default tool profile and auto-memory.

They are data now (<data>/auth/settings.json), changed by an admin in the WebUI and
effective without a restart. At the first start the old environment values are taken
over ONCE; afterwards the environment changes nothing. An unreadable settings file is
never replaced: mail refuses, auto-memory is off, writes are refused.
"""
import importlib
import json

import pytest

import test_webui as tw

MOVED = ("MAIL_ALLOWED_RECIPIENTS", "TOOL_PROFILE_DEFAULT", "LEARN_AUTOCAPTURE")


@pytest.fixture
def adm(monkeypatch, tmp_path):
    """admin_settings as a real installation sees it (no test switch)."""
    monkeypatch.delenv("AICORTEX_TEST_OVERRIDES", raising=False)
    for name in MOVED:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AUTH_STORE_DIR", str(tmp_path / "auth"))
    import admin_settings
    return importlib.reload(admin_settings)


def test_the_first_start_takes_the_old_values_over_once(adm, monkeypatch):
    monkeypatch.setenv("MAIL_ALLOWED_RECIPIENTS", "@example.com carol@example.net")
    monkeypatch.setenv("TOOL_PROFILE_DEFAULT", "full")
    monkeypatch.setenv("LEARN_AUTOCAPTURE", "0")
    adm.ensure_initialized()
    assert adm.current() == {"mail_allowed_recipients": "@example.com, carol@example.net",
                             "tool_profile": "full", "learn_autocapture": False}
    # afterwards the environment is ignored
    monkeypatch.setenv("TOOL_PROFILE_DEFAULT", "core")
    monkeypatch.setenv("MAIL_ALLOWED_RECIPIENTS", "")
    adm.ensure_initialized()
    assert adm.get("tool_profile") == "full"
    assert adm.get("mail_allowed_recipients") == "@example.com, carol@example.net"


def test_an_invalid_old_value_is_not_taken_over(adm, monkeypatch):
    monkeypatch.setenv("TOOL_PROFILE_DEFAULT", "everything")
    monkeypatch.setenv("MAIL_ALLOWED_RECIPIENTS", "not an address!")
    adm.ensure_initialized()
    assert adm.get("tool_profile") == "core"
    assert adm.get("mail_allowed_recipients") == ""


def test_a_fresh_install_starts_with_the_safe_defaults(adm):
    assert adm.current() == {"mail_allowed_recipients": "", "tool_profile": "core",
                             "learn_autocapture": True}


def test_an_unreadable_file_is_never_replaced_and_fails_closed(adm, monkeypatch):
    f = adm.path()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("{torn", encoding="utf-8")
    with pytest.raises(adm.Unreadable):
        adm.get("mail_allowed_recipients")
    ok, msg, _ = adm.update({"tool_profile": "full"})
    assert not ok and "unreadable" in msg
    assert f.read_text(encoding="utf-8") == "{torn"
    import learn, profiles
    assert importlib.reload(learn)._enabled() is False
    assert importlib.reload(profiles).normalize("core") == "core"


def test_invalid_values_are_refused_as_a_whole(adm):
    ok, msg, _ = adm.update({"tool_profile": "full", "mail_allowed_recipients": "a b!"})
    assert not ok and "mail_allowed_recipients" in msg
    assert adm.get("tool_profile") == "core"
    assert not adm.update({"unknown": 1})[0]
    assert not adm.update({"learn_autocapture": "maybe"})[0]


def test_only_an_admin_changes_them_in_the_webui_with_csrf_and_without_restart(
        monkeypatch, tmp_path):
    monkeypatch.delenv("AICORTEX_TEST_OVERRIDES", raising=False)
    for name in MOVED:
        monkeypatch.delenv(name, raising=False)
    webui, mcp = tw._webui(monkeypatch, tmp_path)
    import admin_settings
    importlib.reload(admin_settings)
    status, body = tw._call(mcp, "/ui/api/settings")
    assert status == 200 and body["settings"]["tool_profile"] == "core"
    assert body["profiles"] == ["core", "full"]
    # no CSRF header → refused, nothing stored
    status, _ = tw._call(mcp, "/ui/api/settings", "POST",
                         body=json.dumps({"tool_profile": "full"}).encode())
    assert status == 403 and admin_settings.get("tool_profile") == "core"
    hdr = {"X-CSRF": "local"}
    status, body = tw._call(mcp, "/ui/api/settings", "POST", headers=hdr,
                            body=json.dumps({"tool_profile": "nope"}).encode())
    assert status == 400 and admin_settings.get("tool_profile") == "core"
    status, body = tw._call(mcp, "/ui/api/settings", "POST", headers=hdr, body=json.dumps(
        {"tool_profile": "full", "mail_allowed_recipients": "@example.com",
         "learn_autocapture": False}).encode())
    assert status == 200 and body["ok"]
    assert admin_settings.get("tool_profile") == "full"          # no restart needed
    assert admin_settings.get("mail_allowed_recipients") == "@example.com"
    assert admin_settings.get("learn_autocapture") is False
