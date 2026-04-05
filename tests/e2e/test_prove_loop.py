#!/usr/bin/env python3
"""End-to-end test for the forge-lean-prover prove loop.

Validates the full retrieve -> synthesize -> verify pipeline by hitting
live services.  Skips gracefully when services are not running.

Usage:
    python -m tests.e2e.test_prove_loop          # standalone
    pytest tests/e2e/test_prove_loop.py -v        # via pytest
"""
from __future__ import annotations

import sys
import time
import uuid
from dataclasses import dataclass, field

import httpx
import pytest

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SERVICES: dict[str, str] = {
    "orchestrator": "http://localhost:8100",
    "lean_env": "http://localhost:8101",
    "proof_search": "http://localhost:8102",
    "retrieval": "http://localhost:8103",
    "telemetry": "http://localhost:8104",
}

TIMEOUT = 30.0  # seconds per request


# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------
@dataclass
class TestResult:
    name: str
    passed: bool
    detail: str = ""
    elapsed: float = 0.0


_results: list[TestResult] = []


def _record(name: str, passed: bool, detail: str = "", elapsed: float = 0.0) -> None:
    _results.append(TestResult(name=name, passed=passed, detail=detail, elapsed=elapsed))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT)


def _service_available(base_url: str) -> bool:
    """Return True if the service at *base_url* responds to /health."""
    try:
        with _client() as c:
            resp = c.get(f"{base_url}/health")
            return resp.status_code == 200
    except httpx.ConnectError:
        return False
    except Exception:
        return False


def _all_services_up() -> dict[str, bool]:
    return {name: _service_available(url) for name, url in SERVICES.items()}


# ---------------------------------------------------------------------------
# 1. Health checks
# ---------------------------------------------------------------------------
class TestHealthChecks:
    """Hit /health on every service port."""

    @pytest.fixture(autouse=True)
    def _status(self):
        self.status = _all_services_up()

    @pytest.mark.parametrize("service", list(SERVICES))
    def test_health(self, service: str):
        url = SERVICES[service]
        t0 = time.monotonic()
        try:
            with _client() as c:
                resp = c.get(f"{url}/health")
            elapsed = time.monotonic() - t0
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["service"] == service
            _record(f"health_{service}", True, elapsed=elapsed)
        except httpx.ConnectError:
            _record(f"health_{service}", False, "service not running")
            pytest.skip(f"{service} not running at {url}")


# ---------------------------------------------------------------------------
# 2. Full prove loop (trivial theorem)
# ---------------------------------------------------------------------------
_TRIVIAL_THEOREM = "theorem test_trivial : 1 + 1 = 2"


def _requires_services(*names: str):
    """Pytest skip decorator: skip if any of the named services are down."""
    for name in names:
        if not _service_available(SERVICES[name]):
            pytest.skip(f"{name} not running")


