"""Generic, LLM-agnostic autonomy runner for LLMConnector.

Connects to the connector over MCP using the static RUNNER_TOKEN, exposes its
tools to ANY LLM via LiteLLM (Anthropic, OpenAI, Google/Gemini, Ollama, Azure,
…), and runs an agentic loop that fires due cron jobs. Choose the model with
LLM_MODEL and provide that provider's API key (e.g. OPENAI_API_KEY,
GEMINI_API_KEY, ANTHROPIC_API_KEY) — whatever your provider needs.

Env:
  CONNECTOR_URL   e.g. https://agent.example.com/mcp
  RUNNER_TOKEN    the static token also set on the connector
  LLM_MODEL       LiteLLM model string (default "gpt-4o-mini"); examples:
                  "gpt-4o", "anthropic/claude-3-5-sonnet-latest",
                  "gemini/gemini-1.5-pro", "ollama/llama3.1"
  RUNNER_INTERVAL tick seconds (default 60); cron_due decides what's due
  RUNNER_MAX_STEPS safety cap on tool-call rounds per tick (default 25)
"""
import asyncio
import json
import os
import time
from pathlib import Path

import litellm
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

CONNECTOR_URL = os.environ["CONNECTOR_URL"]
RUNNER_TOKEN = os.environ["RUNNER_TOKEN"]
MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")
INTERVAL = int(os.environ.get("RUNNER_INTERVAL", "60"))
MAX_STEPS = int(os.environ.get("RUNNER_MAX_STEPS", "25"))

ORCH = (Path(__file__).parent / "orchestrator.txt").read_text(encoding="utf-8")


def _tools_to_openai(tools):
    out = []
    for t in tools:
        schema = getattr(t, "inputSchema", None) or {"type": "object", "properties": {}}
        out.append({"type": "function", "function": {
            "name": t.name,
            "description": (t.description or "")[:1024],
            "parameters": schema,
        }})
    return out


def _result_text(result) -> str:
    data = getattr(result, "data", None)
    if data is not None:
        try:
            return json.dumps(data, ensure_ascii=False, default=str)
        except Exception:
            return str(data)
    content = getattr(result, "content", result)
    parts = [getattr(b, "text", None) or str(b) for b in (content or [])]
    return "\n".join(parts) if parts else str(result)


async def run_once():
    transport = StreamableHttpTransport(
        CONNECTOR_URL, headers={"Authorization": f"Bearer {RUNNER_TOKEN}"})
    async with Client(transport) as client:
        tools = await client.list_tools()
        oai_tools = _tools_to_openai(tools)
        messages = [
            {"role": "system", "content": ORCH},
            {"role": "user", "content": "Run all jobs that are due now."},
        ]
        for _ in range(MAX_STEPS):
            resp = await litellm.acompletion(
                model=MODEL, messages=messages, tools=oai_tools, tool_choice="auto")
            msg = resp.choices[0].message
            messages.append(msg.model_dump() if hasattr(msg, "model_dump") else dict(msg))
            calls = getattr(msg, "tool_calls", None)
            if not calls:
                break
            for tc in calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except Exception:
                    args = {}
                try:
                    res = await client.call_tool(name, args)
                    out = _result_text(res)
                except Exception as exc:
                    out = f"ERROR: {exc}"
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "name": name, "content": out[:8000]})


def main():
    print(f"[runner] generic agent — model={MODEL}, interval={INTERVAL}s, connector={CONNECTOR_URL}")
    while True:
        try:
            asyncio.run(run_once())
        except Exception as exc:
            print(f"[runner] run failed: {exc}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
