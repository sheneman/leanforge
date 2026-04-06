#!/usr/bin/env python3
"""End-to-end benchmark runner for forge-lean-prover.

Submits benchmark theorems to the orchestrator service and polls for
completion.  Prints a summary table and saves results to
data/logs/benchmark_results.json.

Usage:
    python -m tests.e2e.run_benchmarks [--url URL] [--timeout SECS]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import os

import httpx
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Benchmark theorems
# ---------------------------------------------------------------------------
BENCHMARKS: list[dict[str, str]] = [
    {
        "name": "add_zero",
        "theorem": "theorem add_zero (n : Nat) : n + 0 = n",
        "imports": "Mathlib.Data.Nat.Basic",
    },
    {
        "name": "add_comm",
        "theorem": "theorem add_comm (n m : Nat) : n + m = m + n",
        "imports": "Mathlib.Data.Nat.Basic",
    },
    {
        "name": "mul_one",
        "theorem": "theorem mul_one (n : Nat) : n * 1 = n",
        "imports": "Mathlib.Data.Nat.Basic",
    },
    {
        "name": "zero_mul",
        "theorem": "theorem zero_mul (n : Nat) : 0 * n = 0",
        "imports": "Mathlib.Data.Nat.Basic",
    },
    {
        "name": "succ_ne_zero",
        "theorem": "theorem succ_ne_zero (n : Nat) : n + 1 \\ne 0",
        "imports": "Mathlib.Data.Nat.Basic",
    },
    {
        "name": "one_plus_one",
        "theorem": "theorem one_plus_one : 1 + 1 = 2",
        "imports": "",
    },
]

RESULTS_DIR = Path("data/logs")


def submit_task(client: httpx.Client, base_url: str, bench: dict) -> str:
    """Submit a proof task and return the task_id."""
    payload = {
        "theorem_statement": bench["theorem"],
        "imports": [bench["imports"]] if bench["imports"] else [],
        "max_branches": 50,
        "timeout_secs": 120,
    }
    resp = client.post(f"{base_url}/tasks", json=payload)
    resp.raise_for_status()
    return resp.json()["task_id"]


def poll_task(client: httpx.Client, base_url: str, task_id: str, timeout: float) -> dict:
    """Poll the orchestrator until the task finishes or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"{base_url}/tasks/{task_id}")
        resp.raise_for_status()
        data = resp.json()
        result = data.get("result", {})
        status = result.get("status", "pending")
        if status != "pending":
            return data
        time.sleep(2.0)
    return {"task_id": task_id, "result": {"status": "timeout"}}


def run_benchmarks(base_url: str, timeout: float) -> list[dict]:
    """Run all benchmarks and return results."""
    results: list[dict] = []
    client = httpx.Client(timeout=timeout + 10)

    print(f"\n{'Theorem':<25} {'Status':<20} {'Time (s)':<10}")
    print("-" * 55)

    for bench in BENCHMARKS:
        t0 = time.monotonic()
        try:
            task_id = submit_task(client, base_url, bench)
            data = poll_task(client, base_url, task_id, timeout)
            status = data.get("result", {}).get("status", "unknown")
            elapsed = time.monotonic() - t0
        except Exception as exc:
            status = f"error: {exc}"
            elapsed = time.monotonic() - t0

        row = {
            "theorem": bench["name"],
            "status": status,
            "elapsed_secs": round(elapsed, 2),
        }
        results.append(row)
        print(f"{bench['name']:<25} {status:<20} {elapsed:<10.2f}")

    client.close()
    return results


def save_results(results: list[dict]) -> Path:
    """Save benchmark results to JSON."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    outfile = RESULTS_DIR / "benchmark_results.json"
    with outfile.open("w") as f:
        json.dump(
            {
                "run_timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "benchmarks": results,
                "total": len(results),
                "verified": sum(1 for r in results if r["status"] == "verified"),
            },
            f,
            indent=2,
        )
    return outfile


def main() -> None:
    parser = argparse.ArgumentParser(description="Run forge-lean-prover benchmarks")
    parser.add_argument("--url", default=os.getenv("ORCHESTRATOR_URL", "http://localhost:8100"), help="Orchestrator base URL")
    parser.add_argument("--timeout", type=float, default=120.0, help="Per-theorem timeout in seconds")
    args = parser.parse_args()

    print(f"Benchmark runner targeting {args.url}")
    results = run_benchmarks(args.url, args.timeout)

    # Summary
    total = len(results)
    verified = sum(1 for r in results if r["status"] == "verified")
    print(f"\n{'='*55}")
    print(f"Total: {total}  |  Verified: {verified}  |  Failed: {total - verified}")

    outfile = save_results(results)
    print(f"Results saved to {outfile}")

    sys.exit(0 if verified == total else 1)


if __name__ == "__main__":
    main()
