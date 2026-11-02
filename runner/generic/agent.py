"""AICortex runner: fires due cron jobs with the model set up in the WebUI.

Every RUNNER_INTERVAL seconds, aligned to the clock, it asks the connector for
due jobs — without any model, so an idle minute costs nothing. Only when a job is
due does it fetch the model access set up in the WebUI (``runner_config``) and
start the model with the due jobs:

- Claude (subscription token or Anthropic key): the Claude Code CLI, with the
  connector as its only MCP server;
- OpenRouter, Anthropic, OpenAI, Gemini: LiteLLM, with the connector's tools.

Without a WebUI setup it falls back to LLM_MODEL and the provider key in the
environment (the behaviour of earlier versions).

Env:
  CONNECTOR_URL    e.g. https://agent.example.com/mcp
  RUNNER_TOKEN     a runner key from the WebUI, or the connector's RUNNER_TOKEN
  RUNNER_INTERVAL  seconds between checks, aligned to the clock (default 60)
  RUNNER_MAX_STEPS tool-call rounds per run with LiteLLM (default 25)
  LLM_MODEL        fallback LiteLLM model string when the WebUI has no setup
"""
import asyncio
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import litellm
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

CONNECTOR_URL = os.environ["CONNECTOR_URL"]
RUNNER_TOKEN = os.environ["RUNNER_TOKEN"]
MODEL = os.environ.get("LLM_MODEL", "")
INTERVAL = int(os.environ.get("RUNNER_INTERVAL", "60"))
MAX_STEPS = int(os.environ.get("RUNNER_MAX_STEPS", "25"))
CLAUDE_TIMEOUT = int(os.environ.get("RUNNER_CLAUDE_TIMEOUT", "1800"))
TICK_OFFSET = 2          # seconds into the new minute, so the server clock has turned
CLAUDE_BACKENDS = ("claude-abo", "claude-api")
LITELLM_PREFIX = {"openrouter": "openrouter/", "anthropic": "anthropic/", "gemini": "gemini/",
                  "openai": ""}
# Runner plumbing the model working on a job must not call.
BLOCKED = {"runner_config", "cron_due"}

ORCH = (Path(__file__).parent / "orchestrator.txt").read_text(encoding="utf-8")


def seconds_to_next_tick(now: float, interval: int = INTERVAL) -> float:
    """Sleep until the next interval boundary, however long the last run took."""
    return interval - (now % interval) + TICK_OFFSET


def _text(result) -> str:
    parts = [getattr(b, "text", "") or "" for b in (getattr(result, "content", None) or [])]
    text = "".join(parts).strip()
    if text:
        return text
    data = getattr(result, "data", None)
    return data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str)


def _tools_to_openai(tools):
    out = []
    for t in tools:
        if t.name in BLOCKED:
            continue
        schema = getattr(t, "inputSchema", None) or {"type": "object", "properties": {}}
        out.append({"type": "function", "function": {
            "name": t.name,
            "description": (t.description or "")[:1024],
            "parameters": schema,
        }})
    return out


async def fetch_due(client) -> list:
    text = _text(await client.call_tool("cron_due", {}, raise_on_error=False))
    if not text.startswith("["):
        raise RuntimeError(f"cron_due: {text[:200]}")
    return json.loads(text)


async def fetch_config(client) -> dict:
    """The WebUI setup, or {} (not set up, or an older connector without it)."""
    text = _text(await client.call_tool("runner_config", {}, raise_on_error=False))
    try:
        cfg = json.loads(text)
    except ValueError:
        print(f"[runner] no model access from the connector: {text[:120]}")
        return {}
    return cfg if isinstance(cfg, dict) and cfg.get("backend") else {}


def build_prompt(due: list) -> str:
    return (f"{ORCH}\n\nDue jobs (JSON, already fetched with cron_due — do not call it):\n"
            f"{json.dumps(due, ensure_ascii=False)}")


