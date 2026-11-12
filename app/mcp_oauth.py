"""OAuth sign-in for upstream MCP servers — the client side of the MCP authorization spec.

Some MCP servers hand out access only through a browser login (often with 2FA) and
short-lived tokens — a service with a 2FA login, for example. ``mcp_add(auth="oauth")``
registers such a server; a person then signs in once in the WebUI and AICortex

1. finds the login: the protected-resource metadata (RFC 9728) named in the server's
   401 challenge or at its well-known address, then the authorization server's
   metadata (RFC 8414);
2. registers itself there as a public client (RFC 7591), once per provider — as a web
   client with the WebUI's callback address, or, when the provider refuses that
   redirect address, as a native client with the RFC 8252 loopback redirect
   ``LOOPBACK_REDIRECT`` ("paste" mode: the browser lands on an error page at
   127.0.0.1 and the person pastes that address into the WebUI — see ``pasted``);
3. runs the authorization-code flow with PKCE S256 and the resource indicator
   (RFC 8707), checking state, the signed-in person and the issuer (RFC 9207);
4. keeps the tokens encrypted in THAT person's own vault area and renews them with
   the refresh token on its own.

What keeps a login safe:
- It belongs to the person who signed in. A call always uses the caller's OWN login;
  there is no shared login and no admin override.
- Tokens never leave the server. The vault name is reserved (``MCP_OAUTH_*`` never
  resolves through ``get_secret``, so no service, webhook or runner can send it
  anywhere), the access token only goes to the server it was issued for (the stored
  resource must equal the server's current URL) and the refresh token only to the
  token endpoint that issued it.
- Everything uses https. Plain http is accepted only when the MCP server itself is a
  plain-http server in the LAN, and then only to non-public addresses. Every request
  from the server passes the SSRF guard.
- Without a scope named by the server (its 401 challenge or its own metadata) only the
  standard identity scopes are asked for — never everything the provider offers.
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets as pysecrets
import socket
import threading
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urlparse, urlsplit

import netguard
import secrets_store
import store

SECRET_PREFIX = secrets_store.LOGIN_PREFIX   # reserved in the vault
LOCAL_OWNER = "local-operator"   # the open local WebUI's person (no OIDC configured)
_SKEW = 60                       # renew this many seconds before the access token runs out
_TIMEOUT = 15
_PROTOCOL = "2025-06-18"
_PARAM = re.compile(r'(\w+)="([^"]*)"')
_IDENTITY_SCOPES = ("openid", "profile", "email", "offline_access")
# The redirect of the native-client fallback (RFC 8252 §7.3, loopback). Nothing listens
# there: the browser shows an error page and the person pastes its address back.
LOOPBACK_REDIRECT = "http://127.0.0.1:8765/callback"
MAX_PASTED = 4096                # characters of a pasted address
_ANSWER = ("code", "state", "iss", "error")
_REDIRECT_ERRORS = ("invalid_redirect_uri", "invalid_client_metadata")
_locks: dict = {}
_locks_guard = threading.Lock()


class OAuthError(Exception):
    """A sign-in or token problem. The message is meant for a person (and their AI);
    ``code`` is a short word the WebUI turns into its own text."""

    def __init__(self, message: str, code: str = "error", reason: str = ""):
        super().__init__(message)
        self.code = code
        self.reason = _reason(reason)      # the provider's own words, when it gave any


_REASON_MAX = 120


def _reason(text) -> str:
    """A provider's error text as one short line of plain text: line breaks become
    spaces, everything that doesn't show as itself (controls, bidi and zero-width
    characters) is dropped."""
    out = []
    for c in str(text or ""):
        if c in "\r\n\t":
            out.append(" ")
        elif c.isprintable():
            out.append(c)
    return "".join(out).strip()[:_REASON_MAX]


# ── names, owner, small checks ───────────────────────────────────────────────
def _slug(server: str) -> str:
    import mcp_gateway
    return mcp_gateway._slug(server)


def secret_name(server: str) -> str:
    """The vault name of the sign-ins for `server` (one per person, in their own area)."""
    return SECRET_PREFIX + _slug(server).upper().replace("-", "_")


def is_oauth(cfg) -> bool:
    return isinstance(cfg, dict) and str(cfg.get("auth") or "").strip().lower() == "oauth"


def _norm(url) -> str:
    return str(url or "").strip().rstrip("/")


def caller() -> str:
    """Whose login a call uses: the effective caller (a REST key's person, a cron job's
    owner, the signed-in person). Without OIDC the local WebUI and anonymous local MCP
    calls are the same single operator."""
    try:
        import authz
        ident = authz.effective_identity()[0]
    except Exception:
        ident = ""
    if ident and ident != "unknown":
        return ident
    if not (os.environ.get("OIDC_CONFIG_URL") and os.environ.get("OIDC_CLIENT_ID")):
        return LOCAL_OWNER
    return ""


def _is_lan(host: str) -> bool:
    try:
        ips = {info[4][0] for info in socket.getaddrinfo(host, None)}
        return bool(ips) and all(not ipaddress.ip_address(ip.split("%")[0]).is_global for ip in ips)
    except (OSError, ValueError):
        return False


def _safe_url(url: str, what: str, plain_ok: bool = False) -> str:
    """`url` if a login may travel over it: https — or, with `plain_ok` (the MCP server
    itself is plain http in the LAN), plain http to a non-public address. Anything
    else — javascript:, plaintext over the internet, a broken URL — is refused."""
    try:
        u = urlparse(str(url or ""))
        host = u.hostname
    except ValueError:
        host = None
    if host and (u.scheme == "https" or (plain_ok and u.scheme == "http" and _is_lan(host))):
        return str(url)
    raise OAuthError(f"The {what} must use https (got '{str(url)[:80]}').", "provider")


def _pkce(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def _lock(server: str, owner: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault((server, owner), threading.Lock())


# ── HTTP (every request passes the egress guard) ─────────────────────────────
def _guarded(url: str):
    ok, reason = netguard.check_url(url)
    if not ok:
        raise OAuthError(f"Blocked by network policy — {reason}", "blocked")
    return netguard.guard(urlparse(url).hostname or "")


def _request(method: str, url: str, plain_ok: bool, **kw):
    """One request from the server: the URL must pass ``_safe_url`` and the SSRF guard."""
    import httpx
    _safe_url(url, "provider address", plain_ok)
    try:
        with _guarded(url):
            return httpx.request(method, url, timeout=_TIMEOUT, **kw)
    except OAuthError:
        raise
    except Exception as exc:          # httpx errors, a bad URL, a broken connection
        raise OAuthError(f"Could not reach {urlparse(url).netloc}: {type(exc).__name__}",
                         "network") from exc


def _json_of(resp) -> dict:
    try:
        doc = resp.json()
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


def _get_doc(url: str, plain_ok: bool):
    r = _request("GET", url, plain_ok, headers={"Accept": "application/json",
                                                "MCP-Protocol-Version": _PROTOCOL},
                 follow_redirects=True)
    return _json_of(r) if r.status_code == 200 else None


def _post(url: str, plain_ok: bool, **kw):
    r = _request("POST", url, plain_ok, headers={"Accept": "application/json"}, **kw)
    return r.status_code, _json_of(r)


# ── discovery ────────────────────────────────────────────────────────────────
def _probe(url: str, plain_ok: bool):
    """(resource-metadata URL, scope) from the server's 401 answer to an unsigned
    request — the first discovery step of the MCP authorization spec."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": _PROTOCOL, "capabilities": {},
                       "clientInfo": {"name": "AICortex", "version": "1"}}}
    try:
        r = _request("POST", url, plain_ok, json=body,
                     headers={"Accept": "application/json, text/event-stream"})
    except OAuthError as exc:
        if exc.code in ("blocked", "provider"):
            raise
        return "", ""      # unreachable now — the well-known documents may still answer
    if r.status_code != 401:
        return "", ""
    params = dict(_PARAM.findall(r.headers.get("www-authenticate", "")))
    return params.get("resource_metadata", ""), params.get("scope", "")


