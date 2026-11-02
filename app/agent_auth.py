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
import secrets

from fastmcp.server.auth.auth import AccessToken, TokenVerifier

import apikeys

# The built-in runner (runner_builtin.py) calls this server's own /mcp as the runner
# with this token. It exists only in this process's memory — never stored or shown.
INTERNAL_RUNNER_TOKEN = "rt_" + secrets.token_urlsafe(32)

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
        # Bytes, not str: compare_digest raises on non-ASCII text (→ 500, not 401).
        if hmac.compare_digest(token.encode(), INTERNAL_RUNNER_TOKEN.encode()):
            return AccessToken(token=token, client_id="runner", scopes=[],
                               claims={AGENT_CLAIM: "built-in runner"})
        if self._runner_token and hmac.compare_digest(token.encode(), self._runner_token.encode()):
            return AccessToken(token=token, client_id="runner", scopes=[], claims={})
        if not token.startswith("ak_"):
            return None
        rec = await asyncio.to_thread(apikeys.verify, token)
        if not rec:
            return None
        expires = int(rec.get("expires") or 0) or None
        if rec.get("runner"):
            # A runner key made in the WebUI: the same caller as RUNNER_TOKEN.
            return AccessToken(token=token, client_id="runner", scopes=[], expires_at=expires,
                               claims={AGENT_CLAIM: rec.get("name") or f"key-{rec['keyid']}"})
        claims = {
            "sub": rec["sub"],
            AGENT_CLAIM: rec.get("name") or f"key-{rec['keyid']}",
            KEY_CLAIM: rec["keyid"],
            SCOPES_CLAIM: list(rec.get("scopes") or []),
            "profile": rec.get("profile") or "",
        }
        return AccessToken(token=token, client_id=f"agent:{rec['keyid']}", scopes=[],
                           expires_at=expires, claims=claims)


def build(base_url: str | None = None) -> AgentTokenVerifier:
    return AgentTokenVerifier(runner_token=os.environ.get("RUNNER_TOKEN"), base_url=base_url)
