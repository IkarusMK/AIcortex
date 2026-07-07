"""Unit tests for the cron act-as capability-token module (actas.py)."""
import importlib
import os


def test_actas_capability_tokens(monkeypatch):
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", "test-key-material-abc123")
    import actas
    importlib.reload(actas)

    t = actas.issue("job-a", "alice@x.com")
    ok, claims = actas.verify(t, expected_job="job-a")
    assert ok and claims["sub"] == "alice@x.com" and claims["job"] == "job-a", "issue/verify happy path"

    assert actas.verify(t, expected_job="job-b") == (False, "job mismatch"), "job mismatch rejected"

    assert actas.verify(t[:-2] + ("aa" if not t.endswith("aa") else "bb"))[0] is False, "tampered signature rejected"
    body, sig = t.split(".", 1)
    assert actas.verify("x" + body + "." + sig)[0] is False, "tampered body rejected"

    assert actas.verify(actas.issue("job-a", "alice@x.com", ttl=-1)) == (False, "expired"), "expired rejected"
    assert actas.verify("garbage")[0] is False and actas.verify("")[0] is False, "malformed rejected"

    # no key → fail-closed
    monkeypatch.delenv("STORAGE_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("RUNNER_TOKEN", raising=False)
    importlib.reload(actas)
    assert actas.available() is False, "no-key: unavailable"
    assert actas.issue("j", "s") == "", "no-key: issue empty"
    assert actas.verify("a.b")[0] is False and "unavailable" in actas.verify("a.b")[1], "no-key: verify denies"

    # key rotation invalidates old tokens
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", "key1")
    importlib.reload(actas)
    t1 = actas.issue("j", "s")
    monkeypatch.setenv("STORAGE_ENCRYPTION_KEY", "key2")
    importlib.reload(actas)
    assert actas.verify(t1)[0] is False, "key change invalidates old tokens"
