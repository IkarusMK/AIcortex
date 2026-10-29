"""Bearer tokens on /mcp for clients that can't run an OAuth login.

Self-hosted agents and SDKs (Hermes, OpenClaw, OpenAI Agents SDK, Codex, LM Studio,
Open WebUI, the Claude API connector, ...) can only send a fixed header. This
verifier accepts two kinds of static token, with or without OIDC configured:

- per-agent keys ``ak_<keyid>_<secret>`` (see apikeys.py): the token stands for
  the key's PERSON (``sub``), carries the agent name, resolves to that person's
  explicit role or "user" (never the interactive default admin) and is limited
  to the key's tool scopes;
- ``RUNNER_TOKEN``: the scheduled-job runner (client_id "runner", role "user").

Interactive apps (claude.ai, ChatGPT) keep using OAuth; MultiAuth tries the OAuth
proxy first and this verifier second.
"""
import asyncio
import hmac
import os

from fastmcp.server.auth.auth import AccessToken, TokenVerifier

import apikeys

# Claim names the rest of the server reads (authz scopes, agent attribution).
KEY_CLAIM = "aicortex_key"
SCOPES_CLAIM = "key_scopes"
AGENT_CLAIM = "agent"


class AgentTokenVerifier(TokenVerifier):
    def __init__(self, runner_token: str | None = None, base_url: str | None = None):
        super().__init__(base_url=base_url)
        self._runner_token = runner_token or ""

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token:
            return None
        if self._runner_token and hmac.compare_digest(token, self._runner_token):
            return AccessToken(token=token, client_id="runner", scopes=[], claims={})
        if not token.startswith("ak_"):
            return None
        rec = await asyncio.to_thread(apikeys.verify, token)
        if not rec:
            return None
        claims = {
            "sub": rec["sub"],
            AGENT_CLAIM: rec.get("name") or f"key-{rec['keyid']}",
            KEY_CLAIM: rec["keyid"],
            SCOPES_CLAIM: list(rec.get("scopes") or []),
        }
        expires = int(rec.get("expires") or 0) or None
        return AccessToken(token=token, client_id=f"agent:{rec['keyid']}", scopes=[],
                           expires_at=expires, claims=claims)


def build(base_url: str | None = None) -> AgentTokenVerifier:
    return AgentTokenVerifier(runner_token=os.environ.get("RUNNER_TOKEN"), base_url=base_url)