def _well_known(url: str, doc: str, with_root: bool = True) -> list:
    """Well-known addresses for `url`: the path-aware one first, then (for protected-
    resource metadata, not for an issuer with a path) the one at the origin."""
    u = urlparse(url)
    origin, path = f"{u.scheme}://{u.netloc}", u.path.rstrip("/")
    if not path:
        return [f"{origin}/.well-known/{doc}"]
    out = [f"{origin}/.well-known/{doc}{path}"]
    return out + ([f"{origin}/.well-known/{doc}"] if with_root else [])


def _first_doc(urls, plain_ok: bool) -> dict:
    for url in urls:
        doc = _get_doc(url, plain_ok) if url else None
        if doc:
            return doc
    return {}


def _scope(challenge: str, prm: dict, meta: dict) -> str:
    """What to ask for: the server's own scope (its 401 challenge, else its metadata) —
    otherwise only the standard identity scopes the provider offers, never everything
    it lists. offline_access is added when offered, so the access can be renewed."""
    offered = [str(s).strip() for s in (meta.get("scopes_supported") or []) if str(s).strip()]
    own = challenge.split() or [str(s).strip() for s in (prm.get("scopes_supported") or [])
                                if str(s).strip()]
    scopes = own or [s for s in offered if s in _IDENTITY_SCOPES]
    if "offline_access" in offered and "offline_access" not in scopes:
        scopes.append("offline_access")
    return " ".join(scopes)


