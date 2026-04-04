#!/usr/bin/env bash
# smoke_test.sh - Quick health check for the forge-lean-prover stack.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

PASS=0
FAIL=0
RESULTS=()

check() {
    local name="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        RESULTS+=("[PASS] $name")
        ((PASS++))
    else
        RESULTS+=("[FAIL] $name")
        ((FAIL++))
    fi
}

echo "=== forge-lean-prover smoke tests ==="
echo ""

# ---------- 1. lean --version ----------
check "lean --version" lean --version

# ---------- 2. lake build ----------
check "lake build (lean/)" bash -c "cd '$REPO_ROOT/lean' && lake build ForgeLean"

# ---------- 3. Mathlib cache ----------
if [ -d "$REPO_ROOT/lean/.lake/packages/mathlib" ]; then
    RESULTS+=("[PASS] mathlib cache exists")
    ((PASS++))
else
    RESULTS+=("[FAIL] mathlib cache missing (.lake/packages/mathlib/)")
    ((FAIL++))
fi

# ---------- 4. Service health checks ----------
declare -A SERVICES=(
    ["lean_env"]="8101"
    ["proof_search"]="8102"
    ["retrieval"]="8103"
    ["telemetry"]="8104"
    ["orchestrator"]="8100"
)

for svc in "${!SERVICES[@]}"; do
    port="${SERVICES[$svc]}"
    module="services.${svc}.main:app"

    # Start the service in the background.
    uvicorn "$module" --host 127.0.0.1 --port "$port" &>/dev/null &
    pid=$!

    # Give it a moment to start.
    sleep 2

    if curl -sf "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
        RESULTS+=("[PASS] $svc /health (port $port)")
        ((PASS++))
    else
        RESULTS+=("[FAIL] $svc /health (port $port)")
        ((FAIL++))
    fi

    kill "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null
done

# ---------- 5. Compile trivial theorem via lean_env ----------
echo "[..] Testing /compile on lean_env ..."
uvicorn services.lean_env.main:app --host 127.0.0.1 --port 8101 &>/dev/null &
LEAN_PID=$!
sleep 2

COMPILE_RESP=$(curl -sf -X POST "http://127.0.0.1:8101/compile" \
    -H "Content-Type: application/json" \
    -d '{"source": "theorem trivial_test : 1 + 1 = 2 := by norm_num"}' 2>&1)

if echo "$COMPILE_RESP" | grep -qi '"success".*true\|"status".*"verified"\|"ok"'; then
    RESULTS+=("[PASS] /compile trivial theorem")
    ((PASS++))
else
    RESULTS+=("[FAIL] /compile trivial theorem (response: $COMPILE_RESP)")
    ((FAIL++))
fi

kill "$LEAN_PID" 2>/dev/null
wait "$LEAN_PID" 2>/dev/null

# ---------- 6. .mcp.json ----------
if [ -f "$REPO_ROOT/.mcp.json" ]; then
    RESULTS+=("[PASS] .mcp.json exists")
    ((PASS++))
else
    RESULTS+=("[FAIL] .mcp.json missing")
    ((FAIL++))
fi

# ---------- Summary ----------
echo ""
echo "=== Smoke Test Results ==="
for r in "${RESULTS[@]}"; do
    echo "  $r"
done
echo ""
echo "Total: $PASS passed, $FAIL failed."

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
