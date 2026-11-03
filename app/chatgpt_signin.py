"""Sign in with ChatGPT for the runner: a ChatGPT plan (Plus/Pro) as its model access.

OpenAI's "Sign in with ChatGPT" lets open-source, self-hosted apps use a person's
ChatGPT plan (developers.openai.com/siwc/token-sharing-open-source). An admin signs
in once in the browser: authorization code + PKCE, first as a dynamic registration
that returns this installation's own client id. OpenAI redirects to
http://127.0.0.1:1455/auth/callback — the computer running the browser, not the NAS —
so the WebUI asks for that address from the address bar and the exchange happens here.

The tokens are kept encrypted in the vault as RUNNER_CHATGPT_SESSION (an internal
name no service can read) and never leave AICortex except to OpenAI. The access token
lasts an hour and is refreshed here; every refresh returns a new 30-day refresh
token, so keep_alive() renews an idle session before it runs out.
"""
import base64
import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx

import store

ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{ISSUER}/api/accounts/oauth/token"
REVOKE_URL = f"{ISSUER}/api/accounts/oauth/revoke"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
RESOURCE = "https://api.openai.com/v1"
MODELS_URL = f"{RESOURCE}/models"
REDIRECT_URI = "http://127.0.0.1:1455/auth/callback"
DYNAMIC_CLIENT = "dynamic_agent_client"
AGENT_NAME = "AICortex"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = f"openid profile email offline_access resource.invoke {PLAN_SCOPE}"
SESSION_SECRET = "RUNNER_CHATGPT_SESSION"
CLIENT_FILE = Path(os.environ.get("CRON_DIR", "/data/cron")) / "chatgpt_client.json"
# Refresh errors after which the refresh token can't be used again (sign in again).
UNUSABLE = frozenset({"invalid_grant", "invalid_refresh_token", "token_expired",
                      "refresh_token_expired", "refresh_token_invalidated",
                      "refresh_token_reused"})
EARLY_REFRESH_SECONDS = 120          # refresh this long before the access token expires
KEEP_ALIVE_SECONDS = 3 * 86400       # renew an idle session well inside the 30 days
KEEP_ALIVE_RETRY_SECONDS = 3600      # after a failed keep-alive, wait this long
PENDING_SECONDS = 900                # a sign-in link is good for 15 minutes
MAX_CALLBACK = 4096
HTTP_TIMEOUT = 30
CLIENT_ID = re.compile(r"[A-Za-z0-9_.-]{1,200}")

_lock = threading.RLock()            # the session: one refresh at a time
_pending_lock = threading.Lock()     # sign-in links only, never held across a request
_pending: dict[str, dict] = {}
_last_keep_alive_try = 0.0
# A refreshed session the vault refused to store: OpenAI has already rotated the
# refresh token, so it is kept here (and stored with the next save) instead of lost.
_unsaved: dict | None = None


class SignInNeeded(RuntimeError):
    """No usable ChatGPT session — an admin has to sign in (again) in the WebUI."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ── HTTP (test seams) ────────────────────────────────────────────────────────
def _answer(r: httpx.Response) -> tuple[int, dict]:
    try:
        body = r.json()
    except ValueError:
        body = {}
    return r.status_code, body if isinstance(body, dict) else {}


def _post_form(url: str, data: dict) -> tuple[int, dict]:
    """(status, JSON body). RuntimeError when OpenAI can't be reached."""
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as http:
            return _answer(http.post(url, data=data, headers={"Accept": "application/json"}))
    except httpx.HTTPError as exc:
        raise RuntimeError(f"OpenAI could not be reached ({type(exc).__name__}).") from exc


def _get_json(url: str, headers: dict | None = None) -> tuple[int, dict]:
    """(status, JSON body). RuntimeError when OpenAI can't be reached."""
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as http:
            return _answer(http.get(url, headers={"Accept": "application/json", **(headers or {})}))
    except httpx.HTTPError as exc:
        raise RuntimeError(f"OpenAI could not be reached ({type(exc).__name__}).") from exc


def _error_code(body: dict) -> str:
    err = body.get("error")
    if isinstance(err, dict):
        return str(err.get("code") or err.get("type") or "")
    return str(err or body.get("code") or "")


