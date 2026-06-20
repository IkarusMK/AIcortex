#!/bin/sh
# Tick loop: run the orchestrator once per interval. cron_due itself decides
# which jobs are actually due, so this just needs to tick often enough (default
# every 60s). Keep RUNNER_INTERVAL >= your finest cron granularity.
set -u

DIR="$(dirname "$0")"
INTERVAL="${RUNNER_INTERVAL:-60}"
echo "[runner] starting — interval=${INTERVAL}s, backend='${RUNNER_CMD:-claude -p}'"

while true; do
  if ! "$DIR/run-once.sh"; then
    echo "[runner] $(date -u +%FT%TZ) run failed — continuing"
  fi
  sleep "$INTERVAL"
done
