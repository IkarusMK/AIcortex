"""The runner: AICortex fires its own due cron jobs with an API model.

Switched on in the WebUI (Scheduled jobs → Set up the runner). A thread in the server
process checks once a minute, on the minute, for due jobs without any model, and only
then asks the model:
- OpenRouter, OpenAI, Gemini: the provider's OpenAI-compatible chat API;
- a Claude subscription token (``claude setup-token``, which Anthropic documents for
  scripts making model requests) or an Anthropic key: Anthropic's Messages API — the
  token as an OAuth bearer, no Claude Code needed;
- a ChatGPT plan (Sign in with ChatGPT, chatgpt_signin.py): OpenAI's Responses API,
  streamed and not stored, as OpenAI documents it for open-source apps.
The model's tool calls go through this server's own /mcp endpoint as the runner
(agent_auth.INTERNAL_RUNNER_TOKEN, held in memory only), so they pass the same gates as
any other caller: roles, act-as, audit.
"""
import asyncio
import codecs
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

import agent_auth
import chatgpt_signin
import runner_setup

ENDPOINTS = {
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "openai": "https://api.openai.com/v1/chat/completions",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    "claude-abo": "https://api.anthropic.com/v1/messages",
    "claude-api": "https://api.anthropic.com/v1/messages",
    "chatgpt-abo": f"{chatgpt_signin.RESOURCE}/responses",
}
MESSAGES_BACKENDS = ("claude-abo", "claude-api")
RESPONSES_BACKENDS = ("chatgpt-abo",)
USAGE_PAGE = "https://chatgpt.com/settings/usage"
CLAUDE_DEFAULT_MODEL = "claude-sonnet-5-5"
CLAUDE_ALIASES = {"sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5",
                  "haiku": "claude-haiku-5-5"}
MAX_TOKENS = 8192
# Runner plumbing the model working on a job must not call.
BLOCKED = {"cron_due"}
MAX_STEPS = 25
TICK_OFFSET = 2            # seconds into the new minute, so the clock has turned
PROVIDER_TIMEOUT = 120     # seconds without any data from the model API
ANSWER_DEADLINE = 300      # seconds for one whole model answer, however it trickles
MAX_ANSWER = 8 * 1024 * 1024   # bytes; a longer model answer is cut off
MAX_ERROR = 64 * 1024          # bytes of an error answer that are read at all
PROMPT = (Path(__file__).parent / "runner_orchestrator.txt").read_text(encoding="utf-8")

_state = {"state": "", "detail": "", "at": ""}
_lock = threading.Lock()


def _set(state: str, detail: str = "") -> None:
    with _lock:
        _state.update(state=state, detail=detail[:200],
                      at=datetime.now(timezone.utc).isoformat(timespec="seconds"))


class Unauthorized(RuntimeError):
    """The model API refused the token (HTTP 401)."""


def status() -> dict:
    """off | waiting | idle | ran | no-key | no-signin | error, for the WebUI."""
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
    if cfg.get("backend") in RESPONSES_BACKENDS:
        return model.removeprefix("openai/")
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


def _dicts(value) -> list[dict]:
    """The objects in a list from a model API; anything else is skipped."""
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


async def _body(r, limit: int, cut: bool = False) -> str:
    """A response body as text, at most `limit` bytes: longer is an error — or, with
    cut=True (error answers), just its start."""
    raw = bytearray()
    async for chunk in r.aiter_bytes():
        raw += chunk
        if len(raw) > limit:
            if cut:
                break
            raise RuntimeError("the model's answer was too long")
    return raw[:limit].decode("utf-8", "replace")


async def _post(url: str, payload: dict, headers: dict) -> dict:
    """One model API request; errors never show the key."""
    try:
        async with asyncio.timeout(ANSWER_DEADLINE), httpx.AsyncClient(timeout=PROVIDER_TIMEOUT) as http:
            async with http.stream("POST", url, json=payload, headers=headers) as r:
                if r.status_code >= 400:
                    text = await _body(r, MAX_ERROR, cut=True)
                    raise RuntimeError(f"model API answered HTTP {r.status_code}: {text[:200]}")
                return json.loads(await _body(r, MAX_ANSWER))
    except TimeoutError as exc:
        raise RuntimeError(f"the model did not finish within {ANSWER_DEADLINE} s") from exc


def _api_error(text: str) -> str:
    """The error code in a model API's JSON error body, else the start of the body."""
    try:
        err = json.loads(text).get("error")
    except (ValueError, AttributeError):
        err = None
    if isinstance(err, dict) and (err.get("code") or err.get("message")):
        return str(err.get("code") or err.get("message"))[:200]
    return text[:200]


def _field(obj: dict, key: str) -> dict:
    value = obj.get(key)
    return value if isinstance(value, dict) else {}


def _event(payload: str) -> dict | None:
    """The final response for response.completed; RuntimeError for response.failed,
    response.incomplete or an error event; None for every other event."""
    try:
        event = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(event, dict):
        return None
    kind = event.get("type", "")
    response = _field(event, "response")
    if kind == "response.completed":
        return response
    if kind == "response.failed":
        code = str(_field(response, "error").get("code") or "unknown_error")
        if code == "subscription_sharing_usage_limit_exceeded":
            raise RuntimeError(f"ChatGPT usage limit reached ({code}) — see {USAGE_PAGE}")
        raise RuntimeError(f"the model answered response.failed: {code}")
    if kind == "response.incomplete":
        reason = _field(response, "incomplete_details").get("reason") or "unknown"
        raise RuntimeError(f"the model's answer was incomplete ({reason})")
    if kind == "error":
        raise RuntimeError(f"the model stream reported an error: "
                           f"{event.get('code') or event.get('message') or 'unknown'}")
    return None


