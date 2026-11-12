"""Settings an admin changes in the WebUI (since 2.7.1).

Three settings that up to 2.7.0 were environment variables now live here, as data
under the data volume, and take effect without a restart:

  mail_allowed_recipients   who mail_send may write to ("" = anyone)
  tool_profile              the tools a client sees by default ("core" or "full")
  learn_autocapture         auto-memory: stage durable facts as candidates

At the first start the values still set in the environment are taken over ONCE
(MAIL_ALLOWED_RECIPIENTS, TOOL_PROFILE_DEFAULT, LEARN_AUTOCAPTURE); afterwards the
environment is ignored and the startup log names them as deletable. An unreadable
settings file is never replaced silently: reads raise ``Unreadable``, so mail refuses
instead of writing to anyone, and writes are refused until the file is fixed.
"""
import json
import os
import re
from pathlib import Path

import settings
import store

ENV_NAMES = {"mail_allowed_recipients": "MAIL_ALLOWED_RECIPIENTS",
             "tool_profile": "TOOL_PROFILE_DEFAULT",
             "learn_autocapture": "LEARN_AUTOCAPTURE"}
PROFILES = ("core", "full")
_MAX_RECIPIENTS_TEXT = 2000
# an address, @domain or a bare domain — what mail_tools._recipient_allowed understands
_RECIPIENT = re.compile(r"^(?:[^@\s,]+@)?@?[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


class Unreadable(Exception):
    """The settings file exists but can't be read or parsed."""


def path() -> Path:
    return settings.data_dir("AUTH_STORE_DIR", "auth") / "settings.json"


def validate(key: str, value):
    """(ok, normalized value, error) for one setting."""
    if key == "mail_allowed_recipients":
        text = "" if value is None else str(value).strip()
        if len(text) > _MAX_RECIPIENTS_TEXT:
            return False, None, f"at most {_MAX_RECIPIENTS_TEXT} characters"
        items = [x for x in re.split(r"[,\s]+", text) if x]
        bad = [x for x in items if not _RECIPIENT.match(x)]
        if bad:
            return False, None, "not an address, @domain or domain: " + ", ".join(bad[:5])
        return True, ", ".join(items), ""
    if key == "tool_profile":
        text = str(value or "").strip().lower()
        if text not in PROFILES:
            return False, None, "must be one of: " + ", ".join(PROFILES)
        return True, text, ""
    if key == "learn_autocapture":
        if isinstance(value, bool):
            return True, value, ""
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True, True, ""
        if text in ("0", "false", "no", "off"):
            return True, False, ""
        return False, None, "must be on or off"
    return False, None, f"unknown setting '{key}'"


def _read():
    """The stored settings, or None when there is no file yet."""
    f = path()
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception as exc:
        raise Unreadable(f"{f} can't be read ({type(exc).__name__})") from exc
    if not isinstance(data, dict):
        raise Unreadable(f"{f} doesn't hold a settings object")
    return data


def _write(data: dict) -> None:
    f = path()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, f)


def ensure_initialized(environ=None) -> dict:
    """First start: take the old environment values over once (an invalid one keeps
    the built-in default). Returns the stored settings. Never touches an existing
    or unreadable file."""
    environ = os.environ if environ is None else environ
    with store.locked(path()):
        current = _read()
        if current is not None:
            return current
        data, taken = dict(settings.ADMIN_DEFAULTS), []
        for key, env in ENV_NAMES.items():
            raw = environ.get(env)
            if raw is None or str(raw).strip() == "":
                continue
            ok, value, _ = validate(key, raw)
            if ok:
                data[key], taken = value, taken + [env]
        _write(data)
    if taken:
        print("[AICortex] settings: took over " + ", ".join(taken)
              + " from the environment into the WebUI settings (once)")
    return data


def current() -> dict:
    """All settings (defaults filled in). Raises ``Unreadable``."""
    data = _read()
    if data is None:
        data = ensure_initialized()
    return {k: data.get(k, v) for k, v in settings.ADMIN_DEFAULTS.items()}


def get(key: str):
    """One setting. Under the test switch an environment variable of the old name
    still wins (the suite tunes it that way). Raises ``Unreadable``."""
    env = ENV_NAMES.get(key)
    if env and settings.test_overrides() and str(os.environ.get(env, "")).strip():
        ok, value, _ = validate(key, os.environ[env])
        if ok:
            return value
    return current()[key]


def update(changes: dict) -> tuple[bool, str, dict]:
    """Validate and store ``changes`` (a subset of the keys). Refused as a whole when
    any value is invalid or the file can't be read."""
    if not isinstance(changes, dict) or not changes:
        return False, "nothing to change", {}
    clean = {}
    for key, value in changes.items():
        if key not in settings.ADMIN_DEFAULTS:
            return False, f"unknown setting '{key}'", {}
        ok, norm, err = validate(key, value)
        if not ok:
            return False, f"{key}: {err}", {}
        clean[key] = norm
    try:
        with store.locked(path()):
            data = _read()
            if data is None:
                data = dict(settings.ADMIN_DEFAULTS)
            merged = {**data, **clean}
            _write(merged)
    except Unreadable as exc:
        return False, f"settings file unreadable — fix or remove it first ({exc})", {}
    return True, "saved", {k: merged.get(k, v) for k, v in settings.ADMIN_DEFAULTS.items()}
