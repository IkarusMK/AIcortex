"""PocketID-aware OIDC proxy — forward the upstream identity claims.

FastMCP's generic OIDCProxy issues a minimal-claim token (its base
`_extract_upstream_claims` returns None), so PocketID's per-user `sub`/`groups`
never reach the connector. This overrides that hook — the sanctioned FastMCP
extension point — to decode the upstream `id_token` and embed sub/email/name/
groups under the issued token's `upstream_claims`, so the authorization layer can
do per-person roles and (later) per-user data isolation.

Fail-safe everywhere: any problem → return None, i.e. behave exactly like the
stock proxy (no upstream claims, no behaviour change, no login risk).

`extract_upstream_claims()` is a plain function (no FastMCP import) so it's unit-
testable without the framework; `build_proxy()` constructs the subclass and is the
only part that imports FastMCP.
"""
import base64
import json

# Claims we forward if present in the upstream id_token. groups/oc_groups carry
# PocketID group membership (used for role mapping); sub is the stable person id.
_KEEP = ("sub", "email", "name", "preferred_username", "groups", "oc_groups")


def _decode_jwt_payload(token: str) -> dict:
    """Decode a JWT's payload segment (no signature check — the token came from
    the IdP over TLS and is only read for identity, never trusted for authz on
    its own)."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def extract_upstream_claims(idp_tokens):
    """Return the forwarded claims dict from the upstream token response, or None.
    Pure + fail-safe so it's safe to call from the proxy hook."""
    try:
        idt = idp_tokens.get("id_token") if isinstance(idp_tokens, dict) else None
        if not idt:
            return None
        claims = _decode_jwt_payload(idt)
        out = {k: claims[k] for k in _KEEP if k in claims}
        return out or None
    except Exception:
        return None


def build_proxy(**kwargs):
    """Build a PocketIDProxy (OIDCProxy subclass that forwards upstream claims).
    FastMCP is imported here only, so this module stays importable for tests."""
    from fastmcp.server.auth.oidc_proxy import OIDCProxy

    class PocketIDProxy(OIDCProxy):
        async def _extract_upstream_claims(self, idp_tokens):
            return extract_upstream_claims(idp_tokens)

    return PocketIDProxy(**kwargs)