class _SSE:
    """A server-sent event stream, fed line by line."""

    def __init__(self):
        self.data: list[str] = []

    def feed(self, line: str) -> dict | None:
        """The final response once response.completed arrives, else None."""
        line = line.rstrip("\r")
        if line.startswith("data:"):
            self.data.append(line[5:].strip())
            return None
        if line or not self.data:
            return None
        payload, self.data = "\n".join(self.data), []
        return None if payload == "[DONE]" else _event(payload)


def _parse_sse(lines, sse: _SSE | None = None) -> dict:
    """The final response object from (the rest of) a Responses API event stream. Only
    response.completed counts as success; response.failed, response.incomplete, an
    error event or a stream that just ends raise RuntimeError."""
    sse = sse or _SSE()
    for line in [*lines, ""]:
        response = sse.feed(line)
        if response is not None:
            return response
    raise RuntimeError("the model's stream ended without response.completed")


async def _read_sse(r) -> dict:
    """Read a Responses API stream up to response.completed — not further, and at
    most MAX_ANSWER bytes."""
    sse, decoder, rest, size = _SSE(), codecs.getincrementaldecoder("utf-8")("replace"), "", 0
    async for chunk in r.aiter_bytes():
        size += len(chunk)
        if size > MAX_ANSWER:
            raise RuntimeError("the model's answer was too long")
        *lines, rest = (rest + decoder.decode(chunk)).split("\n")
        for line in lines:
            response = sse.feed(line)
            if response is not None:
                return response
    return _parse_sse((rest + decoder.decode(b"", final=True)).split("\n"), sse)


async def _stream(url: str, payload: dict, headers: dict) -> dict:
    """One streamed Responses API request; errors never show the token."""
    try:
        async with asyncio.timeout(ANSWER_DEADLINE), httpx.AsyncClient(timeout=PROVIDER_TIMEOUT) as http:
            async with http.stream("POST", url, json=payload,
                                   headers={**headers, "Accept": "text/event-stream"}) as r:
                if r.status_code >= 400:
                    text = await _body(r, MAX_ERROR, cut=True)
                    if r.status_code == 401:
                        raise Unauthorized(f"model API answered HTTP 401: {_api_error(text)}")
                    raise RuntimeError(f"model API answered HTTP {r.status_code}: {_api_error(text)}")
                return await _read_sse(r)
    except TimeoutError as exc:
        raise RuntimeError(f"the model did not finish within {ANSWER_DEADLINE} s") from exc


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


async def _run_responses(client, due: list, cfg: dict) -> None:
    """OpenAI's Responses API on a ChatGPT plan: store false, stream true, the history
    resent every turn (function calls without their item ids, no reasoning items)."""
    tools = [{"type": "function", "name": t.name, "description": (t.description or "")[:1024],
              "parameters": _schema(t), "strict": False}
             for t in _offered(await client.list_tools())]
    model = model_name(cfg)
    if not model:
        listed = await asyncio.to_thread(chatgpt_signin.models)
        if not listed:
            raise RuntimeError("the ChatGPT account lists no model")
        model = listed[0]["slug"]
    items: list[dict] = [{"role": "user", "content": _prompt(due)}]
    url = ENDPOINTS[cfg["backend"]]
    for _ in range(MAX_STEPS):
        body = {"model": model, "input": items, "tools": tools, "store": False, "stream": True}
        token = await asyncio.to_thread(chatgpt_signin.access_token)
        try:
            resp = await _stream(url, body, {"Authorization": f"Bearer {token}"})
        except Unauthorized:
            token = await asyncio.to_thread(chatgpt_signin.access_token, True)
            resp = await _stream(url, body, {"Authorization": f"Bearer {token}"})
        output = _dicts(resp.get("output"))
        text = "".join(str(c.get("text") or "") for o in output if o.get("type") == "message"
                       for c in _dicts(o.get("content")) if c.get("type") == "output_text")
        if text:
            items.append({"role": "assistant", "content": text})
        calls = [o for o in output if o.get("type") == "function_call"
                 and isinstance(o.get("call_id"), str) and isinstance(o.get("name"), str)]
        if not calls:
            return
        calls = [dict(c, arguments=c.get("arguments") if isinstance(c.get("arguments"), str) else "{}")
                 for c in calls]
        items.extend({"type": "function_call", "call_id": c["call_id"], "name": c["name"],
                      "arguments": c["arguments"] or "{}"} for c in calls)
        for call in calls:
            try:
                args = json.loads(call["arguments"] or "{}")
            except ValueError:
                args = {}
            args = args if isinstance(args, dict) else {}
            items.append({"type": "function_call_output", "call_id": call["call_id"],
                          "output": await _tool(client, call["name"], args)})


async def _run_jobs(client, due: list, cfg: dict, key: str) -> None:
    """Run the due jobs with the set-up model until it stops calling tools."""
    if cfg["backend"] in RESPONSES_BACKENDS:
        await _run_responses(client, due, cfg)
        return
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
    if cfg.get("backend") in RESPONSES_BACKENDS:
        await asyncio.to_thread(chatgpt_signin.keep_alive)
        if not chatgpt_signin.status()["plan"]:
            _set("no-signin")
            return 0
        key = ""
    else:
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
