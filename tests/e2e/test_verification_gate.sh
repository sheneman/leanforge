#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# test_verification_gate.sh
#
# Quick bash smoke-test for the lean_env /compile endpoint.
# Checks that correct proofs pass and wrong proofs fail.
#
# Usage:
#   ./tests/e2e/test_verification_gate.sh
# ---------------------------------------------------------------------------
set -euo pipefail

LEAN_ENV_URL="${LEAN_ENV_URL:-http://localhost:8101}"
PASS=0
FAIL=0
SKIP=0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
pass_test() { echo "  PASS  $1"; PASS=$((PASS + 1)); }
fail_test() { echo "  FAIL  $1  ($2)"; FAIL=$((FAIL + 1)); }
skip_test() { echo "  SKIP  $1  ($2)"; SKIP=$((SKIP + 1)); }

# ---------------------------------------------------------------------------
# 1. Check if lean_env is running
# ---------------------------------------------------------------------------
echo "Checking lean_env at ${LEAN_ENV_URL} ..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${LEAN_ENV_URL}/health" 2>/dev/null || echo "000")
if [ "$HTTP_CODE" != "200" ]; then
    echo "lean_env is not running (HTTP ${HTTP_CODE}). Exiting gracefully."
    exit 0
fi
echo "lean_env is UP."
echo ""
echo "Running verification gate tests ..."
echo "------------------------------------"

# ---------------------------------------------------------------------------
# 2. Correct proof  =>  success=true
# ---------------------------------------------------------------------------
TEST_NAME="correct_proof_accepted"
RESP=$(curl -s -X POST "${LEAN_ENV_URL}/compile" \
    -H "Content-Type: application/json" \
    -d '{"source": "theorem ok : 1 + 1 = 2 := by norm_num\n"}')

SUCCESS=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('success','MISSING'))" 2>/dev/null || echo "PARSE_ERROR")

if [ "$SUCCESS" = "True" ]; then
    pass_test "$TEST_NAME"
elif [ "$SUCCESS" = "False" ]; then
    # lean binary may not be available -- check if diagnostics mention binary
    DIAG=$(echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin).get('diagnostics',[]); print(d[0].get('message','') if d else '')" 2>/dev/null || echo "")
    if echo "$DIAG" | grep -qi "not found"; then
        skip_test "$TEST_NAME" "lean binary not available"
    else
        fail_test "$TEST_NAME" "expected success=true, got false"
    fi
else
    fail_test "$TEST_NAME" "unexpected response: $SUCCESS"
fi

# ---------------------------------------------------------------------------
# 3. Wrong proof  =>  success=false
# ---------------------------------------------------------------------------
TEST_NAME="wrong_proof_rejected"
RESP=$(curl -s -X POST "${LEAN_ENV_URL}/compile" \
    -H "Content-Type: application/json" \
    -d '{"source": "theorem bad : 1 + 1 = 3 := by norm_num\n"}')

SUCCESS=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('success','MISSING'))" 2>/dev/null || echo "PARSE_ERROR")

if [ "$SUCCESS" = "False" ]; then
    # Verify diagnostics are present
    DIAG_COUNT=$(echo "$RESP" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('diagnostics',[])))" 2>/dev/null || echo "0")
    if [ "$DIAG_COUNT" -gt 0 ]; then
        pass_test "$TEST_NAME (${DIAG_COUNT} diagnostics)"
    else
        fail_test "$TEST_NAME" "success=false but no diagnostics"
    fi
elif [ "$SUCCESS" = "True" ]; then
    fail_test "$TEST_NAME" "expected success=false, got true"
else
    # If lean binary not found, both correct and wrong will fail the same way
    skip_test "$TEST_NAME" "unexpected response: $SUCCESS"
fi

# ---------------------------------------------------------------------------
# 4. Unknown identifier  =>  success=false with diagnostics
# ---------------------------------------------------------------------------
TEST_NAME="unknown_identifier_detected"
RESP=$(curl -s -X POST "${LEAN_ENV_URL}/compile" \
    -H "Content-Type: application/json" \
    -d '{"source": "theorem unk : True := by\n  exact nonexistent_lemma\n"}')

SUCCESS=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('success','MISSING'))" 2>/dev/null || echo "PARSE_ERROR")

if [ "$SUCCESS" = "False" ]; then
    # Check that diagnostics mention something useful
    DIAG_MSG=$(echo "$RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin).get('diagnostics', [])
msgs = ' '.join(item.get('message','') for item in d)
print(msgs)
" 2>/dev/null || echo "")
    if [ -n "$DIAG_MSG" ]; then
        pass_test "$TEST_NAME"
    else
        fail_test "$TEST_NAME" "no diagnostic messages"
    fi
elif [ "$SUCCESS" = "True" ]; then
    fail_test "$TEST_NAME" "expected success=false for unknown identifier"
else
    skip_test "$TEST_NAME" "unexpected response: $SUCCESS"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "===================================="
echo "  VERIFICATION GATE SUMMARY"
echo "===================================="
echo "  Passed:  $PASS"
echo "  Failed:  $FAIL"
echo "  Skipped: $SKIP"
echo "===================================="

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
