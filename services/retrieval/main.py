"""Retrieval service for forge-lean-prover.

Provides theorem search over a vector index with a hardcoded mathlib fallback.
"""
from __future__ import annotations

import os
import time
from typing import Optional

import structlog
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from services.schemas import (
    RetrievalResult,
    TheoremMatch,
    TheoremSearchRequest,
)

load_dotenv()

log = structlog.get_logger()

INDEX_PATH = os.getenv("INDEX_PATH", "data/corpus/index.faiss")

app = FastAPI(title="Retrieval Service", version="0.1.0")


# ---------------------------------------------------------------------------
# Hardcoded fallback corpus (~10 common mathlib theorems)
# ---------------------------------------------------------------------------
_FALLBACK_CORPUS: list[TheoremMatch] = [
    TheoremMatch(
        name="Nat.add_comm",
        statement="theorem Nat.add_comm (n m : Nat) : n + m = m + n",
        module="Mathlib.Data.Nat.Basic",
        source="mathlib",
    ),
    TheoremMatch(
        name="Nat.add_assoc",
        statement="theorem Nat.add_assoc (n m k : Nat) : n + m + k = n + (m + k)",
        module="Mathlib.Data.Nat.Basic",
        source="mathlib",
    ),
    TheoremMatch(
        name="Nat.mul_comm",
        statement="theorem Nat.mul_comm (n m : Nat) : n * m = m * n",
        module="Mathlib.Data.Nat.Basic",
        source="mathlib",
    ),
    TheoremMatch(
        name="Nat.mul_assoc",
        statement="theorem Nat.mul_assoc (n m k : Nat) : n * m * k = n * (m * k)",
        module="Mathlib.Data.Nat.Basic",
        source="mathlib",
    ),
    TheoremMatch(
        name="List.length_append",
        statement="theorem List.length_append (l1 l2 : List α) : (l1 ++ l2).length = l1.length + l2.length",
        module="Mathlib.Data.List.Basic",
        source="mathlib",
    ),
    TheoremMatch(
        name="Int.add_comm",
        statement="theorem Int.add_comm (a b : Int) : a + b = b + a",
        module="Mathlib.Data.Int.Basic",
        source="mathlib",
    ),
    TheoremMatch(
        name="Nat.zero_add",
        statement="theorem Nat.zero_add (n : Nat) : 0 + n = n",
        module="Mathlib.Data.Nat.Basic",
        source="mathlib",
    ),
    TheoremMatch(
        name="Nat.succ_ne_zero",
        statement="theorem Nat.succ_ne_zero (n : Nat) : n + 1 ≠ 0",
        module="Mathlib.Data.Nat.Basic",
        source="mathlib",
    ),
    TheoremMatch(
        name="mul_self_nonneg",
        statement="theorem mul_self_nonneg (a : α) [LinearOrderedRing α] : 0 ≤ a * a",
        module="Mathlib.Algebra.Order.Ring.Lemmas",
        source="mathlib",
    ),
    TheoremMatch(
        name="abs_nonneg",
        statement="theorem abs_nonneg (a : α) [LinearOrderedAddCommGroup α] : 0 ≤ |a|",
        module="Mathlib.Algebra.Order.AbsoluteValue",
        source="mathlib",
    ),
]


# ---------------------------------------------------------------------------
# VectorIndex (stub)
# ---------------------------------------------------------------------------
class VectorIndex:
    """Stub for sentence-transformers + FAISS based theorem retrieval.

    In production:
    - Load a FAISS index from INDEX_PATH
    - Embed queries with a sentence-transformer model
    - Return nearest neighbours
    """

    def __init__(self, index_path: str = INDEX_PATH) -> None:
        self.index_path = index_path
        self._indexed = False
        self._corpus: list[TheoremMatch] = list(_FALLBACK_CORPUS)
        self._doc_count = len(self._corpus)

    def search(self, query: str, top_k: int = 10, filters: dict | None = None) -> list[TheoremMatch]:
        """Search for theorems matching the query.

        Currently uses naive keyword overlap; will be replaced with vector search.
        """
        query_lower = query.lower()
        scored: list[tuple[float, TheoremMatch]] = []
        for thm in self._corpus:
            # Simple keyword overlap scoring
            text = f"{thm.name} {thm.statement}".lower()
            tokens = set(query_lower.split())
            matches = sum(1 for t in tokens if t in text)
            score = matches / max(len(tokens), 1)
            scored.append((score, thm))

        scored.sort(key=lambda x: x[0], reverse=True)
        results: list[TheoremMatch] = []
        for score, thm in scored[:top_k]:
            result = thm.model_copy()
            result.score = round(score, 4)
            results.append(result)
        return results

    def reindex(self) -> dict:
        """Trigger re-indexing. Stub returns stats."""
        log.info("reindex_triggered")
        # In production: scan lean/ and data/corpus/, embed, build FAISS index
        self._indexed = True
        return {"status": "complete", "doc_count": self._doc_count}

    def stats(self) -> dict:
        return {
            "indexed": self._indexed,
            "doc_count": self._doc_count,
            "index_path": self.index_path,
        }


_index = VectorIndex()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "service": "retrieval"}


@app.post("/search")
async def search(req: TheoremSearchRequest):
    t0 = time.monotonic()
    matches = _index.search(req.query, req.top_k, req.filters)
    elapsed = time.monotonic() - t0
    log.info("search", query=req.query, results=len(matches), elapsed=elapsed)
    return RetrievalResult(
        query=req.query,
        results=matches,
        source="local",
    ).model_dump()


@app.post("/index")
async def reindex():
    result = _index.reindex()
    return result


@app.get("/stats")
async def stats():
    return _index.stats()
