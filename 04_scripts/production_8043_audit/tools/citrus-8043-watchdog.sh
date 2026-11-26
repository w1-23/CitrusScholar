#!/usr/bin/env bash
set -u
unit=citrusscholar-8043-merged-web.service
state=/tmp/citrus-8043-watchdog.count
[ "$(systemctl --user is-active "$unit" 2>/dev/null)" = active ] || { echo 0 > "$state"; exit 0; }
since=$(date -d "$(systemctl --user show -p ActiveEnterTimestamp --value "$unit" 2>/dev/null)" +%s 2>/dev/null || echo 0)
now=$(date +%s)
if [ "$since" -gt 0 ] && [ $((now-since)) -lt 120 ]; then echo 0 > "$state"; exit 0; fi
if curl -fsS -m 5 -o /dev/null http://127.0.0.1:8043/ready 2>/dev/null; then echo 0 > "$state"; exit 0; fi
n=$(( $(cat "$state" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$state"
if [ "$n" -ge 3 ]; then systemctl --user restart "$unit"; echo 0 > "$state"; fi
