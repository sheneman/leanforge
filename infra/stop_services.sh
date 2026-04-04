#!/usr/bin/env bash
# stop_services.sh - Stop all forge-lean-prover services started by start_services.sh.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PIDS_FILE="$REPO_ROOT/.pids"

if [ ! -f "$PIDS_FILE" ]; then
    echo "No .pids file found. Nothing to stop."
    exit 0
fi

echo "=== Stopping forge-lean-prover services ==="

while read -r pid svc port; do
    [ -z "$pid" ] && continue
    if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null
        echo "[OK] Stopped $svc (pid $pid, port $port)"
    else
        echo "[--] $svc (pid $pid) was not running."
    fi
done < "$PIDS_FILE"

rm -f "$PIDS_FILE"
echo "Done."
