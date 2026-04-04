"""Orchestrator service for forge-lean-prover.

Coordinates the prove loop: retrieve -> synthesize -> verify -> repair.
"""
from __future__ import annotations

import os
import time
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

load_dotenv()

log = structlog.get_logger()

RETRIEVAL_URL = os.getenv("RETRIEVAL_URL", "http://localhost:8103")
LEAN_ENV_URL = os.getenv("LEAN_ENV_URL", "http://localhost:8101")
PROOF_SEARCH_URL = os.getenv("PROOF_SEARCH_URL", "http://localhost:8102")
TELEMETRY_URL = os.getenv("TELEMETRY_URL", "http://localhost:8104")

app = FastAPI(title="Orchestrator Service", version="0.1.0")


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
        self.client = httpx.AsyncClient(timeout=task.timeout_secs)

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
            return resp.json().get("results", [])
        except Exception as exc:
            log.warning("retrieval_failed", error=str(exc))
            return []

    async def synthesize(self, hints: list[dict]) -> list[str]:
        """Placeholder: call LLM / Leanstral to produce candidate tactics.

        In production this would call a model service. For now returns
        a small set of generic tactics.
        """
        log.info("synthesize_placeholder", task_id=self.task.task_id)
        return [
            "exact?",
            "simp",
            "ring",
            "omega",
            "aesop",
            "decide",
            "norm_num",
            "linarith",
        ]

    async def verify(self, proof_text: str) -> VerificationResult:
        """Send proof text to lean_env for compilation."""
        source = self._build_source(proof_text)
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
        """Placeholder: attempt to repair a failed proof based on diagnostics.

        Would call an LLM with the diagnostics to produce a repaired proof.
        """
        log.info("repair_placeholder", task_id=self.task.task_id)
        return None

    async def step(self) -> VerificationResult:
        """Execute one orchestration step: retrieve -> synthesize -> verify -> repair."""
        log.info("orchestrator_step", task_id=self.task.task_id, branch=self.branches_used)

        if self.branches_used >= self.task.max_branches:
            return VerificationResult(
                task_id=self.task.task_id,
                status=ProofStatus.FAILED,
                diagnostics=["branch budget exhausted"],
            )

        # 1. Retrieve
        hints = await self.retrieve()

        # 2. Synthesize candidate tactics
        candidates = await self.synthesize(hints)
        self.branches_used += len(candidates)

        # 3. Try each candidate
        for tactic in candidates:
            proof_text = self._wrap_tactic(tactic)
            result = await self.verify(proof_text)
            if result.status == ProofStatus.VERIFIED:
                return result

            # 4. Attempt repair
            repaired = await self.repair(result)
            if repaired:
                result = await self.verify(repaired)
                if result.status == ProofStatus.VERIFIED:
                    return result

        return VerificationResult(
            task_id=self.task.task_id,
            status=ProofStatus.FAILED,
            diagnostics=["no candidate succeeded in this step"],
        )

    # -- helpers ------------------------------------------------------------

    def _build_source(self, proof_body: str) -> str:
        imports = "\n".join(f"import {i}" for i in self.task.imports) or "import Mathlib"
        ctx = self.task.context
        return f"{imports}\n\n{ctx}\n\n{self.task.theorem_statement} := by\n  {proof_body}\n"

    def _wrap_tactic(self, tactic: str) -> str:
        return tactic


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
