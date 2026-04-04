#!/usr/bin/env bash
# bootstrap.sh - Set up the forge-lean-prover development environment.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

echo "=== forge-lean-prover bootstrap ==="
echo "Repository root: $REPO_ROOT"
echo ""

# ---------- 1. elan / Lean toolchain ----------
if command -v elan &>/dev/null; then
    echo "[OK] elan found: $(elan --version 2>&1 | head -1)"
else
    echo "[..] elan not found. Installing..."
    curl https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -sSf | sh -s -- -y --default-toolchain none
    # Refresh PATH so the rest of the script can see elan / lean.
    export PATH="$HOME/.elan/bin:$PATH"
    echo "[OK] elan installed: $(elan --version 2>&1 | head -1)"
fi

if command -v lean &>/dev/null; then
    echo "[OK] lean found: $(lean --version 2>&1 | head -1)"
else
    echo "[WARN] lean binary not on PATH. elan may need a shell restart."
fi

# ---------- 2. Python 3.11+ ----------
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" &>/dev/null; then
        ver=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        major="${ver%%.*}"
        minor="${ver##*.}"
        if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "[FAIL] Python 3.11+ is required but not found."
    exit 1
fi
echo "[OK] Python: $($PYTHON --version)"

# ---------- 3. Python venv + deps ----------
VENV_DIR="$REPO_ROOT/.venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "[..] Creating Python venv at $VENV_DIR ..."
    "$PYTHON" -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
echo "[OK] Activated venv: $VENV_DIR"

echo "[..] Installing Python dependencies (pip install -e '.[dev]') ..."
pip install --quiet --upgrade pip
pip install --quiet -e ".[dev]"
echo "[OK] Python dependencies installed."

# ---------- 4. Node / npm (for MCP servers) ----------
if command -v node &>/dev/null && command -v npm &>/dev/null; then
    echo "[OK] Node $(node --version) / npm $(npm --version)"
else
    echo "[WARN] node/npm not found. MCP servers require Node.js. Install it if you plan to use them."
fi

# ---------- 5. Lean / Lake build ----------
echo ""
echo "[..] Building Lean project (lake update, cache get, build) ..."
pushd "$REPO_ROOT/lean" >/dev/null

lake update
lake exe cache get || echo "[WARN] lake exe cache get had non-zero exit (cache may not be configured yet)."
lake build ForgeLean

popd >/dev/null
echo "[OK] Lean build complete."

# ---------- 6. Data directories ----------
echo ""
echo "[..] Ensuring data/ subdirectories exist ..."
mkdir -p "$REPO_ROOT/data/embeddings"
mkdir -p "$REPO_ROOT/data/telemetry"
mkdir -p "$REPO_ROOT/data/proofs"
mkdir -p "$REPO_ROOT/data/cache"
echo "[OK] data/ directories ready."

# ---------- 7. .env ----------
if [ ! -f "$REPO_ROOT/.env" ]; then
    if [ -f "$REPO_ROOT/.env.example" ]; then
        cp "$REPO_ROOT/.env.example" "$REPO_ROOT/.env"
        echo "[OK] Copied .env.example -> .env (edit it with your real keys)."
    else
        echo "[WARN] No .env.example found; skipping .env creation."
    fi
else
    echo "[OK] .env already exists."
fi

# ---------- Done ----------
echo ""
echo "========================================"
echo "  Bootstrap complete!"
echo "  Activate the venv:  source .venv/bin/activate"
echo "  Start services:     bash infra/start_services.sh"
echo "  Run smoke tests:    bash infra/smoke_test.sh"
echo "========================================"
