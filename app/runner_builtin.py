"""The runners at work: AICortex carries out its own due cron jobs.

Runners are set up in the WebUI (tab "Runner", runners.py). A thread in the server
process ticks once a minute, on the minute, and checks for due jobs without any model:
- a Claude cloud runner is woken right there, with its jobs (runner_cloud.py);
- every other runner asks a model, in one worker thread, so a long model run never
  holds up a wake-up: OpenRouter, OpenAI, Gemini and any OpenAI-compatible address
  (e.g. Ollama) through the chat completions API; an Anthropic key through the
  Messages API; the ChatGPT plan (Sign in with ChatGPT, chatgpt_signin.py) through
  OpenAI's Responses API, streamed and not stored, as OpenAI documents it.
Each job gets a conversation of its own. AICortex confines it to the job owner's area
itself (act-as) before the model sees it, ends that afterwards and records the run;
the model never sees a token or the runner plumbing. Its tool calls go through this
server's own /mcp endpoint as the runner (an in-memory token per runner,
agent_auth.internal_token), so they pass the same gates as any other caller: roles,
act-as, audit.
"""
import asyncio
import codecs
import contextlib
import json
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

import agent_auth
import chatgpt_signin
import cron
import netguard
import runner_cloud
import runner_state
import runners

ENDPOINTS = {
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "openai": "https://api.openai.com/v1/chat/completions",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    "anthropic": "https://api.anthropic.com/v1/messages",
    "chatgpt": f"{chatgpt_signin.RESOURCE}/responses",
}
MESSAGES_KINDS = ("anthropic",)
RESPONSES_KINDS = ("chatgpt",)
USAGE_PAGE = "https://chatgpt.com/settings/usage"
CLAUDE_DEFAULT_MODEL = "claude-sonnet-5-5"
CLAUDE_ALIASES = {"sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5",
                  "haiku": "claude-haiku-5-5"}
MAX_TOKENS = 8192
# Runner plumbing the model working on a job must not call.
# Runner plumbing a model working on a job must not call: AICortex hands out the
# jobs, confines each one to its owner and records the run itself.
BLOCKED = {"cron_due", "act_as_begin", "act_as_end", "cron_mark_run"}
MAX_STEPS = 25
JOB_BUDGET = 300           # seconds for one job's whole conversation with the model
PARALLEL = 4               # runners asking their models at the same time
TICK_OFFSET = 2            # seconds into the new minute, so the clock has turned
PROVIDER_TIMEOUT = 120     # seconds without any data from the model API
ANSWER_DEADLINE = 300      # seconds for one whole model answer, however it trickles
MAX_ANSWER = 8 * 1024 * 1024   # bytes; a longer model answer is cut off
MAX_ERROR = 64 * 1024          # bytes of an error answer that are read at all
PROMPT = (Path(__file__).parent / "runner_orchestrator.txt").read_text(encoding="utf-8")

_busy = threading.Lock()          # the worker that asks the models — one at a time


class Unauthorized(RuntimeError):
    """The model API refused the token (HTTP 401)."""


def seconds_to_next_tick(now: float, interval: int = 60) -> float:
    return interval - (now % interval) + TICK_OFFSET


def model_name(rec: dict) -> str:
    """The provider's own model name (a LiteLLM-style '<kind>/' prefix is dropped;
    for Claude, an alias like 'sonnet' becomes the full id and empty the default)."""
    model = rec.get("model", "")
    if rec.get("kind") in RESPONSES_KINDS:
        return model.removeprefix("openai/")
    if rec.get("kind") in MESSAGES_KINDS:
        model = model.removeprefix("anthropic/")
        return CLAUDE_ALIASES.get(model, model) or CLAUDE_DEFAULT_MODEL
    prefix = f"{rec.get('kind', '')}/"
    return model[len(prefix):] if model.startswith(prefix) else model


def endpoint(rec: dict) -> str:
    if rec.get("kind") == "compatible":
        return f"{rec.get('base_url', '').rstrip('/')}/chat/completions"
    return ENDPOINTS[rec["kind"]]


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


