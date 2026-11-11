"""Inbound webhooks (POST /hooks/<name>): cheap refusals, bounded reads, no replays.

Before: any name of any length was looked up and every unknown-hook request wrote an
audit line (a cheap way to flood the log); the whole body was read before the size
limit was applied; and a captured, correctly signed delivery could be replayed
forever.

Now the name is checked (length + characters) before any work, unknown-hook lines are
deduplicated and capped per minute, the body limit is enforced while reading, an exact
repeat of an accepted delivery is acknowledged (200, "duplicate") but not delivered
again, and an optional ``X-Webhook-Timestamp`` is part of the HMAC and must be within
±5 minutes. Existing senders (HMAC over the body, the token header) keep working.
"""
import asyncio
import hashlib
import hmac
import json
import sys
import time

import httpx
import pytest
from starlette.requests import Request

TOKEN, HMAC_KEY = "test-token", "test-hmac-key"


@pytest.fixture(autouse=True)
def _secrets(monkeypatch):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    monkeypatch.setenv("HOOK_TOKEN", TOKEN)
    monkeypatch.setenv("HOOK_HMAC", HMAC_KEY)


def _hook(name="ci", **cfg):
    cfg.setdefault("secret_env", "HOOK_TOKEN")
    folder = sys.modules["webhook_tools"].WEBHOOK_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.json").write_text(json.dumps({"name": name, **cfg}), encoding="utf-8")


def _post(base, name="ci", body=b'{"event":"ping"}', **headers):
    return httpx.post(f"{base}/hooks/{name}", content=body, headers=headers, timeout=10)


def _sig(msg, key=HMAC_KEY):
    return "sha256=" + hmac.new(key.encode(), msg, hashlib.sha256).hexdigest()


def _audit():
    path = sys.modules["authz"].AUDIT_FILE
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _inbox_count(server, agent="user"):
    coordination = sys.modules["coordination"]
    text = coordination.read_inbox(agent, unread_only=False) if hasattr(coordination, "read_inbox") else None
    if text is None:
        from fastmcp import Client

        async def go():
            async with Client(server.mcp) as client:
                res = await client.call_tool("inbox_read", {"agent": agent, "unread_only": False})
                return res.content[0].text if res.content else ""
        text = asyncio.run(go())
    return text.count("UNTRUSTED EXTERNAL")


# ── the name is checked before any work ─────────────────────────────────────
@pytest.mark.parametrize("name", ["a" * 65, "x y", "-lead", "name;drop", "a+b", "ü-hook"])
def test_odd_or_overlong_names_are_refused_before_any_lookup(http_server, fresh_server, monkeypatch, name):
    looked_up = []
    wt = sys.modules["webhook_tools"]
    real = wt._load
    monkeypatch.setattr(wt, "_load", lambda n: looked_up.append(n) or real(n))
    r = _post(http_server, name, **{"X-Webhook-Token": TOKEN})
    assert (r.status_code, r.text) == (401, '{"error":"unauthorized"}')
    assert looked_up == []


def test_unknown_hook_requests_do_not_write_one_audit_line_each(http_server, fresh_server):
    for _ in range(5):
        _post(http_server, "ghost")
    for i in range(60):
        _post(http_server, f"probe-{i}")
    unknown = [e for e in _audit() if e["reason"].startswith("unknown hook")]
    assert [e["identity"] for e in unknown].count("webhook:ghost") == 1
    assert len(unknown) <= 20


# ── the size limit is enforced while reading ────────────────────────────────
class _FakeMCP:
    def __init__(self):
        self.routes = {}

    def tool(self, fn=None, **kw):
        return fn if fn else (lambda f: f)

    def custom_route(self, path, methods):
        def deco(fn):
            self.routes[path] = fn
            return fn
        return deco


def _streamed_request(name, chunks, headers):
    reads = []
    queue = list(chunks)

    async def receive():
        reads.append(1)
        chunk = queue.pop(0) if queue else b""
        return {"type": "http.request", "body": chunk, "more_body": bool(queue)}
    scope = {"type": "http", "method": "POST", "path": f"/hooks/{name}", "query_string": b"",
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
             "path_params": {"name": name}}
    return Request(scope, receive), reads


