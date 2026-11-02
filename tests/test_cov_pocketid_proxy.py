"""pocketid_proxy: the upstream identity is forwarded, and only the safe part of it.

Proves that ``extract_upstream_claims`` decodes the IdP ``id_token`` payload (any
base64url padding length, non-ASCII names), forwards ONLY the whitelisted identity
claims (a forged ``role``/``upstream_claims``/``admin`` claim is dropped), and
never raises: every malformed input degrades to ``None`` so login behaves like the
stock proxy. Also pins the FastMCP contract that makes the override effective:
``build_proxy`` returns an ``OIDCProxy`` subclass whose async hook delegates to the
extractor, and the hook name still exists on the installed FastMCP base class.
"""
import asyncio
import base64
import inspect
import json

import pytest

pytest.importorskip("fastmcp", reason="fastmcp not installed in this environment")


def _b64(raw: bytes, pad: bool = False) -> str:
    out = base64.urlsafe_b64encode(raw).decode("ascii")
    return out if pad else out.rstrip("=")


def _jwt(payload, pad=False) -> str:
    """An unsigned-looking JWT with the given payload (signature is irrelevant here)."""
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    return f"{_b64(b'{}', pad)}.{_b64(body, pad)}.c2ln"


@pytest.fixture
def pp():
    import pocketid_proxy
    return pocketid_proxy


# ── extract_upstream_claims: the happy path ─────────────────────────────────

def test_forwards_the_identity_claims_of_the_id_token(pp):
    claims = {"sub": "user-1234", "email": "alice@example.com", "name": "Alice Example",
              "preferred_username": "alice", "groups": ["family", "admins"],
              "oc_groups": ["admins"]}
    assert pp.extract_upstream_claims({"id_token": _jwt(claims)}) == claims


def test_only_whitelisted_claims_are_forwarded(pp):
    token = _jwt({"sub": "user-1", "iss": "https://idp.example.com", "aud": "client",
                  "exp": 1, "nonce": "n", "picture": "https://example.com/a.png",
                  "email_verified": True, "name": "Alice"})
    assert pp.extract_upstream_claims({"id_token": token}) == {"sub": "user-1", "name": "Alice"}


@pytest.mark.parametrize("forged", [
    {"role": "admin"}, {"admin": True}, {"is_admin": True}, {"roles": ["admin"]},
    {"upstream_claims": {"sub": "root"}}, {"aicortex_key": True}, {"key_scopes": ["all"]},
])
def test_forged_privilege_claims_never_reach_the_issued_token(pp, forged):
    token = _jwt({"sub": "user-1", **forged})
    assert pp.extract_upstream_claims({"id_token": token}) == {"sub": "user-1"}


@pytest.mark.parametrize("name_len", [1, 2, 3, 4, 5, 6, 7, 8])
def test_every_base64_padding_length_decodes(pp, name_len):
    claims = {"sub": "s", "name": "x" * name_len}
    assert pp.extract_upstream_claims({"id_token": _jwt(claims)}) == claims
    assert pp.extract_upstream_claims({"id_token": _jwt(claims, pad=True)}) == claims


def test_non_ascii_names_survive(pp):
    claims = {"sub": "s", "name": "Zoë Müller 山田 🚀"}
    assert pp.extract_upstream_claims({"id_token": _jwt(claims)}) == claims


def test_falsy_claim_values_are_kept_not_dropped(pp):
    got = pp.extract_upstream_claims({"id_token": _jwt({"sub": "s", "groups": [], "name": ""})})
    assert got == {"sub": "s", "groups": [], "name": ""}


def test_other_token_response_fields_are_ignored(pp):
    tokens = {"access_token": "test-token", "refresh_token": "test-refresh",
              "id_token": _jwt({"sub": "s"}), "expires_in": 3600}
    assert pp.extract_upstream_claims(tokens) == {"sub": "s"}


# ── extract_upstream_claims: fail-safe ──────────────────────────────────────

