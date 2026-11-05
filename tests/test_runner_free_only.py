"""An OpenRouter runner can be limited to free models ("Free models only").

Asked for on 03.11.2026: limit the OpenRouter runner to free models for now.
OpenRouter marks a model's free variant with the suffix ":free" — measured on
05.11.2026 against its public model list: all 12 of 452 models with that suffix cost
$0, and 11 of them can call tools (which a runner needs). A runner with "free models
only" refuses any other model when it is saved, and again before every request, so a
hand-edited runners.json can't spend money either. Existing runners keep working as
they are until the box is ticked; the WebUI ticks it for a new OpenRouter runner.
"""
import asyncio

import pytest

from test_cov_webui import Ui, ui  # noqa: F401 - ui is a fixture
from test_runners import _save, reg  # noqa: F401 - reg is a fixture

KEY = "sk-or-free-test-0000"


def test_a_free_only_runner_refuses_a_paid_model(reg):
    ok, message, rec = _save(reg, model="vendor/paid-model", free_only=True, key=KEY)
    assert not ok and rec is None
    assert ":free" in message


@pytest.mark.parametrize("model", ["vendor/model-a:free", "openrouter/vendor/model-a:free"])
def test_a_free_model_is_accepted(reg, model):
    ok, message, rec = _save(reg, model=model, free_only=True, key=KEY)
    assert ok, message
    assert rec["free_only"] is True and reg.public(rec)["free_only"] is True


def test_an_existing_runner_keeps_its_setting(reg):
    ok, _, rec = _save(reg, model="vendor/paid-model", key=KEY)
    assert ok and rec["free_only"] is False                 # nothing changes unasked
    ok, _, _ = _save(reg, id=rec["id"], free_only=True)
    assert not ok                                           # still a paid model
    ok, _, rec = _save(reg, id=rec["id"], free_only=True, model="vendor/model-a:free")
    assert ok and rec["free_only"] is True
    ok, _, rec = reg.save({"id": rec["id"], "name": "Renamed"}, by="boss", admin=True)
    assert ok and rec["free_only"] is True                  # an edit without it keeps it
    assert rec["model"] == "vendor/model-a:free"


def test_only_an_openrouter_runner_has_the_switch(reg):
    ok, message, rec = _save(reg, kind="anthropic", model="", free_only=True, key=KEY)
    assert ok, message
    assert rec["free_only"] is False


def test_a_hand_edited_paid_model_is_refused_before_any_request(reg, monkeypatch):
    import runner_builtin

    async def no_request(*args, **kwargs):
        raise AssertionError("the model must not be asked")

    monkeypatch.setattr(runner_builtin, "_post", no_request)
    rec = {"id": "r_abcdef", "kind": "openrouter", "model": "vendor/paid-model",
           "free_only": True, "key": None, "enabled": True}
    assert runner_builtin._access(rec) == ("", "paid-model")
    ok, message = asyncio.run(runner_builtin.probe(rec))
    assert not ok and ":free" in message


@pytest.mark.oidc
def test_the_webui_passes_the_switch_on(ui):
    status, body = ui.call("/ui/api/runners", "POST", body={
        "kind": "openrouter", "model": "vendor/paid-model", "free_only": True, "key": KEY})
    assert status == 400 and ":free" in body["message"], body
    status, body = ui.call("/ui/api/runners", "POST", body={
        "kind": "openrouter", "model": "vendor/model-a:free", "free_only": True, "key": KEY})
    assert status == 200 and body["runner"]["free_only"] is True, body


def test_the_runner_form_offers_the_switch_in_both_languages():
    from pathlib import Path
    static = Path(__file__).resolve().parent.parent / "app" / "webui_static"
    assert 'id="rn-free-only"' in (static / "index.html").read_text(encoding="utf-8")
    js = (static / "view-runner.js").read_text(encoding="utf-8")
    assert js.count("rn_f_free_only:") == 2 and js.count('"rn_s_paid-model":') == 2   # DE and EN
    assert "body.free_only = $(\"#rn-free-only\").checked" in js