def discover(url: str) -> dict:
    """The server's login as one dict: resource, issuer, the three endpoints, scope,
    whether plain http is acceptable (``plain_ok``) and whether ``iss`` must come back."""
    plain_ok = urlparse(_safe_url(url, "MCP server address", plain_ok=True)).scheme == "http"
    hinted, challenge_scope = _probe(url, plain_ok)
    prm = _first_doc([hinted, *_well_known(url, "oauth-protected-resource")], plain_ok)
    if not prm:
        raise OAuthError("The server publishes no OAuth metadata (RFC 9728), so it offers no "
                         "OAuth sign-in.", "provider")
    resource = str(prm.get("resource") or "")
    if _norm(resource) != _norm(url):
        raise OAuthError(f"The server's metadata names a different resource ('{resource[:80]}') "
                         f"than its address — refused.", "provider")
    servers = prm.get("authorization_servers")
    issuer = str(servers[0]) if isinstance(servers, list) and servers else ""
    if not issuer:
        raise OAuthError("The server's metadata names no authorization server.", "provider")
    u = urlparse(issuer)
    meta = _first_doc(_well_known(issuer, "oauth-authorization-server", False)
                      + _well_known(issuer, "openid-configuration", False)
                      + ([f"{_norm(issuer)}/.well-known/openid-configuration"] if u.path.strip("/") else []),
                      plain_ok)
    if not meta:
        raise OAuthError(f"The authorization server '{issuer[:80]}' publishes no metadata "
                         f"(RFC 8414).", "provider")
    return _login(resource, issuer, meta, _scope(challenge_scope, prm, meta), plain_ok)


def _login(resource: str, issuer: str, meta: dict, scope: str, plain_ok: bool) -> dict:
    if _norm(meta.get("issuer")) != _norm(issuer):
        raise OAuthError("The authorization server's metadata names another issuer — refused.",
                         "provider")
    if "S256" not in (meta.get("code_challenge_methods_supported") or []):
        raise OAuthError("The authorization server does not offer PKCE S256 — refused.", "provider")
    if not meta.get("registration_endpoint"):
        raise OAuthError("The authorization server offers no dynamic client registration, so "
                         "AICortex cannot register itself there.", "provider")
    return {"resource": resource, "issuer": issuer, "scope": scope, "plain_ok": plain_ok,
            "iss_required": meta.get("authorization_response_iss_parameter_supported") is True,
            "authorization_endpoint": _safe_url(meta.get("authorization_endpoint"),
                                                "provider's login page", plain_ok),
            "token_endpoint": _safe_url(meta.get("token_endpoint"),
                                        "provider's token endpoint", plain_ok),
            "registration_endpoint": _safe_url(meta.get("registration_endpoint"),
                                               "provider's registration endpoint", plain_ok)}


# ── client registration ──────────────────────────────────────────────────────
def _save_client(server: str, client: dict) -> None:
    import mcp_gateway
    path = mcp_gateway._cfg_path(server)
    with store.locked(path):
        if not path.exists():
            return            # deleted in the meantime — do not bring it back
        data = store.read_json(path, {})
        if isinstance(data, dict):
            store.write_json(path, {**data, "oauth_client": client})