def litellm_model(cfg: dict) -> str:
    prefix = LITELLM_PREFIX.get(cfg["backend"], "")
    model = cfg.get("model", "")
    return model if not prefix or model.startswith(prefix) else prefix + model


async def run_litellm(client, prompt: str, model: str, api_key=None) -> None:
    tools = _tools_to_openai(await client.list_tools())
    messages = [{"role": "user", "content": prompt}]
    for _ in range(MAX_STEPS):
        resp = await litellm.acompletion(model=model, messages=messages, tools=tools,
                                         tool_choice="auto", api_key=api_key)
        msg = resp.choices[0].message
        messages.append(msg.model_dump() if hasattr(msg, "model_dump") else dict(msg))
        calls = getattr(msg, "tool_calls", None)
        if not calls:
            break
        for tc in calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except ValueError:
                args = {}
            if name in BLOCKED or (name == "integration_call" and args.get("name") in BLOCKED):
                out = "Refused: runner plumbing is not available to jobs."
            else:
                try:
                    out = _text(await client.call_tool(name, args, raise_on_error=False))
                except Exception as exc:
                    out = f"ERROR: {exc}"
            messages.append({"role": "tool", "tool_call_id": tc.id, "name": name,
                             "content": out[:8000]})


def run_claude(prompt: str, cfg: dict) -> None:
    """One Claude Code run with the connector as its only MCP server."""
    config = {"mcpServers": {"aicortex": {"type": "http", "url": CONNECTOR_URL,
                                          "headers": {"Authorization": f"Bearer {RUNNER_TOKEN}"}}}}
    fd, path = tempfile.mkstemp(prefix="aicortex-mcp-", suffix=".json")   # mode 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(config, fh)
        # The prompt right after -p: the tool-list options take several values and
        # would swallow a trailing prompt.
        cmd = ["claude", "-p", prompt, "--output-format", "text", "--strict-mcp-config",
               "--mcp-config", path, "--allowedTools", "mcp__aicortex",
               "--disallowedTools", ",".join(f"mcp__aicortex__{t}" for t in sorted(BLOCKED))]
        if cfg.get("model"):
            cmd += ["--model", cfg["model"]]
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")}
        env[cfg["key_env"]] = cfg["key"]
        res = subprocess.run(cmd, env=env, capture_output=True, text=True,
                             timeout=CLAUDE_TIMEOUT)
        tail = (res.stdout or res.stderr or "").strip()[-600:]
        print(f"[runner] claude exited {res.returncode}: {tail}")
    finally:
        os.unlink(path)


async def run_once() -> int:
    """Handle what is due now; returns how many jobs were due."""
    transport = StreamableHttpTransport(
        CONNECTOR_URL, headers={"Authorization": f"Bearer {RUNNER_TOKEN}"})
    async with Client(transport) as client:
        due = await fetch_due(client)
        if not due:
            return 0
        print(f"[runner] {len(due)} job(s) due: {', '.join(j.get('id', '?') for j in due)}")
        cfg = await fetch_config(client)
        prompt = build_prompt(due)
        if cfg.get("backend") in CLAUDE_BACKENDS:
            pass                                  # runs below, after the session closes
        elif cfg:
            await run_litellm(client, prompt, litellm_model(cfg), api_key=cfg.get("key"))
            return len(due)
        elif MODEL:
            await run_litellm(client, prompt, MODEL)
            return len(due)
        else:
            print("[runner] no model set up — choose one under Scheduled jobs → Set up the runner")
            return len(due)
    await asyncio.to_thread(run_claude, prompt, cfg)
    return len(due)


def main():
    print(f"[runner] checking every {INTERVAL}s at the boundary, connector={CONNECTOR_URL}")
    while True:
        try:
            asyncio.run(run_once())
        except Exception as exc:
            print(f"[runner] run failed: {exc}")
        time.sleep(seconds_to_next_tick(time.time()))


if __name__ == "__main__":
    main()
