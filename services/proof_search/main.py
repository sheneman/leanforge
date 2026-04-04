"""Proof search service for forge-lean-prover.

Manages a search tree of proof branches with best-first scoring.
"""
from __future__ import annotations

import os
import uuid
from typing import Optional

import structlog
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from services.schemas import BranchState, ProofStatus

load_dotenv()

log = structlog.get_logger()

app = FastAPI(title="Proof Search Service", version="0.1.0")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class ScoreUpdate(BaseModel):
    score: float


class ExpandRequest(BaseModel):
    tactics: list[str]
    scores: list[float] | None = None


# ---------------------------------------------------------------------------
# SearchTree
# ---------------------------------------------------------------------------
class SearchTree:
    """In-memory best-first search tree over proof branches."""

    def __init__(self) -> None:
        # branch_id -> BranchState
        self._branches: dict[str, BranchState] = {}
        # task_id -> list[branch_id]
        self._task_index: dict[str, list[str]] = {}

    def add(self, branch: BranchState) -> BranchState:
        self._branches[branch.branch_id] = branch
        self._task_index.setdefault(branch.task_id, []).append(branch.branch_id)
        return branch

    def get(self, branch_id: str) -> Optional[BranchState]:
        return self._branches.get(branch_id)

    def list_for_task(self, task_id: str) -> list[BranchState]:
        ids = self._task_index.get(task_id, [])
        return [self._branches[bid] for bid in ids if bid in self._branches]

    def update_score(self, branch_id: str, score: float) -> Optional[BranchState]:
        b = self._branches.get(branch_id)
        if b:
            b.score = score
        return b

    def best_for_task(self, task_id: str) -> Optional[BranchState]:
        branches = self.list_for_task(task_id)
        if not branches:
            return None
        # Prefer verified, then highest score
        verified = [b for b in branches if b.status == ProofStatus.VERIFIED]
        if verified:
            return max(verified, key=lambda b: b.score)
        return max(branches, key=lambda b: b.score)

    def expand(
        self,
        branch_id: str,
        tactics: list[str],
        scores: list[float] | None = None,
    ) -> list[BranchState]:
        parent = self._branches.get(branch_id)
        if not parent:
            return []
        if scores is None:
            scores = [0.0] * len(tactics)
        children: list[BranchState] = []
        for tactic, score in zip(tactics, scores):
            child = BranchState(
                branch_id=str(uuid.uuid4()),
                task_id=parent.task_id,
                parent_id=parent.branch_id,
                tactics=parent.tactics + [tactic],
                goals_before=list(parent.goals_after),
                goals_after=[],
                status=ProofStatus.PENDING,
                score=score,
                depth=parent.depth + 1,
            )
            self.add(child)
            children.append(child)
        return children

    def minimize(self, branch_id: str) -> Optional[BranchState]:
        """Placeholder: remove redundant tactics from a successful branch."""
        branch = self._branches.get(branch_id)
        if not branch:
            return None
        # Stub -- real implementation would try removing each tactic and re-verify
        log.info("minimize_placeholder", branch_id=branch_id)
        return branch


_tree = SearchTree()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "service": "proof_search"}


@app.post("/branches")
async def create_branch(branch: BranchState):
    result = _tree.add(branch)
    log.info("branch_created", branch_id=result.branch_id, task_id=result.task_id)
    return result.model_dump()


@app.get("/branches/{task_id}")
async def list_branches(task_id: str):
    branches = _tree.list_for_task(task_id)
    return {"task_id": task_id, "branches": [b.model_dump() for b in branches]}


@app.post("/branches/{branch_id}/score")
async def update_score(branch_id: str, body: ScoreUpdate):
    b = _tree.update_score(branch_id, body.score)
    if not b:
        raise HTTPException(status_code=404, detail="branch not found")
    return b.model_dump()


@app.post("/branches/{branch_id}/children")
async def expand_branch(branch_id: str, body: ExpandRequest):
    children = _tree.expand(branch_id, body.tactics, body.scores)
    if not children:
        raise HTTPException(status_code=404, detail="parent branch not found")
    return {"children": [c.model_dump() for c in children]}


@app.get("/best/{task_id}")
async def best_branch(task_id: str):
    b = _tree.best_for_task(task_id)
    if not b:
        raise HTTPException(status_code=404, detail="no branches for task")
    return b.model_dump()


@app.post("/minimize/{branch_id}")
async def minimize_branch(branch_id: str):
    b = _tree.minimize(branch_id)
    if not b:
        raise HTTPException(status_code=404, detail="branch not found")
    return b.model_dump()