def _usable(known: dict, login: dict, web_redirect: str) -> bool:
    """Whether the stored client still fits: same provider, same server, and the redirect
    it was registered with is still the one its mode uses. A record from before the
    native fallback (no mode) is a web client."""
    if not (known.get("client_id") and known.get("issuer") == login["issuer"]
            and known.get("resource") == login["resource"]):
        return False
    want = LOOPBACK_REDIRECT if known.get("mode") == "native" else web_redirect
    return known.get("redirect_uri") == want


def _register(login: dict, redirect_uri: str, native: bool):
    """One registration attempt: (HTTP status, the answer, the provider's reason)."""
    body = {"client_name": "AICortex", "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none"}
    if native:
        body["application_type"] = "native"
    if login["scope"]:
        body["scope"] = login["scope"]
    r = _request("POST", login["registration_endpoint"], login["plain_ok"],
                 headers={"Accept": "application/json"}, json=body)
    doc = _json_of(r)
    why = doc.get("error_description") or doc.get("message") or doc.get("error") or ""
    if not doc and r.status_code >= 400 and "html" not in r.headers.get("content-type", ""):
        why = r.text                      # some providers answer in plain text
    return r.status_code, doc, " ".join(str(why).split())[:160]


def _refuses_redirect(status: int, doc: dict, why: str) -> bool:
    """Whether a refused web registration is about its redirect address (RFC 7591
    invalid_redirect_uri / invalid_client_metadata, or a message saying so)."""
    return status == 400 and (doc.get("error") in _REDIRECT_ERRORS or "redirect" in why.lower())


def _http(status: int, why: str) -> str:
    return f"HTTP {status}{': ' + why if why else ''}"


