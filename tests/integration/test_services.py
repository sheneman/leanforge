"""Integration tests for forge-lean-prover services.

Uses httpx.AsyncClient with FastAPI TestClient to test endpoints
without starting real servers.
"""
from __future__ import annotations

import pytest
import httpx
from httpx import ASGITransport

from services.orchestrator.main import app as orchestrator_app
from services.lean_env.main import app as lean_env_app
from services.proof_search.main import app as proof_search_app
from services.retrieval.main import app as retrieval_app
from services.telemetry.main import app as telemetry_app


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

class TestHealthEndpoints:
    @pytest.mark.asyncio
    async def test_orchestrator_health(self):
        transport = ASGITransport(app=orchestrator_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["service"] == "orchestrator"

    @pytest.mark.asyncio
    async def test_lean_env_health(self):
        transport = ASGITransport(app=lean_env_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["service"] == "lean_env"

    @pytest.mark.asyncio
    async def test_proof_search_health(self):
        transport = ASGITransport(app=proof_search_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["service"] == "proof_search"

    @pytest.mark.asyncio
    async def test_retrieval_health(self):
        transport = ASGITransport(app=retrieval_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["service"] == "retrieval"

    @pytest.mark.asyncio
    async def test_telemetry_health(self):
        transport = ASGITransport(app=telemetry_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["service"] == "telemetry"


# ---------------------------------------------------------------------------
# Retrieval service
# ---------------------------------------------------------------------------

class TestRetrievalService:
    @pytest.mark.asyncio
    async def test_search_returns_results(self):
        transport = ASGITransport(app=retrieval_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/search", json={"query": "Nat add comm", "top_k": 5})
            assert resp.status_code == 200
            data = resp.json()
            assert "results" in data
            assert len(data["results"]) > 0
            assert data["query"] == "Nat add comm"

    @pytest.mark.asyncio
    async def test_search_respects_top_k(self):
        transport = ASGITransport(app=retrieval_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/search", json={"query": "Nat", "top_k": 2})
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["results"]) <= 2


# ---------------------------------------------------------------------------
# Proof search service -- branch lifecycle
# ---------------------------------------------------------------------------

class TestProofSearchBranchLifecycle:
    @pytest.mark.asyncio
    async def test_create_and_expand_branch(self):
        transport = ASGITransport(app=proof_search_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Create a root branch
            branch_data = {
                "branch_id": "integ-root",
                "task_id": "integ-task",
                "tactics": [],
                "status": "pending",
                "score": 0.0,
                "depth": 0,
            }
            resp = await client.post("/branches", json=branch_data)
            assert resp.status_code == 200
            created = resp.json()
            assert created["branch_id"] == "integ-root"

            # 2. Expand with child tactics
            expand_resp = await client.post(
                "/branches/integ-root/children",
                json={"tactics": ["simp", "ring"], "scores": [0.8, 0.6]},
            )
            assert expand_resp.status_code == 200
            children = expand_resp.json()["children"]
            assert len(children) == 2
            assert children[0]["tactics"] == ["simp"]
            assert children[0]["parent_id"] == "integ-root"

            # 3. Update score on a child
            child_id = children[0]["branch_id"]
            score_resp = await client.post(
                f"/branches/{child_id}/score",
                json={"score": 0.95},
            )
            assert score_resp.status_code == 200
            assert score_resp.json()["score"] == 0.95

            # 4. Get best branch
            best_resp = await client.get("/best/integ-task")
            assert best_resp.status_code == 200
            best = best_resp.json()
            assert best["score"] == 0.95

            # 5. List all branches for task
            list_resp = await client.get("/branches/integ-task")
            assert list_resp.status_code == 200
            all_branches = list_resp.json()["branches"]
            # root + 2 children
            assert len(all_branches) == 3
