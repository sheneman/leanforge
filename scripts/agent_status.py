#!/usr/bin/env python3
"""Check status of proof agent sessions.

Usage:
    python3 scripts/agent_status.py                    # list all sessions
    python3 scripts/agent_status.py collatz_descent    # detailed status
    python3 scripts/agent_status.py collatz_descent --turns 10
    python3 scripts/agent_status.py collatz_descent --strategies
    python3 scripts/agent_status.py collatz_descent --promising
"""
import json
import os
import sys
from pathlib import Path

# Load .env
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

from pymongo import MongoClient

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "leanforge")

db = MongoClient(MONGO_URI)[MONGO_DB]


def list_sessions():
    sessions = list(db.sessions.find().sort("updated_at", -1))
    if not sessions:
        print("No sessions found.")
        return
    print(f"{'SESSION':<30} {'STATUS':<15} {'TURNS':<8} {'UPDATED'}")
    print("-" * 80)
    for s in sessions:
        print(f"{s['_id']:<30} {s['status']:<15} {s['total_turns']:<8} {str(s.get('updated_at', ''))[:19]}")


def show_session(session_id, show_turns=5, show_strategies=False, show_promising=False):
    s = db.sessions.find_one({"_id": session_id})
    if not s:
        print(f"Session '{session_id}' not found.")
        sys.exit(1)

    print(f"Session:  {s['_id']}")
    print(f"Status:   {s['status']}")
    print(f"Problem:  {s['problem'][:150]}")
    print(f"Lean:     {s['lean_statement'][:150]}")
    print(f"Turns:    {s['total_turns']}")
    print(f"Updated:  {s.get('updated_at', '')}")

    if s.get("verified_proof"):
        print(f"\n=== VERIFIED PROOF ===")
        print(s["verified_proof"])
        return

    if s.get("best_partial_proof"):
        print(f"\nBest partial proof:")
        print(s["best_partial_proof"][:500])

    # Strategies summary
    strats = list(db.strategies.find({"session_id": session_id}))
    dead = [s for s in strats if s.get("outcome") == "dead_end"]
    promising = [s for s in strats if s.get("outcome") in ("promising", "partial")]
    print(f"\nStrategies: {len(strats)} total, {len(dead)} dead ends, {len(promising)} promising")

    if show_strategies or show_promising:
        target = promising if show_promising else strats
        label = "Promising" if show_promising else "All"
        print(f"\n=== {label} strategies ===")
        for st in target:
            icon = "✗" if st.get("outcome") == "dead_end" else "~" if st.get("outcome") == "partial" else "✓"
            print(f"  [{icon}] {st['name']}")
            if st.get("description"):
                print(f"      {st['description'][:200]}")

    if show_turns > 0:
        turns = list(db.turns.find({"session_id": session_id}).sort("turn", -1).limit(show_turns))
        print(f"\n=== Last {len(turns)} turns ===")
        for t in reversed(turns):
            icon = "✓" if t["result"] == "verified" else "✗" if not t.get("promising") else "~"
            diag = "; ".join(t.get("diagnostics", [])[:2])[:100]
            print(f"  [{icon}] Turn {t['turn']}: {t['strategy']}")
            if diag:
                print(f"      → {diag}")


def main():
    args = sys.argv[1:]

    if not args:
        list_sessions()
        return

    session_id = args[0]
    show_turns = 5
    show_strategies = False
    show_promising = False

    if "--turns" in args:
        idx = args.index("--turns")
        show_turns = int(args[idx + 1]) if idx + 1 < len(args) else 10
    if "--strategies" in args:
        show_strategies = True
    if "--promising" in args:
        show_promising = True

    show_session(session_id, show_turns, show_strategies, show_promising)


if __name__ == "__main__":
    main()
