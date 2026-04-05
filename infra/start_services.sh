#!/usr/bin/env bash
# start_services.sh - Launch all five forge-lean-prover services in the background.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# Ensure elan/lean/lake are on PATH
if [ -d "$HOME/.elan/bin" ]; then
    export PATH="$HOME/.elan/bin:$PATH"
fi

PIDS_FILE="$REPO_ROOT/.pids"
: > "$PIDS_FILE"

echo "=== Starting forge-lean-prover services ==="

start_svc() {
    local name="$1" module="$2" port="$3"
    echo "[..] Starting $name on port $port ..."
    uvicorn "$module" --host 127.0.0.1 --port "$port" \
        --log-level info \
        >> "$REPO_ROOT/data/${name}.log" 2>&1 &
    local pid=$!
    echo "$pid $name $port" >> "$PIDS_FILE"
    echo "[OK] $name  pid=$pid  port=$port"
}

start_svc orchestrator   "services.orchestrator.main:app"   8100
start_svc lean_env       "services.lean_env.main:app"       8101
start_svc proof_search   "services.proof_search.main:app"   8102
start_svc retrieval      "services.retrieval.main:app"       8103
start_svc telemetry      "services.telemetry.main:app"       8104

echo ""
echo "All services started.  PID file: $PIDS_FILE"
echo "Logs: data/<service>.log"
echo "Stop with:  bash infra/stop_services.sh"
