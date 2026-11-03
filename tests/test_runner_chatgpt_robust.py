"""The ChatGPT plan stays sound when something goes wrong (from the security review).

Before:
- the runner read a model's whole answer before looking at it — no size limit and no
  overall deadline, so an answer that never ends held the runner (and every later
  job) forever, and an error answer was read in full too;
- starting a sign-in waited for a token refresh in progress;
- a sign-in that could not be stored answered with a server error and left a live
  grant at OpenAI; a rotated refresh token that could not be stored was lost, so the
  next refresh failed;
- odd answers (a broken pasted address, a non-numeric expiry, an ID token whose
  claims or expiry have the wrong shape, a signing key without its numbers, a model
  list that isn't a list, stream events that aren't objects, output items without a
  call id) crashed instead of being refused;
- a client id of any shape from the pasted address was used;
- the audit log did not say which ChatGPT account was attached.
Proves each case now ends in a clear answer.
"""
import asyncio
import json
import sys
import threading
import time

import httpx
import pytest

from test_cov_webui import _audit_lines, ui  # noqa: F401 - ui is a fixture
from test_runner_chatgpt import (FakeOpenAI, Responses, _callback, _expire, _job, _params,  # noqa: F401
                                 _run, _setup, _sign_in, _state, connector, openai)

COMPLETED = b'data: {"type":"response.completed","response":{"output":[]}}\n\n'
URL = "https://api.openai.com/v1/responses"
REAL_CLIENT = httpx.AsyncClient


async def _endless(first: bytes = b"", every: bytes = b": keep-alive\n\n"):
    yield first
    while True:
        await asyncio.sleep(0.005)
        yield every


def _answering(monkeypatch, status, content):
    """Every model API request of the runner gets this answer."""
    builtin = sys.modules["runner_builtin"]
    transport = httpx.MockTransport(lambda request: httpx.Response(status, content=content()))
    monkeypatch.setattr(builtin.httpx, "AsyncClient", lambda **kw: REAL_CLIENT(transport=transport, **kw))
    return builtin


def _within(coro, seconds=5):
    return asyncio.run(asyncio.wait_for(coro, seconds))


# ── reading a model's answer ─────────────────────────────────────────────────
def test_the_stream_is_left_at_response_completed(connector, monkeypatch):
    builtin = _answering(monkeypatch, 200, lambda: _endless(COMPLETED))
    assert _within(builtin._stream(URL, {}, {})) == {"output": []}


def test_an_answer_that_never_ends_is_cut_off(connector, monkeypatch):
    builtin = _answering(monkeypatch, 200, lambda: _endless())
    monkeypatch.setattr(builtin, "ANSWER_DEADLINE", 0.3)
    with pytest.raises(RuntimeError, match="did not finish within"):
        _within(builtin._stream(URL, {}, {}))
    with pytest.raises(RuntimeError, match="did not finish within"):
        _within(builtin._post(URL, {}, {}))


def test_an_answer_that_is_too_long_is_cut_off(connector, monkeypatch):
    builtin = _answering(monkeypatch, 200, lambda: b'{"choices":[]}')
    monkeypatch.setattr(builtin, "MAX_ANSWER", 2000)
    assert _within(builtin._post(URL, {}, {})) == {"choices": []}       # a normal answer
    builtin = _answering(monkeypatch, 200, lambda: _endless(every=b'data: {"type":"x"}\n\n'))
    with pytest.raises(RuntimeError, match="too long"):
        _within(builtin._stream(URL, {}, {}))
    with pytest.raises(RuntimeError, match="too long"):
        _within(builtin._post(URL, {}, {}))


def test_an_error_answer_is_read_only_in_part(connector, monkeypatch):
    builtin = _answering(monkeypatch, 401, lambda: _endless(b'{"error":{"code":"token_expired"}}', b"x" * 4096))
    with pytest.raises(builtin.Unauthorized):
        _within(builtin._stream(URL, {}, {}))
    with pytest.raises(RuntimeError, match="HTTP 401"):
        _within(builtin._post(URL, {}, {}))


def test_odd_stream_events_are_skipped_not_crashed(connector):
    parse = sys.modules["runner_builtin"]._parse_sse
    assert parse(["data: [1, 2]", "", 'data: {"type":"response.completed","response":{"output":[]}}', ""]) == {"output": []}
    with pytest.raises(RuntimeError, match="response.failed: unknown_error"):
        parse(['data: {"type":"response.failed","response":{"error":"boom"}}', ""])
    with pytest.raises(RuntimeError, match="incomplete"):
        parse(['data: {"type":"response.incomplete","response":"x"}', ""])


def test_odd_output_items_are_skipped(connector, openai, monkeypatch):
    assert _sign_in(openai)[0][0]
    _setup()
    sys.modules["cron"]._write([_job()])

    async def odd(url, payload, headers):
        return {"output": ["junk", {"type": "function_call", "name": "cron_mark_run"},
                           {"type": "message", "content": "not a list"}]}
    count, builtin = _run(connector, monkeypatch, odd)
    assert count == 1 and _state() == "ran"


