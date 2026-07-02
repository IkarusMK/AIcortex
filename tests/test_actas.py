"""Unit tests for the cron act-as capability-token module (actas.py)."""
import importlib
import os
import sys

sys.path.insert(0, "/Users/steffenmac/Downloads/LLMConnector/app")

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        failures.append(name)


os.environ["STORAGE_ENCRYPTION_KEY"] = "test-key-material-abc123"
import actas  # noqa: E402
importlib.reload(actas)

t = actas.issue("job-a", "alice@x.com")
ok, claims = actas.verify(t, expected_job="job-a")
check("issue/verify happy path", ok and claims["sub"] == "alice@x.com" and claims["job"] == "job-a")

check("job mismatch rejected", actas.verify(t, expected_job="job-b") == (False, "job mismatch"))

check("tampered signature rejected", actas.verify(t[:-2] + ("aa" if not t.endswith("aa") else "bb"))[0] is False)
body, sig = t.split(".", 1)
check("tampered body rejected", actas.verify("x" + body + "." + sig)[0] is False)

check("expired rejected", actas.verify(actas.issue("job-a", "alice@x.com", ttl=-1)) == (False, "expired"))
check("malformed rejected", actas.verify("garbage")[0] is False and actas.verify("")[0] is False)

# no key → fail-closed
del os.environ["STORAGE_ENCRYPTION_KEY"]
os.environ.pop("RUNNER_TOKEN", None)
importlib.reload(actas)
check("no-key: unavailable", actas.available() is False)
check("no-key: issue empty", actas.issue("j", "s") == "")
check("no-key: verify denies", actas.verify("a.b")[0] is False and "unavailable" in actas.verify("a.b")[1])

# key rotation invalidates old tokens
os.environ["STORAGE_ENCRYPTION_KEY"] = "key1"
importlib.reload(actas)
t1 = actas.issue("j", "s")
os.environ["STORAGE_ENCRYPTION_KEY"] = "key2"
importlib.reload(actas)
check("key change invalidates old tokens", actas.verify(t1)[0] is False)

print()
if failures:
    print(f"{len(failures)} FAILURES:", failures)
    sys.exit(1)
print("ALL TESTS PASSED")
