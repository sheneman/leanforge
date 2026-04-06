#!/usr/bin/env python3
"""Call Leanstral to synthesize proof tactics for a theorem.

Usage:
    python3 scripts/synthesize.py "theorem even_add (a b : Nat) (ha : Even a) (hb : Even b) : Even (a + b)"
    python3 scripts/synthesize.py "theorem test : 1 + 1 = 2" --hints "norm_num closes numeric goals"

Reads LLM_API_BASE, LLM_API_KEY, LEANSTRAL_API_MODEL from .env.
"""
import json
import os
import sys
from pathlib import Path

try:
    import httpx
    def _post(url, headers, data):
        with httpx.Client(timeout=90) as client:
            resp = client.post(url, headers=headers, json=data)
            resp.raise_for_status()
            return resp.json()
except ImportError:
    import urllib.request
    def _post(url, headers, data):
        req = urllib.request.Request(url, data=json.dumps(data).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read())

_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

LLM_API_BASE = os.getenv("LLM_API_BASE", "").rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LEANSTRAL_API_MODEL = os.getenv("LEANSTRAL_API_MODEL", "")


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/synthesize.py <theorem_statement> [--hints <context>]")
        sys.exit(2)

    if not LLM_API_BASE or not LLM_API_KEY or not LEANSTRAL_API_MODEL:
        print("ERROR: Set LLM_API_BASE, LLM_API_KEY, LEANSTRAL_API_MODEL in .env", file=sys.stderr)
        sys.exit(1)

    theorem = sys.argv[1]
    hints = ""
    if "--hints" in sys.argv:
        idx = sys.argv.index("--hints")
        hints = sys.argv[idx + 1]

    user_msg = f"Prove: {theorem}"
    if hints:
        user_msg += f"\n\nRelevant lemmas:\n{hints}"

    url = f"{LLM_API_BASE}/chat/completions"
    result = _post(url, {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }, {
        "model": LEANSTRAL_API_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a Lean 4 proof-synthesis assistant. "
                    "Return ONLY the tactic-mode proof body. "
                    "No imports, no theorem declaration, no code fences, no explanation. "
                    "Just the tactics that go after := by"
                ),
            },
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": 1024,
        "temperature": 0.6,
    })

    content = result["choices"][0]["message"]["content"]
    print(content)


if __name__ == "__main__":
    main()
