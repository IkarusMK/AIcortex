"""The runner on a ChatGPT plan: OpenAI's "Sign in with ChatGPT" for open-source apps.

Before: the runner could use a Claude subscription, but only an OpenAI API key — no
ChatGPT plan. Now an admin signs in with ChatGPT in the WebUI and the runner uses the
plan, as OpenAI documents it (developers.openai.com/siwc/token-sharing-open-source).
Proves:
- the sign-in link carries exactly what OpenAI asks for (dynamic registration, a
  stable host id, PKCE S256, the plan scope, the resource, a loopback redirect);
- finishing checks state, the issued client id and the ID token (signature, issuer,
  audience, nonce), keeps the tokens encrypted in the vault — never shown, never
  readable by a service — and reuses the issued client id later;
- the access token is refreshed (once, also when two callers race) with the rotated
  refresh token; an unusable one asks for a new sign-in, a temporary failure doesn't;
  an idle session is kept alive, not before earliest_refresh_at;
- sign-out revokes the refresh token and clears the session;
- a due job runs through the Responses API (store false, stream true, function tools,
  no unsupported fields), a usage limit stops the run and says so, a rejected token
  is refreshed and retried once;
- the WebUI signs in and out (admin only, CSRF) and lists the account's models.
"""
import asyncio
import base64
import json
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from test_cov_webui import Ui, _audit_lines, ui  # noqa: F401 - ui is a fixture