async def _post(url: str, payload: dict, headers: dict, host: str = "") -> dict:
    """One model API request; errors never show the key. With `host` (an address the
    person chose), the SSRF guard checks every address it connects to."""
    try:
        with netguard.guard(host) if host else contextlib.nullcontext():
            return await _send(url, payload, headers)
    except TimeoutError as exc:
        raise RuntimeError(f"the model did not finish within {ANSWER_DEADLINE} s") from exc


async def _send(url: str, payload: dict, headers: dict) -> dict:
    async with asyncio.timeout(ANSWER_DEADLINE), httpx.AsyncClient(timeout=PROVIDER_TIMEOUT) as http:
        async with http.stream("POST", url, json=payload, headers=headers) as r:
            if r.status_code >= 400:
                text = await _body(r, MAX_ERROR, cut=True)
                raise RuntimeError(f"model API answered HTTP {r.status_code}: {text[:200]}")
            return json.loads(await _body(r, MAX_ANSWER))


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


def _prompt(job: dict) -> str:
    listed = {k: job.get(k, "") for k in ("id", "name", "prompt", "notify")}
    return f"{PROMPT}\n\nThe job (JSON):\n{json.dumps(listed, ensure_ascii=False)}"


def _scrub(text: str, key: str) -> str:
    """An error for the state and the log: without the key, on one line."""
    return " ".join((text.replace(key, "***") if key else text).split())


async def _run_messages(client, job: dict, rec: dict, key: str) -> str:
    """Anthropic's Messages API with an API key."""
    headers = {"anthropic-version": "2023-06-01", "x-api-key": key}
    tools = [{"name": t.name, "description": (t.description or "")[:1024], "input_schema": _schema(t)}
             for t in _offered(await client.list_tools())]
    messages = [{"role": "user", "content": _prompt(job)}]
    text = ""
    for _ in range(MAX_STEPS):
        resp = await _post(ENDPOINTS["anthropic"], {
            "model": model_name(rec), "max_tokens": MAX_TOKENS,
            "messages": messages, "tools": tools}, headers)
        blocks = _dicts(resp.get("content"))
        messages.append({"role": "assistant", "content": blocks})
        text = "".join(str(b.get("text") or "") for b in blocks if b.get("type") == "text") or text
        uses = [b for b in blocks if b.get("type") == "tool_use"]
        if not uses:
            return text
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": u["id"],
             "content": await _tool(client, u["name"], u.get("input") or {})} for u in uses]})
    return text


async def _chatgpt_model(rec: dict) -> str:
    model = model_name(rec)
    if model:
        return model
    listed = await asyncio.to_thread(chatgpt_signin.models)
    if not listed:
        raise RuntimeError("the ChatGPT account lists no model")
    return listed[0]["slug"]


async def _run_responses(client, job: dict, rec: dict) -> str:
    """OpenAI's Responses API on a ChatGPT plan: store false, stream true, the history
    resent every turn (function calls without their item ids, no reasoning items)."""
    tools = [{"type": "function", "name": t.name, "description": (t.description or "")[:1024],
              "parameters": _schema(t), "strict": False}
             for t in _offered(await client.list_tools())]
    model = await _chatgpt_model(rec)
    items: list[dict] = [{"role": "user", "content": _prompt(job)}]
    url = ENDPOINTS["chatgpt"]
    last = ""
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
            last = text
        calls = [o for o in output if o.get("type") == "function_call"
                 and isinstance(o.get("call_id"), str) and isinstance(o.get("name"), str)]
        if not calls:
            return last
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
    return last


def _chat_target(rec: dict) -> tuple[str, str]:
    """(url, host to guard) — an address the person chose is checked again right now."""
    url = endpoint(rec)
    if rec.get("kind") != "compatible":
        return url, ""
    ok, reason = netguard.check_url(url)
    if not ok:
        raise RuntimeError(f"AICortex may not reach {urlsplit(url).hostname} — {reason}")
    return url, urlsplit(url).hostname or ""


def _chat_headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"} if key else {}


