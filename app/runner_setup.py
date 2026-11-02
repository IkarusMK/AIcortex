"""The NAS runner's model access, set up in the WebUI.

The runner (runner/generic) asks ``cron_due`` once a minute without any model.
Only when a job is due does it call ``runner_config`` to learn which model to run
and with which key. An admin chooses the backend and model in the WebUI; the key
is kept encrypted in the vault as ``RUNNER_LLM_KEY``.

``runner_config`` is the one place a vault value leaves the server, so it is
fenced in: only the runner itself (RUNNER_TOKEN or a runner key) gets it — never
a person, an admin, a person's agent key, the REST API or a job's act-as run —
and only once per run, so a model working on a job cannot fetch it again. Services
cannot use the key either (``RUNNER_LLM_KEY`` is an internal name).
"""
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import store

CONFIG_FILE = Path(os.environ.get("CRON_DIR", "/data/cron")) / "runner_config.json"
KEY_SECRET = "RUNNER_LLM_KEY"
# backend → the environment variable the runner puts the key in
BACKENDS = {
    "claude-abo": "CLAUDE_CODE_OAUTH_TOKEN",   # Claude subscription (claude setup-token)
    "claude-api": "ANTHROPIC_API_KEY",         # Anthropic API key, run by Claude Code
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}
CLAUDE_BACKENDS = ("claude-abo", "claude-api")
# A run fetches its model access once; a second fetch within this many seconds is
# refused (it would come from the model working on a job, not from the runner).
ONCE_PER_RUN_SECONDS = 55

_last_handout: dict[str, float] = {}
_lock = threading.Lock()


def read_config() -> dict:
    data = store.read_json(CONFIG_FILE, {})
    return data if isinstance(data, dict) else {}


def _stored_key() -> str:
    import secrets_store
    return str(secrets_store._read_all().get(KEY_SECRET) or "")


def save_config(backend: str, model: str = "", key: str = "", by: str = "") -> tuple[bool, str]:
    """(ok, message). An empty key keeps the stored one."""
    backend = (backend or "").strip().lower()
    model = (model or "").strip()
    if backend not in BACKENDS:
        return False, f"Unknown backend '{backend}' — choose one of {', '.join(BACKENDS)}."
    if backend not in CLAUDE_BACKENDS and not model:
        return False, "This backend needs a model name."
    if len(model) > 200:
        return False, "The model name is too long."
    key = (key or "").strip()
    if key:
        import secrets_store
        msg = secrets_store.vault_set(KEY_SECRET, key, description="Runner model access", by=by)
        if not msg.startswith("Stored"):
            return False, msg
    elif not _stored_key():
        return False, "Enter the key once — nothing is stored yet."
    store.write_json(CONFIG_FILE, {
        "backend": backend, "model": model, "updated_by": (by or "").strip(),
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    return True, "Runner setup saved."


def status() -> dict:
    """What the WebUI shows — never the key itself."""
    import apikeys
    import cron
    cfg = read_config()
    keys = [k for k in apikeys.list_keys() if k.get("runner")]
    return {"backend": cfg.get("backend", ""), "model": cfg.get("model", ""),
            "updated": cfg.get("updated", ""), "key_set": bool(_stored_key()),
            "backends": list(BACKENDS), "runner": cron.runner_status(), "keys": keys}


def _gate() -> str:
    """'' when the real caller is the runner outside an act-as run, else why not."""
    import actas
    import authz
    ident, _, is_runner = authz.real_caller()
    if not is_runner:
        return "Denied: runner_config is for the NAS runner only."
    if actas.current(ident):
        return "Denied: not during an act-as run."
    with _lock:
        last = _last_handout.get(ident, 0.0)
        if time.monotonic() - last < ONCE_PER_RUN_SECONDS:
            return "Refused: the model access was already handed out for this run."
        _last_handout[ident] = time.monotonic()
    return ""


def register(mcp):
    @mcp.tool
    def runner_config() -> str:
        """For the NAS runner only: the model to run due jobs with, as JSON
        {backend, model, key_env, key} ({"backend": ""} when nothing is set up).
        Refused for everyone else, during an act-as run, and a second time within
        the same run."""
        import authz
        refusal = _gate()
        ident, role, _ = authz.real_caller()
        if refusal:
            authz.audit(ident, role, "runner_config", "deny", refusal)
            return refusal
        cfg = read_config()
        backend = cfg.get("backend", "")
        key = _stored_key()
        if backend not in BACKENDS or not key:
            return json.dumps({"backend": ""})
        authz.audit(ident, role, "runner_config", "allow", f"backend={backend}")
        return json.dumps({"backend": backend, "model": cfg.get("model", ""),
                           "key_env": BACKENDS[backend], "key": key})