def _client(server: str, cfg: dict, login: dict, web_redirect: str) -> dict:
    """AICortex's client at this provider: {"client_id", "redirect_uri", "mode"}.

    Registered once — as a web client with `web_redirect`; when the provider refuses that
    redirect address, once more as a native client with the loopback redirect (mode
    "native") — and again only when the provider, the server or the redirect its mode
    uses changed. The mode and the redirect actually registered are stored with it."""
    known = cfg.get("oauth_client") if isinstance(cfg.get("oauth_client"), dict) else {}
    if _usable(known, login, web_redirect):
        return {"client_id": str(known["client_id"]), "redirect_uri": known["redirect_uri"],
                "mode": "native" if known.get("mode") == "native" else "web"}
    mode, redirect = "web", web_redirect
    status, doc, why = _register(login, redirect, native=False)
    if status not in (200, 201) and _refuses_redirect(status, doc, why):
        web_refusal = _http(status, why)
        mode, redirect = "native", LOOPBACK_REDIRECT
        status, doc, why = _register(login, redirect, native=True)
        if status not in (200, 201) or not doc.get("client_id"):
            raise OAuthError(f"The provider refused AICortex as a web client ({web_refusal}) "
                             f"and as a desktop client with the loopback address "
                             f"{LOOPBACK_REDIRECT} ({_http(status, why)}), so there is no way "
                             f"to sign in there.", "provider")
    client_id = str(doc.get("client_id") or "")
    if status not in (200, 201) or not client_id:
        raise OAuthError(f"The provider refused the client registration ({_http(status, why)}).",
                         "provider")
    _save_client(server, {"client_id": client_id, "issuer": login["issuer"],
                          "resource": login["resource"], "redirect_uri": redirect, "mode": mode,
                          "registered": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    return {"client_id": client_id, "redirect_uri": redirect, "mode": mode}


# ── sign-in ──────────────────────────────────────────────────────────────────
def begin(server: str, redirect_uri: str, owner: str):
    """Start a sign-in for `owner`: (the login URL for their browser, the pending state
    to keep — signed — until the provider's answer comes back).

    `redirect_uri` is the WebUI's callback. ``pending["mode"]`` says where the answer
    goes: "web" — to that callback; "paste" — the provider only took the native client,
    so the browser lands on the loopback address and the person pastes it (``pasted``)."""
    import mcp_gateway
    cfg = mcp_gateway._load(server)
    if not is_oauth(cfg):
        raise OAuthError(f"'{server}' does not use OAuth sign-in.", "config")
    if not owner:
        raise OAuthError("Cannot tell who is signing in.", "person")
    login = discover(_norm(cfg.get("url")))
    client = _client(server, cfg, login, redirect_uri)
    redirect, client_id = client["redirect_uri"], client["client_id"]
    state, verifier = pysecrets.token_urlsafe(24), pysecrets.token_urlsafe(48)
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect,
              "state": state, "code_challenge": _pkce(verifier),
              "code_challenge_method": "S256", "resource": login["resource"]}
    if login["scope"]:
        params["scope"] = login["scope"]
    sep = "&" if urlparse(login["authorization_endpoint"]).query else "?"
    pending = {"server": _slug(server), "sub": owner, "state": state, "verifier": verifier,
               "redirect_uri": redirect, "client_id": client_id, "issuer": login["issuer"],
               "resource": login["resource"], "token_endpoint": login["token_endpoint"],
               "plain_ok": login["plain_ok"], "iss_required": login["iss_required"],
               "mode": "paste" if client["mode"] == "native" else "web"}
    return f"{login['authorization_endpoint']}{sep}{urlencode(params)}", pending


def pasted(pending: dict, address) -> dict:
    """The provider's answer (code, state, iss, error) from the address the browser
    landed on in paste mode. Taken only when it is the loopback redirect this sign-in was
    started with — same scheme, host, port and path, no user info — with at most one of
    each answer parameter. The address is only parsed, never fetched; ``finish`` then
    checks state, issuer and the code like any other answer, and the code is useless
    without the PKCE verifier, which never leaves AICortex."""
    want = str(pending.get("redirect_uri") or "")
    if pending.get("mode") != "paste" or not want:
        raise OAuthError("This sign-in does not take a pasted address — please start again.",
                         "state")
    hint = f"Paste the whole address your browser shows after signing in — it starts with {want}."
    text = address.strip() if isinstance(address, str) else ""
    if not text or len(text) > MAX_PASTED:
        raise OAuthError(hint, "address")
    try:
        got, exp = urlsplit(text), urlsplit(want)
        query = parse_qs(got.query, keep_blank_values=True, max_num_fields=32)
    except ValueError:
        raise OAuthError(hint, "address") from None
    if ((got.scheme, got.netloc, got.path) != (exp.scheme, exp.netloc, exp.path)
            or any(len(query.get(k, ())) > 1 for k in _ANSWER)):
        raise OAuthError(hint, "address")
    answer = {k: query[k][0] for k in _ANSWER if k in query}
    if not answer.get("code") and not answer.get("error"):
        raise OAuthError(f"The address holds no sign-in code. {hint}", "address")
    return answer


def finish(pending: dict, params: dict) -> None:
    """Complete a sign-in from the provider's redirect (`params` = its query) and store
    the tokens in the vault area of the person who started it."""
    state = str(params.get("state") or "")
    if not state or not hmac.compare_digest(state.encode(), str(pending.get("state", "")).encode()):
        raise OAuthError("The sign-in could not be matched (state) — please start again.", "state")
    if params.get("error"):
        raise OAuthError(f"The provider did not grant access ({_reason(params['error'])[:60]}).",
                         "denied", reason=params["error"])
    iss = params.get("iss")
    if (iss or pending.get("iss_required")) and _norm(iss) != _norm(pending.get("issuer")):
        raise OAuthError("The answer came from another issuer (or without one) — refused.",
                         "issuer")
    code = str(params.get("code") or "")
    if not code:
        raise OAuthError("No authorization code came back — please start again.", "state")
    status, doc = _post(pending["token_endpoint"], bool(pending.get("plain_ok")), data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": pending["redirect_uri"], "client_id": pending["client_id"],
        "code_verifier": pending["verifier"], "resource": pending["resource"]})
    record = _tokens(status, doc, pending)
    with _lock(pending["server"], pending["sub"]):     # not over a renewal running right now
        _store(pending["server"], pending["sub"], record)