@pytest.mark.parametrize("tokens", [
    None, "id_token", 42, [], ["id_token"], {}, {"access_token": "test-token"},
    {"id_token": None}, {"id_token": ""}, {"id_token": 0},
])
def test_missing_or_wrongly_typed_token_response_gives_none(pp, tokens):
    assert pp.extract_upstream_claims(tokens) is None


@pytest.mark.parametrize("bad", [
    "not-a-jwt",                                    # no dots -> IndexError
    "a.b",                                          # payload is not valid JSON
    "a.!!!!.c",                                     # not base64
    "a..c",                                         # empty payload
    "a.e30.c",                                      # payload {} -> nothing to forward
    "a.W10.c",                                      # payload [] (not an object)
    "a.Im5vcGUi.c",                                 # payload "nope" (a string)
    "a.InN1YiI.c",                                  # payload "sub": `in` is True, indexing fails
    "a.MTIz.c",                                     # payload 123
    "a.bnVsbA.c",                                   # payload null
    "a.éé.c",                             # non-ASCII payload segment
])
def test_malformed_id_token_degrades_to_none_without_raising(pp, bad):
    assert pp.extract_upstream_claims({"id_token": bad}) is None


def test_payload_without_any_forwardable_claim_gives_none(pp):
    token = _jwt({"iss": "https://idp.example.com", "aud": "client", "exp": 1})
    assert pp.extract_upstream_claims({"id_token": token}) is None


def test_non_utf8_payload_gives_none(pp):
    payload = _b64(b"\xff\xfe\x00")
    assert pp.extract_upstream_claims({"id_token": f"a.{payload}.c"}) is None


def test_non_string_id_token_gives_none(pp):
    assert pp.extract_upstream_claims({"id_token": 12345}) is None
    assert pp.extract_upstream_claims({"id_token": ["a", "b", "c"]}) is None


def test_decode_helper_reads_the_middle_segment_only(pp):
    assert pp._decode_jwt_payload(_jwt({"sub": "s"})) == {"sub": "s"}
    with pytest.raises(IndexError):
        pp._decode_jwt_payload("no-dots-here")


# ── build_proxy: the FastMCP wiring ─────────────────────────────────────────

@pytest.fixture
def proxy(pp, fake_idp):
    return pp.build_proxy(
        config_url=f"{fake_idp}/.well-known/openid-configuration",
        client_id="test-client", client_secret="test-secret",
        base_url="https://connector.example.com", jwt_signing_key="0" * 64)


def test_build_proxy_returns_an_oidc_proxy_subclass_with_the_override(proxy):
    from fastmcp.server.auth.oidc_proxy import OIDCProxy
    cls = type(proxy)
    assert isinstance(proxy, OIDCProxy) and cls is not OIDCProxy
    assert "_extract_upstream_claims" in vars(cls)
    assert inspect.iscoroutinefunction(cls._extract_upstream_claims)


def test_proxy_hook_forwards_identity_claims(proxy):
    tokens = {"id_token": _jwt({"sub": "user-1", "groups": ["family"], "role": "admin"})}
    got = asyncio.run(proxy._extract_upstream_claims(tokens))
    assert got == {"sub": "user-1", "groups": ["family"]}


def test_proxy_hook_is_fail_safe_like_the_stock_proxy(proxy):
    assert asyncio.run(proxy._extract_upstream_claims({"id_token": "garbage"})) is None
    assert asyncio.run(proxy._extract_upstream_claims({})) is None


def test_fastmcp_base_class_still_exposes_the_hook_we_override():
    from fastmcp.server.auth.oauth_proxy import OAuthProxy
    base = OAuthProxy._extract_upstream_claims
    assert inspect.iscoroutinefunction(base)
    assert list(inspect.signature(base).parameters) == ["self", "idp_tokens"]


def test_build_proxy_surfaces_construction_errors(pp, fake_idp):
    # server.py wraps build_proxy in try/except and falls back to the stock proxy;
    # that only works if a bad configuration actually raises here.
    with pytest.raises(Exception):
        pp.build_proxy(config_url=f"{fake_idp}/no-such-discovery-document",
                       client_id="c", client_secret="s", base_url="https://connector.example.com")
