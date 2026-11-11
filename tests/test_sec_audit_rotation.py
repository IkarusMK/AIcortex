"""The audit log is rotated by size instead of growing forever.

Regression: ``authz.audit`` appended to one file without limit, and unauthenticated
callers can produce lines (e.g. refused ``/hooks`` deliveries), so the log could
fill the data volume. It now rotates at AUDIT_MAX_BYTES (default 5 MB) into
``audit.log.1`` … ``audit.log.<AUDIT_KEEP>`` (default 3) and never raises.
"""
import json
import sys

import pytest


@pytest.fixture
def az(monkeypatch, fresh_server):
    monkeypatch.setenv("AUDIT_MAX_BYTES", "2000")
    monkeypatch.delenv("AUDIT_KEEP", raising=False)
    return sys.modules["authz"]


def _fill(az, n, tag="x"):
    for i in range(n):
        az.audit(f"{tag}-{i}", "user", "ping", "deny", "r" * 100)


def _gens(az):
    return sorted(p.name for p in az.AUDIT_FILE.parent.glob("audit.log*"))


def test_the_log_rotates_at_the_size_limit_and_keeps_three_generations(az):
    _fill(az, 200)                                     # ~40 KB of lines, 2 KB per file
    assert _gens(az) == ["audit.log", "audit.log.1", "audit.log.2", "audit.log.3"]
    for name in _gens(az):
        size = (az.AUDIT_FILE.parent / name).stat().st_size
        assert size <= 2000 + 400                     # at most one line over the limit
    def numbers(name):
        lines = (az.AUDIT_FILE.parent / name).read_text(encoding="utf-8").splitlines()
        return [int(json.loads(ln)["identity"].split("-")[1]) for ln in lines]
    live, previous = numbers("audit.log"), numbers("audit.log.1")
    assert live[-1] == 199                             # the newest line is in the live file
    assert max(previous) < min(live)                   # older lines moved on, in order


def test_the_number_of_generations_and_the_limit_are_configurable(az, monkeypatch):
    monkeypatch.setenv("AUDIT_KEEP", "1")
    _fill(az, 100)
    assert _gens(az) == ["audit.log", "audit.log.1"]
    monkeypatch.setenv("AUDIT_MAX_BYTES", "0")        # 0 = never rotate
    before = az.AUDIT_FILE.stat().st_size
    _fill(az, 50, "y")
    assert az.AUDIT_FILE.stat().st_size > before + 5000
    assert _gens(az) == ["audit.log", "audit.log.1"]


@pytest.mark.parametrize("limit, keep", [("lots", "3"), ("2000", "many"), ("-5", "-1")])
def test_junk_settings_fall_back_to_safe_values(az, monkeypatch, limit, keep):
    monkeypatch.setenv("AUDIT_MAX_BYTES", limit)
    monkeypatch.setenv("AUDIT_KEEP", keep)
    _fill(az, 30)
    assert az.AUDIT_FILE.exists()                      # nothing raised, lines were written


def test_rotation_never_breaks_a_call(az, monkeypatch, tmp_path):
    _fill(az, 5)
    import os

    def refuse(*_a, **_k):
        raise PermissionError("read-only volume")
    monkeypatch.setattr(os, "replace", refuse)
    _fill(az, 50)                                      # rotation fails, auditing goes on
    assert az.audit("alice", "user", "ping", "deny", "x") is None
    assert "alice" in az.AUDIT_FILE.read_text(encoding="utf-8")
