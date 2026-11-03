"""The runner's model access, set up in the WebUI.

The built-in runner (runner_builtin.py) checks for due jobs once a minute without any
model. Only when a job is due does it ask the model set up here. An admin chooses the
backend and model in the WebUI; the key is kept encrypted in the vault as
``RUNNER_LLM_KEY`` and never leaves AICortex — no tool hands it out, and services
cannot use it either (``RUNNER_LLM_KEY`` is an internal name). A ChatGPT plan needs no
key: the admin signs in with ChatGPT instead (chatgpt_signin.py).
"""
import os
from datetime import datetime, timezone
from pathlib import Path

import store

CONFIG_FILE = Path(os.environ.get("CRON_DIR", "/data/cron")) / "runner_config.json"
KEY_SECRET = "RUNNER_LLM_KEY"
BACKENDS = (
    "claude-abo",      # Claude subscription (claude setup-token)
    "chatgpt-abo",     # ChatGPT plan (Sign in with ChatGPT)
    "claude-api",      # Anthropic API key
    "openrouter",
    "openai",
    "gemini",
)
CLAUDE_BACKENDS = ("claude-abo", "claude-api")
SIGN_IN_BACKENDS = ("chatgpt-abo",)               # no key — a sign-in instead
MODEL_OPTIONAL = CLAUDE_BACKENDS + SIGN_IN_BACKENDS
# Older names, still read: "anthropic" was a second option for an Anthropic key.
LEGACY_BACKENDS = {"anthropic": "claude-api"}


def _backend(name) -> str:
    name = str(name or "").strip().lower()
    return LEGACY_BACKENDS.get(name, name)


def read_config() -> dict:
    data = store.read_json(CONFIG_FILE, {})
    if not isinstance(data, dict):
        return {}
    return dict(data, backend=_backend(data.get("backend"))) if data.get("backend") else data


def _stored_key() -> str:
    import secrets_store
    return str(secrets_store._read_all().get(KEY_SECRET) or "")


def save_config(backend: str, model: str = "", key: str = "", by: str = "",
                builtin=None) -> tuple[bool, str]:
    """(ok, message). An empty key keeps the stored one (a ChatGPT plan takes none);
    builtin=None keeps the runner switch as it is."""
    backend = _backend(backend)
    model = (model or "").strip()
    if backend not in BACKENDS:
        return False, f"Unknown backend '{backend}' — choose one of {', '.join(BACKENDS)}."
    if backend not in MODEL_OPTIONAL and not model:
        return False, "This backend needs a model name."
    if len(model) > 200:
        return False, "The model name is too long."
    key = (key or "").strip()
    if backend not in SIGN_IN_BACKENDS:
        if key:
            import secrets_store
            msg = secrets_store.vault_set(KEY_SECRET, key, description="Runner model access", by=by)
            if not msg.startswith("Stored"):
                return False, msg
        elif not _stored_key():
            return False, "Enter the key once — nothing is stored yet."
    if builtin is None:
        builtin = bool(read_config().get("builtin"))
    store.write_json(CONFIG_FILE, {
        "backend": backend, "model": model, "builtin": bool(builtin),
        "updated_by": (by or "").strip(),
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    return True, "Runner setup saved."


def status() -> dict:
    """What the WebUI shows — never the key itself."""
    import chatgpt_signin
    import cron
    import runner_builtin
    cfg = read_config()
    return {"backend": cfg.get("backend", ""), "model": cfg.get("model", ""),
            "updated": cfg.get("updated", ""), "key_set": bool(_stored_key()),
            "builtin": bool(cfg.get("builtin")), "builtin_state": runner_builtin.status(),
            "backends": list(BACKENDS), "runner": cron.runner_status(),
            "chatgpt": chatgpt_signin.status()}
