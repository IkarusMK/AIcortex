"""secret_list fails safe: a glitch shows the shared names, never other people's.

The code comment promised that an unresolved caller only sees shared secret names,
but when resolving the caller raised, the list fell back to "no confinement" and
showed every person's private secret names (never values).
"""
import sys

from cryptography.fernet import Fernet


def _secret_list(server):
    import asyncio
    authz = sys.modules["authz"]

    async def go():
        with authz.rest_identity("alice", "user"):
            res = await server.mcp.call_tool("secret_list", {})
            return "\n".join(getattr(c, "text", "") for c in res.content or [])
    return asyncio.run(go())


def test_a_failing_identity_lookup_hides_private_names(monkeypatch, request):
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    server = request.getfixturevalue("fresh_server")
    vault = sys.modules["secrets_store"]
    assert vault.vault_set("SHARED_TOKEN", "test-token").startswith("Stored")
    assert vault.vault_set("BOB_TOKEN", "test-token", owner="bob").startswith("Stored")

    def broken():
        raise RuntimeError("identity lookup failed")
    monkeypatch.setattr(sys.modules["tenancy"], "current_identity", broken)
    text = _secret_list(server)
    assert "SHARED_TOKEN" in text
    assert "BOB_TOKEN" not in text and "bob" not in text
