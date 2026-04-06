#!/usr/bin/env python3
"""Verify a Lean 4 proof by compiling it via the lean_env service.

Usage:
    python3 scripts/verify.py <file_or_source>

Examples:
    # Verify a .lean file
    python3 scripts/verify.py /tmp/proof.lean

    # Verify inline source (use - to read from stdin)
    echo 'theorem test : 1 + 1 = 2 := by norm_num' | python3 scripts/verify.py -

    # Verify a string directly
    python3 scripts/verify.py "theorem test : 1 + 1 = 2 := by norm_num"

The script reads LEAN_ENV_URL from .env (defaults to http://localhost:8101).
Prints JSON result with success/diagnostics. Exits 0 if verified, 1 if not.
"""
import json
import os
import sys
from pathlib import Path

try:
    import httpx
except ImportError:
    # Fallback to urllib if httpx not installed
    import urllib.request
    import urllib.error

    def _post(url, data):
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return {"success": False, "diagnostics": [{"message": f"HTTP {e.code}: {e.read().decode()[:500]}"}]}
else:
    def _post(url, data):
        with httpx.Client(timeout=300) as client:
            resp = client.post(url, json=data)
            resp.raise_for_status()
            return resp.json()

# Load .env if present
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

LEAN_ENV_URL = os.getenv("LEAN_ENV_URL", "http://localhost:8101").rstrip("/")


def verify(source: str) -> dict:
    """Compile Lean source and return the result."""
    # Auto-add import if missing
    if "import " not in source.split("\n")[0]:
        source = "import Mathlib.Tactic\n\n" + source
    url = f"{LEAN_ENV_URL}/compile"
    return _post(url, {"source": source})


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)

    arg = sys.argv[1]

    if arg == "-":
        source = sys.stdin.read()
    elif os.path.isfile(arg):
        source = Path(arg).read_text()
    else:
        # Treat as inline source
        source = arg

    result = verify(source)
    print(json.dumps(result, indent=2))

    if result.get("success"):
        print("\n✓ VERIFIED", file=sys.stderr)
        sys.exit(0)
    else:
        diags = result.get("diagnostics", [])
        for d in diags:
            msg = d.get("message", "") if isinstance(d, dict) else str(d)
            print(f"  ✗ {msg}", file=sys.stderr)
        print("\n✗ FAILED", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