def _tokens(status: int, doc: dict, bound: dict, old_refresh: str = "") -> dict:
    """A vault record from a token answer, bound to where it may be used."""
    access = str(doc.get("access_token") or "")
    if status != 200 or not access:
        why = doc.get("error_description") or doc.get("error") or f"HTTP {status}"
        raise OAuthError(f"The provider did not issue a token ({_reason(why)}).", "exchange",
                         reason=why)
    if str(doc.get("token_type") or "Bearer").lower() != "bearer":
        raise OAuthError("The provider issued a token type other than Bearer.", "exchange")
    try:
        expires_at = int(time.time()) + int(doc["expires_in"])
    except (KeyError, TypeError, ValueError, OverflowError):
        expires_at = None
    return {"access_token": access, "refresh_token": str(doc.get("refresh_token") or old_refresh),
            "expires_at": expires_at, "scope": str(doc.get("scope") or ""),
            "resource": bound["resource"], "issuer": bound["issuer"],
            "client_id": bound["client_id"], "token_endpoint": bound["token_endpoint"],
            "plain_ok": bool(bound.get("plain_ok"))}


def _store(server: str, owner: str, record: dict) -> None:
    msg = secrets_store.vault_set(secret_name(server), json.dumps(record), owner=owner,
                                  description=f"OAuth sign-in for MCP server {_slug(server)} "
                                              f"(managed by AICortex)", by=owner,
                                  reserved_ok=True)
    if not msg.startswith("Stored"):
        raise OAuthError(msg, "vault")


def _record(server: str, owner: str):
    raw = secrets_store.get_owned(secret_name(server), owner) if owner else None
    try:
        rec = json.loads(raw) if raw else None
    except ValueError:
        return None
    return rec if isinstance(rec, dict) and rec.get("access_token") else None


# ── using a login ────────────────────────────────────────────────────────────
def _again(server: str) -> str:
    return f"please sign in again (AICortex WebUI → Services → {_slug(server)} → Sign in)"


def bearer(server: str, cfg: dict, owner: str, force: bool = False) -> str:
    """`owner`'s access token for `server`, renewed first when it ran out — or when
    `force`, after the server rejected it. Raises OAuthError when there is none."""
    if not owner:
        raise OAuthError("Cannot tell who is calling, so no sign-in can be used.", "person")
    with _lock(_slug(server), owner):
        rec = _record(server, owner)
        if not rec:
            raise OAuthError(f"Not signed in to '{_slug(server)}' as {owner}. Sign in: AICortex "
                             f"WebUI → Services → {_slug(server)} → Sign in.", "signin")
        _safe_url(cfg.get("url"), "MCP server address", plain_ok=True)
        if _norm(rec.get("resource")) != _norm(cfg.get("url")):
            raise OAuthError(f"The sign-in for '{_slug(server)}' was made for another address — "
                             f"{_again(server)}.", "signin")
        expires_at = rec.get("expires_at")
        if force or (isinstance(expires_at, (int, float)) and expires_at - _SKEW <= time.time()):
            rec = _renew(server, owner, rec)
        return str(rec["access_token"])


def _renew(server: str, owner: str, rec: dict) -> dict:
    refresh = str(rec.get("refresh_token") or "")
    if not refresh:
        raise OAuthError(f"The sign-in for '{_slug(server)}' has expired — {_again(server)}.",
                         "signin")
    status, doc = _post(str(rec.get("token_endpoint") or ""), bool(rec.get("plain_ok")), data={
        "grant_type": "refresh_token", "refresh_token": refresh,
        "client_id": rec.get("client_id", ""), "resource": rec.get("resource", "")})
    if status != 200 or not doc.get("access_token"):
        raise OAuthError(f"The provider no longer accepts the sign-in for '{_slug(server)}' — "
                         f"{_again(server)}.", "signin")
    new = _tokens(status, doc, rec, old_refresh=refresh)
    try:
        _store(server, owner, new)
    except OAuthError:
        pass      # vault not writable right now: this call still gets the fresh token
    return new


def status(server: str, cfg: dict, owner: str) -> dict:
    """{"signed_in", "expires_at" (of the current access token), "renews"} for `owner` —
    never a token."""
    rec = _record(server, owner)
    ok = bool(rec) and _norm(rec.get("resource")) == _norm(cfg.get("url"))
    return {"signed_in": ok, "expires_at": rec.get("expires_at") if ok else None,
            "renews": bool(ok and rec.get("refresh_token"))}


def sign_out(server: str, owner: str) -> bool:
    return bool(owner) and secrets_store.vault_delete(secret_name(server), owner).startswith("Deleted")


def forget(server: str) -> int:
    """Remove everyone's sign-in for `server` (when the server is deleted)."""
    return secrets_store.vault_purge(secret_name(server))
