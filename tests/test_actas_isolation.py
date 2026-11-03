"""Act-as must stay with the caller that started it.

Regression: the binding used to be process-global, so while the cron runner acted
as a job owner, EVERY concurrent client (another person, another agent) was gated
and data-scoped as that owner too — a cross-tenant leak.
"""
import asyncio
import sys

from cryptography.fernet import Fernet
from fastmcp import Client


def test_act_as_binding_does_not_leak_to_other_callers(monkeypatch, request):
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    server = request.getfixturevalue("fresh_server")
    authz = sys.modules["authz"]
    import actas  # loaded lazily by authz/cron; same module object they use

    who = {"id": "runner", "runner": True}
    monkeypatch.setattr(authz, "_identity", lambda: (who["id"], who["runner"], {}))

    token = actas.issue("job1", "alice")

    async def begin():
        async with Client(server.mcp) as client:
            res = await client.call_tool("act_as_begin", {"act_as_token": token, "job_id": "job1"})
            return res.content[0].text

    assert "Acting as alice" in asyncio.run(begin())

    # The runner itself now acts as the job owner ...
    assert authz.effective_identity()[0] == "alice"

    # ... but a different caller hitting the server at the same time stays itself.
    who.update(id="bob", runner=False)
    assert authz.effective_identity()[0] == "bob"


def test_an_own_runners_act_as_run_leaves_the_built_in_runner_alone(monkeypatch, request):
    """The built-in runner and an own runner (RUNNER_TOKEN) are both identity "runner".
    Keyed by identity alone, the own runner's act-as run blocked the built-in runner's
    cron_due — and would have confined its calls to the other job's owner."""
    from test_runner_setup import _call
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUNNER_TOKEN", "runner-secret-123")
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    url = request.getfixturevalue("http_server")
    import actas
    internal = sys.modules["agent_auth"].INTERNAL_RUNNER_TOKEN
    token = actas.issue("job1", "alice")
    assert "Acting as alice" in _call(url, "runner-secret-123", "act_as_begin",
                                      {"act_as_token": token, "job_id": "job1"})
    assert _call(url, internal, "cron_due").startswith("[")
    assert "act-as run is active" in _call(url, "runner-secret-123", "cron_due")
