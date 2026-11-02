"""The runner: AICortex fires its own due cron jobs with an API model.

Switched on in the WebUI (Scheduled jobs → Set up the runner). A thread in the server
process checks once a minute, on the minute, for due jobs without any model, and only
then asks the model:
- OpenRouter, OpenAI, Gemini: the provider's OpenAI-compatible chat API;
- a Claude subscription token (``claude setup-token``, which Anthropic documents for
  scripts making model requests) or an Anthropic key: Anthropic's Messages API — the
  token as an OAuth bearer, no Claude Code needed.
The model's tool calls go through this server's own /mcp endpoint as the runner
(agent_auth.INTERNAL_RUNNER_TOKEN, held in memory only), so they pass the same gates as
any other caller: roles, act-as, audit.
"""
import asyncio
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

import agent_auth
import runner_setup

ENDPOINTS = {
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "openai": "https://api.openai.com/v1/chat/completions",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    "claude-abo": "https://api.anthropic.com/v1/messages",
    "claude-api": "https://api.anthropic.com/v1/messages",
}
MESSAGES_BACKENDS = ("claude-abo", "claude-api")
CLAUDE_DEFAULT_MODEL = "claude-sonnet-5-5"
CLAUDE_ALIASES = {"sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5",
                  "haiku": "claude-haiku-5-5"}
MAX_TOKENS = 8192
# Runner plumbing the model working on a job must not call.
BLOCKED = {"cron_due"}
MAX_STEPS = 25
TICK_OFFSET = 2            # seconds into the new minute, so the clock has turned
PROVIDER_TIMEOUT = 120
PROMPT = (Path(__file__).parent / "runner_orchestrator.txt").read_text(encoding="utf-8")

_state = {"state": "", "detail": "", "at": ""}
_lock = threading.Lock()


def _set(state: str, detail: str = "") -> None:
    with _lock:
        _state.update(state=state, detail=detail[:200],
                      at=datetime.now(timezone.utc).isoformat(timespec="seconds"))


def status() -> dict:
    """off | waiting | idle | ran | no-key | error, for the WebUI."""
    if not runner_setup.read_config().get("builtin"):
        return {"state": "off", "detail": "", "at": ""}
    with _lock:
        return dict(_state) if _state["state"] not in ("", "off") else {
            "state": "waiting", "detail": "", "at": ""}


def seconds_to_next_tick(now: float, interval: int = 60) -> float:
    return interval - (now % interval) + TICK_OFFSET


def model_name(cfg: dict) -> str:
    """The provider's own model name (a LiteLLM-style '<backend>/' prefix is dropped;
    for Claude, an alias like 'sonnet' becomes the full id and empty the default)."""
    model = cfg.get("model", "")
    if cfg.get("backend") in MESSAGES_BACKENDS:
        model = model.removeprefix("anthropic/")
        return CLAUDE_ALIASES.get(model, model) or CLAUDE_DEFAULT_MODEL
    prefix = f"{cfg.get('backend', '')}/"
    return model[len(prefix):] if model.startswith(prefix) else model


def _text(result) -> str:
    parts = [getattr(b, "text", "") or "" for b in (getattr(result, "content", None) or [])]
    return "".join(parts).strip()


def _schema(t) -> dict:
    return (getattr(t, "input_schema", None) or getattr(t, "inputSchema", None)
            or {"type": "object", "properties": {}})


def _offered(tools) -> list:
    return [t for t in tools if t.name not in BLOCKED]


