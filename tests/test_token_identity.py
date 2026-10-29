"""Static bearer tokens keep their identity and least privilege behind MultiAuth.

Regression (fastmcp 4): MultiAuth rewrites ``client_id`` to a JSON-qualified
string and keeps the real one in ``original_client_id``. authz compared
``client_id == "runner"``, so the RUNNER_TOKEN stopped being recognised as the
runner and fell through to the interactive default role — admin.
"""
import asyncio
import sys

from fastmcp.server.auth import MultiAuth
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier


def test_runner_token_behind_multiauth_is_runner_with_user_role(monkeypatch, request):
    request.getfixturevalue("fresh_server")
    authz = sys.modules["authz"]
    multi = MultiAuth(verifiers=[StaticTokenVerifier(
        tokens={"secret-runner": {"client_id": "runner", "scopes": []}})])
    token = asyncio.run(multi.verify_token("secret-runner"))
    assert token is not None

    import fastmcp.server.dependencies as deps
    monkeypatch.setattr(deps, "get_access_token", lambda: token)

    ident, is_runner, _ = authz._identity()
    assert is_runner is True
    assert ident == "runner"
    assert authz.effective_identity()[1] == "user"
