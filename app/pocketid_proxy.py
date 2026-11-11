"""PocketID-aware OIDC proxy — forward the upstream identity claims.

FastMCP's generic OIDCProxy issues a minimal-claim token (its base
`_extract_upstream_claims` returns None), so PocketID's per-user `sub`/`groups`
never reach the connector. This overrides that hook — the sanctioned FastMCP
extension point — to decode the upstream `id_token` and embed sub/email/name/
groups under the issued token's `upstream_claims`, so the authorization layer can
do per-person roles and (later) per-user data isolation.

Fail-safe everywhere: any problem → return None, i.e. behave exactly like the
stock proxy (no upstream claims, no behaviour change, no login risk).

It also marks every token it accepts with the APP that holds it (``APP_CLAIM``): the
redirect host of the OAuth client that app registered here — claude.ai, chatgpt.com,
"localhost" for desktop/CLI apps. Who may use a person's sign-in to an upstream MCP
server is decided per app (mcp_access), so the mark comes only from the client record
in the proxy's own store, never from the token's other claims; when the app can't be
told, the mark is removed and the app counts as unknown.

`extract_upstream_claims()` is a plain function (no FastMCP import) so it's unit-
testable without the framework; `build_proxy()` constructs the subclass and is the
only part that imports FastMCP.
"""
import base64
import ipaddress
import json
from urllib.parse import urlsplit

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


APP_CLAIM = "aicortex_app"
_LOOPBACK = "localhost"
_apps: dict = {}           # client_id -> {"host", "name"} (a client's redirects don't change)
_APPS_MAX = 512


def _host(uri) -> str:
    try:
        host = (urlsplit(str(uri)).hostname or "").lower()
    except ValueError:
        return ""
    if host == "localhost":
        return _LOOPBACK
    try:
        return _LOOPBACK if ipaddress.ip_address(host).is_loopback else host
    except ValueError:
        return host


def app_of(redirect_uris) -> str:
    """The app a client stands for: the one host all its redirect addresses share
    (loopback addresses are "localhost"), or "" when there is none or more than one."""
    hosts = {_host(u) for u in redirect_uris or []}
    return hosts.pop() if len(hosts) == 1 and "" not in hosts else ""


async def annotate(proxy, token: str, found):
    """`found` (the token the proxy accepted) marked with the app of the client the
    token was issued to — or with the mark removed when that can't be told."""
    claims = dict(getattr(found, "claims", None) or {})
    claims.pop(APP_CLAIM, None)
    try:
        client_id = str(proxy.jwt_issuer.verify_token(token).get("client_id") or "")
        app = _apps.get(client_id)
        if app is None and client_id:
            client = await proxy.get_client(client_id)
            host = app_of(getattr(client, "redirect_uris", None) or []) if client else ""
            if host:
                app = {"host": host, "name": str(getattr(client, "client_name", "") or "")[:80]}
                if len(_apps) >= _APPS_MAX:
                    _apps.clear()
                _apps[client_id] = app
        if app:
            claims[APP_CLAIM] = dict(app)
    except Exception:
        pass                     # unknown app: a sign-in to an upstream server stays closed
    return found.model_copy(update={"claims": claims}, deep=True)


def build_proxy(**kwargs):
    """Build a PocketIDProxy (OIDCProxy subclass that forwards upstream claims).
    FastMCP is imported here only, so this module stays importable for tests."""
    from fastmcp.server.auth.oidc_proxy import OIDCProxy

    class PocketIDProxy(OIDCProxy):
        async def _extract_upstream_claims(self, idp_tokens):
            return extract_upstream_claims(idp_tokens)

        async def load_access_token(self, token: str):
            found = await super().load_access_token(token)
            return await annotate(self, token, found) if found is not None else None

    return PocketIDProxy(**kwargs)