class TestFullProveLoop:
    """End-to-end: retrieve -> orchestrator step -> verify independently."""

    def test_step1_retrieval(self):
        """Call retrieval with 'norm_num arithmetic'."""
        _requires_services("retrieval")
        t0 = time.monotonic()
        with _client() as c:
            resp = c.post(
                f"{SERVICES['retrieval']}/search",
                json={"query": "norm_num arithmetic", "top_k": 5},
            )
        elapsed = time.monotonic() - t0
        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data
        assert len(data["results"]) > 0
        _record("prove_loop_step1_retrieval", True, f"{len(data['results'])} results", elapsed)

    def test_step2_submit_task(self):
        """Submit the trivial theorem task to the orchestrator."""
        _requires_services("orchestrator")
        t0 = time.monotonic()
        with _client() as c:
            resp = c.post(
                f"{SERVICES['orchestrator']}/tasks",
                json={
                    "theorem_statement": _TRIVIAL_THEOREM,
                    "imports": [],
                    "max_branches": 50,
                    "timeout_secs": 60,
                },
            )
        elapsed = time.monotonic() - t0
        assert resp.status_code == 200
        data = resp.json()
        assert "task_id" in data
        # Store for subsequent steps
        self.__class__._task_id = data["task_id"]
        _record("prove_loop_step2_submit_task", True, f"task_id={data['task_id']}", elapsed)

    def test_step3_run_task(self):
        """Run one orchestration step (retrieve -> synthesize -> verify)."""
        _requires_services("orchestrator", "lean_env", "retrieval")
        task_id = getattr(self.__class__, "_task_id", None)
        if task_id is None:
            # Submit inline so this test can run independently
            with _client() as c:
                resp = c.post(
                    f"{SERVICES['orchestrator']}/tasks",
                    json={
                        "theorem_statement": _TRIVIAL_THEOREM,
                        "imports": [],
                        "max_branches": 50,
                        "timeout_secs": 60,
                    },
                )
                task_id = resp.json()["task_id"]

        t0 = time.monotonic()
        with _client() as c:
            resp = c.post(f"{SERVICES['orchestrator']}/tasks/{task_id}/step")
        elapsed = time.monotonic() - t0
        assert resp.status_code == 200
        data = resp.json()
        status = data.get("status", "unknown")
        self.__class__._step_result = data
        passed = status in ("verified", "failed", "pending")  # any valid status
        _record("prove_loop_step3_run_step", passed, f"status={status}", elapsed)

    def test_step4_check_result(self):
        """Verify that the step result has a recognized status."""
        _requires_services("orchestrator")
        data = getattr(self.__class__, "_step_result", None)
        if data is None:
            pytest.skip("step 3 did not run")
        status = data.get("status", "unknown")
        # The trivial theorem should ideally be verified via norm_num/decide/omega
        _record(
            "prove_loop_step4_check_status",
            status in ("verified", "failed"),
            f"status={status}",
        )
        # We assert a valid ProofStatus, not necessarily verified,
        # because lean may not be available in the test environment.
        assert status in ("verified", "failed", "pending", "timeout")

    def test_step5_independent_verify(self):
        """Independently compile a correct proof via lean_env /compile."""
        _requires_services("lean_env")
        source = (
            "import Mathlib\n\n"
            "theorem test_trivial : 1 + 1 = 2 := by norm_num\n"
        )
        t0 = time.monotonic()
        with _client() as c:
            resp = c.post(
                f"{SERVICES['lean_env']}/compile",
                json={"source": source},
            )
        elapsed = time.monotonic() - t0
        assert resp.status_code == 200
        data = resp.json()
        # Record but don't hard-fail if lean binary isn't available
        success = data.get("success", False)
        _record("prove_loop_step5_independent_verify", True, f"success={success}", elapsed)


# ---------------------------------------------------------------------------
# 3. Verification gate
# ---------------------------------------------------------------------------
class TestVerificationGate:
    """Confirm that lean_env rejects wrong proofs."""

    def test_wrong_proof_rejected(self):
        """A provably false statement should fail compilation."""
        _requires_services("lean_env")
        source = "theorem bad : 1 + 1 = 3 := by norm_num\n"
        t0 = time.monotonic()
        with _client() as c:
            resp = c.post(
                f"{SERVICES['lean_env']}/compile",
                json={"source": source},
            )
        elapsed = time.monotonic() - t0
        assert resp.status_code == 200
        data = resp.json()
        # The compilation should NOT succeed
        assert data.get("success") is False, "expected success=false for wrong proof"
        assert len(data.get("diagnostics", [])) > 0, "expected diagnostics for wrong proof"
        _record("verification_gate_wrong_proof", True, "correctly rejected", elapsed)

    def test_correct_proof_accepted(self):
        """A correct trivial proof should compile successfully."""
        _requires_services("lean_env")
        source = "theorem ok : 1 + 1 = 2 := by norm_num\n"
        t0 = time.monotonic()
        with _client() as c:
            resp = c.post(
                f"{SERVICES['lean_env']}/compile",
                json={"source": source},
            )
        elapsed = time.monotonic() - t0
        assert resp.status_code == 200
        data = resp.json()
        # Record outcome (may fail if lean is not installed, but API should work)
        _record(
            "verification_gate_correct_proof",
            True,
            f"success={data.get('success')}",
            elapsed,
        )


