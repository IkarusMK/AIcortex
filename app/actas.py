"""Short-lived capability tokens for cron *act-as* (per-job runner trust).

Decision (2026-07-02, briefing): the NAS runner must NOT hold a standing power of
attorney. Instead ``cron_due`` mints a SHORT-LIVED, per-job token; the runner
presents it when executing that one job, and the connector switches identity to the
job's owner ONLY after validating the token. A compromised runner can therefore do
no more than: run jobs that are actually due, each confined to its own owner's area,
for the token's lifetime.

Token = ``<body>.<sig>`` (URL-safe base64, no padding):
- body  = JSON claims {"job", "sub", "iat", "exp", "jti"}
- sig   = HMAC-SHA256(key, body)

The HMAC key is DERIVED (HKDF-SHA256, RFC 5869) from the server's existing
``STORAGE_ENCRYPTION_KEY`` with a fixed label — we never reuse a raw key for a second
purpose. If no key material exists, tokens cannot be issued OR verified: act-as is
simply unavailable (fail-closed — never a forgeable/empty-key token).
"""
import base64
import hashlib
import hmac
import json
import os
import time

_LABEL = b"aicortex cron-act-as v1"     # HKDF info / domain-separation label
_TTL_DEFAULT = 300                        # 5 minutes (cron tick + generous buffer)


def _hkdf_sha256(ikm: bytes, info: bytes, length: int = 32) -> bytes:
    """Minimal HKDF (extract+expand) for one <=32-byte output block."""
    prk = hmac.new(b"\x00" * hashlib.sha256().digest_size, ikm, hashlib.sha256).digest()
    okm = hmac.new(prk, info + b"\x01", hashlib.sha256).digest()
    return okm[:length]


def _signing_key():
    """Derive the act-as HMAC key from server key material, or None if none is set
    (→ act-as unavailable, fail-closed)."""
    base = os.environ.get("STORAGE_ENCRYPTION_KEY") or os.environ.get("RUNNER_TOKEN")
    if not base:
        return None
    return _hkdf_sha256(base.encode() if isinstance(base, str) else base, _LABEL)


def available() -> bool:
    """Whether act-as tokens can be minted/verified at all (key material present)."""
    return _signing_key() is not None


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue(job_id: str, owner_sub: str, ttl: int = _TTL_DEFAULT) -> str:
    """Mint a token binding a due job to its owner for `ttl` seconds. Returns "" if
    no signing key is configured (caller must then treat the job as NOT runnable
    as that owner — fail-closed)."""
    key = _signing_key()
    if not key or not job_id or not owner_sub:
        return ""
    now = int(time.time())
    claims = {"job": job_id, "sub": owner_sub, "iat": now, "exp": now + int(ttl),
              "jti": _b64(os.urandom(9))}
    body = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64(hmac.new(key, body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify(token: str, expected_job: str = None):
    """Validate a token. Returns ``(ok, claims_or_reason)``. Constant-time signature
    check; enforces expiry and (optionally) that the token was minted for
    `expected_job`. Fail-closed: any problem → ``(False, reason)``."""
    key = _signing_key()
    if not key:
        return False, "act-as unavailable: no STORAGE_ENCRYPTION_KEY signing material"
    if not token or "." not in token:
        return False, "malformed token"
    body, _, sig = token.partition(".")
    expected = _b64(hmac.new(key, body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return False, "bad signature"
    try:
        claims = json.loads(_b64d(body))
    except Exception:
        return False, "unparseable claims"
    if not isinstance(claims, dict):
        return False, "unparseable claims"
    if int(claims.get("exp", 0)) < int(time.time()):
        return False, "expired"
    if expected_job is not None and claims.get("job") != expected_job:
        return False, "job mismatch"
    if not claims.get("sub"):
        return False, "no owner in token"
    return True, claims
