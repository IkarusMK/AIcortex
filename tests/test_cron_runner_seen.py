"""The scheduled-jobs page says whether a runner is actually there.

Before: jobs could be created in the WebUI, but only a runner on the NAS (calling
cron_due every minute) carries them out. Without one nothing happened — silently.
Now every cron_due call by the runner is noted, and the page shows when it was last
seen.
"""
import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

pytestmark = pytest.mark.oidc       # keep the WebUI's sign-in mode for the ui fixture


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
        cookie = webui._sign({"sub": sub, "name": sub, "role": role, "csrf": "tok",
                              "exp": time.time() + 600})
        return httpx.Client(base_url=base, cookies={webui.SESSION_COOKIE: cookie},
                            headers={"X-CSRF": "tok"}, timeout=30)
    return client


def _cron_due_as(sub, role):
    server, authz = sys.modules["server"], sys.modules["authz"]

    async def go():
        with authz.rest_identity(sub, role):
            res = await server.mcp.call_tool("cron_due", {})
            return "\n".join(getattr(c, "text", "") for c in res.content or [])
    return asyncio.run(go())


def test_no_runner_yet_is_shown_as_such(ui):
    runner = ui("alice", "user").get("/ui/api/cron").json()["runner"]
    assert runner == {"last_seen": "", "ok": False}


def test_a_runner_asking_for_due_jobs_is_seen(ui):
    assert _cron_due_as("boss", "admin").startswith("[")
    runner = ui("alice", "user").get("/ui/api/cron").json()["runner"]
    assert runner["ok"] is True
    seen = datetime.fromisoformat(runner["last_seen"])
    assert datetime.now(timezone.utc) - seen < timedelta(minutes=1)


def test_a_refused_caller_does_not_count_as_runner(ui):
    assert "Denied" in _cron_due_as("bob", "user")
    assert ui("alice", "user").get("/ui/api/cron").json()["runner"]["last_seen"] == ""


def test_a_runner_silent_for_minutes_is_flagged(ui):
    _cron_due_as("boss", "admin")
    cron = sys.modules["cron"]
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
    cron.RUNNER_FILE.write_text(json.dumps({"last_seen": old}), encoding="utf-8")
    runner = ui("alice", "user").get("/ui/api/cron").json()["runner"]
    assert runner == {"last_seen": old, "ok": False}