ISSUED = "oaiapp_test123"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class FakeOpenAI:
    """auth.openai.com and api.openai.com/v1/models, as the docs describe them."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.forms, self.gets = [], []
        self.nonce, self.aud, self.scope = "", ISSUED, (
            "chatgpt.tokens.use.direct email offline_access openid profile resource.invoke")
        self.refresh_error, self.revoke_status, self.generation = None, 200, 0
        self.lock = threading.Lock()

    def jwt(self, claims, kid="k1", key=None):
        h = _b64(json.dumps({"alg": "RS256", "kid": kid, "typ": "JWT"}).encode())
        p = _b64(json.dumps(claims).encode())
        sig = (key or self.key).sign(f"{h}.{p}".encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{h}.{p}.{_b64(sig)}"

    def id_token(self, **over):
        claims = {"iss": "https://auth.openai.com", "aud": self.aud, "sub": "user-1",
                  "email": "alice@example.com", "nonce": self.nonce,
                  "iat": int(time.time()), "exp": int(time.time()) + 3600}
        claims.update(over)
        return self.jwt(claims)

    def tokens(self):
        self.generation += 1
        return {"access_token": f"at-{self.generation}", "refresh_token": f"rt-{self.generation}",
                "id_token": self.id_token(), "token_type": "Bearer", "expires_in": 3600,
                "scope": self.scope, "earliest_refresh_at": 0}

    def post_form(self, url, data):
        with self.lock:
            self.forms.append((url, dict(data)))
            if url.endswith("/oauth/revoke"):
                return self.revoke_status, {}
            if data.get("grant_type") == "refresh_token":
                time.sleep(0.05)                      # widen the window for a race
                if self.refresh_error:
                    return self.refresh_error
                assert data["refresh_token"] == f"rt-{self.generation}", "an old refresh token was used"
            return 200, self.tokens()

    def get_json(self, url, headers=None):
        self.gets.append((url, dict(headers or {})))
        if url.endswith("/jwks.json"):
            n = self.key.public_key().public_numbers()
            return 200, {"keys": [{"kty": "RSA", "kid": "k1", "alg": "RS256", "use": "sig",
                                   "n": _b64(n.n.to_bytes((n.n.bit_length() + 7) // 8, "big")),
                                   "e": _b64(n.e.to_bytes(3, "big"))}]}
        if url.endswith("/v1/models"):
            return 200, {"models": [
                {"slug": "gpt-6-astra", "display_name": "GPT-6 Astra", "visibility": "list"},
                {"slug": "gpt-hidden", "display_name": "Hidden", "visibility": "hide"},
                {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1 Sol", "visibility": "list"}]}
        return 404, {}


@pytest.fixture
def connector(monkeypatch, request):
    monkeypatch.setenv("ALLOW_PLAINTEXT_VAULT", "1")
    monkeypatch.setenv("AUTH_ENFORCE", "1")
    return request.getfixturevalue("http_server")


@pytest.fixture
def openai(connector, monkeypatch):
    fake = FakeOpenAI()
    cs = sys.modules["chatgpt_signin"]
    monkeypatch.setattr(cs, "_post_form", fake.post_form)
    monkeypatch.setattr(cs, "_get_json", fake.get_json)
    return fake


def _params(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def _callback(params, **extra):
    q = {"code": "the-code", "state": params["state"], "client_id": ISSUED,
         "scope": "chatgpt.tokens.use.direct email offline_access openid profile resource.invoke"}
    q.update(extra)
    return "http://127.0.0.1:1455/auth/callback?" + "&".join(f"{k}={v}" for k, v in q.items() if v is not None)


def _sign_in(fake, by="boss", **extra):
    cs = sys.modules["chatgpt_signin"]
    params = _params(cs.start(by=by))
    fake.nonce = params["nonce"]
    return cs.finish(_callback(params, **extra), by=by), params


# ── sign-in ──────────────────────────────────────────────────────────────────
def test_the_sign_in_link_carries_what_openai_asks_for(openai):
    cs = sys.modules["chatgpt_signin"]
    url = cs.start(by="boss")
    assert url.startswith("https://auth.openai.com/api/accounts/authorize?")
    p = _params(url)
    assert p["client_id"] == "dynamic_agent_client" and p["agent_name_hint"] == "AICortex"
    assert p["ext_agent_host_id"].startswith("urn:uuid:")
    assert p["response_type"] == "code" and p["redirect_uri"] == "http://127.0.0.1:1455/auth/callback"
    assert set(p["scope"].split()) == {"openid", "profile", "email", "offline_access",
                                       "resource.invoke", "chatgpt.tokens.use.direct"}
    assert p["resource"] == "https://api.openai.com/v1" and p["code_challenge_method"] == "S256"
    assert len(p["code_challenge"]) == 43 and p["state"] and p["nonce"]
    again = _params(cs.start(by="boss"))
    assert again["ext_agent_host_id"] == p["ext_agent_host_id"] and again["state"] != p["state"]


def test_finishing_keeps_the_session_in_the_vault_and_shows_no_token(openai):
    (ok, msg), params = _sign_in(openai)
    assert ok, msg
    url, form = openai.forms[-1]
    assert url == "https://auth.openai.com/api/accounts/oauth/token"
    assert form["grant_type"] == "authorization_code" and form["client_id"] == ISSUED
    assert form["code"] == "the-code" and form["redirect_uri"] == params["redirect_uri"]
    assert form["resource"] == "https://api.openai.com/v1" and form["code_verifier"]
    import hashlib
    assert _b64(hashlib.sha256(form["code_verifier"].encode()).digest()) == params["code_challenge"]
    st = sys.modules["chatgpt_signin"].status()
    assert st["signed_in"] and st["plan"] and st["email"] == "alice@example.com"
    assert "at-1" not in json.dumps(st) and "rt-1" not in json.dumps(st)
    assert sys.modules["secrets_store"].get_secret("RUNNER_CHATGPT_SESSION") is None


def test_a_wrong_or_reused_state_and_a_denied_sign_in_are_refused(openai):
    cs = sys.modules["chatgpt_signin"]
    params = _params(cs.start(by="boss"))
    openai.nonce = params["nonce"]
    assert not cs.finish(_callback(params, state="forged"), by="boss")[0]
    assert not cs.finish(_callback(params), by="eve")[0]                 # another admin's link
    ok, _ = cs.finish(_callback(params), by="boss")
    assert ok
    assert not cs.finish(_callback(params), by="boss")[0]                # used once only
    params = _params(cs.start(by="boss"))
    ok, msg = cs.finish(_callback(params, code=None, error="access_denied"), by="boss")
    assert not ok and "access_denied" in msg
    assert not cs.finish("x" * 5000, by="boss")[0]


def test_registration_needs_the_issued_client_id_and_later_sign_ins_reuse_it(openai):
    cs = sys.modules["chatgpt_signin"]
    params = _params(cs.start(by="boss"))
    openai.nonce = params["nonce"]
    ok, msg = cs.finish(_callback(params, client_id=None), by="boss")
    assert not ok and "registration" in msg.lower()
    assert _sign_in(openai)[0][0]
    p = _params(cs.start(by="boss"))
    assert p["client_id"] == ISSUED and "agent_name_hint" not in p
    openai.nonce = p["nonce"]
    assert not cs.finish(_callback(p, client_id="oaiapp_other"), by="boss")[0]
    p = _params(cs.start(by="boss"))
    openai.nonce = p["nonce"]
    assert cs.finish(_callback(p, client_id=None), by="boss")[0]           # may be left out


@pytest.mark.parametrize("over", [{"nonce": "other"}, {"aud": "oaiapp_other"},
                                  {"iss": "https://evil.example.com"}, {"exp": 1}])
def test_an_id_token_that_does_not_check_out_is_refused(openai, monkeypatch, over):
    real = openai.id_token
    monkeypatch.setattr(openai, "id_token", lambda **o: real(**{**over, **o}))
    (ok, msg), _ = _sign_in(openai)
    assert not ok and "ID token" in msg
    assert not sys.modules["chatgpt_signin"].status()["signed_in"]


def test_an_id_token_signed_by_another_key_is_refused(openai, monkeypatch):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    real_jwt = openai.jwt
    monkeypatch.setattr(openai, "jwt", lambda claims, kid="k1", key=None: real_jwt(claims, kid, other))
    (ok, msg), _ = _sign_in(openai)
    assert not ok and "ID token" in msg


def test_without_the_plan_scope_it_says_so(openai):
    openai.scope = "email offline_access openid profile"
    (ok, msg), _ = _sign_in(openai, scope="email+openid+profile+offline_access")
    assert ok and "plan" in msg.lower()
    st = sys.modules["chatgpt_signin"].status()
    assert st["signed_in"] and not st["plan"]
    with pytest.raises(sys.modules["chatgpt_signin"].SignInNeeded):
        sys.modules["chatgpt_signin"].access_token()


# ── tokens ───────────────────────────────────────────────────────────────────
def _expire(cs):
    sess = cs._session()
    cs._save_session(dict(sess, expires_at=time.time() - 1))


def test_an_expiring_token_is_refreshed_once_with_the_rotated_refresh_token(openai):
    cs = sys.modules["chatgpt_signin"]
    assert _sign_in(openai)[0][0]
    assert cs.access_token() == "at-1"
    _expire(cs)
    got = []
    threads = [threading.Thread(target=lambda: got.append(cs.access_token())) for _ in range(3)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    refreshes = [f for _, f in openai.forms if f.get("grant_type") == "refresh_token"]
    assert got == ["at-2"] * 3 and len(refreshes) == 1
    form = refreshes[0]
    assert form == {"grant_type": "refresh_token", "client_id": ISSUED, "refresh_token": "rt-1",
                    "resource": "https://api.openai.com/v1"}
    _expire(cs)
    assert cs.access_token() == "at-3"                                     # rt-2 was stored


def test_an_unusable_refresh_token_asks_for_a_new_sign_in_but_keeps_the_client(openai):
    cs = sys.modules["chatgpt_signin"]
    assert _sign_in(openai)[0][0]
    _expire(cs)
    openai.refresh_error = (400, {"error": "invalid_grant"})
    with pytest.raises(cs.SignInNeeded):
        cs.access_token()
    st = cs.status()
    assert not st["signed_in"] and st["problem"] == "sign_in_again"
    assert _params(cs.start(by="boss"))["client_id"] == ISSUED


def test_a_temporary_refresh_failure_keeps_the_session(openai):
    cs = sys.modules["chatgpt_signin"]
    assert _sign_in(openai)[0][0]
    _expire(cs)
    openai.refresh_error = (503, {"error": "temporarily_unavailable"})
    with pytest.raises(RuntimeError):
        cs.access_token()
    assert cs.status()["signed_in"]
    openai.refresh_error = None
    assert cs.access_token() == "at-2"


def test_keep_alive_renews_an_idle_session_but_not_before_earliest_refresh_at(openai):
    cs = sys.modules["chatgpt_signin"]
    assert _sign_in(openai)[0][0]
    cs.keep_alive()
    assert openai.generation == 1                                          # fresh: nothing to do
    old = time.time() - cs.KEEP_ALIVE_SECONDS - 10
    cs._save_session(dict(cs._session(), saved_at=old, earliest_refresh_at=time.time() + 600))
    cs._last_keep_alive_try = 0
    cs.keep_alive()
    assert openai.generation == 1
    cs._save_session(dict(cs._session(), earliest_refresh_at=0))
    cs._last_keep_alive_try = 0
    cs.keep_alive()
    assert openai.generation == 2 and cs._session()["refresh_token"] == "rt-2"


def test_sign_out_revokes_and_clears(openai):
    cs = sys.modules["chatgpt_signin"]
    assert _sign_in(openai)[0][0]
    ok, msg = cs.sign_out(by="boss")
    url, form = openai.forms[-1]
    assert ok and url == "https://auth.openai.com/api/accounts/oauth/revoke"
    assert form == {"token": "rt-1", "token_type_hint": "refresh_token", "client_id": ISSUED}
    assert not cs.status()["signed_in"] and not cs._session()
    assert _sign_in(openai)[0][0]
    openai.revoke_status = 500
    ok, msg = cs.sign_out(by="boss")
    assert ok and "not confirm" in msg and not cs.status()["signed_in"]


def test_openai_out_of_reach_gives_a_clear_answer_not_a_crash(openai, monkeypatch):
    cs = sys.modules["chatgpt_signin"]
    params = _params(cs.start(by="boss"))

    def unreachable(url, data):
        raise RuntimeError("OpenAI could not be reached (ConnectError).")
    monkeypatch.setattr(cs, "_post_form", unreachable)
    ok, msg = cs.finish(_callback(params), by="boss")
    assert not ok and "could not be reached" in msg


def test_the_models_are_the_listed_ones_in_server_order(openai):
    assert _sign_in(openai)[0][0]
    models = sys.modules["chatgpt_signin"].models()
    assert [m["slug"] for m in models] == ["gpt-6-astra", "gpt-6.1-sol"]
    url, headers = openai.gets[-1]
    assert url == "https://api.openai.com/v1/models" and headers["Authorization"] == "Bearer at-1"


# ── the runner on the plan ───────────────────────────────────────────────────
def _job(job_id="report"):
    return {"id": job_id, "name": job_id, "schedule": "* * * * *", "prompt": f"do {job_id}",
            "notify": "user", "enabled": True, "owner": "", "created": "2026-01-01T00:00:00",
            "last_run": ""}


class Responses:
    """Stands in for POST /v1/responses: first a function call (and a reasoning item),
    then a closing message."""

    def __init__(self, fail=None):
        self.requests, self.fail = [], list(fail or [])

    async def __call__(self, url, payload, headers):
        self.requests.append({"url": url, "payload": json.loads(json.dumps(payload)),
                              "headers": dict(headers)})
        if self.fail:
            raise self.fail.pop(0)
        if not any(i.get("type") == "function_call_output" for i in payload["input"]):
            return {"status": "completed", "output": [
                {"type": "reasoning", "id": "rs_1", "summary": []},
                {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "integration_call",
                 "arguments": json.dumps({"name": "cron_due"})},
                {"type": "function_call", "id": "fc_2", "call_id": "call_2", "name": "cron_mark_run",
                 "arguments": json.dumps({"name": "report", "result": "ok"})}]}
        return {"status": "completed", "output": [
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}]}


def _setup(model="gpt-6-astra"):
    ok, msg = sys.modules["runner_setup"].save_config("chatgpt-abo", model, "", by="boss", builtin=True)
    assert ok, msg


def _run(connector, monkeypatch, responses):
    builtin = sys.modules["runner_builtin"]
    monkeypatch.setattr(builtin, "_stream", responses)
    count = asyncio.run(builtin.run_once(f"{connector}/mcp"))
    return count, builtin


def test_a_due_job_runs_on_the_chatgpt_plan_via_the_responses_api(connector, openai, monkeypatch):
    assert _sign_in(openai)[0][0]
    _setup()
    sys.modules["cron"]._write([_job()])
    responses = Responses()
    count, builtin = _run(connector, monkeypatch, responses)
    first = responses.requests[0]
    assert count == 1 and first["url"] == "https://api.openai.com/v1/responses"
    assert first["headers"]["Authorization"] == "Bearer at-1"
    body = first["payload"]
    assert body["store"] is False and body["stream"] is True and body["model"] == "gpt-6-astra"
    assert not set(body) & {"max_output_tokens", "temperature", "top_p", "previous_response_id",
                            "metadata", "user", "truncation", "background"}
    assert all(t["type"] == "function" and t["strict"] is False and "parameters" in t
               for t in body["tools"])
    assert "cron_due" not in {t["name"] for t in body["tools"]}
    assert '"id": "report"' in body["input"][0]["content"]
    second = responses.requests[1]["payload"]["input"]
    calls = [i for i in second if i.get("type") == "function_call"]
    outputs = [i for i in second if i.get("type") == "function_call_output"]
    assert [c["call_id"] for c in calls] == ["call_1", "call_2"] and all("id" not in c for c in calls)
    assert not [i for i in second if i.get("type") == "reasoning"]
    assert outputs[0]["call_id"] == "call_1" and outputs[0]["output"].startswith("Refused")
    assert "Marked 'report' run" in outputs[1]["output"]
    assert sys.modules["cron"]._read()[0]["last_run"] and builtin.status()["state"] == "ran"


def test_an_empty_model_takes_the_first_listed_one(connector, openai, monkeypatch):
    assert _sign_in(openai)[0][0]
    _setup(model="")
    sys.modules["cron"]._write([_job()])
    responses = Responses()
    _run(connector, monkeypatch, responses)
    assert responses.requests[0]["payload"]["model"] == "gpt-6-astra"


def test_a_usage_limit_stops_the_run_and_says_so(connector, openai, monkeypatch):
    assert _sign_in(openai)[0][0]
    _setup()
    sys.modules["cron"]._write([_job()])
    builtin = sys.modules["runner_builtin"]
    lines = ['event: response.created', 'data: {"type":"response.created"}', '',
             'event: response.failed',
             'data: {"type":"response.failed","response":{"error":{"code":"subscription_sharing_usage_limit_exceeded"}}}',
             '']
    with pytest.raises(RuntimeError) as exc:
        builtin._parse_sse(lines)
    assert "usage limit" in str(exc.value) and "chatgpt.com/settings/usage" in str(exc.value)
    responses = Responses(fail=[exc.value])
    with pytest.raises(RuntimeError):
        _run(connector, monkeypatch, responses)
    assert not sys.modules["cron"]._read()[0]["last_run"]


def test_a_rejected_token_is_refreshed_and_retried_once(connector, openai, monkeypatch):
    assert _sign_in(openai)[0][0]
    _setup()
    sys.modules["cron"]._write([_job()])
    builtin = sys.modules["runner_builtin"]
    responses = Responses(fail=[builtin.Unauthorized("HTTP 401")])
    count, _ = _run(connector, monkeypatch, responses)
    assert count == 1
    assert [r["headers"]["Authorization"] for r in responses.requests[:2]] == ["Bearer at-1", "Bearer at-2"]


def test_without_a_sign_in_the_runner_waits(connector, openai, monkeypatch):
    _setup()
    sys.modules["cron"]._write([_job()])
    responses = Responses()
    count, builtin = _run(connector, monkeypatch, responses)
    assert count == 0 and responses.requests == [] and builtin.status()["state"] == "no-signin"


def test_the_stream_is_read_to_response_completed(connector):
    parse = sys.modules["runner_builtin"]
    done = parse._parse_sse(['event: response.output_text.delta', 'data: {"type":"response.output_text.delta","delta":"hi"}',
                             '', 'event: response.completed',
                             'data: {"type":"response.completed","response":{"status":"completed","output":[]}}', ''])
    assert done == {"status": "completed", "output": []}
    with pytest.raises(RuntimeError, match="without response.completed"):
        parse._parse_sse(['data: {"type":"response.created"}', ''])
    with pytest.raises(RuntimeError, match="incomplete"):
        parse._parse_sse(['data: {"type":"response.incomplete","response":{"incomplete_details":{"reason":"max_output_tokens"}}}', ''])


# ── setup and WebUI ──────────────────────────────────────────────────────────
def test_the_chatgpt_plan_needs_no_key_and_no_model(connector):
    rs = sys.modules["runner_setup"]
    ok, msg = rs.save_config("chatgpt-abo", "", "", by="boss")
    assert ok, msg
    assert rs._stored_key() == "" and rs.read_config()["backend"] == "chatgpt-abo"


@pytest.mark.oidc
def test_the_webui_signs_in_and_out(ui, monkeypatch):
    fake = FakeOpenAI()
    cs = sys.modules["chatgpt_signin"]
    monkeypatch.setattr(cs, "_post_form", fake.post_form)
    monkeypatch.setattr(cs, "_get_json", fake.get_json)
    status, body = ui.call("/ui/api/runner/chatgpt/start", "POST", body={})
    assert status == 200 and body["url"].startswith("https://auth.openai.com/")
    params = _params(body["url"])
    fake.nonce = params["nonce"]
    assert ui.call("/ui/api/runner/chatgpt/start", "POST", role="user", body={})[0] == 403
    assert ui.call("/ui/api/runner/chatgpt/finish", "POST", csrf=None, body={"url": _callback(params)})[0] == 403
    status, body = ui.call("/ui/api/runner/chatgpt/finish", "POST", body={"url": _callback(params)})
    assert status == 200 and body["ok"], body
    got = ui.call("/ui/api/runner")[1]
    assert got["chatgpt"]["signed_in"] and "at-1" not in json.dumps(got) and "rt-1" not in json.dumps(got)
    status, body = ui.call("/ui/api/runner/chatgpt/models")
    assert status == 200 and [m["slug"] for m in body["models"]] == ["gpt-6-astra", "gpt-6.1-sol"]
    assert ui.call("/ui/api/runner/chatgpt/models", role="user")[0] == 403
    status, body = ui.call("/ui/api/runner/chatgpt/signout", "POST", body={})
    assert status == 200 and body["ok"] and not ui.call("/ui/api/runner")[1]["chatgpt"]["signed_in"]
    assert "the-code" not in json.dumps(_audit_lines()) and "rt-1" not in json.dumps(_audit_lines())