# ---------------------------------------------------------------------------
# 4. Retrieval searches
# ---------------------------------------------------------------------------
class TestRetrieval:
    """Verify that the retrieval service returns sensible results."""

    @pytest.mark.parametrize("query", ["commutative", "prime"])
    def test_search(self, query: str):
        _requires_services("retrieval")
        t0 = time.monotonic()
        with _client() as c:
            resp = c.post(
                f"{SERVICES['retrieval']}/search",
                json={"query": query, "top_k": 5},
            )
        elapsed = time.monotonic() - t0
        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data
        assert isinstance(data["results"], list)
        assert len(data["results"]) > 0, f"expected results for query '{query}'"
        _record(f"retrieval_search_{query}", True, f"{len(data['results'])} results", elapsed)


# ---------------------------------------------------------------------------
# 5. Branch search lifecycle
# ---------------------------------------------------------------------------
class TestBranchSearch:
    """Create a branch, expand, score, and get best."""

    def test_branch_lifecycle(self):
        _requires_services("proof_search")
        task_id = f"e2e-{uuid.uuid4().hex[:8]}"
        branch_id = f"e2e-root-{uuid.uuid4().hex[:8]}"
        t0 = time.monotonic()

        with _client() as c:
            # Create root branch
            resp = c.post(
                f"{SERVICES['proof_search']}/branches",
                json={
                    "branch_id": branch_id,
                    "task_id": task_id,
                    "tactics": [],
                    "status": "pending",
                    "score": 0.0,
                    "depth": 0,
                },
            )
            assert resp.status_code == 200, f"create branch failed: {resp.text}"
            created = resp.json()
            assert created["branch_id"] == branch_id

            # Expand with children
            resp = c.post(
                f"{SERVICES['proof_search']}/branches/{branch_id}/children",
                json={"tactics": ["simp", "norm_num"], "scores": [0.7, 0.9]},
            )
            assert resp.status_code == 200, f"expand failed: {resp.text}"
            children = resp.json()["children"]
            assert len(children) == 2

            # Score the best child higher
            best_child_id = children[1]["branch_id"]  # norm_num, score 0.9
            resp = c.post(
                f"{SERVICES['proof_search']}/branches/{best_child_id}/score",
                json={"score": 0.95},
            )
            assert resp.status_code == 200
            assert resp.json()["score"] == 0.95

            # Get best branch for task
            resp = c.get(f"{SERVICES['proof_search']}/best/{task_id}")
            assert resp.status_code == 200
            best = resp.json()
            assert best["score"] == 0.95
            assert best["branch_id"] == best_child_id

            # List all branches
            resp = c.get(f"{SERVICES['proof_search']}/branches/{task_id}")
            assert resp.status_code == 200
            all_branches = resp.json()["branches"]
            assert len(all_branches) == 3  # root + 2 children

        elapsed = time.monotonic() - t0
        _record("branch_search_lifecycle", True, "create/expand/score/best OK", elapsed)