async def _run_chat(client, job: dict, rec: dict, key: str) -> str:
    """The OpenAI-compatible chat completions API (OpenRouter, OpenAI, Gemini, Ollama …)."""
    url, host = _chat_target(rec)
    model = model_name(rec)
    tools = [{"type": "function", "function": {
        "name": t.name, "description": (t.description or "")[:1024], "parameters": _schema(t)}}
        for t in _offered(await client.list_tools())]
    messages = [{"role": "user", "content": _prompt(job)}]
    text = ""
    for _ in range(MAX_STEPS):
        resp = await _post(url, {"model": model, "messages": messages, "tools": tools},
                           _chat_headers(key), host)
        msg = resp["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        turn = {"role": "assistant", "content": msg.get("content") or ""}
        text = str(turn["content"]) or text
        if calls:
            turn["tool_calls"] = calls
        messages.append(turn)
        if not calls:
            return text
        for call in calls:
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            out = await _tool(client, call["function"]["name"], args)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": out})
    return text


async def _ask_model(client, job: dict, rec: dict, key: str) -> str:
    """One job with the runner's model until it stops calling tools → its last text."""
    if rec["kind"] in RESPONSES_KINDS:
        return await _run_responses(client, job, rec)
    if rec["kind"] in MESSAGES_KINDS:
        return await _run_messages(client, job, rec, key)
    return await _run_chat(client, job, rec, key)


async def _one_job(client, job: dict, rec: dict, key: str) -> str:
    """One job, confined to its owner's area by AICortex itself (act-as) — the model
    never sees the token — within JOB_BUDGET; the confinement always ends."""
    token = job.get("act_as_token") or ""
    if token:
        out = _text(await client.call_tool("act_as_begin", {"act_as_token": token, "job_id": job["id"]},
                                           raise_on_error=False))
        if not out.startswith("Acting as"):
            raise RuntimeError(f"could not run as the job's owner — {out[:120]}")
    try:
        async with asyncio.timeout(JOB_BUDGET):
            return await _ask_model(client, job, rec, key)
    except TimeoutError as exc:
        raise RuntimeError(f"the job did not finish within {JOB_BUDGET} s") from exc
    finally:
        if token:
            await client.call_tool("act_as_end", {}, raise_on_error=False)


def _access(rec: dict) -> tuple[str, str]:
    """(key, problem): problem is "paid-model", "no-signin" or "no-key" when the model
    can't be asked — "paid-model" before any key is even read."""
    if runners.model_problem(rec):
        return "", "paid-model"
    if rec["kind"] in RESPONSES_KINDS:
        chatgpt_signin.keep_alive()
        return "", "" if chatgpt_signin.status()["plan"] else "no-signin"
    key = runners.resolve_key(rec)
    return key, "" if key or rec["kind"] == "compatible" else "no-key"


async def _run_runner(url: str, rec: dict) -> int:
    """One model runner's due jobs → how many it ran."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    rid = rec["id"]
    key, problem = await asyncio.to_thread(_access, rec)
    if problem:
        runner_state.put(rid, problem)
        return 0
    due = await asyncio.to_thread(cron.claim_due, rid)
    if not due:
        runner_state.put(rid, "idle")
        return 0
    transport = StreamableHttpTransport(
        url, headers={"Authorization": f"Bearer {agent_auth.internal_token(rid)}"})
    done, errors = [], []
    try:
        async with Client(transport) as client:
            for job in due:
                try:
                    result = await _one_job(client, job, rec, key)
                except Exception as exc:
                    errors.append(f"{job['id']}: {_scrub(str(exc), key)}")
                    continue
                await asyncio.to_thread(cron.mark_run, job["id"], _scrub(result, key)[:200] or "done")
                done.append(job["id"])
    except Exception as exc:
        errors.append(_scrub(str(exc), key))
    if errors:
        runner_state.put(rid, "error", "; ".join(errors))
        print(f"[AICortex] runner {rid}: {'; '.join(errors)[:300]}")
    else:
        runner_state.put(rid, "ran", f"{len(done)} job(s): {', '.join(done)}")
    return len(done)


def _enabled() -> list:
    return [r for r in runners.load()["runners"] if r.get("enabled")]


async def _run_models(url: str) -> int:
    """Every model runner, side by side (at most PARALLEL at once), so a slow one holds
    up nobody else; each has its own token, so their act-as runs stay apart."""
    gate = asyncio.Semaphore(PARALLEL)

    async def one(rec):
        async with gate:
            return await _run_runner(url, rec)
    done = await asyncio.gather(*(one(r) for r in _enabled() if r.get("kind") in runners.BUILTIN_KINDS))
    return sum(done)


def _wake_clouds(enabled: list) -> int:
    count = 0
    for rec in [r for r in enabled if r.get("kind") == runners.CLOUD]:
        try:
            count += runner_cloud.wake(rec)
        except Exception as exc:
            runner_state.put(rec["id"], "error", _scrub(str(exc), ""))
            print(f"[AICortex] runner {rec['id']}: {_scrub(str(exc), '')}")
    return count


async def run_once(url: str) -> int:
    """One whole minute, waiting for the models → how many jobs ran or were handed over."""
    enabled = _enabled()
    if enabled:
        cron._note_runner()
    woken = await asyncio.to_thread(_wake_clouds, enabled)
    return woken + await _run_models(url)


def _work(url: str) -> None:
    asyncio.run(_run_models(url))


def _start_worker(url: str) -> bool:
    """Ask the models in a worker thread — unless the last one is still at it."""
    if not _busy.acquire(blocking=False):
        return False

    def run():
        try:
            _work(url)
        except Exception as exc:
            print(f"[AICortex] runners: {exc}")
        finally:
            _busy.release()
    threading.Thread(target=run, name="runner-models", daemon=True).start()
    return True


def tick(url: str) -> None:
    """The minute: note that runners are on, wake the Claude cloud tasks right away,
    hand the model runners to the worker."""
    enabled = _enabled()
    if not enabled:
        return
    cron._note_runner()
    _wake_clouds(enabled)
    if any(r.get("kind") in runners.BUILTIN_KINDS for r in enabled):
        _start_worker(url)


def _loop(url: str) -> None:
    while True:
        time.sleep(seconds_to_next_tick(time.time()))
        try:
            tick(url)
        except Exception as exc:
            print(f"[AICortex] runners: {exc}")


def start(port: int) -> None:
    """Start the ticking thread (the server's own process; idle until a runner is on)."""
    threading.Thread(target=_loop, args=(f"http://127.0.0.1:{port}/mcp",),
                     name="runners", daemon=True).start()


# ── "Test now" in the WebUI ──────────────────────────────────────────────────
PROBE = "Reply with the single word OK."


async def _ask(rec: dict, key: str) -> str:
    """One tiny question without tools → the model's answer."""
    if rec["kind"] in MESSAGES_KINDS:
        resp = await _post(ENDPOINTS["anthropic"], {
            "model": model_name(rec), "max_tokens": 16,
            "messages": [{"role": "user", "content": PROBE}]},
            {"anthropic-version": "2023-06-01", "x-api-key": key})
        return "".join(str(b.get("text") or "") for b in _dicts(resp.get("content")))
    if rec["kind"] in RESPONSES_KINDS:
        token = await asyncio.to_thread(chatgpt_signin.access_token)
        resp = await _stream(ENDPOINTS["chatgpt"], {
            "model": await _chatgpt_model(rec), "input": [{"role": "user", "content": PROBE}],
            "store": False, "stream": True}, {"Authorization": f"Bearer {token}"})
        return "".join(str(c.get("text") or "") for o in _dicts(resp.get("output"))
                       for c in _dicts(o.get("content")) if c.get("type") == "output_text")
    url, host = _chat_target(rec)
    resp = await _post(url, {"model": model_name(rec), "max_tokens": 16,
                             "messages": [{"role": "user", "content": PROBE}]},
                       _chat_headers(key), host)
    return str(((_dicts(resp.get("choices")) or [{}])[0].get("message") or {}).get("content") or "")


async def probe(rec: dict) -> tuple[bool, str]:
    """(ok, message) for "Test now": a cloud task gets a test wake-up, a model a tiny
    question without tools — no job runs."""
    if rec.get("kind") == runners.CLOUD:
        return await asyncio.to_thread(runner_cloud.test, rec)
    key, problem = await asyncio.to_thread(_access, rec)
    if problem:
        return False, {"paid-model": runners.FREE_ONLY_NOTE,
                       "no-signin": "Not signed in with ChatGPT."}.get(problem, "No key stored yet.")
    try:
        answer = (await _ask(rec, key)).strip()
    except Exception as exc:
        return False, _scrub(str(exc), key)[:200]
    return True, f"The model answered: {answer[:80] or '(nothing)'}"
