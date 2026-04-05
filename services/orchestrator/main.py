"""Orchestrator service for forge-lean-prover.

Coordinates the prove loop: retrieve -> synthesize -> verify -> repair.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Optional

import httpx
import structlog
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException

from services.schemas import (
    BranchState,
    ProofStatus,
    ProofTask,
    TheoremSearchRequest,
    VerificationResult,
)
from services.orchestrator.llm import (
    call_leanstral,
    call_nemotron,
    leanstral_is_configured,
    nemotron_is_configured,
)

load_dotenv()

log = structlog.get_logger()

RETRIEVAL_URL = os.getenv("RETRIEVAL_URL", "http://localhost:8103")
LEAN_ENV_URL = os.getenv("LEAN_ENV_URL", "http://localhost:8101")
PROOF_SEARCH_URL = os.getenv("PROOF_SEARCH_URL", "http://localhost:8102")
TELEMETRY_URL = os.getenv("TELEMETRY_URL", "http://localhost:8104")

# ---------------------------------------------------------------------------
# Load configuration at startup
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]

_system_prompt: str = ""
_synthesis_template: str = ""
_budgets: dict = {}

try:
    _system_prompt = (_REPO_ROOT / "config" / "prompts" / "orchestrator_system.txt").read_text()
except Exception as exc:
    log.warning("failed_to_load_system_prompt", error=str(exc))

try:
    _synthesis_template = (_REPO_ROOT / "config" / "prompts" / "synthesis_prompt.txt").read_text()
except Exception as exc:
    log.warning("failed_to_load_synthesis_template", error=str(exc))

try:
    _budgets = json.loads((_REPO_ROOT / "config" / "budgets.json").read_text())
except Exception as exc:
    log.warning("failed_to_load_budgets", error=str(exc))

app = FastAPI(title="Orchestrator Service", version="0.2.0")


# ---------------------------------------------------------------------------
# In-memory task store
# ---------------------------------------------------------------------------
_tasks: dict[str, ProofTask] = {}
_results: dict[str, VerificationResult] = {}


# ---------------------------------------------------------------------------
# OrchestratorEngine
# ---------------------------------------------------------------------------
class OrchestratorEngine:
    """Drives one full prove attempt for a ProofTask."""

    def __init__(self, task: ProofTask) -> None:
        self.task = task
        self.branches_used = 0
        self.max_branches = task.max_branches or _budgets.get("default_branch_budget", 50)
        self.max_repair_attempts = _budgets.get("max_repair_attempts", 5)
        self.client = httpx.AsyncClient(timeout=task.timeout_secs)
        # Accumulated state across steps
        self.retrieved_lemmas: list[dict] = []
        self.previous_attempts: list[str] = []
        self.error_diagnostics: list[str] = []

    async def close(self) -> None:
        await self.client.aclose()

    # -- sub-steps ----------------------------------------------------------

    async def retrieve(self) -> list[dict]:
        """Call the retrieval service for relevant lemmas."""
        try:
            resp = await self.client.post(
                f"{RETRIEVAL_URL}/search",
                json=TheoremSearchRequest(
                    query=self.task.theorem_statement,
                    top_k=10,
                ).model_dump(),
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
            self.retrieved_lemmas = results
            return results
        except Exception as exc:
            log.warning("retrieval_failed", error=str(exc))
            return []

    async def synthesize(self, hints: list[dict]) -> list[str]:
        """Call Leanstral to produce candidate tactic proofs.

        Builds a synthesis prompt from the template, fills in context,
        and calls the LLM.  Falls back to generic tactics if not configured.
        """
        # Format retrieved lemmas for the prompt
        lemma_text = ""
        for h in hints:
            name = h.get("name", "")
            stmt = h.get("statement", "")
            lemma_text += f"- {name}: {stmt}\n"
        if not lemma_text:
            lemma_text = "(none retrieved)"

        # Format previous attempts
        prev_text = "\n".join(
            f"Attempt {i+1}: {a}" for i, a in enumerate(self.previous_attempts)
        ) or "(none)"

        # Format error diagnostics
        diag_text = "\n".join(self.error_diagnostics) or "(none)"

        # Build the full synthesis prompt from template
        imports_text = "\n".join(f"import {i}" for i in self.task.imports) or "import Mathlib.Tactic"

        prompt = _synthesis_template.format(
            theorem_statement=self.task.theorem_statement,
            imports=imports_text,
            context=self.task.context or "(none)",
            retrieved_lemmas=lemma_text,
            previous_attempts=prev_text,
            error_diagnostics=diag_text,
        ) if _synthesis_template else (
            f"Prove the following Lean 4 theorem:\n\n"
            f"{self.task.theorem_statement}\n\n"
            f"Relevant lemmas:\n{lemma_text}\n\n"
            f"Previous attempts:\n{prev_text}\n\n"
            f"Error diagnostics:\n{diag_text}\n"
        )

        log.info(
            "synthesize_called",
            task_id=self.task.task_id,
            prompt_len=len(prompt),
            leanstral_configured=leanstral_is_configured(),
        )

        candidates = await call_leanstral(prompt)

        log.info(
            "synthesize_result",
            task_id=self.task.task_id,
            num_candidates=len(candidates),
        )

        return candidates

    async def verify(self, proof_text: str) -> VerificationResult:
        """Send proof text to lean_env for compilation.

        # VERIFICATION GATE: No proof is accepted without Lean compilation
        """
        source = _build_lean_source(
            theorem_statement=self.task.theorem_statement,
            imports=self.task.imports,
            context=self.task.context,
            proof_body=proof_text,
        )
        try:
            resp = await self.client.post(
                f"{LEAN_ENV_URL}/compile",
                json={"source": source},
            )
            resp.raise_for_status()
            data = resp.json()
            status = (
                ProofStatus.VERIFIED if data.get("success") else ProofStatus.FAILED
            )
            return VerificationResult(
                task_id=self.task.task_id,
                status=status,
                proof_text=proof_text,
                diagnostics=[d.get("message", "") for d in data.get("diagnostics", [])],
                lean_output=str(data),
                elapsed_secs=data.get("elapsed_secs", 0.0),
            )
        except Exception as exc:
            log.warning("verify_failed", error=str(exc))
            return VerificationResult(
                task_id=self.task.task_id,
                status=ProofStatus.FAILED,
                proof_text=proof_text,
                diagnostics=[str(exc)],
            )

    async def repair(self, result: VerificationResult) -> Optional[str]:
        """Attempt to repair a failed proof by re-synthesizing with error context.

        Calls synthesize again with the failed proof and diagnostics added to
        previous_attempts and error_diagnostics.  Returns the first new
        candidate, or None if nothing new was produced.
        """
        # Record the failure for the next synthesis round
        self.previous_attempts.append(result.proof_text)
        self.error_diagnostics = list(result.diagnostics)

        log.info(
            "repair_attempt",
            task_id=self.task.task_id,
            diagnostics=result.diagnostics[:3],
        )

        candidates = await self.synthesize(self.retrieved_lemmas)
        # Return the first candidate that differs from previous attempts
        for c in candidates:
            if c not in self.previous_attempts:
                return c

        return None

    async def step(self) -> VerificationResult:
        """Execute one full orchestration cycle: retrieve -> synthesize -> verify -> repair.

        For each candidate proof, builds the full Lean source and verifies
        via lean_env /compile.  If verified, returns immediately.  If all
        candidates fail, attempts repair up to max_repair_attempts.
        """
        log.info("orchestrator_step", task_id=self.task.task_id, branch=self.branches_used)

        if self.branches_used >= self.max_branches:
            return VerificationResult(
                task_id=self.task.task_id,
                status=ProofStatus.FAILED,
                diagnostics=["branch budget exhausted"],
            )

        # 1. Retrieve relevant lemmas
        hints = await self.retrieve()

        # 2. Synthesize candidate tactics
        candidates = await self.synthesize(hints)

        # 3. Try each candidate through the VERIFICATION GATE
        last_result: Optional[VerificationResult] = None
        for tactic in candidates:
            if self.branches_used >= self.max_branches:
                break

            self.branches_used += 1
            proof_text = self._wrap_tactic(tactic)

            # VERIFICATION GATE: No proof is accepted without Lean compilation
            result = await self.verify(proof_text)
            last_result = result

            # Log to telemetry
            await self._log_telemetry("verify_attempt", {
                "branch": self.branches_used,
                "status": result.status.value,
                "proof_text": proof_text[:500],
            })

            if result.status == ProofStatus.VERIFIED:
                return result

            # 4. Attempt repair loop for this failed candidate
            repair_count = 0
            prev_error: str = ""
            repeated_error_count = 0
            while repair_count < self.max_repair_attempts and self.branches_used < self.max_branches:
                # Detect repeated structural errors — abort early if same
                # parse/structural error repeats 3+ times (won't be fixed by
                # re-synthesizing tactics; the theorem statement itself is broken)
                cur_error = " ".join(result.diagnostics[:2]) if result.diagnostics else ""
                if cur_error and cur_error == prev_error:
                    repeated_error_count += 1
                else:
                    repeated_error_count = 0
                prev_error = cur_error
                if repeated_error_count >= 2:
                    log.warning(
                        "repair_abort_repeated_error",
                        task_id=self.task.task_id,
                        error=cur_error[:200],
                    )
                    break

                repair_count += 1
                self.branches_used += 1
                repaired = await self.repair(result)
                if not repaired:
                    break

                # VERIFICATION GATE: repaired proof must also compile
                result = await self.verify(repaired)
                last_result = result

                await self._log_telemetry("repair_attempt", {
                    "branch": self.branches_used,
                    "repair_round": repair_count,
                    "status": result.status.value,
                })

                if result.status == ProofStatus.VERIFIED:
                    return result

        return last_result or VerificationResult(
            task_id=self.task.task_id,
            status=ProofStatus.FAILED,
            diagnostics=["no candidate succeeded in this step"],
        )

    async def run(self) -> VerificationResult:
        """Run the full prove loop until verified or budget exhausted.

        Calls step() repeatedly.  This is the main entry point that
        ForgeCode should use for end-to-end proving.
        """
        log.info("orchestrator_run_start", task_id=self.task.task_id, max_branches=self.max_branches)

        best_result: Optional[VerificationResult] = None
        max_steps = _budgets.get("max_synthesis_calls_per_branch", 10)
        step_count = 0

        while self.branches_used < self.max_branches and step_count < max_steps:
            step_count += 1
            result = await self.step()

            if result.status == ProofStatus.VERIFIED:
                log.info(
                    "orchestrator_run_verified",
                    task_id=self.task.task_id,
                    branches_used=self.branches_used,
                    steps=step_count,
                )
                return result

            best_result = result
            log.info(
                "orchestrator_run_step_failed",
                task_id=self.task.task_id,
                step=step_count,
                branches_used=self.branches_used,
            )

        final = best_result or VerificationResult(
            task_id=self.task.task_id,
            status=ProofStatus.FAILED,
            diagnostics=["budget exhausted without verified proof"],
        )
        log.info(
            "orchestrator_run_done",
            task_id=self.task.task_id,
            status=final.status.value,
            branches_used=self.branches_used,
        )
        return final

    # -- helpers ------------------------------------------------------------

    def _wrap_tactic(self, tactic: str) -> str:
        return tactic

    async def _log_telemetry(self, event_type: str, data: dict) -> None:
        """Best-effort telemetry logging."""
        try:
            await self.client.post(
                f"{TELEMETRY_URL}/events",
                json={
                    "event_type": event_type,
                    "task_id": self.task.task_id,
                    "data": data,
                },
            )
        except Exception:
            pass  # telemetry is best-effort


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _normalize_theorem_statement(stmt: str) -> str:
    """Ensure the statement is a proper Lean 4 theorem/lemma declaration.

    If the user provides a bare proposition like ``∀ (a b : ℕ), Even a → ...``,
    wrap it in a ``theorem`` declaration.  If it already starts with ``theorem``,
    ``lemma``, ``def``, or ``example``, return it unchanged.
    """
    stripped = stmt.strip()
    # Already a proper declaration
    if re.match(r"^(theorem|lemma|def|example)\s", stripped):
        return stripped
    # Bare proposition — wrap it
    # Generate a simple name from a hash to avoid collisions
    name = "auto_" + hashlib.md5(stripped.encode()).hexdigest()[:8]
    return f"theorem {name} : {stripped}"


def _build_lean_source(
    theorem_statement: str,
    imports: list[str],
    context: str,
    proof_body: str,
) -> str:
    """Build a complete compilable Lean 4 source file.

    Includes proper import statements (defaults to ``import Mathlib.Tactic``),
    any context declarations, the theorem statement, and the proof body.

    If the theorem_statement is a bare proposition (e.g. ``∀ ...``), it is
    automatically wrapped in a ``theorem`` declaration.
    """
    stmt = _normalize_theorem_statement(theorem_statement)
    import_lines = "\n".join(f"import {i}" for i in imports) if imports else "import Mathlib.Tactic"
    parts = [import_lines, ""]
    if context and context.strip():
        parts.append(context)
        parts.append("")
    parts.append(f"{stmt} := by")
    parts.append(f"  {proof_body}")
    parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# In-memory engine registry (per task)
# ---------------------------------------------------------------------------
_engines: dict[str, OrchestratorEngine] = {}


def _get_engine(task_id: str) -> OrchestratorEngine:
    if task_id not in _engines:
        if task_id not in _tasks:
            raise HTTPException(status_code=404, detail="task not found")
        _engines[task_id] = OrchestratorEngine(_tasks[task_id])
    return _engines[task_id]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "service": "orchestrator"}


@app.post("/tasks")
async def create_task(task: ProofTask):
    _tasks[task.task_id] = task
    _results[task.task_id] = VerificationResult(
        task_id=task.task_id, status=ProofStatus.PENDING
    )
    log.info("task_created", task_id=task.task_id)
    return {"task_id": task.task_id, "status": ProofStatus.PENDING}


@app.get("/tasks/{task_id}")
async def get_task(task_id: str):
    if task_id not in _tasks:
        raise HTTPException(status_code=404, detail="task not found")
    result = _results.get(task_id)
    return {
        "task": _tasks[task_id].model_dump(),
        "result": result.model_dump() if result else None,
    }


@app.post("/tasks/{task_id}/step")
async def step_task(task_id: str):
    engine = _get_engine(task_id)
    t0 = time.monotonic()
    result = await engine.step()
    result.elapsed_secs = time.monotonic() - t0
    _results[task_id] = result
    log.info(
        "step_complete",
        task_id=task_id,
        status=result.status,
        elapsed=result.elapsed_secs,
    )
    return result.model_dump()


@app.post("/tasks/{task_id}/run")
async def run_task(task_id: str):
    """Run the full prove loop until verified or budget exhausted.

    This is the primary endpoint that ForgeCode should call for
    end-to-end proving of a task.
    """
    engine = _get_engine(task_id)
    t0 = time.monotonic()
    result = await engine.run()
    result.elapsed_secs = time.monotonic() - t0
    _results[task_id] = result
    log.info(
        "run_complete",
        task_id=task_id,
        status=result.status,
        elapsed=result.elapsed_secs,
        branches_used=engine.branches_used,
    )
    return result.model_dump()
