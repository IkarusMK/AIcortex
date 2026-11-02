"""A REST call with wrong arguments is the caller's mistake, not a server error.

Before: memory_read without `name` answered 500 "tool error", so a client could not
tell a bad request from a broken server (and retried it).
"""
import sys

import httpx
import pytest


@pytest.fixture
def api(request):
    base = request.getfixturevalue("http_server")
    key, _ = sys.modules["apikeys"].create("alice", name="script", scopes="all")
    return httpx.Client(base_url=base, headers={"Authorization": f"Bearer {key}"}, timeout=30)


def test_missing_or_wrong_arguments_answer_400(api):
    r = api.post("/api/v1/tools/memory_read", json={})
    assert r.status_code == 400, r.text
    assert r.json()["error"] == "invalid arguments" and "name" in r.json()["detail"]
    r = api.post("/api/v1/tools/memory_search", json={"query": "x", "limit": "many"})
    assert r.status_code == 400, r.text


def test_valid_arguments_still_work(api):
    r = api.post("/api/v1/tools/memory_search", json={"query": "anything"})
    assert r.status_code == 200 and r.json()["ok"] is True