def test_an_oversized_body_is_cut_off_while_reading(fresh_server, monkeypatch):
    wt = sys.modules["webhook_tools"]
    monkeypatch.setattr(wt, "_MAX_BODY", 100)
    _hook()
    mcp = _FakeMCP()
    wt.register(mcp)
    request, reads = _streamed_request("ci", [b"x" * 64] * 1000, {"X-Webhook-Token": TOKEN})
    resp = asyncio.run(mcp.routes["/hooks/{name}"](request))
    assert resp.status_code == 413
    assert len(reads) <= 3                 # stopped right after the limit, not after 64 KB


def test_a_declared_oversized_length_is_refused_without_reading(fresh_server, monkeypatch):
    wt = sys.modules["webhook_tools"]
    monkeypatch.setattr(wt, "_MAX_BODY", 100)
    _hook()
    mcp = _FakeMCP()
    wt.register(mcp)
    request, reads = _streamed_request("ci", [b"x" * 64] * 10,
                                       {"X-Webhook-Token": TOKEN, "Content-Length": "640"})
    assert asyncio.run(mcp.routes["/hooks/{name}"](request)).status_code == 413
    assert reads == []


# ── replay protection ───────────────────────────────────────────────────────
def test_an_exact_repeat_of_a_signed_delivery_is_acknowledged_not_delivered(http_server, fresh_server):
    _hook(secret_env="", hmac_secret_env="HOOK_HMAC")
    body = b'{"event":"push","id":1}'
    first = _post(http_server, body=body, **{"X-Hub-Signature-256": _sig(body)})
    again = _post(http_server, body=body, **{"X-Hub-Signature-256": _sig(body)})
    assert first.status_code == 202
    assert again.status_code == 200 and again.json() == {"ok": True, "duplicate": True}
    other = b'{"event":"push","id":2}'
    assert _post(http_server, body=other, **{"X-Hub-Signature-256": _sig(other)}).status_code == 202
    assert _inbox_count(fresh_server) == 2


def test_a_repeat_after_the_replay_window_is_delivered_again(http_server, fresh_server, monkeypatch):
    wt = sys.modules["webhook_tools"]
    _hook()
    clock = [1000.0]
    monkeypatch.setattr(wt, "_now", lambda: clock[0])
    assert _post(http_server, **{"X-Webhook-Token": TOKEN}).status_code == 202
    assert _post(http_server, **{"X-Webhook-Token": TOKEN}).status_code == 200
    clock[0] += wt._REPLAY_TTL + 1
    assert _post(http_server, **{"X-Webhook-Token": TOKEN}).status_code == 202


def test_the_replay_memory_is_bounded(fresh_server, monkeypatch):
    wt = sys.modules["webhook_tools"]
    monkeypatch.setattr(wt, "_REPLAY_MAX", 8)
    for i in range(50):
        assert wt._remember("ci", f"digest-{i}") is True
    assert len(wt._seen) <= 8
    assert wt._remember("ci", "digest-49") is False     # the newest is still known


# ── optional signed timestamp ───────────────────────────────────────────────
def test_a_timestamp_is_part_of_the_signature_and_must_be_fresh(http_server, fresh_server):
    _hook(secret_env="", hmac_secret_env="HOOK_HMAC")
    body = b'{"event":"build"}'
    now = str(int(time.time()))
    signed = _sig(now.encode() + b"." + body)
    ok = _post(http_server, body=body, **{"X-Webhook-Timestamp": now, "X-Hub-Signature-256": signed})
    assert ok.status_code == 202
    # the timestamp header without signing it: refused (it could be swapped freely)
    body2 = b'{"event":"build","n":2}'
    assert _post(http_server, body=body2, **{"X-Webhook-Timestamp": now,
                                             "X-Hub-Signature-256": _sig(body2)}).status_code == 401
    # signed, but older than five minutes: refused
    old = str(int(time.time()) - 600)
    body3 = b'{"event":"build","n":3}'
    assert _post(http_server, body=body3, **{"X-Webhook-Timestamp": old,
                                             "X-Hub-Signature-256": _sig(old.encode() + b"." + body3)}).status_code == 401
    assert _post(http_server, body=body3, **{"X-Webhook-Timestamp": "soon",
                                             "X-Hub-Signature-256": _sig(b"soon." + body3)}).status_code == 401


def test_senders_without_a_timestamp_keep_working(http_server, fresh_server):
    _hook(secret_env="HOOK_TOKEN", hmac_secret_env="HOOK_HMAC")
    body = b'{"event":"legacy"}'
    assert _post(http_server, body=body, **{"X-Webhook-Token": TOKEN,
                                            "X-Hub-Signature-256": _sig(body)}).status_code == 202