# ── stored state ─────────────────────────────────────────────────────────────
def _client() -> dict:
    """This installation's host id (made once) and the client id OpenAI issued."""
    data = store.read_json(CLIENT_FILE, {})
    data = data if isinstance(data, dict) else {}
    if not data.get("ext_agent_host_id"):
        data = dict(data, ext_agent_host_id=f"urn:uuid:{uuid.uuid4()}")
        store.write_json(CLIENT_FILE, data)
    return data


def _remember_client(client_id: str) -> None:
    store.write_json(CLIENT_FILE, dict(_client(), client_id=client_id))


def _session() -> dict:
    if _unsaved:
        return dict(_unsaved)
    import secrets_store
    raw = secrets_store._read_all().get(SESSION_SECRET) or ""
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _save_session(sess: dict) -> None:
    global _unsaved
    import secrets_store
    msg = secrets_store.vault_set(SESSION_SECRET, json.dumps(sess),
                                  description="ChatGPT sign-in for the runner", by="runner")
    if not msg.startswith("Stored"):
        raise RuntimeError(msg)
    _unsaved = None


def _drop_session() -> None:
    global _unsaved
    import secrets_store
    _unsaved = None
    secrets_store.vault_delete(SESSION_SECRET)


def _number(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _epoch(value) -> float:
    """earliest_refresh_at as seconds — a number or an ISO timestamp; 0 if unknown."""
    number = _number(value, -1.0)
    if number >= 0:
        return number
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _with_tokens(sess: dict, body: dict) -> dict:
    """The session with a token response applied — access token, expiry, scopes and
    the rotated refresh token replaced together."""
    now = time.time()
    scope = body.get("scope")
    scopes = scope.split() if isinstance(scope, str) else sess.get("scopes", [])
    return dict(sess, access_token=body["access_token"],
                refresh_token=body.get("refresh_token") or sess.get("refresh_token", ""),
                id_token=body.get("id_token") or sess.get("id_token", ""),
                expires_at=now + (_number(body.get("expires_in")) or 3600),
                earliest_refresh_at=_epoch(body.get("earliest_refresh_at")),
                scopes=sorted(scopes), saved_at=now, problem="")


# ── ID token ─────────────────────────────────────────────────────────────────
def _verify_id_token(token: str, client_id: str, nonce: str) -> dict:
    """The ID token's claims after checking signature (OpenAI's JWKS), issuer,
    audience, expiry and nonce. Raises ValueError."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    parts = token.split(".") if isinstance(token, str) else []
    if len(parts) != 3:
        raise ValueError("missing or malformed")
    try:
        header = json.loads(_b64url_decode(parts[0]))
        claims = json.loads(_b64url_decode(parts[1]))
        signature = _b64url_decode(parts[2])
    except ValueError as exc:
        raise ValueError("malformed") from exc
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise ValueError("malformed")
    if header.get("alg") != "RS256":
        raise ValueError("unexpected signing algorithm")
    status, jwks = _get_json(JWKS_URL)
    keys = [k for k in (jwks.get("keys") or []) if isinstance(k, dict) and k.get("kty") == "RSA"]
    kid = header.get("kid")
    match = [k for k in keys if k.get("kid") == kid] if kid else keys[:1]
    if status != 200 or not match:
        raise ValueError("no matching OpenAI signing key")
    try:
        public_key = rsa.RSAPublicNumbers(int.from_bytes(_b64url_decode(match[0]["e"]), "big"),
                                          int.from_bytes(_b64url_decode(match[0]["n"]), "big")).public_key()
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("OpenAI's signing key is malformed") from exc
    try:
        public_key.verify(signature, f"{parts[0]}.{parts[1]}".encode(), padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as exc:
        raise ValueError("signature does not match") from exc
    if claims.get("iss") != ISSUER:
        raise ValueError("wrong issuer")
    aud = claims.get("aud")
    if client_id not in (aud if isinstance(aud, list) else [aud]):
        raise ValueError("wrong audience")
    if _number(claims.get("exp")) < time.time() - 60:
        raise ValueError("expired")
    if not nonce or claims.get("nonce") != nonce:
        raise ValueError("nonce does not match")
    return claims


# ── sign-in ──────────────────────────────────────────────────────────────────
def start(by: str = "") -> str:
    """A fresh sign-in link (state, nonce and PKCE verifier are new every time)."""
    client = _client()
    client_id = client.get("client_id") or DYNAMIC_CLIENT
    verifier = _b64url(os.urandom(32))
    state, nonce = _b64url(os.urandom(24)), _b64url(os.urandom(24))
    params = {"client_id": client_id, "ext_agent_host_id": client["ext_agent_host_id"],
              "response_type": "code", "redirect_uri": REDIRECT_URI, "scope": SCOPES,
              "resource": RESOURCE, "state": state, "nonce": nonce,
              "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
              "code_challenge_method": "S256"}
    if client_id == DYNAMIC_CLIENT:
        params["agent_name_hint"] = AGENT_NAME
    now = time.time()
    with _pending_lock:
        for old in [s for s, p in _pending.items() if now - p["created"] > PENDING_SECONDS]:
            del _pending[old]
        _pending[state] = {"verifier": verifier, "nonce": nonce, "client_id": client_id,
                           "by": by, "created": now}
    return f"{AUTHORIZE_URL}?{urlencode(params, quote_via=quote)}"


def finish(callback: str, by: str = "") -> tuple[bool, str]:
    """Complete a sign-in with the address the browser landed on after it."""
    callback = (callback or "").strip()
    try:
        if not callback or len(callback) > MAX_CALLBACK:
            raise ValueError("empty or too long")
        query = urlsplit(callback).query if "?" in callback else callback
    except ValueError:
        return False, "Paste the whole address from the browser's address bar."
    q = {k: v[0] for k, v in parse_qs(query).items()}
    with _pending_lock:
        pend = _pending.get(q.get("state", ""))
        if not pend or pend["by"] != by or time.time() - pend["created"] > PENDING_SECONDS:
            return False, "This sign-in is unknown or expired — start it again."
        del _pending[q["state"]]
    if q.get("error"):
        return False, f"ChatGPT sign-in was not completed ({q['error'][:80]})."
    if not q.get("code"):
        return False, "The address has no sign-in code — paste the whole address."
    returned = q.get("client_id", "")
    if pend["client_id"] == DYNAMIC_CLIENT:
        if not returned:
            return False, "OpenAI did not complete the registration (no client id) — start again."
        client_id = returned
    elif returned and returned != pend["client_id"]:
        return False, "OpenAI returned a different client id — start the sign-in again."
    else:
        client_id = pend["client_id"]
    if not CLIENT_ID.fullmatch(client_id):
        return False, "The address carries a client id AICortex can't use — start the sign-in again."
    try:
        status, body = _post_form(TOKEN_URL, {
            "grant_type": "authorization_code", "client_id": client_id, "code": q["code"],
            "code_verifier": pend["verifier"], "redirect_uri": REDIRECT_URI, "resource": RESOURCE})
        if status != 200 or not body.get("access_token"):
            return False, (f"OpenAI did not accept the sign-in (HTTP {status}"
                           f"{' ' + _error_code(body) if _error_code(body) else ''}) — start again.")
        claims = _verify_id_token(body.get("id_token", ""), client_id, pend["nonce"])
    except ValueError as exc:
        return False, f"The ID token did not check out ({exc}) — start the sign-in again."
    except RuntimeError as exc:
        return False, f"{exc} Start the sign-in again."
    # A session already stored is replaced, not revoked: revoking it could end the new
    # grant too, and an unused refresh token runs out after 30 days anyway.
    try:
        _remember_client(client_id)
        sess = _with_tokens({"client_id": client_id, "email": str(claims.get("email") or ""),
                             "subject": str(claims.get("sub") or "")}, body)
        with _lock:
            _save_session(sess)
    except (OSError, RuntimeError) as exc:
        _revoke(client_id, str(body.get("refresh_token") or ""))
        return False, (f"The sign-in could not be stored ({exc}) — AICortex revoked it at OpenAI "
                       "again. Fix that, then sign in again.")
    who = sess["email"] or "your ChatGPT account"
    if PLAN_SCOPE not in sess["scopes"]:
        return True, (f"Signed in as {who}, but using the ChatGPT plan was not allowed — "
                      "sign in again and allow it.")
    return True, f"Signed in as {who}."


def status() -> dict:
    """For the WebUI — never a token."""
    sess = _session()
    signed_in = bool(sess.get("refresh_token") or sess.get("access_token"))
    return {"signed_in": signed_in, "plan": signed_in and PLAN_SCOPE in sess.get("scopes", []),
            "email": sess.get("email", ""), "problem": sess.get("problem", ""),
            "expires_at": sess.get("expires_at", 0) if signed_in else 0}


# ── tokens ───────────────────────────────────────────────────────────────────
def _refresh(sess: dict) -> dict:
    global _unsaved
    status, body = _post_form(TOKEN_URL, {
        "grant_type": "refresh_token", "client_id": sess["client_id"],
        "refresh_token": sess["refresh_token"], "resource": RESOURCE})
    if status == 200 and body.get("access_token"):
        new = _with_tokens(sess, body)
        try:
            _save_session(new)
        except RuntimeError as exc:
            _unsaved = new
            print(f"[AICortex] ChatGPT sign-in kept in memory, the vault refused it: {exc}")
        return new
    code = _error_code(body)
    if code in UNUSABLE:
        _save_session({k: v for k, v in sess.items()
                       if k not in ("access_token", "refresh_token", "id_token")} | {
                           "problem": "sign_in_again"})
        raise SignInNeeded("The ChatGPT sign-in has expired or was disconnected — sign in again.")
    raise RuntimeError(f"ChatGPT token refresh failed (HTTP {status}{' ' + code if code else ''}).")


def access_token(force_refresh: bool = False) -> str:
    """A valid access token, refreshed when it is about to expire (one refresh at a
    time, so two callers never race the rotating refresh token)."""
    with _lock:
        sess = _session()
        if not sess.get("refresh_token"):
            raise SignInNeeded("Not signed in with ChatGPT — sign in on the ChatGPT runner in the Runners tab.")
        if PLAN_SCOPE not in sess.get("scopes", []):
            raise SignInNeeded("Using the ChatGPT plan was not allowed — sign in again and allow it.")
        if force_refresh or time.time() >= float(sess.get("expires_at") or 0) - EARLY_REFRESH_SECONDS:
            sess = _refresh(sess)
        return sess["access_token"]


def keep_alive() -> None:
    """Renew an idle session before its 30-day refresh token runs out — never before
    OpenAI's earliest_refresh_at, and at most once an hour after a failure."""
    global _last_keep_alive_try
    with _lock:
        sess = _session()
        now = time.time()
        if (not sess.get("refresh_token") or now - float(sess.get("saved_at") or 0) < KEEP_ALIVE_SECONDS
                or now < float(sess.get("earliest_refresh_at") or 0)
                or now - _last_keep_alive_try < KEEP_ALIVE_RETRY_SECONDS):
            return
        _last_keep_alive_try = now
        try:
            _refresh(sess)
        except SignInNeeded:
            pass                       # status() now says to sign in again
        except RuntimeError as exc:
            print(f"[AICortex] ChatGPT keep-alive: {exc}")


def _revoke(client_id: str, token: str) -> bool:
    """Revoke a refresh token at OpenAI (two tries); True once OpenAI confirmed it."""
    for _ in range(2 if token else 0):
        try:
            status, _ = _post_form(REVOKE_URL, {"token": token, "token_type_hint": "refresh_token",
                                                "client_id": client_id})
        except RuntimeError:
            status = 0
        if status == 200:
            return True
    return not token


def sign_out(by: str = "") -> tuple[bool, str]:
    """Revoke the refresh token at OpenAI and forget the session (the issued client
    id stays, so the next sign-in reuses it)."""
    with _lock:
        sess = _session()
        confirmed = _revoke(sess.get("client_id", ""), sess.get("refresh_token", ""))
        _drop_session()
    if confirmed:
        return True, "Signed out of ChatGPT."
    return True, ("Signed out here, but OpenAI did not confirm the revocation — to be sure, "
                  "disconnect AICortex in ChatGPT under Settings.")


def models() -> list[dict]:
    """The models this account may use, in OpenAI's order: [{slug, name}]."""
    token = access_token()
    status, body = _get_json(MODELS_URL, {"Authorization": f"Bearer {token}"})
    if status != 200:
        raise RuntimeError(f"OpenAI's model list answered HTTP {status}.")
    listed = body.get("models")
    return [{"slug": str(m["slug"]), "name": str(m.get("display_name") or m["slug"])}
            for m in (listed if isinstance(listed, list) else [])
            if isinstance(m, dict) and m.get("slug") and m.get("visibility") == "list"]