async def _post(url: str, payload: dict, headers: dict) -> dict:
    """One model API request; errors never show the key."""
    async with httpx.AsyncClient(timeout=PROVIDER_TIMEOUT) as http:
        r = await http.post(url, json=payload, headers=headers)
    if r.status_code >= 400:
        raise RuntimeError(f"model API answered HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


async def _tool(client, name: str, args: dict) -> str:
    if name in BLOCKED or (name == "integration_call" and args.get("name") in BLOCKED):
        return "Refused: runner plumbing is not available to jobs."
    try:
        return _text(await client.call_tool(name, args, raise_on_error=False))[:8000]
    except Exception as exc:
        return f"ERROR: {exc}"


def _prompt(due: list) -> str:
    return (f"{PROMPT}\n\nDue jobs (JSON, already fetched with cron_due — do not call it):\n"
            f"{json.dumps(due, ensure_ascii=False)}")


async def _run_messages(client, due: list, cfg: dict, key: str) -> None:
    """Anthropic's Messages API: a subscription token as an OAuth bearer, an API key
    as x-api-key."""
    headers = {"anthropic-version": "2023-06-01"}
    if cfg["backend"] == "claude-abo":
        headers.update({"Authorization": f"Bearer {key}", "anthropic-beta": "oauth-2025-04-20"})
    else:
        headers["x-api-key"] = key
    tools = [{"name": t.name, "description": (t.description or "")[:1024], "input_schema": _schema(t)}
             for t in _offered(await client.list_tools())]
    messages = [{"role": "user", "content": _prompt(due)}]
    for _ in range(MAX_STEPS):
        resp = await _post(ENDPOINTS[cfg["backend"]], {
            "model": model_name(cfg), "max_tokens": MAX_TOKENS,
            "messages": messages, "tools": tools}, headers)
        blocks = resp.get("content") or []
        messages.append({"role": "assistant", "content": blocks})
        uses = [b for b in blocks if b.get("type") == "tool_use"]
        if not uses:
            return
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": u["id"],
             "content": await _tool(client, u["name"], u.get("input") or {})} for u in uses]})


async def _run_jobs(client, due: list, cfg: dict, key: str) -> None:
    """Run the due jobs with the set-up model until it stops calling tools."""
    if cfg["backend"] in MESSAGES_BACKENDS:
        await _run_messages(client, due, cfg, key)
        return
    url, model = ENDPOINTS[cfg["backend"]], model_name(cfg)
    tools = [{"type": "function", "function": {
        "name": t.name, "description": (t.description or "")[:1024], "parameters": _schema(t)}}
        for t in _offered(await client.list_tools())]
    messages = [{"role": "user", "content": _prompt(due)}]
    headers = {"Authorization": f"Bearer {key}"}
    for _ in range(MAX_STEPS):
        resp = await _post(url, {"model": model, "messages": messages, "tools": tools}, headers)
        msg = resp["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        turn = {"role": "assistant", "content": msg.get("content") or ""}
        if calls:
            turn["tool_calls"] = calls
        messages.append(turn)
        if not calls:
            return
        for call in calls:
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            out = await _tool(client, call["function"]["name"], args)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": out})


async def run_once(url: str) -> int:
    """One check against the connector at `url` (…/mcp); returns how many jobs ran."""
    cfg = runner_setup.read_config()
    if not cfg.get("builtin"):
        _set("off")
        return 0
    key = runner_setup._stored_key()
    if not key:
        _set("no-key")
        return 0
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    transport = StreamableHttpTransport(
        url, headers={"Authorization": f"Bearer {agent_auth.INTERNAL_RUNNER_TOKEN}"})
    async with Client(transport) as client:
        text = _text(await client.call_tool("cron_due", {}, raise_on_error=False))
        if not text.startswith("["):
            raise RuntimeError(f"cron_due: {text[:200]}")
        due = json.loads(text)
        if not due:
            _set("idle")
            return 0
        await _run_jobs(client, due, cfg, key)
    _set("ran", f"{len(due)} job(s): {', '.join(j['id'] for j in due)}")
    return len(due)


def _loop(url: str) -> None:
    while True:
        time.sleep(seconds_to_next_tick(time.time()))
        try:
            asyncio.run(run_once(url))
        except Exception as exc:
            _set("error", str(exc))
            print(f"[AICortex] built-in runner: {exc}")


def start(port: int) -> None:
    """Start the checking thread (the server's own process; idle until switched on)."""
    threading.Thread(target=_loop, args=(f"http://127.0.0.1:{port}/mcp",),
                     name="builtin-runner", daemon=True).start()