# ── the sign-in ──────────────────────────────────────────────────────────────
def test_starting_a_sign_in_does_not_wait_for_a_token_refresh(openai):
    cs = sys.modules["chatgpt_signin"]
    held, release = threading.Event(), threading.Event()

    def refreshing():
        with cs._lock:
            held.set()
            release.wait(5)
    t = threading.Thread(target=refreshing)
    t.start()
    held.wait(2)
    got = []
    s = threading.Thread(target=lambda: got.append(cs.start(by="boss")))
    s.start()
    s.join(2)
    early = list(got)
    release.set()
    t.join()
    s.join()
    assert early and early[0].startswith("https://auth.openai.com/")


def test_a_sign_in_that_cannot_be_stored_is_revoked_and_says_so(openai, monkeypatch):
    cs = sys.modules["chatgpt_signin"]
    monkeypatch.setattr(sys.modules["secrets_store"], "vault_set",
                        lambda *a, **k: "Refusing to store: STORAGE_ENCRYPTION_KEY is not set.")
    (ok, msg), _ = _sign_in(openai)
    assert not ok and "could not be stored" in msg and "STORAGE_ENCRYPTION_KEY" in msg
    url, form = openai.forms[-1]
    assert url.endswith("/oauth/revoke") and form["token"] == "rt-1"
    assert not cs.status()["signed_in"]


def test_a_rotated_refresh_token_that_cannot_be_stored_is_kept(openai, monkeypatch):
    cs, ss = sys.modules["chatgpt_signin"], sys.modules["secrets_store"]
    assert _sign_in(openai)[0][0]
    _expire(cs)
    real = ss.vault_set
    monkeypatch.setattr(ss, "vault_set", lambda *a, **k: "Refusing to write: disk full.")
    assert cs.access_token() == "at-2"
    monkeypatch.setattr(ss, "vault_set", real)
    assert cs.access_token(force_refresh=True) == "at-3"                  # with rt-2, not rt-1
    monkeypatch.setattr(cs, "_unsaved", None)
    assert cs._session()["refresh_token"] == "rt-3"                        # stored again


def test_a_broken_address_or_a_strange_client_id_is_refused(openai):
    cs = sys.modules["chatgpt_signin"]
    ok, msg = cs.finish("http://[x?code=1&state=s", by="boss")
    assert not ok and "whole address" in msg
    params = _params(cs.start(by="boss"))
    openai.nonce = params["nonce"]
    ok, msg = cs.finish(_callback(params, client_id="oaiapp_<script>"), by="boss")
    assert not ok and "client id" in msg
    assert not [f for u, f in openai.forms if u.endswith("/oauth/token")]


def test_a_non_numeric_expiry_falls_back_to_an_hour(openai, monkeypatch):
    real = openai.tokens
    monkeypatch.setattr(openai, "tokens", lambda: dict(real(), expires_in="soon"))
    assert _sign_in(openai)[0][0]
    left = sys.modules["chatgpt_signin"]._session()["expires_at"] - time.time()
    assert 3500 < left <= 3600


@pytest.mark.parametrize("claims", [[1, 2], {"exp": [1]}])
def test_an_id_token_of_the_wrong_shape_is_refused(openai, monkeypatch, claims):
    real = openai.id_token
    monkeypatch.setattr(openai, "id_token",
                        lambda **o: openai.jwt(claims) if isinstance(claims, list) else real(**claims))
    (ok, msg), _ = _sign_in(openai)
    assert not ok and "ID token" in msg


def test_a_signing_key_without_its_numbers_is_refused(openai, monkeypatch):
    monkeypatch.setattr(sys.modules["chatgpt_signin"], "_get_json", lambda url, headers=None: (
        (200, {"keys": [{"kty": "RSA", "kid": "k1"}]}) if url.endswith("/jwks.json") else openai.get_json(url, headers)))
    (ok, msg), _ = _sign_in(openai)
    assert not ok and "ID token" in msg


def test_a_model_list_that_is_not_a_list_gives_no_models(openai, monkeypatch):
    assert _sign_in(openai)[0][0]
    monkeypatch.setattr(sys.modules["chatgpt_signin"], "_get_json", lambda url, headers=None: (
        (200, {"models": 5}) if url.endswith("/v1/models") else openai.get_json(url, headers)))
    assert sys.modules["chatgpt_signin"].models() == []


@pytest.mark.oidc
def test_the_audit_log_names_the_attached_chatgpt_account(ui, monkeypatch):
    fake = FakeOpenAI()
    cs = sys.modules["chatgpt_signin"]
    monkeypatch.setattr(cs, "_post_form", fake.post_form)
    monkeypatch.setattr(cs, "_get_json", fake.get_json)
    params = _params(ui.call("/ui/api/runner/chatgpt/start", "POST", body={})[1]["url"])
    fake.nonce = params["nonce"]
    assert ui.call("/ui/api/runner/chatgpt/finish", "POST", body={"url": _callback(params)})[0] == 200
    entry = [e for e in _audit_lines() if e["tool"] == "ui:runner_chatgpt_signin"][-1]
    assert "alice@example.com" in json.dumps(entry) and "rt-1" not in json.dumps(entry)
