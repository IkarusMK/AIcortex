#!/bin/sh
# Runner tick loop: one tick per RUNNER_INTERVAL, aligned to the clock, so a slow
# run never skips a minute (the connector also catches up a few missed minutes).
set -u
DIR="$(dirname "$0")"
INTERVAL="${RUNNER_INTERVAL:-60}"

if [ -n "${RUNNER_TOKEN:-}" ]; then
  # The connector as the agent's only MCP server, with the runner key as bearer.
  : "${CONNECTOR_URL:?set CONNECTOR_URL (…/mcp) together with RUNNER_TOKEN}"
  umask 077
  printf '{"mcpServers":{"aicortex":{"type":"http","url":"%s","headers":{"Authorization":"Bearer %s"}}}}\n' \
    "$CONNECTOR_URL" "$RUNNER_TOKEN" > "${HOME:-/config}/aicortex-mcp.json"
fi

echo "[runner] starting — every ${INTERVAL}s at the boundary, backend='${RUNNER_CMD:-claude -p}'"
while true; do
  if ! "$DIR/run-once.sh"; then
    echo "[runner] $(date -u +%FT%TZ) run failed — continuing"
  fi
  NOW=$(date +%s)
  sleep $(( INTERVAL - NOW % INTERVAL + 2 ))
done
