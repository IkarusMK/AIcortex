#!/bin/sh
# Run the autonomy orchestrator ONCE via the configured LLM backend.
#
# The backend is swappable via RUNNER_CMD. It must:
#   • accept the prompt as its final argument (headless / non-interactive), and
#   • already have the AICortex MCP connector available (tools callable).
#
# Reference backend = Claude Code:
#   RUNNER_CMD="claude -p --output-format text"
# Model auth is independent of the connector login:
#   • API key  → set ANTHROPIC_API_KEY  (the "Mittelweg": pay-per-use runner)
#   • or subscription → one-time `claude` OAuth login (no API key)
# Another LLM/agent CLI can be dropped in by overriding RUNNER_CMD.
set -eu

DIR="$(dirname "$0")"
PROMPT="$(cat "$DIR/orchestrator.txt")"

# Default backend: Claude Code, headless. Permissions must be pre-granted for
# unattended runs — see runner/README.md (allowedTools / permission mode).
: "${RUNNER_CMD:=claude -p --output-format text}"

# shellcheck disable=SC2086
exec $RUNNER_CMD "$PROMPT"
