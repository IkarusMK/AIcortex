"""REST errors carry a reference, not the exception text.

Before: a failing tool answered ``{"error": "tool error", "detail": str(exc)}`` — the
raw exception text (internal hosts, paths, upstream answers) went to whoever held an
API key, and a bad-arguments answer echoed the submitted values back.

Now a tool error answers a generic message plus a short ``error_id``; the full detail
is logged server-side (audit line + log) under the same id. Bad arguments name the
offending argument and the problem, never the value. Invented data only.
"""
import json
import logging
import sys

import httpx
import pytest
from cryptography.fernet import Fernet

LEAK = "upstream db at 198.51.100.7 refused user alice"


@pytest.fixture
def api(monkeypatch, request):
    monkeypatch.setenv("ALLOW_INSECURE", "1")
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    base = request.getfixturevalue("http_server")
    server, apikeys = sys.modules["server"], sys.modules["apikeys"]

    @server.mcp.tool
    def leaky_probe() -> str:
        """Always fails with an internal detail."""
        raise RuntimeError(LEAK)

    key = apikeys.create("alice", scopes="all")[0]

    def call(path, **kw):
        return httpx.post(base + path, headers={"Authorization": f"Bearer {key}",
                                                **kw.pop("headers", {})}, timeout=10, **kw)
    return call


def _events(text):
    out = []
    for block in text.split("\n\n"):
        lines = dict(ln.split(": ", 1) for ln in block.splitlines() if ": " in ln and not ln.startswith(":"))
        if "event" in lines:
            out.append((lines["event"], json.loads(lines["data"])))
    return out


def _audit_reasons():
    path = sys.modules["authz"].AUDIT_FILE
    return [json.loads(x)["reason"] for x in path.read_text().splitlines() if x] if path.exists() else []


def test_a_tool_error_answers_a_reference_not_the_exception_text(api, caplog):
    with caplog.at_level(logging.WARNING):
        r = api("/api/v1/tools/leaky_probe")
    body = r.json()
    assert r.status_code == 500 and body["error"] == "tool error"
    assert "198.51.100.7" not in r.text and "alice" not in r.text
    ref = body["error_id"]
    assert len(ref) >= 8 and ref in body["detail"]
    assert any(ref in reason and LEAK in reason for reason in _audit_reasons())
    assert any(ref in rec.getMessage() and LEAK in rec.getMessage() for rec in caplog.records)


def test_a_streamed_tool_error_answers_a_reference_too(api):
    r = api("/api/v1/tools/leaky_probe?stream=1")
    (kind, data), = _events(r.text)
    assert kind == "error" and data["error"] == "tool error" and data["error_id"] in data["detail"]
    assert "198.51.100.7" not in r.text


def test_bad_arguments_name_the_argument_but_never_echo_the_value(api):
    r = api("/api/v1/tools/memory_search", json={"query": "x", "limit": "SECRET-VALUE-42"})
    assert r.status_code == 400 and r.json()["error"] == "invalid arguments"
    assert "limit" in r.json()["detail"] and "SECRET-VALUE-42" not in r.text
    missing = api("/api/v1/tools/memory_read", json={})
    assert missing.status_code == 400 and "name" in missing.json()["detail"]
