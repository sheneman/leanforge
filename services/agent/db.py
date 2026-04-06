"""MongoDB state management for long-running proof sessions.

Collections:
    sessions    — one doc per proof problem (metadata, status, lean statement)
    turns       — one doc per attempt (strategy, tactics, result, diagnostics)
    strategies  — deduplicated strategy descriptions with outcomes
    lemmas      — relevant lemmas discovered during search
    lessons     — technical lessons learned (API facts, syntax rules, etc.)

The agent queries these collections dynamically to build a focused LLM
prompt without loading full history into context.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from pymongo import MongoClient, DESCENDING
from pymongo.collection import Collection

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "leanforge")


def _client() -> MongoClient:
    return MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)


def _db():
    return _client()[MONGO_DB]


def sessions() -> Collection:
    return _db()["sessions"]


def turns() -> Collection:
    return _db()["turns"]


def strategies() -> Collection:
    return _db()["strategies"]


def lemmas() -> Collection:
    return _db()["lemmas"]


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------

def create_session(
    session_id: str,
    problem: str,
    lean_statement: str,
    imports: list[str] | None = None,
    metadata: dict | None = None,
) -> dict:
    """Create a new proof session."""
    doc = {
        "_id": session_id,
        "problem": problem,
        "lean_statement": lean_statement,
        "imports": imports or ["Mathlib.Tactic"],
        "status": "in_progress",  # in_progress | verified | stuck | abandoned
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "total_turns": 0,
        "best_partial_proof": "",
        "verified_proof": "",
        "metadata": metadata or {},
    }
    sessions().insert_one(doc)
    return doc


def get_session(session_id: str) -> dict | None:
    return sessions().find_one({"_id": session_id})


def update_session(session_id: str, **fields) -> None:
    fields["updated_at"] = datetime.now(timezone.utc)
    sessions().update_one({"_id": session_id}, {"$set": fields})


def increment_turns(session_id: str) -> None:
    sessions().update_one(
        {"_id": session_id},
        {
            "$inc": {"total_turns": 1},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
    )


def list_sessions(status: str | None = None) -> list[dict]:
    query = {}
    if status:
        query["status"] = status
    return list(sessions().find(query).sort("updated_at", DESCENDING))


# ---------------------------------------------------------------------------
# Turns — one per agent iteration
# ---------------------------------------------------------------------------

def log_turn(
    session_id: str,
    turn_number: int,
    strategy: str,
    tactics_tried: list[str],
    lean_source: str,
    result: str,       # verified | failed | partial | error
    diagnostics: list[str],
    promising: bool,
    notes: str = "",
    subgoals_remaining: list[str] | None = None,
) -> str:
    """Log one turn of proof search."""
    doc = {
        "session_id": session_id,
        "turn": turn_number,
        "strategy": strategy,
        "tactics_tried": tactics_tried,
        "lean_source": lean_source,
        "result": result,
        "diagnostics": diagnostics,
        "promising": promising,
        "notes": notes,
        "subgoals_remaining": subgoals_remaining or [],
        "timestamp": datetime.now(timezone.utc),
    }
    r = turns().insert_one(doc)
    increment_turns(session_id)
    return str(r.inserted_id)


def get_recent_turns(session_id: str, limit: int = 5) -> list[dict]:
    """Get the most recent N turns for a session."""
    return list(
        turns()
        .find({"session_id": session_id})
        .sort("turn", DESCENDING)
        .limit(limit)
    )


def get_promising_turns(session_id: str, limit: int = 10) -> list[dict]:
    """Get turns marked as promising."""
    return list(
        turns()
        .find({"session_id": session_id, "promising": True})
        .sort("turn", DESCENDING)
        .limit(limit)
    )


def get_failed_strategies(session_id: str) -> list[str]:
    """Get deduplicated list of strategies that failed (not promising)."""
    pipeline = [
        {"$match": {"session_id": session_id, "promising": False}},
        {"$group": {"_id": "$strategy"}},
    ]
    return [doc["_id"] for doc in turns().aggregate(pipeline)]


def get_turn_count(session_id: str) -> int:
    return turns().count_documents({"session_id": session_id})


# ---------------------------------------------------------------------------
# Strategies — deduplicated high-level approaches
# ---------------------------------------------------------------------------

def log_strategy(
    session_id: str,
    name: str,
    description: str,
    outcome: str,  # promising | dead_end | partial | verified
    turn_refs: list[int] | None = None,
) -> None:
    """Log or update a high-level strategy."""
    strategies().update_one(
        {"session_id": session_id, "name": name},
        {
            "$set": {
                "description": description,
                "outcome": outcome,
                "updated_at": datetime.now(timezone.utc),
            },
            "$addToSet": {"turn_refs": {"$each": turn_refs or []}},
        },
        upsert=True,
    )


def get_strategies(session_id: str) -> list[dict]:
    return list(strategies().find({"session_id": session_id}))


def get_dead_ends(session_id: str) -> list[str]:
    return [
        s["name"]
        for s in strategies().find({"session_id": session_id, "outcome": "dead_end"})
    ]


def get_promising_strategies(session_id: str) -> list[dict]:
    return list(
        strategies().find(
            {"session_id": session_id, "outcome": {"$in": ["promising", "partial"]}}
        )
    )


# ---------------------------------------------------------------------------
# Lemmas — relevant mathlib lemmas discovered
# ---------------------------------------------------------------------------

def log_lemma(session_id: str, name: str, statement: str, module: str = "", notes: str = "") -> None:
    lemmas().update_one(
        {"session_id": session_id, "name": name},
        {
            "$set": {
                "statement": statement,
                "module": module,
                "notes": notes,
                "updated_at": datetime.now(timezone.utc),
            },
        },
        upsert=True,
    )


def get_lemmas(session_id: str) -> list[dict]:
    return list(lemmas().find({"session_id": session_id}))


# ---------------------------------------------------------------------------
# Lessons — technical facts learned across turns
# ---------------------------------------------------------------------------

def lessons() -> Collection:
    return _db()["lessons"]


def log_lesson(session_id: str, lesson: str, category: str = "technical") -> None:
    """Log a technical lesson (idempotent — deduplicates by text)."""
    lessons().update_one(
        {"session_id": session_id, "lesson": lesson},
        {
            "$set": {
                "category": category,
                "updated_at": datetime.now(timezone.utc),
            },
            "$inc": {"hit_count": 1},
        },
        upsert=True,
    )


def get_lessons(session_id: str) -> list[dict]:
    return list(lessons().find({"session_id": session_id}).sort("hit_count", DESCENDING))


def auto_extract_lessons(session_id: str) -> int:
    """Scan recent turns for repeated diagnostics and auto-create lessons.

    If the same diagnostic substring appears in 3+ different turns,
    extract it as a lesson so the planner stops repeating the mistake.
    Returns the number of new lessons created.
    """
    # Get all diagnostics from this session
    pipeline = [
        {"$match": {"session_id": session_id}},
        {"$unwind": "$diagnostics"},
        {"$group": {"_id": "$diagnostics", "count": {"$sum": 1}}},
        {"$match": {"count": {"$gte": 3}}},
        {"$sort": {"count": DESCENDING}},
        {"$limit": 20},
    ]
    repeated = list(turns().aggregate(pipeline))

    # Known diagnostic patterns → lesson text
    _LESSON_MAP = {
        "unknown constant": "This constant/lemma does not exist in the current mathlib. Search for the correct name.",
        "unknown identifier": "This identifier does not exist. Use scripts/search.py to find the correct name.",
        "introN": "'introN' is NOT a valid Lean 4 tactic. Use 'intro a b c' (naming each variable).",
        "iterate_succ_apply'": "Nat.iterate_succ_apply' does NOT exist. Use Function.iterate_succ_apply' or unfold Nat.iterate manually.",
        "unexpected token": "Syntax error — check Lean 4 syntax. Common issues: missing 'by', wrong indentation, stale Lean 3 syntax.",
    }

    new_count = 0
    for item in repeated:
        diag = item["_id"]
        if not isinstance(diag, str) or len(diag) < 10:
            continue
        # Check if this matches a known pattern
        lesson_text = None
        for pattern, text in _LESSON_MAP.items():
            if pattern.lower() in diag.lower():
                lesson_text = f"{text} (seen {item['count']}x: '{diag[:100]}')"
                break
        if not lesson_text:
            # Generic lesson from repeated diagnostic
            lesson_text = f"Repeated error ({item['count']}x): {diag[:150]}"

        # Only log if not already a lesson
        existing = lessons().find_one({"session_id": session_id, "lesson": {"$regex": diag[:50]}})
        if not existing:
            log_lesson(session_id, lesson_text, category="auto_extracted")
            new_count += 1

    return new_count


# ---------------------------------------------------------------------------
# Context builder — assemble a focused prompt from DB queries
# ---------------------------------------------------------------------------

def build_context(session_id: str, max_recent: int = 5, max_promising: int = 5) -> dict:
    """Build a context dict for the planner LLM by querying MongoDB.

    Returns a dict with keys that can be formatted into a prompt
    without consuming the full turn history.
    """
    session = get_session(session_id)
    if not session:
        raise ValueError(f"Session {session_id} not found")

    recent = get_recent_turns(session_id, limit=max_recent)
    promising = get_promising_turns(session_id, limit=max_promising)
    dead_ends = get_dead_ends(session_id)
    promising_strats = get_promising_strategies(session_id)
    found_lemmas = get_lemmas(session_id)
    found_lessons = get_lessons(session_id)
    total_turns = get_turn_count(session_id)

    return {
        "session_id": session_id,
        "problem": session["problem"],
        "lean_statement": session["lean_statement"],
        "imports": session["imports"],
        "status": session["status"],
        "total_turns": total_turns,
        "best_partial_proof": session.get("best_partial_proof", ""),
        "lessons": [
            l["lesson"] for l in found_lessons[:20]
        ],
        "recent_turns": [
            {
                "turn": t["turn"],
                "strategy": t["strategy"],
                "result": t["result"],
                "diagnostics": t["diagnostics"][:2],
                "promising": t["promising"],
            }
            for t in recent
        ],
        "promising_turns": [
            {
                "turn": t["turn"],
                "strategy": t["strategy"],
                "notes": t.get("notes", "")[:100],
            }
            for t in promising
        ],
        "dead_ends": dead_ends[:15],
        "promising_strategies": [
            {"name": s["name"], "description": s["description"][:100]}
            for s in promising_strats[:10]  # cap at 10 most recent
        ],
        "lemmas_found": [
            {"name": l["name"], "statement": l["statement"][:150]}
            for l in found_lemmas[:15]
        ],
    }
