#!/bin/sh
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

# Ensure the writable mount points are owned by the runtime user.
for d in /data/memory /data/skills /data/services /data/vault /data/work /data/mqtt /data/ftp /data/mcp /data/coordination /data/cron /data/sessions /data/printers /data/scanners /data/auth /logs; do
  if [ -d "$d" ]; then
    chown -R "$PUID:$PGID" "$d" 2>/dev/null || true
  fi
done

echo "[entrypoint] starting as ${PUID}:${PGID}"
exec gosu "${PUID}:${PGID}" "$@"