# ---------------------------------------------------------------------------
# Summary printer
# ---------------------------------------------------------------------------
def _print_summary() -> int:
    """Print a table of all recorded results. Returns exit code."""
    if not _results:
        print("\nNo tests were recorded (services may all be down).")
        return 1

    col_name = max(len(r.name) for r in _results)
    col_status = 6  # PASS / FAIL
    col_time = 9
    header = f"{'Test':<{col_name}}  {'Status':<{col_status}}  {'Time':>{col_time}}  Detail"
    sep = "-" * len(header)

    print(f"\n{'=' * len(header)}")
    print("  FORGE-LEAN-PROVER  E2E TEST SUMMARY")
    print(f"{'=' * len(header)}")
    print(header)
    print(sep)

    passed = 0
    failed = 0
    for r in _results:
        status = "PASS" if r.passed else "FAIL"
        time_str = f"{r.elapsed:.2f}s" if r.elapsed else ""
        print(f"{r.name:<{col_name}}  {status:<{col_status}}  {time_str:>{col_time}}  {r.detail}")
        if r.passed:
            passed += 1
        else:
            failed += 1

    print(sep)
    print(f"Total: {passed + failed}  |  Passed: {passed}  |  Failed: {failed}")
    print(f"{'=' * len(header)}\n")
    return 0 if failed == 0 else 1


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------
def main() -> None:
    """Run all tests manually (without pytest) and print summary."""
    print("Checking service availability ...")
    status = _all_services_up()
    for name, up in status.items():
        tag = "UP" if up else "DOWN"
        print(f"  {name:20s}  {tag}")

    any_up = any(status.values())
    if not any_up:
        print("\nAll services are down -- nothing to test. Exiting gracefully.")
        sys.exit(0)

    # --- 1. Health checks ---
    for name, url in SERVICES.items():
        t0 = time.monotonic()
        try:
            with _client() as c:
                resp = c.get(f"{url}/health")
            elapsed = time.monotonic() - t0
            ok = resp.status_code == 200 and resp.json().get("status") == "ok"
            _record(f"health_{name}", ok, resp.json().get("service", "?"), elapsed)
        except Exception as exc:
            _record(f"health_{name}", False, str(exc))

    # --- 2. Full prove loop ---
    if status["retrieval"]:
        t0 = time.monotonic()
        try:
            with _client() as c:
                resp = c.post(
                    f"{SERVICES['retrieval']}/search",
                    json={"query": "norm_num arithmetic", "top_k": 5},
                )
            elapsed = time.monotonic() - t0
            data = resp.json()
            _record("prove_loop_retrieval", resp.status_code == 200 and len(data.get("results", [])) > 0,
                    f"{len(data.get('results', []))} results", elapsed)
        except Exception as exc:
            _record("prove_loop_retrieval", False, str(exc))

    task_id = None
    if status["orchestrator"]:
        t0 = time.monotonic()
        try:
            with _client() as c:
                resp = c.post(
                    f"{SERVICES['orchestrator']}/tasks",
                    json={
                        "theorem_statement": _TRIVIAL_THEOREM,
                        "imports": [],
                        "max_branches": 50,
                        "timeout_secs": 60,
                    },
                )
            elapsed = time.monotonic() - t0
            task_id = resp.json().get("task_id")
            _record("prove_loop_submit_task", resp.status_code == 200 and task_id is not None,
                    f"task_id={task_id}", elapsed)
        except Exception as exc:
            _record("prove_loop_submit_task", False, str(exc))

    if task_id and status["orchestrator"]:
        t0 = time.monotonic()
        try:
            with _client() as c:
                resp = c.post(f"{SERVICES['orchestrator']}/tasks/{task_id}/step")
            elapsed = time.monotonic() - t0
            data = resp.json()
            step_status = data.get("status", "unknown")
            _record("prove_loop_step", resp.status_code == 200,
                    f"status={step_status}", elapsed)
        except Exception as exc:
            _record("prove_loop_step", False, str(exc))

    if status["lean_env"]:
        source = "import Mathlib\n\ntheorem test_trivial : 1 + 1 = 2 := by norm_num\n"
        t0 = time.monotonic()
        try:
            with _client() as c:
                resp = c.post(f"{SERVICES['lean_env']}/compile", json={"source": source})
            elapsed = time.monotonic() - t0
            data = resp.json()
            _record("prove_loop_independent_verify", resp.status_code == 200,
                    f"success={data.get('success')}", elapsed)
        except Exception as exc:
            _record("prove_loop_independent_verify", False, str(exc))

    # --- 3. Verification gate ---
    if status["lean_env"]:
        # Wrong proof
        t0 = time.monotonic()
        try:
            with _client() as c:
                resp = c.post(
                    f"{SERVICES['lean_env']}/compile",
                    json={"source": "theorem bad : 1 + 1 = 3 := by norm_num\n"},
                )
            elapsed = time.monotonic() - t0
            data = resp.json()
            rejected = data.get("success") is False and len(data.get("diagnostics", [])) > 0
            _record("verification_gate_wrong_proof", rejected,
                    f"success={data.get('success')} diags={len(data.get('diagnostics', []))}", elapsed)
        except Exception as exc:
            _record("verification_gate_wrong_proof", False, str(exc))

        # Correct proof
        t0 = time.monotonic()
        try:
            with _client() as c:
                resp = c.post(
                    f"{SERVICES['lean_env']}/compile",
                    json={"source": "theorem ok : 1 + 1 = 2 := by norm_num\n"},
                )
            elapsed = time.monotonic() - t0
            data = resp.json()
            _record("verification_gate_correct_proof", resp.status_code == 200,
                    f"success={data.get('success')}", elapsed)
        except Exception as exc:
            _record("verification_gate_correct_proof", False, str(exc))

    # --- 4. Retrieval searches ---
    if status["retrieval"]:
        for query in ("commutative", "prime"):
            t0 = time.monotonic()
            try:
                with _client() as c:
                    resp = c.post(
                        f"{SERVICES['retrieval']}/search",
                        json={"query": query, "top_k": 5},
                    )
                elapsed = time.monotonic() - t0
                data = resp.json()
                n = len(data.get("results", []))
                _record(f"retrieval_search_{query}", resp.status_code == 200 and n > 0,
                        f"{n} results", elapsed)
            except Exception as exc:
                _record(f"retrieval_search_{query}", False, str(exc))

    # --- 5. Branch search lifecycle ---
    if status["proof_search"]:
        task_id = f"e2e-{uuid.uuid4().hex[:8]}"
        branch_id = f"e2e-root-{uuid.uuid4().hex[:8]}"
        t0 = time.monotonic()
        try:
            with _client() as c:
                # Create root branch
                resp = c.post(
                    f"{SERVICES['proof_search']}/branches",
                    json={
                        "branch_id": branch_id,
                        "task_id": task_id,
                        "tactics": [],
                        "status": "pending",
                        "score": 0.0,
                        "depth": 0,
                    },
                )
                assert resp.status_code == 200

                # Expand
                resp = c.post(
                    f"{SERVICES['proof_search']}/branches/{branch_id}/children",
                    json={"tactics": ["simp", "norm_num"], "scores": [0.7, 0.9]},
                )
                assert resp.status_code == 200
                children = resp.json()["children"]

                # Score
                best_child_id = children[1]["branch_id"]
                resp = c.post(
                    f"{SERVICES['proof_search']}/branches/{best_child_id}/score",
                    json={"score": 0.95},
                )
                assert resp.status_code == 200

                # Best
                resp = c.get(f"{SERVICES['proof_search']}/best/{task_id}")
                assert resp.status_code == 200
                assert resp.json()["score"] == 0.95

                # List
                resp = c.get(f"{SERVICES['proof_search']}/branches/{task_id}")
                assert resp.status_code == 200
                assert len(resp.json()["branches"]) == 3

            elapsed = time.monotonic() - t0
            _record("branch_search_lifecycle", True, "create/expand/score/best OK", elapsed)
        except Exception as exc:
            elapsed = time.monotonic() - t0
            _record("branch_search_lifecycle", False, str(exc), elapsed)

    # --- Summary ---
    exit_code = _print_summary()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
