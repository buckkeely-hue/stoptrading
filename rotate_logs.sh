#!/bin/bash
# Hourly log rotation for launchd-managed logs (no root / newsyslog needed).
# When a log exceeds MAX_BYTES, keep the last KEEP_BYTES as a .1 backup and truncate
# the live file in place (copy-tail + truncate works with the writer's fd still open).
MAX_BYTES=$((50*1024*1024))   # rotate when logical size > 50MB
KEEP_BYTES=$((10*1024*1024))  # keep last 10MB in the .1 backup
for log in /Users/buckk/stoptrading/service.log /Users/buckk/stoptrading/server.log; do
    [ -f "$log" ] || continue
    sz=$(stat -f%z "$log" 2>/dev/null || echo 0)
    if [ "$sz" -gt "$MAX_BYTES" ]; then
        tail -c "$KEEP_BYTES" "$log" > "$log.1" 2>/dev/null
        : > "$log"
    fi
done
