#!/usr/bin/env python3
"""Search mathlib for relevant lemmas via the retrieval service.

Usage:
    python3 scripts/search.py "sum of two even numbers"
    python3 scripts/search.py "continuous compact bounded" --top_k 5

Reads RETRIEVAL_URL from .env.
"""
import json
import os
import sys
from pathlib import Path

try:
    import httpx
    def _post(url, data):
        with httpx.Client(timeout=30) as client:
            resp = client.post(url, json=data)
            resp.raise_for_status()
            return resp.json()
except ImportError:
    import urllib.request
    def _post(url, data):
        req = urllib.request.Request(url, data=json.dumps(data).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

RETRIEVAL_URL = os.getenv("RETRIEVAL_URL", "http://localhost:8103").rstrip("/")


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/search.py <query> [--top_k N]")
        sys.exit(2)

    query = sys.argv[1]
    top_k = 10
    if "--top_k" in sys.argv:
        idx = sys.argv.index("--top_k")
        top_k = int(sys.argv[idx + 1])

    result = _post(f"{RETRIEVAL_URL}/search", {"query": query, "top_k": top_k})

    for r in result.get("results", []):
        score = r.get("score", 0)
        name = r.get("name", "?")
        stmt = r.get("statement", "")[:120]
        print(f"  {score:.3f}  {name:40s}  {stmt}")


if __name__ == "__main__":
    main()
