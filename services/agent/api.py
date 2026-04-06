"""REST API for monitoring and controlling proof search sessions.

Mount this on the orchestrator or run standalone:
    uvicorn services.agent.api:app --port 8105
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from services.agent import db

app = FastAPI(title="Proof Agent API", version="0.1.0")


class CreateSessionRequest(BaseModel):
    session_id: str
    problem: str
    lean_statement: str
    imports: list[str] = ["Mathlib.Tactic"]
    metadata: dict = {}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "service": "agent"}


@app.get("/sessions")
async def list_sessions(status: str | None = None):
    """List all sessions, optionally filtered by status."""
    sessions = db.list_sessions(status)
    return [
        {
            "session_id": s["_id"],
            "problem": s["problem"][:200],
            "status": s["status"],
            "total_turns": s["total_turns"],
            "updated_at": str(s.get("updated_at", "")),
        }
        for s in sessions
    ]


@app.post("/sessions")
async def create_session(req: CreateSessionRequest):
    """Create a new proof session."""
    if db.get_session(req.session_id):
        raise HTTPException(400, f"Session {req.session_id} already exists")
    doc = db.create_session(
        session_id=req.session_id,
        problem=req.problem,
        lean_statement=req.lean_statement,
        imports=req.imports,
        metadata=req.metadata,
    )
    return {"session_id": doc["_id"], "status": doc["status"]}


@app.get("/sessions/{session_id}")
async def get_session(session_id: str):
    """Get full session details."""
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    return {
        "session_id": session["_id"],
        "problem": session["problem"],
        "lean_statement": session["lean_statement"],
        "status": session["status"],
        "total_turns": session["total_turns"],
        "best_partial_proof": session.get("best_partial_proof", ""),
        "verified_proof": session.get("verified_proof", ""),
        "created_at": str(session.get("created_at", "")),
        "updated_at": str(session.get("updated_at", "")),
    }


@app.get("/sessions/{session_id}/turns")
async def get_turns(session_id: str, limit: int = 20, promising_only: bool = False):
    """Get turns for a session."""
    if not db.get_session(session_id):
        raise HTTPException(404, "Session not found")
    if promising_only:
        turns = db.get_promising_turns(session_id, limit=limit)
    else:
        turns = db.get_recent_turns(session_id, limit=limit)
    return [
        {
            "turn": t["turn"],
            "strategy": t["strategy"],
            "result": t["result"],
            "promising": t["promising"],
            "diagnostics": t.get("diagnostics", [])[:3],
            "notes": t.get("notes", "")[:300],
            "timestamp": str(t.get("timestamp", "")),
        }
        for t in turns
    ]


@app.get("/sessions/{session_id}/strategies")
async def get_strategies(session_id: str):
    """Get all strategies tried for a session."""
    if not db.get_session(session_id):
        raise HTTPException(404, "Session not found")
    strats = db.get_strategies(session_id)
    return [
        {
            "name": s["name"],
            "description": s.get("description", ""),
            "outcome": s.get("outcome", ""),
        }
        for s in strats
    ]


@app.get("/sessions/{session_id}/context")
async def get_context(session_id: str):
    """Get the context that would be sent to the planner LLM."""
    try:
        ctx = db.build_context(session_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return ctx


@app.post("/sessions/{session_id}/abandon")
async def abandon_session(session_id: str):
    """Mark a session as abandoned."""
    if not db.get_session(session_id):
        raise HTTPException(404, "Session not found")
    db.update_session(session_id, status="abandoned")
    return {"session_id": session_id, "status": "abandoned"}


@app.post("/sessions/{session_id}/resume")
async def resume_session(session_id: str):
    """Resume a stuck/abandoned session."""
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    db.update_session(session_id, status="in_progress")
    return {"session_id": session_id, "status": "in_progress"}
