"""Unit tests for the inbound webhook auth (webhook_tools) — secret + HMAC."""
import hashlib
import hmac
import importlib


def test_webhook_auth(monkeypatch):
    monkeypatch.setenv("HOOK_TOKEN", "s3cret-token")
    monkeypatch.setenv("HOOK_HMAC", "hmac-key")
    import webhook_tools as w
    importlib.reload(w)

    RAW = b'{"event":"ping"}'
    GOOD = hmac.new(b"hmac-key", RAW, hashlib.sha256).hexdigest()

    assert w._slug("GitHub Push!") == "github-push", "slug"

    # HMAC verify — GitHub sha256= prefix + bare hex + rejects bad/empty
    assert w._verify_hmac("hmac-key", RAW, "sha256=" + GOOD) is True, "hmac sha256= prefix"
    assert w._verify_hmac("hmac-key", RAW, GOOD) is True, "hmac bare hex"
    assert w._verify_hmac("hmac-key", RAW, "sha256=deadbeef") is False, "hmac bad"
    assert w._verify_hmac("hmac-key", RAW, "") is False, "hmac empty"

    # token path (header ONLY — query is no longer accepted, anti log-leak)
    cfg_t = {"secret_env": "HOOK_TOKEN"}
    assert w._validate(cfg_t, {"x-webhook-token": "s3cret-token"}, RAW) == (True, "ok"), "token header ok"
    assert w._validate(cfg_t, {"x-webhook-token": "wrong"}, RAW)[0] is False, "token wrong"
    assert w._validate(cfg_t, {}, RAW)[0] is False, "token missing"
    # non-ASCII presented token → deny, NOT a TypeError/500 (byte-safe compare)
    assert w._validate(cfg_t, {"x-webhook-token": "s3cret-töken"}, RAW)[0] is False, "token non-ascii → deny (no crash)"

    # hmac path
    cfg_h = {"hmac_secret_env": "HOOK_HMAC", "hmac_header": "X-Hub-Signature-256"}
    assert w._validate(cfg_h, {"x-hub-signature-256": "sha256=" + GOOD}, RAW) == (True, "ok"), "hmac ok"
    assert w._validate(cfg_h, {"x-hub-signature-256": "sha256=bad"}, RAW)[0] is False, "hmac deny"

    # both configured → AND
    cfg_b = {"secret_env": "HOOK_TOKEN", "hmac_secret_env": "HOOK_HMAC"}
    assert w._validate(cfg_b, {"x-webhook-token": "s3cret-token", "x-hub-signature-256": "sha256=" + GOOD}, RAW) == (True, "ok"), "both ok"
    assert w._validate(cfg_b, {"x-webhook-token": "s3cret-token"}, RAW)[0] is False, "both: hmac missing → deny"

    # fail-closed: no secret configured at all
    assert w._validate({}, {"x-webhook-token": "x"}, RAW)[0] is False, "no-secret hook → deny"
