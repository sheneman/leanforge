#!/usr/bin/env bash
# start_services.sh - Launch all five forge-lean-prover services in the background.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

PIDS_FILE="$REPO_ROOT/.pids"

# Clear any stale PID file.
: > "$PIDS_FILE"

declare -A SERVICES=(
    ["orchestrator"]="services.orchestrator.main:app:8100"
    ["lean_env"]="services.lean_env.main:app:8101"
    ["proof_search"]="services.proof_search.main:app:8102"
    ["retrieval"]="services.retrieval.main:app:8103"
    ["telemetry"]="services.telemetry.main:app:8104"
)

echo "=== Starting forge-lean-prover services ==="

for svc in "${!SERVICES[@]}"; do
    IFS=":" read -r mod1 mod2 port <<< "${SERVICES[$svc]}"
    module="${mod1}:${mod2}"

    echo "[..] Starting $svc on port $port ..."
    uvicorn "$module" --host 127.0.0.1 --port "$port" \
        --log-level info \
        >> "$REPO_ROOT/data/$svc.log" 2>&1 &
    pid=$!
    echo "$pid $svc $port" >> "$PIDS_FILE"
    echo "[OK] $svc  pid=$pid  port=$port"
done

echo ""
echo "All services started.  PID file: $PIDS_FILE"
echo "Logs: data/<service>.log"
echo "Stop with:  bash infra/stop_services.sh"
