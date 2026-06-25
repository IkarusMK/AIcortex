#!/bin/sh
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

# Create the persistent roots and hand them to the runtime user. The app creates
# every subfolder under /data itself (mkdir -p) — memory, skills, coordination,
# sessions, candidates, … — so new data folders never need a compose/entrypoint
# change.
mkdir -p /data /data/work /logs 2>/dev/null || true
chown -R "$PUID:$PGID" /logs 2>/dev/null || true

# /data: chown each top-level entry recursively EXCEPT the work hub, which may be
# large and live on an HDD — recursing it on every restart would be slow. The work
# mountpoint itself is chowned shallowly; the app writes new files there as the
# runtime user anyway.
find /data -mindepth 1 -maxdepth 1 ! -name work -exec chown -R "$PUID:$PGID" {} + 2>/dev/null || true
chown "$PUID:$PGID" /data /data/work 2>/dev/null || true

echo "[entrypoint] starting as ${PUID}:${PGID}"
exec gosu "${PUID}:${PGID}" "$@"
