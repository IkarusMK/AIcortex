"""Refusals on a KNOWN hook are logged sparingly; odd timestamps are a refusal, not a 500.

Regression for the re-review findings on the public receiver (POST /hooks/<name>):
- a wrong token for a hook name that exists wrote one audit line per request — a
  stranger who knows (or guesses) the name could fill the log, which the
  unknown-hook lines were already protected against. Every refusal is now
  deduplicated per hook and reason and capped per minute, like those.
- ``X-Webhook-Timestamp`` was checked with ``str.isdigit()`` and then ``int()``:
  "²" is a digit to isdigit() but not to int(), so the request ended in a 500. The
  timestamp (and Content-Length) is now matched against a strict ASCII pattern.
"""
import json
import sys

import httpx
import pytest

TOKEN = "test-token"


@pytest.fixture(autouse=True)
def _secrets(monkeypatch):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    monkeypatch.setenv("HOOK_TOKEN", TOKEN)


def _hook(name="ci"):
    folder = sys.modules["webhook_tools"].WEBHOOK_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.json").write_text(json.dumps({"name": name, "secret_env": "HOOK_TOKEN"}),
                                         encoding="utf-8")


def _post(base, name="ci", **headers):
    return httpx.post(f"{base}/hooks/{name}", content=b'{"event":"ping"}', headers=headers, timeout=10)


def _audit():
    path = sys.modules["authz"].AUDIT_FILE
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_wrong_tokens_for_a_known_hook_do_not_write_one_line_each(http_server, fresh_server):
    _hook("ci")
    for _ in range(30):
        assert _post(http_server, "ci", **{"X-Webhook-Token": "guess"}).status_code == 401
    bad = [e for e in _audit() if e["identity"] == "webhook:ci" and e["reason"].startswith("bad token")]
    assert len(bad) == 1
    for i in range(40):                                   # many known hooks: capped per window
        _hook(f"h{i}")
        _post(http_server, f"h{i}", **{"X-Webhook-Token": "guess"})
    assert len([e for e in _audit() if e["decision"] == "deny"]) <= 20
    assert _post(http_server, "ci", **{"X-Webhook-Token": TOKEN}).status_code == 202
    assert any(e["identity"] == "webhook:ci" and e["decision"] == "allow" for e in _audit())


@pytest.mark.parametrize("ts", ["²", "1²", "١٧٦٠٠٠٠٠٠٠", "１７６０００００００"])
def test_a_non_ascii_timestamp_is_refused_not_a_crash(fresh_server, ts):
    wt = sys.modules["webhook_tools"]
    assert wt._check_timestamp({"x-webhook-timestamp": ts}) == (False, "invalid timestamp")


def test_an_odd_timestamp_over_http_is_a_401(http_server, fresh_server):
    _hook("ci")
    r = httpx.post(f"{http_server}/hooks/ci", content=b"{}",
                   headers=[(b"X-Webhook-Token", TOKEN.encode()),
                            (b"X-Webhook-Timestamp", "²".encode("latin-1"))], timeout=10)
    assert r.status_code == 401
