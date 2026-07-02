"""Unit tests for the inbound webhook auth (webhook_tools) — secret + HMAC."""
import hashlib
import hmac
import os
import sys

os.environ["HOOK_TOKEN"] = "s3cret-token"
os.environ["HOOK_HMAC"] = "hmac-key"
sys.path.insert(0, "/Users/steffenmac/Downloads/LLMConnector/app")

import webhook_tools as w  # noqa: E402

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        failures.append(name)


RAW = b'{"event":"ping"}'
GOOD = hmac.new(b"hmac-key", RAW, hashlib.sha256).hexdigest()

check("slug", w._slug("GitHub Push!") == "github-push")

# HMAC verify — GitHub sha256= prefix + bare hex + rejects bad/empty
check("hmac sha256= prefix", w._verify_hmac("hmac-key", RAW, "sha256=" + GOOD) is True)
check("hmac bare hex", w._verify_hmac("hmac-key", RAW, GOOD) is True)
check("hmac bad", w._verify_hmac("hmac-key", RAW, "sha256=deadbeef") is False)
check("hmac empty", w._verify_hmac("hmac-key", RAW, "") is False)

# token path (header ONLY — query is no longer accepted, anti log-leak)
cfg_t = {"secret_env": "HOOK_TOKEN"}
check("token header ok", w._validate(cfg_t, {"x-webhook-token": "s3cret-token"}, RAW) == (True, "ok"))
check("token wrong", w._validate(cfg_t, {"x-webhook-token": "wrong"}, RAW)[0] is False)
check("token missing", w._validate(cfg_t, {}, RAW)[0] is False)
# non-ASCII presented token → deny, NOT a TypeError/500 (byte-safe compare)
check("token non-ascii → deny (no crash)", w._validate(cfg_t, {"x-webhook-token": "s3cret-töken"}, RAW)[0] is False)

# hmac path
cfg_h = {"hmac_secret_env": "HOOK_HMAC", "hmac_header": "X-Hub-Signature-256"}
check("hmac ok", w._validate(cfg_h, {"x-hub-signature-256": "sha256=" + GOOD}, RAW) == (True, "ok"))
check("hmac deny", w._validate(cfg_h, {"x-hub-signature-256": "sha256=bad"}, RAW)[0] is False)

# both configured → AND
cfg_b = {"secret_env": "HOOK_TOKEN", "hmac_secret_env": "HOOK_HMAC"}
check("both ok", w._validate(cfg_b, {"x-webhook-token": "s3cret-token", "x-hub-signature-256": "sha256=" + GOOD}, RAW) == (True, "ok"))
check("both: hmac missing → deny", w._validate(cfg_b, {"x-webhook-token": "s3cret-token"}, RAW)[0] is False)

# fail-closed: no secret configured at all
check("no-secret hook → deny", w._validate({}, {"x-webhook-token": "x"}, RAW)[0] is False)

print()
if failures:
    print(f"{len(failures)} FAILURES:", failures)
    sys.exit(1)
print("ALL TESTS PASSED")
