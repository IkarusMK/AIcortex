"""rest_api helpers: the small pure pieces behind the HTTP layer.

Complements test_cov_rest_api.py (end-to-end over HTTP). Proves: the Authorization
header parser accepts only a Bearer credential, the stream/enable switches read their
values strictly, a tool result is unwrapped only when it is the plain string wrapper,
tool discovery survives FastMCP accessor differences (an empty catalog, never a
crash), visibility is hard-deny first and fails closed when the role check errors,
auditing can never break a request, and SSE framing cannot be injected through data.
"""
import asyncio
import json
import sys
from types import SimpleNamespace as NS

import pytest


@pytest.fixture
def ra(fresh_server, monkeypatch):
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    return sys.modules["rest_api"]


def _boom(*a, **k):
    raise RuntimeError("backend down")


# ── switches and request parsing ────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    (None, True), ("", True), ("1", True), ("yes", True), ("garbage", True),
    ("0", False), ("false", False), ("No", False), ("OFF", False), (" off ", False)])
def test_api_is_enabled_unless_explicitly_switched_off(ra, monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("API_ENABLED", raising=False)
    else:
        monkeypatch.setenv("API_ENABLED", value)
    assert ra._enabled() is expected


@pytest.mark.parametrize("header,expected", [
    ("Bearer k1", "k1"), ("bearer   k1  ", "k1"), ("BEARER k1", "k1"),
    ("Basic k1", ""), ("Bearer", ""), ("Bearer ", ""), ("Bearerk1xxxx", ""), ("k1", ""), ("", "")])
def test_only_a_bearer_credential_is_extracted(ra, header, expected):
    assert ra._bearer(NS(headers={"authorization": header})) == expected
    assert ra._bearer(NS(headers={})) == ""


@pytest.mark.parametrize("query,accept,expected", [
    ("1", "", True), ("true", "", True), ("YES", "", True), (" sse ", "", True),
    ("0", "", False), ("", "", False), ("false", "", False), ("no", "", False),
    ("", "text/event-stream", True), ("", "Application/JSON, TEXT/EVENT-STREAM", True),
    ("", "application/json", False), ("0", "text/event-stream", True)])
def test_stream_mode_detection(ra, query, accept, expected):
    request = NS(query_params={"stream": query}, headers={"accept": accept})
    assert ra._wants_stream(request) is expected
    assert ra._wants_stream(NS(query_params={}, headers={})) is False


def test_base_url_prefers_the_configured_public_url(ra, monkeypatch):
    request = NS(base_url="http://127.0.0.1:8787/")
    monkeypatch.delenv("BASE_URL", raising=False)
    assert ra._base_url(request) == "http://127.0.0.1:8787/api/v1"
    monkeypatch.setenv("BASE_URL", "https://agent.example.net///")
    assert ra._base_url(request) == "https://agent.example.net/api/v1"


# ── result unwrapping ───────────────────────────────────────────────────────

@pytest.mark.parametrize("result,expected", [
    (NS(structured_content={"result": "text"}), "text"),
    (NS(structured_content={"result": {"n": 1}}), {"n": 1}),
    (NS(structured_content={"result": "x", "extra": 1}), {"result": "x", "extra": 1}),
    (NS(structured_content={"a": 1}), {"a": 1}),
    (NS(structured_content={}), {}),
    (NS(structured_content=None, content=[NS(text="a"), NS(text="b")]), "a\nb"),
    (NS(structured_content=["x"], content=[NS(text="a")]), "a"),
    (NS(content=[NS(data="img"), NS(text="only text")]), "only text"),
    (NS(content=[NS(text="")]), ""),
    (NS(structured_content=None, content=[]), None),
    (NS(structured_content=None, content=None), None),
    (NS(content=[NS(data="img")]), None),
    (NS(), None)])
def test_result_payload(ra, result, expected):
    assert ra._result_payload(result) == expected


def test_sse_frames_one_event_and_cannot_be_injected_through_data(ra):
    frame = ra._sse("result", {"text": "Zoë 山田"})
    assert frame == 'event: result\ndata: {"text": "Zoë 山田"}\n\n'.encode()
    evil = ra._sse("result", {"text": "x\n\nevent: error\ndata: {}"})
    assert evil.count(b"\n\n") == 1 and evil.endswith(b"\n\n")
    assert json.loads(evil.split(b"data: ", 1)[1])["text"].startswith("x\n\nevent")


# ── tool discovery ──────────────────────────────────────────────────────────

def _names(ra, mcp):
    return sorted(t.name for t in asyncio.run(ra._all_tools(mcp)))


async def _alist(items):
    return items


def test_discovery_accepts_async_or_sync_lists_and_dicts(ra):
    t1, t2 = NS(name="a_tool"), NS(name="b_tool")
    assert _names(ra, NS(list_tools=lambda: _alist([t2, t1]))) == ["a_tool", "b_tool"]
    assert _names(ra, NS(list_tools=lambda: [t1])) == ["a_tool"]
    assert _names(ra, NS(list_tools=lambda: {"a_tool": t1, "b_tool": t2})) == ["a_tool", "b_tool"]
    assert _names(ra, NS(get_tools=lambda: _alist({"a_tool": t1}))) == ["a_tool"]


def test_discovery_falls_back_to_the_next_accessor_when_one_fails(ra):
    t = NS(name="a_tool")
    assert _names(ra, NS(list_tools=_boom, get_tools=lambda: [t])) == ["a_tool"]
    assert _names(ra, NS(list_tools=5, get_tools=lambda: [t])) == ["a_tool"]       # not callable


@pytest.mark.parametrize("attr", ["_local_provider", "local_provider"])
def test_discovery_falls_back_to_the_local_provider(ra, attr):
    t = NS(name="a_tool")
    assert _names(ra, NS(**{attr: NS(list_tools=lambda: _alist([t]))})) == ["a_tool"]
    assert _names(ra, NS(list_tools=_boom, **{attr: NS(get_tools=lambda: {"a_tool": t})})) == ["a_tool"]
    assert _names(ra, NS(**{attr: NS(list_tools=_boom, get_tools=lambda: [t])})) == ["a_tool"]


def test_discovery_yields_an_empty_catalog_instead_of_raising(ra):
    for mcp in (NS(), NS(list_tools=_boom, get_tools=_boom),
                NS(list_tools=_boom, _local_provider=NS(list_tools=_boom, get_tools=5)),
                NS(_local_provider=None, local_provider=None)):
        assert asyncio.run(ra._all_tools(mcp)) == []


# ── visibility and auditing ─────────────────────────────────────────────────

@pytest.mark.parametrize("rec,role,tool,expected", [
    ({"scopes": ["all"]}, "admin", "ping", True),
    ({"scopes": ["all"]}, "admin", "secret_set", False),             # hard deny beats scope + role
    ({"scopes": ["all"]}, "admin", "tenancy_set", False),
    ({"scopes": ["memory_read"]}, "admin", "memory_write", False),   # out of scope
    ({"scopes": ["memory"]}, "user", "memory_write", True),
    ({"scopes": ["all"]}, "user", "service_add", False),             # role denies
    ({"scopes": ["all"]}, "viewer", "memory_write", False),
    ({"scopes": ["all"]}, "viewer", "memory_read", True),
    ({"scopes": ["all"]}, "", "ping", False),                        # unknown role
    ({"scopes": ["all"]}, "root", "ping", False),
    ({}, "admin", "ping", False)])                                   # no scopes -> default deny
def test_visibility_combines_denylist_scope_and_role(ra, rec, role, tool, expected):
    assert ra._visible(rec, role, tool) is expected


def test_visibility_fails_closed_when_the_role_check_errors(ra, monkeypatch):
    monkeypatch.setattr(ra.authz, "decide", _boom)
    assert ra._visible({"scopes": ["all"]}, "admin", "ping") is False


def test_audit_records_the_key_and_never_raises(ra, monkeypatch):
    ra._audit({"sub": "alice", "keyid": "abc123def456"}, "memory_read", "deny", "out of key scope")
    ra._audit({}, "memory_read", "allow", "ok")
    lines = [json.loads(x) for x in ra.authz.AUDIT_FILE.read_text().splitlines()]
    assert lines[0] == {**lines[0], "identity": "alice", "role": "apikey", "tool": "api:memory_read",
                        "decision": "deny", "reason": "key=abc123def456: out of key scope"}
    assert (lines[1]["identity"], lines[1]["reason"]) == ("?", "key=?: ok")
    monkeypatch.setattr(ra.authz, "audit", _boom)
    ra._audit({"sub": "alice"}, "t", "allow", "ok")             # swallowed
