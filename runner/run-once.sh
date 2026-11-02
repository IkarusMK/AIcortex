#!/bin/sh
# One runner tick. With RUNNER_TOKEN set, ask the connector what is due first
# (due.mjs, no model) and start the agent only when a job is due — with the jobs
# already in the prompt. Without RUNNER_TOKEN (older setups that logged the agent
# into the connector themselves) the agent asks cron_due itself, as before.
set -eu
DIR="$(dirname "$0")"
PROMPT="$(cat "$DIR/orchestrator.txt")"
MCP_CONFIG="${HOME:-/config}/aicortex-mcp.json"

if [ -n "${RUNNER_TOKEN:-}" ]; then
  DUE="$(node "$DIR/due.mjs")"
  case "$DUE" in
    "[]") exit 0 ;;
    "["*) ;;
    *) echo "[runner] cron_due: $DUE" >&2; exit 1 ;;
  esac
  echo "[runner] $(date -u +%FT%TZ) jobs due — starting the agent"
  PROMPT="$PROMPT

Due jobs (JSON, already fetched with cron_due — do not call it):
$DUE"
  if [ -z "${RUNNER_CMD:-}" ]; then
    # The prompt right after -p: the tool-list options take several values and
    # would swallow a trailing prompt.
    exec claude -p "$PROMPT" --output-format text --strict-mcp-config \
      --mcp-config "$MCP_CONFIG" --allowedTools mcp__aicortex \
      --disallowedTools "mcp__aicortex__cron_due,mcp__aicortex__runner_config"
  fi
fi
: "${RUNNER_CMD:=claude -p --output-format text}"
exec $RUNNER_CMD "$PROMPT"
