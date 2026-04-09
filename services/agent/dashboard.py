"""Interactive theorem proving workbench for LeanForge.

A single-page application with live event streaming, session management,
and a terminal-style proof search visualization.

Run: uvicorn services.agent.dashboard:app --port 8105
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import threading

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
from pymongo import MongoClient, DESCENDING

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "leanforge")
PREFIX = os.getenv("DASHBOARD_PREFIX", "/dashboard")

app = FastAPI(title="LeanForge Workbench", version="2.0.0")


def _db():
    return MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)[MONGO_DB]


def _esc(s) -> str:
    return html.escape(str(s))


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class CreateSessionRequest(BaseModel):
    session_id: str
    problem: str
    lean_statement: str = ""  # Optional — auto-formalized if not provided
    imports: str = "Mathlib.Tactic"
    max_turns: int = 500


# ---------------------------------------------------------------------------
# Background runner
# ---------------------------------------------------------------------------

def _start_runner(session_id: str, max_turns: int) -> None:
    import traceback
    try:
        from services.agent.runner import run_loop
        run_loop(session_id, max_turns=max_turns, delay=10)
    except Exception as e:
        print(f"RUNNER CRASHED: {session_id}: {e}", flush=True)
        traceback.print_exc()
        try:
            from services.agent import db as agent_db
            agent_db.emit_event(session_id, "error", {"message": f"Runner crashed: {e}"})
            agent_db.update_session(session_id, status="stuck")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# API endpoints (mounted at /api/)
# ---------------------------------------------------------------------------

@app.get("/api/sessions")
async def api_list_sessions():
    db = _db()
    sessions = list(db.sessions.find().sort("updated_at", DESCENDING))
    result = []
    for s in sessions:
        result.append({
            "session_id": str(s["_id"]),
            "status": s.get("status", "unknown"),
            "problem": s.get("problem", ""),
            "total_turns": s.get("total_turns", 0),
            "updated_at": str(s.get("updated_at", ""))[:19],
            "created_at": str(s.get("created_at", ""))[:19],
        })
    return JSONResponse(result)


@app.post("/api/sessions")
async def api_create_session(req: CreateSessionRequest):
    from services.agent import db as agent_db
    existing = agent_db.get_session(req.session_id)
    if existing:
        return JSONResponse({"error": f"Session '{req.session_id}' already exists"}, status_code=409)

    imports_list = [i.strip() for i in req.imports.split(",") if i.strip()]
    agent_db.create_session(
        session_id=req.session_id,
        problem=req.problem,
        lean_statement=req.lean_statement,
        imports=imports_list,
    )
    thread = threading.Thread(
        target=_start_runner,
        args=(req.session_id, req.max_turns),
        daemon=True,
    )
    thread.start()
    return JSONResponse({"session_id": req.session_id, "status": "started"})


@app.get("/api/sessions/{session_id}")
async def api_get_session(session_id: str):
    db = _db()
    s = db.sessions.find_one({"_id": session_id})
    if not s:
        return JSONResponse({"error": "Not found"}, status_code=404)
    strats = list(db.strategies.find({"session_id": session_id}))
    lessons = list(db.lessons.find({"session_id": session_id}).sort("hit_count", DESCENDING).limit(30))
    dead = sum(1 for x in strats if x.get("outcome") == "dead_end")
    promising = sum(1 for x in strats if x.get("outcome") in ("promising", "partial"))
    verified_s = sum(1 for x in strats if x.get("outcome") == "verified")
    return JSONResponse({
        "session_id": str(s["_id"]),
        "status": s.get("status", "unknown"),
        "problem": s.get("problem", ""),
        "lean_statement": s.get("lean_statement", ""),
        "imports": s.get("imports", []),
        "total_turns": s.get("total_turns", 0),
        "best_partial_proof": s.get("best_partial_proof", ""),
        "verified_proof": s.get("verified_proof", ""),
        "updated_at": str(s.get("updated_at", ""))[:19],
        "created_at": str(s.get("created_at", ""))[:19],
        "strategies": {
            "total": len(strats),
            "dead_ends": dead,
            "promising": promising,
            "verified": verified_s,
        },
        "lessons": [
            {"lesson": l["lesson"], "category": l.get("category", ""), "hits": l.get("hit_count", 0)}
            for l in lessons
        ],
    })


@app.post("/api/sessions/{session_id}/stop")
async def api_stop_session(session_id: str):
    from services.agent import db as agent_db
    s = agent_db.get_session(session_id)
    if not s:
        return JSONResponse({"error": "Not found"}, status_code=404)
    agent_db.update_session(session_id, status="abandoned")
    agent_db.emit_event(session_id, "error", {"message": "Session stopped by user"})
    return JSONResponse({"session_id": session_id, "status": "abandoned"})


@app.delete("/api/sessions/{session_id}")
async def api_delete_session(session_id: str):
    from services.agent import db as agent_db
    s = agent_db.get_session(session_id)
    if not s:
        return JSONResponse({"error": "Not found"}, status_code=404)
    # Stop if running
    agent_db.update_session(session_id, status="abandoned")
    # Delete all related data
    agent_db.turns().delete_many({"session_id": session_id})
    agent_db.strategies().delete_many({"session_id": session_id})
    agent_db.lemmas().delete_many({"session_id": session_id})
    agent_db.lessons().delete_many({"session_id": session_id})
    agent_db.events().delete_many({"session_id": session_id})
    agent_db.sessions().delete_one({"_id": session_id})
    return JSONResponse({"session_id": session_id, "status": "deleted"})


@app.post("/api/sessions/{session_id}/resume")
async def api_resume_session(session_id: str):
    from services.agent import db as agent_db
    s = agent_db.get_session(session_id)
    if not s:
        return JSONResponse({"error": "Not found"}, status_code=404)
    agent_db.update_session(session_id, status="in_progress")
    thread = threading.Thread(
        target=_start_runner,
        args=(session_id, 500),
        daemon=True,
    )
    thread.start()
    return JSONResponse({"session_id": session_id, "status": "in_progress"})


@app.get("/api/sessions/{session_id}/events")
async def api_get_events(session_id: str, since: str | None = None, limit: int = 50):
    from services.agent import db as agent_db
    events = agent_db.get_events_since(session_id, since_id=since, limit=limit)
    result = []
    for e in events:
        result.append({
            "id": str(e["_id"]),
            "type": e["type"],
            "data": e.get("data", {}),
            "timestamp": str(e.get("timestamp", ""))[:19],
        })
    return JSONResponse(result)


@app.get("/api/stream/{session_id}")
async def api_stream_events(session_id: str):
    """SSE endpoint — replays all existing events then streams new ones."""
    async def event_generator():
        db = _db()
        last_id = None

        # REPLAY: send all existing events first so the UI shows full history
        existing = list(db.events.find({"session_id": session_id}).sort("_id", 1))
        for evt in existing:
            last_id = evt["_id"]
            data = {
                "id": str(evt["_id"]),
                "type": evt["type"],
                "data": evt.get("data", {}),
                "timestamp": str(evt.get("timestamp", ""))[:19],
            }
            yield f"data: {json.dumps(data)}\n\n"

        # Send a heartbeat so the connection stays alive
        yield f": heartbeat\n\n"

        # STREAM: poll for new events — fast polling (0.5s)
        while True:
            await asyncio.sleep(0.5)
            query = {"session_id": session_id}
            if last_id:
                query["_id"] = {"$gt": last_id}
            new_events = list(
                db.events.find(query).sort("_id", 1).limit(50)
            )
            for evt in new_events:
                last_id = evt["_id"]
                data = {
                    "id": str(evt["_id"]),
                    "type": evt["type"],
                    "data": evt.get("data", {}),
                    "timestamp": str(evt.get("timestamp", ""))[:19],
                }
                yield f"data: {json.dumps(data)}\n\n"

            # Send periodic heartbeat to prevent proxy/browser timeout
            if not new_events:
                yield f": heartbeat\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


# ---------------------------------------------------------------------------
# Main SPA page
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
@app.get("", response_class=HTMLResponse)
async def workbench():
    prefix = _esc(PREFIX)
    return HTMLResponse(f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LeanForge Workbench</title>
<style>
:root {{
  --bg: #0d1117;
  --surface: #161b22;
  --surface2: #1c2128;
  --border: #30363d;
  --text: #c9d1d9;
  --text-dim: #8b949e;
  --text-bright: #f0f6fc;
  --blue: #58a6ff;
  --green: #3fb950;
  --red: #f85149;
  --yellow: #d29922;
  --purple: #bc8cff;
  --teal: #39d353;
  --orange: #f0883e;
}}

* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
  height: 100vh;
  overflow: hidden;
}}

/* Layout */
.app {{
  display: grid;
  grid-template-columns: 300px 1fr 320px;
  grid-template-rows: 48px 1fr;
  height: 100vh;
}}

/* Header */
.header {{
  grid-column: 1 / -1;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  padding: 0 16px;
  gap: 12px;
}}
.header h1 {{
  font-size: 16px;
  font-weight: 600;
  color: var(--text-bright);
}}
.header .logo {{
  color: var(--green);
  font-size: 18px;
  font-weight: 700;
}}
.header .subtitle {{
  color: var(--text-dim);
  font-size: 13px;
}}

/* Left sidebar */
.sidebar-left {{
  background: var(--surface);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}}
.sidebar-left h2 {{
  font-size: 13px;
  font-weight: 600;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  padding: 12px 16px 8px;
}}
.session-list {{
  flex: 1;
  overflow-y: auto;
  padding: 0 8px;
}}
.session-item {{
  padding: 10px 12px;
  border-radius: 6px;
  cursor: pointer;
  margin-bottom: 2px;
  border: 1px solid transparent;
  transition: background 0.15s;
}}
.session-item:hover {{ background: var(--surface2); }}
.session-item.active {{
  background: var(--surface2);
  border-color: var(--blue);
}}
.session-item .sid {{
  font-size: 13px;
  font-weight: 600;
  color: var(--text-bright);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  display: flex;
  justify-content: space-between;
  align-items: center;
}}
.session-item .trash-btn {{
  background: none;
  border: none;
  color: var(--text-dim);
  cursor: pointer;
  font-size: 14px;
  padding: 0 2px;
  opacity: 0;
  transition: opacity 0.15s, color 0.15s;
}}
.session-item:hover .trash-btn {{ opacity: 1; }}
.session-item .trash-btn:hover {{ color: var(--red); }}
.session-item .meta {{
  font-size: 11px;
  color: var(--text-dim);
  margin-top: 3px;
  display: flex;
  gap: 8px;
  align-items: center;
}}

/* Status badges */
.badge {{
  display: inline-block;
  padding: 1px 8px;
  border-radius: 12px;
  font-size: 11px;
  font-weight: 600;
  line-height: 18px;
}}
.badge-verified {{ background: rgba(63,185,80,0.15); color: var(--green); }}
.badge-in_progress {{ background: rgba(88,166,255,0.15); color: var(--blue); }}
.badge-failed {{ background: rgba(248,81,73,0.15); color: var(--red); }}
.badge-stuck {{ background: rgba(210,153,34,0.15); color: var(--yellow); }}
.badge-abandoned {{ background: rgba(139,148,158,0.15); color: var(--text-dim); }}

/* Create form */
.create-form {{
  border-top: 1px solid var(--border);
  padding: 12px;
  background: var(--surface2);
}}
.create-form h3 {{
  font-size: 12px;
  font-weight: 600;
  color: var(--text-dim);
  text-transform: uppercase;
  margin-bottom: 8px;
}}
.create-form .toggle-btn {{
  width: 100%;
  padding: 8px;
  background: var(--blue);
  color: #fff;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 600;
}}
.create-form .toggle-btn:hover {{ opacity: 0.9; }}
.form-fields {{ display: none; }}
.form-fields.visible {{ display: block; }}
.form-fields label {{
  display: block;
  font-size: 11px;
  color: var(--text-dim);
  margin: 8px 0 3px;
  font-weight: 600;
}}
.form-fields input,
.form-fields textarea {{
  width: 100%;
  padding: 6px 8px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--text);
  font-size: 12px;
  font-family: inherit;
}}
.form-fields textarea {{
  font-family: 'SF Mono', 'Consolas', 'Monaco', monospace;
  resize: vertical;
  min-height: 60px;
}}
.form-fields input:focus,
.form-fields textarea:focus {{
  outline: none;
  border-color: var(--blue);
}}
.form-fields .start-btn {{
  width: 100%;
  margin-top: 10px;
  padding: 8px;
  background: var(--green);
  color: #fff;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 600;
}}
.form-fields .start-btn:hover {{ opacity: 0.9; }}
.form-fields .start-btn:disabled {{
  opacity: 0.5;
  cursor: not-allowed;
}}
.form-error {{
  color: var(--red);
  font-size: 11px;
  margin-top: 4px;
}}

/* Main area */
.main {{
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg);
}}
.main-header {{
  padding: 12px 20px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 12px;
  flex-shrink: 0;
}}
.main-header h2 {{
  font-size: 15px;
  font-weight: 600;
  color: var(--text-bright);
}}
.live-dot {{
  display: inline-block;
  width: 8px;
  height: 8px;
  background: var(--green);
  border-radius: 50%;
  animation: pulse 1.5s infinite;
}}
@keyframes pulse {{
  0%, 100% {{ opacity: 1; }}
  50% {{ opacity: 0.3; }}
}}
.no-session {{
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-dim);
  font-size: 14px;
}}

/* Event stream */
.event-stream {{
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px;
  font-family: 'SF Mono', 'Consolas', 'Monaco', monospace;
  font-size: 13px;
  line-height: 1.6;
}}
.event-stream::-webkit-scrollbar {{ width: 6px; }}
.event-stream::-webkit-scrollbar-track {{ background: transparent; }}
.event-stream::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 3px; }}

/* Event types */
.evt {{
  margin-bottom: 4px;
  padding: 2px 0;
}}
.evt-time {{
  color: var(--text-dim);
  font-size: 11px;
  margin-right: 8px;
}}
.evt-turn-start {{
  background: rgba(88,166,255,0.08);
  border-left: 3px solid var(--blue);
  padding: 6px 12px;
  margin: 16px 0 4px;
  border-radius: 0 4px 4px 0;
  font-weight: 600;
  color: var(--blue);
}}
.evt-turn-start:first-child {{ margin-top: 0; }}
.evt-planner {{
  color: var(--text);
}}
.evt-planner .strategy {{
  color: var(--blue);
  font-weight: 600;
}}
.evt-planner .reasoning {{
  color: var(--text-dim);
  font-style: italic;
}}
.evt-reasoning {{
  background: rgba(88,166,255,0.06);
  border: 1px solid rgba(88,166,255,0.1);
  border-radius: 4px;
  padding: 8px 12px;
  margin: 4px 0;
  color: var(--text-dim);
  white-space: pre-wrap;
  font-size: 12px;
  max-height: 400px;
  overflow-y: auto;
  line-height: 1.5;
}}
.evt-tactics {{
  background: rgba(63,185,80,0.06);
  border: 1px solid rgba(63,185,80,0.15);
  border-radius: 4px;
  padding: 8px 12px;
  margin: 4px 0;
  color: var(--green);
  white-space: pre-wrap;
  word-break: break-word;
}}
.evt-search {{
  color: var(--purple);
}}
.evt-search .lemma-name {{
  color: var(--purple);
  font-weight: 600;
}}
.evt-web {{
  color: var(--orange);
}}
.evt-verify-start {{
  color: var(--text-dim);
}}
.evt-verify-start details {{
  margin: 4px 0;
}}
.evt-verify-start summary {{
  cursor: pointer;
  color: var(--text-dim);
  font-size: 12px;
}}
.evt-verify-start pre {{
  background: var(--surface);
  padding: 10px;
  border-radius: 4px;
  overflow-x: auto;
  max-height: 300px;
  overflow-y: auto;
  font-size: 12px;
  color: var(--text);
  margin-top: 4px;
  border: 1px solid var(--border);
}}
.evt-verify-ok {{
  color: var(--green);
  font-weight: 600;
}}
.evt-verify-fail {{
  color: var(--red);
}}
.evt-verify-diag {{
  color: var(--red);
  font-size: 12px;
  padding-left: 20px;
}}
.evt-turn-complete {{
  padding: 4px 12px;
  margin: 4px 0 16px;
  border-radius: 4px;
  font-weight: 600;
  font-size: 12px;
}}
.evt-turn-complete.ok {{
  background: rgba(63,185,80,0.08);
  color: var(--green);
}}
.evt-turn-complete.partial {{
  background: rgba(210,153,34,0.08);
  color: var(--yellow);
}}
.evt-turn-complete.fail {{
  background: rgba(248,81,73,0.08);
  color: var(--red);
}}
.evt-lesson {{
  background: rgba(210,153,34,0.08);
  border-left: 3px solid var(--yellow);
  padding: 6px 12px;
  margin: 4px 0;
  border-radius: 0 4px 4px 0;
  color: var(--yellow);
  font-size: 12px;
}}
.evt-error {{
  background: rgba(248,81,73,0.08);
  border-left: 3px solid var(--red);
  padding: 6px 12px;
  margin: 4px 0;
  border-radius: 0 4px 4px 0;
  color: var(--red);
}}
.evt-synthesize {{
  color: var(--teal);
  font-size: 12px;
}}

/* Right sidebar */
.sidebar-right {{
  background: var(--surface);
  border-left: 1px solid var(--border);
  overflow-y: auto;
  padding: 0;
}}
.sidebar-right::-webkit-scrollbar {{ width: 6px; }}
.sidebar-right::-webkit-scrollbar-track {{ background: transparent; }}
.sidebar-right::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 3px; }}
.right-section {{
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}}
.right-section h3 {{
  font-size: 12px;
  font-weight: 600;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 8px;
}}
.right-section .value {{
  font-size: 24px;
  font-weight: 700;
  color: var(--text-bright);
}}
.right-section .label {{
  font-size: 11px;
  color: var(--text-dim);
  margin-top: 2px;
}}
.stat-grid {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}}
.stat-box {{
  background: var(--bg);
  border-radius: 6px;
  padding: 10px;
  text-align: center;
}}
.stat-box .num {{
  font-size: 20px;
  font-weight: 700;
}}
.stat-box .num.green {{ color: var(--green); }}
.stat-box .num.red {{ color: var(--red); }}
.stat-box .num.yellow {{ color: var(--yellow); }}
.stat-box .num.blue {{ color: var(--blue); }}
.stat-box .slabel {{
  font-size: 10px;
  color: var(--text-dim);
  text-transform: uppercase;
  margin-top: 2px;
}}

.action-btns {{
  display: flex;
  gap: 8px;
  margin-top: 8px;
}}
.action-btns button {{
  flex: 1;
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface2);
  color: var(--text);
  font-size: 12px;
  cursor: pointer;
  font-weight: 600;
  transition: background 0.15s;
}}
.action-btns button:hover {{ background: var(--border); }}
.action-btns .stop-btn {{ border-color: var(--red); color: var(--red); }}
.action-btns .resume-btn {{ border-color: var(--green); color: var(--green); }}
.action-btns .delete-btn {{ border-color: #888; color: #888; font-size: 0.8em; }}
.action-btns .export-btn {{ border-color: var(--blue); color: var(--blue); font-size: 0.8em; }}

.lesson-list {{
  max-height: 300px;
  overflow-y: auto;
}}
.lesson-item {{
  font-size: 11px;
  color: var(--text);
  padding: 6px 8px;
  border-left: 2px solid var(--yellow);
  margin-bottom: 4px;
  background: rgba(210,153,34,0.04);
  border-radius: 0 4px 4px 0;
  line-height: 1.4;
}}
.lesson-item .cat {{
  color: var(--text-dim);
  font-size: 10px;
  text-transform: uppercase;
}}

.problem-text {{
  font-size: 12px;
  color: var(--text);
  line-height: 1.5;
  word-break: break-word;
}}
.lean-stmt {{
  font-family: 'SF Mono', 'Consolas', 'Monaco', monospace;
  font-size: 11px;
  color: var(--green);
  background: var(--bg);
  padding: 8px;
  border-radius: 4px;
  word-break: break-all;
  margin-top: 4px;
}}
.right-placeholder {{
  color: var(--text-dim);
  font-size: 13px;
  padding: 20px 16px;
  text-align: center;
}}

/* Collapsible */
.collapsible-header {{
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 6px;
}}
.collapsible-header .arrow {{
  font-size: 10px;
  color: var(--text-dim);
  transition: transform 0.15s;
}}
.collapsible-header .arrow.open {{ transform: rotate(90deg); }}
.collapsible-body {{ display: none; }}
.collapsible-body.visible {{ display: block; }}
</style>
</head>
<body>
<div class="app">
  <!-- Header -->
  <div class="header">
    <span class="logo">LF</span>
    <h1>LeanForge Workbench</h1>
    <span class="subtitle">Interactive Theorem Proving</span>
  </div>

  <!-- Left sidebar -->
  <div class="sidebar-left">
    <h2>Sessions</h2>
    <div class="session-list" id="session-list">
      <div style="color:var(--text-dim);padding:16px;font-size:13px;">Loading...</div>
    </div>
    <div class="create-form">
      <button class="toggle-btn" id="toggle-create" onclick="toggleCreateForm()">+ New Session</button>
      <div class="form-fields" id="create-fields">
        <label for="f-sid">Session ID</label>
        <input id="f-sid" type="text" placeholder="e.g. collatz_descent">
        <label for="f-problem">Problem Description</label>
        <textarea id="f-problem" placeholder="Natural language description..."></textarea>
        <label for="f-lean">Lean Theorem Statement <span style="color:#484f58;font-weight:normal">(optional — auto-generated from problem if blank)</span></label>
        <textarea id="f-lean" placeholder="Leave blank to auto-formalize, or enter e.g.: theorem foo (n : Nat) : n + 0 = n" style="min-height:80px;"></textarea>
        <label for="f-imports">Imports (comma-separated)</label>
        <input id="f-imports" type="text" value="Mathlib.Tactic">
        <label for="f-turns">Max Turns</label>
        <input id="f-turns" type="number" value="500" min="1" max="10000">
        <div class="form-error" id="form-error"></div>
        <button class="start-btn" id="start-btn" onclick="createSession()">Start Proving</button>
      </div>
    </div>
  </div>

  <!-- Main area -->
  <div class="main" id="main-area">
    <div class="no-session" id="no-session">
      Select a session or create a new one to begin
    </div>
    <div id="main-content" style="display:none;flex-direction:column;flex:1;overflow:hidden;">
      <div class="main-header">
        <span class="live-dot" id="live-dot" style="display:none;"></span>
        <h2 id="main-title">Session</h2>
        <span class="badge" id="main-badge"></span>
      </div>
      <div class="event-stream" id="event-stream"></div>
    </div>
  </div>

  <!-- Right sidebar -->
  <div class="sidebar-right" id="sidebar-right">
    <div class="right-placeholder">Select a session to view details</div>
  </div>
</div>

<script>
const PREFIX = {json.dumps(prefix)};
let currentSession = null;
let eventSource = null;
let autoScroll = true;
let sessionPollTimer = null;
let detailPollTimer = null;

// --- Utility ---
function esc(s) {{
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}}

function formatTime(ts) {{
  if (!ts || ts.length < 19) return '';
  return ts.substring(11, 19);
}}

function badgeClass(status) {{
  return 'badge badge-' + (status || 'unknown');
}}

// --- Session list ---
async function loadSessions() {{
  try {{
    const resp = await fetch(PREFIX + '/api/sessions');
    const sessions = await resp.json();
    const list = document.getElementById('session-list');
    if (sessions.length === 0) {{
      list.innerHTML = '<div style="color:var(--text-dim);padding:16px;font-size:13px;">No sessions yet. Create one below.</div>';
      return;
    }}
    let html = '';
    for (const s of sessions) {{
      const active = currentSession === s.session_id ? ' active' : '';
      html += '<div class="session-item' + active + '" onclick="selectSession(\\''+esc(s.session_id)+'\\')"><div class="sid"><span>'+esc(s.session_id)+'</span><button class="trash-btn" onclick="event.stopPropagation();quickDelete(\\''+esc(s.session_id)+'\\')">&#128465;</button></div>';
      html += '<div class="meta"><span class="'+badgeClass(s.status)+'">'+esc(s.status)+'</span>';
      html += '<span>T'+s.total_turns+'</span>';
      html += '<span>'+esc(s.updated_at||'')+'</span></div></div>';
    }}
    list.innerHTML = html;
  }} catch(e) {{
    console.error('Failed to load sessions', e);
  }}
}}

// --- Create form ---
function toggleCreateForm() {{
  const fields = document.getElementById('create-fields');
  fields.classList.toggle('visible');
}}

async function createSession() {{
  const sid = document.getElementById('f-sid').value.trim();
  const problem = document.getElementById('f-problem').value.trim();
  const lean = document.getElementById('f-lean').value.trim();
  const imports = document.getElementById('f-imports').value.trim();
  const turns = parseInt(document.getElementById('f-turns').value) || 500;
  const errEl = document.getElementById('form-error');

  if (!sid || !problem) {{
    errEl.textContent = 'Session ID and Problem are required.';
    return;
  }}

  const btn = document.getElementById('start-btn');
  btn.disabled = true;
  btn.textContent = 'Creating...';
  errEl.textContent = '';

  try {{
    const resp = await fetch(PREFIX + '/api/sessions', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{
        session_id: sid,
        problem: problem,
        lean_statement: lean,
        imports: imports,
        max_turns: turns,
      }}),
    }});
    const data = await resp.json();
    if (!resp.ok) {{
      errEl.textContent = data.error || 'Failed to create session';
      return;
    }}
    // Clear form
    document.getElementById('f-sid').value = '';
    document.getElementById('f-problem').value = '';
    document.getElementById('f-lean').value = '';
    document.getElementById('create-fields').classList.remove('visible');
    // Select the new session
    await loadSessions();
    selectSession(sid);
  }} catch(e) {{
    errEl.textContent = 'Network error: ' + e.message;
  }} finally {{
    btn.disabled = false;
    btn.textContent = 'Start Proving';
  }}
}}

// --- Select session ---
async function selectSession(sid) {{
  currentSession = sid;
  seenEventIds.clear();

  // Update list highlight
  document.querySelectorAll('.session-item').forEach(el => {{
    el.classList.toggle('active', el.querySelector('.sid')?.textContent === sid);
  }});

  // Stop existing SSE
  if (eventSource) {{
    eventSource.close();
    eventSource = null;
  }}
  if (detailPollTimer) {{
    clearInterval(detailPollTimer);
  }}

  // Show main content
  document.getElementById('no-session').style.display = 'none';
  const mc = document.getElementById('main-content');
  mc.style.display = 'flex';
  document.getElementById('event-stream').innerHTML = '';
  document.getElementById('main-title').textContent = sid;
  autoScroll = true;

  // Load session detail for right sidebar
  await loadSessionDetail(sid);

  // Load existing events
  await loadExistingEvents(sid);

  // Start SSE
  startSSE(sid);

  // Poll session detail every 5 seconds
  detailPollTimer = setInterval(() => loadSessionDetail(sid), 5000);
}}

async function loadSessionDetail(sid) {{
  try {{
    const resp = await fetch(PREFIX + '/api/sessions/' + encodeURIComponent(sid));
    if (!resp.ok) return;
    const s = await resp.json();
    renderRightSidebar(s);
  }} catch(e) {{ /* ignore */ }}
}}

function renderRightSidebar(s) {{
  const sb = document.getElementById('sidebar-right');
  const statusBadge = '<span class="'+badgeClass(s.status)+'">'+esc(s.status)+'</span>';
  const isRunning = s.status === 'in_progress';

  let html = '';
  // Status
  html += '<div class="right-section"><h3>Status</h3>';
  html += '<div style="display:flex;align-items:center;gap:8px;">';
  if (isRunning) html += '<span class="live-dot"></span>';
  html += statusBadge + '</div>';
  html += '<div class="value" style="margin-top:8px;">'+s.total_turns+'</div>';
  html += '<div class="label">turns completed</div>';
  html += '<div class="action-btns">';
  if (isRunning) {{
    html += '<button class="stop-btn" onclick="stopSession(\\''+esc(s.session_id)+'\\')">Stop</button>';
  }} else if (s.status !== 'verified') {{
    html += '<button class="resume-btn" onclick="resumeSession(\\''+esc(s.session_id)+'\\')">Resume</button>';
  }}
  if (!isRunning) {{
    html += '<button class="delete-btn" onclick="deleteSession(\\''+esc(s.session_id)+'\\')">Delete</button>';
  }}
  html += '<button class="export-btn" onclick="exportSession(\\''+esc(s.session_id)+'\\')">Export PDF</button>';
  html += '</div></div>';

  // Strategies
  html += '<div class="right-section"><h3>Strategies</h3>';
  html += '<div class="stat-grid">';
  html += '<div class="stat-box"><div class="num blue">'+s.strategies.total+'</div><div class="slabel">Total</div></div>';
  html += '<div class="stat-box"><div class="num green">'+s.strategies.verified+'</div><div class="slabel">Verified</div></div>';
  html += '<div class="stat-box"><div class="num yellow">'+s.strategies.promising+'</div><div class="slabel">Promising</div></div>';
  html += '<div class="stat-box"><div class="num red">'+s.strategies.dead_ends+'</div><div class="slabel">Dead Ends</div></div>';
  html += '</div></div>';

  // Problem
  html += '<div class="right-section"><h3>Problem</h3>';
  html += '<div class="problem-text">'+esc(s.problem)+'</div>';
  html += '<div class="lean-stmt">'+esc(s.lean_statement)+'</div></div>';

  // Verified proof
  if (s.verified_proof) {{
    html += '<div class="right-section"><h3 style="color:var(--green)">Verified Proof</h3>';
    html += '<div class="lean-stmt" style="max-height:200px;overflow-y:auto;">'+esc(s.verified_proof)+'</div></div>';
  }}

  // Lessons
  if (s.lessons && s.lessons.length > 0) {{
    html += '<div class="right-section">';
    html += '<div class="collapsible-header" onclick="toggleCollapsible(this)">';
    html += '<span class="arrow">&#9654;</span>';
    html += '<h3 style="margin:0;">Lessons ('+s.lessons.length+')</h3></div>';
    html += '<div class="collapsible-body"><div class="lesson-list">';
    for (const l of s.lessons) {{
      html += '<div class="lesson-item"><span class="cat">'+esc(l.category)+'</span> '+esc(l.lesson)+'</div>';
    }}
    html += '</div></div></div>';
  }}

  sb.innerHTML = html;

  // Update main header badge
  document.getElementById('main-badge').className = badgeClass(s.status);
  document.getElementById('main-badge').textContent = s.status;
  document.getElementById('live-dot').style.display = isRunning ? 'inline-block' : 'none';
}}

function toggleCollapsible(header) {{
  const arrow = header.querySelector('.arrow');
  const body = header.nextElementSibling;
  arrow.classList.toggle('open');
  body.classList.toggle('visible');
}}

// --- Session actions ---
async function stopSession(sid) {{
  await fetch(PREFIX + '/api/sessions/' + encodeURIComponent(sid) + '/stop', {{method:'POST'}});
  await loadSessionDetail(sid);
  await loadSessions();
}}

async function resumeSession(sid) {{
  await fetch(PREFIX + '/api/sessions/' + encodeURIComponent(sid) + '/resume', {{method:'POST'}});
  await loadSessionDetail(sid);
  await loadSessions();
}}

async function deleteSession(sid) {{
  if (!confirm('Delete session ' + sid + '? This removes its turns and events. Global lessons are preserved.')) return;
  await quickDelete(sid);
}}

async function quickDelete(sid) {{
  await fetch(PREFIX + '/api/sessions/' + encodeURIComponent(sid), {{method:'DELETE'}});
  if (currentSession === sid) {{
    document.getElementById('session-detail').innerHTML = '<div class="placeholder">Select a session</div>';
    document.getElementById('event-stream').innerHTML = '';
    currentSession = null;
  }}
  await loadSessions();
}}

function exportSession(sid) {{
  window.open(PREFIX + '/api/sessions/' + encodeURIComponent(sid) + '/export', '_blank');
}}

// --- Events ---
async function loadExistingEvents(sid) {{
  try {{
    const resp = await fetch(PREFIX + '/api/sessions/' + encodeURIComponent(sid) + '/events?limit=200');
    const events = await resp.json();
    const stream = document.getElementById('event-stream');
    for (const evt of events) {{
      if (evt.id && seenEventIds.has(evt.id)) continue;
      if (evt.id) seenEventIds.add(evt.id);
      stream.appendChild(renderEvent(evt));
    }}
    if (autoScroll) stream.scrollTop = stream.scrollHeight;
  }} catch(e) {{ /* ignore */ }}
}}

const seenEventIds = new Set();

function startSSE(sid) {{
  eventSource = new EventSource(PREFIX + '/api/stream/' + encodeURIComponent(sid));
  const stream = document.getElementById('event-stream');

  stream.addEventListener('scroll', function() {{
    autoScroll = (stream.scrollHeight - stream.scrollTop - stream.clientHeight < 50);
  }});

  eventSource.onmessage = function(event) {{
    if (currentSession !== sid) return;
    const evt = JSON.parse(event.data);
    // Deduplicate — SSE replays all events on reconnect
    if (evt.id && seenEventIds.has(evt.id)) return;
    if (evt.id) seenEventIds.add(evt.id);
    stream.appendChild(renderEvent(evt));
    if (autoScroll) stream.scrollTop = stream.scrollHeight;
  }};

  eventSource.onerror = function() {{
    // Will auto-reconnect and replay, but seenEventIds prevents duplicates
  }};
}}

function renderEvent(evt) {{
  const div = document.createElement('div');
  div.className = 'evt';
  const time = '<span class="evt-time">' + esc(formatTime(evt.timestamp)) + '</span>';
  const d = evt.data || {{}};

  switch (evt.type) {{
    case 'turn_start':
      div.className = 'evt evt-turn-start';
      div.innerHTML = time + 'Turn ' + (d.turn || '?');
      break;

    case 'formalize_start':
      div.className = 'evt evt-planner';
      div.innerHTML = time + '<span style="color:var(--purple);">Auto-formalizing problem into Lean...</span>';
      break;

    case 'formalize_thinking':
      div.className = 'evt';
      div.innerHTML = time + '<details><summary style="color:var(--purple);cursor:pointer">Formalization reasoning (click to expand)</summary><div class="evt-reasoning">' + esc(d.reasoning || '') + '</div></details>';
      break;

    case 'formalize_result':
      div.className = 'evt evt-planner';
      div.innerHTML = time + 'Formalized: <div class="evt-code">' + esc(d.lean_statement || '') + '</div>';
      break;

    case 'planner_thinking':
      div.className = 'evt';
      div.innerHTML = time + '<details><summary style="color:var(--blue);cursor:pointer">Planner reasoning (click to expand)</summary><div class="evt-reasoning">' + esc(d.reasoning || '') + '</div></details>';
      break;

    case 'planner_start':
      div.innerHTML = time + '<span style="color:var(--text-dim);">Planning next step...</span>';
      break;

    case 'planner_result':
      div.className = 'evt evt-planner';
      let ph = time + 'Strategy: <span class="strategy">' + esc(d.strategy || '') + '</span>';
      if (d.reasoning) ph += '<br><span class="reasoning">' + esc(d.reasoning) + '</span>';
      if (d.suggested_tactics) {{
        ph += '<div class="evt-tactics">' + esc(d.suggested_tactics) + '</div>';
      }}
      div.innerHTML = ph;
      break;

    case 'search_start':
      div.innerHTML = time + '<span class="evt-search">Searching: ' + esc(d.query || '') + '...</span>';
      break;

    case 'search_result':
      div.className = 'evt evt-search';
      let sh = time + 'Found ' + ((d.results||[]).length) + ' lemmas for "' + esc(d.query || '') + '"';
      for (const r of (d.results || [])) {{
        sh += '<br>&nbsp;&nbsp;<span class="lemma-name">' + esc(r.name) + '</span> ' + esc(r.statement || '');
      }}
      div.innerHTML = sh;
      break;

    case 'web_search_result':
      div.className = 'evt evt-web';
      let wh = time + 'Web: "' + esc(d.query || '') + '"';
      for (const r of (d.results || [])) {{
        wh += '<br>&nbsp;&nbsp;' + esc(r.title || '');
      }}
      div.innerHTML = wh;
      break;

    case 'fix_hallucination':
      div.className = 'evt';
      let fh = time + '<span style="color:var(--purple);">Fixed hallucinated names:</span>';
      for (const [bad, good] of Object.entries(d.replacements || {{}})) {{
        fh += '<br>&nbsp;&nbsp;<s style="color:var(--red)">' + esc(bad) + '</s> → <b style="color:var(--green)">' + esc(good) + '</b>';
      }}
      div.innerHTML = fh;
      break;

    case 'repair_start':
      div.className = 'evt';
      div.innerHTML = time + '<span style="color:var(--yellow);">Sending errors to Lean Agent for repair...</span>';
      break;

    case 'repair_result':
      div.className = 'evt';
      div.innerHTML = time + '<span style="color:var(--yellow);">Repaired tactics:</span><div class="evt-tactics">' + esc(d.tactics || '') + '</div>';
      break;

    case 'repair_thinking':
      div.className = 'evt';
      div.innerHTML = time + '<details><summary style="color:var(--yellow);cursor:pointer">Lean Agent repair reasoning (click to expand)</summary><div class="evt-reasoning">' + esc(d.reasoning || '') + '</div></details>';
      break;

    case 'synthesize_thinking':
      div.className = 'evt';
      div.innerHTML = time + '<details><summary style="color:var(--teal);cursor:pointer">Lean Agent reasoning (click to expand)</summary><div class="evt-reasoning">' + esc(d.reasoning || '') + '</div></details>';
      break;

    case 'synthesize_start':
      div.className = 'evt evt-synthesize';
      div.innerHTML = time + 'Synthesizing tactics (hints: ' + (d.hints_len || 0) + ' chars)...';
      break;

    case 'synthesize_result':
      div.className = 'evt';
      div.innerHTML = time + 'Lean Agent:<div class="evt-tactics">' + esc(d.tactics || '') + '</div>';
      break;

    case 'verify_start':
      div.className = 'evt evt-verify-start';
      div.innerHTML = time + '<details><summary>Compiling Lean source...</summary><pre>' + esc(d.source || '') + '</pre></details>';
      break;

    case 'verify_result':
      if (d.success) {{
        div.className = 'evt evt-verify-ok';
        div.innerHTML = time + '&#10003; Compilation succeeded (' + (d.elapsed || '?') + 's)';
      }} else {{
        div.className = 'evt evt-verify-fail';
        let vh = time + '&#10007; Compilation failed (' + (d.elapsed || '?') + 's)';
        div.innerHTML = vh;
        for (const diag of (d.diagnostics || [])) {{
          const dd = document.createElement('div');
          dd.className = 'evt-verify-diag';
          dd.textContent = diag;
          div.appendChild(dd);
        }}
      }}
      break;

    case 'turn_complete':
      const isOk = d.result === 'verified';
      const isPartial = d.promising && !isOk;
      div.className = 'evt evt-turn-complete ' + (isOk ? 'ok' : isPartial ? 'partial' : 'fail');
      const icon = isOk ? '&#10003;' : isPartial ? '~' : '&#10007;';
      div.innerHTML = icon + ' Turn ' + (d.turn||'?') + ': ' + esc(d.result||'?') + ' (' + (d.error_count||0) + ' errors)';
      break;

    case 'lesson_learned':
      div.className = 'evt evt-lesson';
      div.innerHTML = time + 'Lessons extracted: ' + esc(d.lesson || '') + ' (+' + (d.count||0) + ')';
      break;

    case 'creativity_start':
      div.className = 'evt';
      div.innerHTML = time + '<span style="color:var(--purple);">&#10024; Creative brainstorm...</span>';
      break;

    case 'creativity_thinking':
      div.className = 'evt';
      div.innerHTML = time + '<details><summary style="color:var(--purple);cursor:pointer">&#10024; Creative reasoning (click to expand)</summary><div class="evt-reasoning">' + esc(d.reasoning || '') + '</div></details>';
      break;

    case 'creativity_ideas':
      div.className = 'evt';
      let ch = time + '<span style="color:var(--purple);font-weight:600;">&#10024; Creative Ideas:</span>';
      for (const idea of (d.ideas || [])) {{
        ch += '<br>&nbsp;&nbsp;&#9679; <b>' + esc(idea.title || '') + '</b>: ' + esc(idea.insight || '');
      }}
      div.innerHTML = ch;
      break;

    case 'diagnosis':
      div.className = 'evt';
      let dh = time + '<span style="color:var(--yellow);font-weight:600;">&#128269; Diagnosis:</span>';
      if (d.root_cause) dh += '<br>&nbsp;&nbsp;Root cause: ' + esc(d.root_cause);
      if (d.fix) dh += '<br>&nbsp;&nbsp;Fix: ' + esc(d.fix);
      if (d.lesson) dh += '<br>&nbsp;&nbsp;Lesson: ' + esc(d.lesson);
      div.innerHTML = dh;
      break;

    case 'diagnosis_thinking':
      div.className = 'evt';
      div.innerHTML = time + '<details><summary style="color:var(--yellow);cursor:pointer">&#128269; Diagnosis reasoning (click to expand)</summary><div class="evt-reasoning">' + esc(d.reasoning || '') + '</div></details>';
      break;

    case 'exact_suggestion':
      div.className = 'evt';
      div.innerHTML = time + '<span style="color:var(--green);">&#9889; Applied exact? suggestion</span>';
      break;

    case 'error':
      div.className = 'evt evt-error';
      div.innerHTML = time + 'Error: ' + esc(d.message || '');
      break;

    default:
      div.innerHTML = time + '<span style="color:var(--text-dim);">[' + esc(evt.type) + ']</span> ' + esc(JSON.stringify(d).substring(0, 200));
  }}

  return div;
}}

// --- Init ---
loadSessions();
sessionPollTimer = setInterval(loadSessions, 10000);
</script>
</body>
</html>''')


@app.get("/api/sessions/{session_id}/export")
async def api_export_session(session_id: str):
    """Generate a comprehensive print-ready HTML page with EVERYTHING:
    all thinking traces, creative ideas, search results, diagnoses, etc.
    """
    from services.agent import db as agent_db
    import html as html_mod

    s = agent_db.get_session(session_id)
    if not s:
        return HTMLResponse("<h1>Session not found</h1>", status_code=404)

    # Get ALL events in chronological order — this is the full record
    all_events = list(agent_db.events().find(
        {"session_id": session_id}
    ).sort("timestamp", 1))

    lessons_list = list(agent_db.lessons().find(
        {"session_id": session_id}
    ).sort("hit_count", -1).limit(30))

    global_lessons = list(agent_db.lessons().find(
        {"session_id": "_global"}
    ).sort("hit_count", -1).limit(15))

    esc = html_mod.escape

    status_color = {
        "verified": "#4caf50", "in_progress": "#2196f3",
        "abandoned": "#888", "stuck": "#ff9800",
    }.get(s.get("status", ""), "#888")

    parts = [f'''<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>LeanForge — {esc(session_id)}</title>
<style>
  @media print {{
    body {{ margin: 0.4in; font-size: 11px; }}
    .no-print {{ display: none; }}
    pre {{ white-space: pre-wrap; word-wrap: break-word; }}
    .event {{ page-break-inside: avoid; }}
  }}
  body {{
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    max-width: 900px; margin: 40px auto; padding: 0 20px;
    color: #222; line-height: 1.45; font-size: 13px;
  }}
  h1 {{ font-size: 22px; margin-bottom: 4px; }}
  h2 {{ font-size: 17px; border-bottom: 2px solid #ddd; padding-bottom: 4px; margin-top: 32px; }}
  .meta {{ color: #666; font-size: 13px; margin-bottom: 16px; }}
  .status {{ display: inline-block; padding: 2px 10px; border-radius: 12px;
    color: white; font-size: 12px; font-weight: 600; }}
  pre, code {{ font-family: 'JetBrains Mono', 'Fira Code', monospace; font-size: 11.5px; }}
  pre {{ background: #f5f5f5; padding: 10px 14px; border-radius: 6px;
    overflow-x: auto; border: 1px solid #e0e0e0; margin: 6px 0; }}
  .event {{ margin: 8px 0; padding: 6px 0; border-bottom: 1px solid #f0f0f0; }}
  .evt-time {{ color: #999; font-size: 11px; margin-right: 8px; }}
  .evt-label {{ font-weight: 600; font-size: 12px; }}
  .thinking {{ background: #f8f6ff; border-left: 3px solid #9c7cf4; padding: 8px 12px;
    margin: 6px 0; font-size: 12px; white-space: pre-wrap; color: #444; }}
  .search-result {{ margin-left: 16px; font-size: 12px; }}
  .diag {{ color: #c62828; font-size: 12px; margin-left: 16px; }}
  .creative {{ background: #faf0ff; border-left: 3px solid #ab47bc; padding: 8px 12px;
    margin: 6px 0; }}
  .diagnosis {{ background: #fff8e1; border-left: 3px solid #ffc107; padding: 8px 12px;
    margin: 6px 0; }}
  .lesson {{ margin: 4px 0; padding: 4px 8px; background: #fff8e1; border-left: 3px solid #ffc107;
    font-size: 12px; }}
  .verified-proof {{ border: 2px solid #4caf50; background: #e8f5e9; padding: 16px;
    border-radius: 8px; }}
  .turn-marker {{ background: #e3f2fd; padding: 6px 12px; border-radius: 4px;
    font-weight: 700; font-size: 14px; margin-top: 20px; }}
  .turn-ok {{ color: #4caf50; }}
  .turn-fail {{ color: #c62828; }}
  .turn-partial {{ color: #ff9800; }}
  .print-btn {{ background: #2196f3; color: white; border: none; padding: 8px 20px;
    border-radius: 6px; cursor: pointer; font-size: 14px; margin: 16px 0; }}
  .print-btn:hover {{ background: #1976d2; }}
</style>
</head><body>
<button class="print-btn no-print" onclick="window.print()">Print / Save as PDF</button>
<h1>LeanForge Proof Session: {esc(session_id)}</h1>
<div class="meta">
  <span class="status" style="background:{status_color}">{esc(s.get("status", "?"))}</span>
  &nbsp; {s.get("total_turns", 0)} turns
  &nbsp;&middot;&nbsp; {len(all_events)} events
  &nbsp;&middot;&nbsp; Created: {str(s.get("created_at", ""))[:19]}
  &nbsp;&middot;&nbsp; Updated: {str(s.get("updated_at", ""))[:19]}
</div>

<h2>Problem</h2>
<p>{esc(s.get("problem", ""))}</p>

<h2>Lean 4 Statement</h2>
<pre>{esc(s.get("lean_statement", ""))}</pre>
''']

    if s.get("verified_proof"):
        parts.append(f'''
<h2>Verified Proof</h2>
<div class="verified-proof"><pre>{esc(s["verified_proof"])}</pre></div>
''')

    # Full event stream — EVERYTHING
    parts.append(f'<h2>Complete Event Log ({len(all_events)} events)</h2>')

    for evt in all_events:
        etype = evt.get("type", "")
        d = evt.get("data", {})
        ts = str(evt.get("timestamp", ""))[:19]
        time_html = f'<span class="evt-time">{esc(ts)}</span>'

        if etype == "turn_start":
            parts.append(f'<div class="turn-marker">{time_html} Turn {d.get("turn", "?")}</div>')

        elif etype == "formalize_start":
            parts.append(f'<div class="event">{time_html} <span class="evt-label" style="color:#9c27b0;">Auto-formalizing...</span></div>')

        elif etype == "formalize_thinking":
            parts.append(f'<div class="event">{time_html} <span class="evt-label" style="color:#9c27b0;">Formalization Reasoning:</span>')
            parts.append(f'<div class="thinking">{esc(d.get("reasoning", ""))}</div></div>')

        elif etype == "formalize_result":
            parts.append(f'<div class="event">{time_html} <span class="evt-label" style="color:#9c27b0;">Formalized:</span>')
            parts.append(f'<pre>{esc(d.get("lean_statement", ""))}</pre></div>')

        elif etype == "creativity_start":
            parts.append(f'<div class="event">{time_html} <span class="evt-label" style="color:#ab47bc;">&#10024; Creative Brainstorm</span></div>')

        elif etype == "creativity_thinking":
            parts.append(f'<div class="event">{time_html} <span class="evt-label" style="color:#ab47bc;">Creative Reasoning:</span>')
            parts.append(f'<div class="creative">{esc(d.get("reasoning", ""))}</div></div>')

        elif etype == "creativity_ideas":
            parts.append(f'<div class="event">{time_html} <span class="evt-label" style="color:#ab47bc;">&#10024; Creative Ideas:</span>')
            parts.append('<div class="creative">')
            for idea in d.get("ideas", []):
                parts.append(f'<p><b>{esc(idea.get("title", ""))}</b>: {esc(idea.get("insight", ""))}</p>')
            parts.append('</div></div>')

        elif etype == "planner_start":
            parts.append(f'<div class="event">{time_html} <span class="evt-label" style="color:#1976d2;">Planning next step...</span></div>')

        elif etype == "planner_thinking":
            parts.append(f'<div class="event">{time_html} <span class="evt-label" style="color:#1976d2;">Planner Reasoning:</span>')
            parts.append(f'<div class="thinking">{esc(d.get("reasoning", ""))}</div></div>')

        elif etype == "planner_result":
            parts.append(f'<div class="event">{time_html} <span class="evt-label" style="color:#1976d2;">Strategy:</span> ')
            action = d.get("action", "")
            if action:
                parts.append(f'[{esc(action)}] ')
            parts.append(f'<b>{esc(d.get("strategy", ""))}</b>')
            if d.get("reasoning"):
                parts.append(f'<br><em>{esc(d["reasoning"])}</em>')
            parts.append('</div>')

        elif etype == "search_start":
            parts.append(f'<div class="event">{time_html} <span class="evt-label" style="color:#0277bd;">Searching:</span> {esc(d.get("query", ""))}</div>')

        elif etype == "search_result":
            parts.append(f'<div class="event">{time_html} <span class="evt-label" style="color:#0277bd;">Found {len(d.get("results", []))} lemmas</span> for "{esc(d.get("query", ""))}"')
            for r in d.get("results", []):
                parts.append(f'<div class="search-result"><code>{esc(r.get("name", ""))}</code> {esc(r.get("statement", ""))}</div>')
            parts.append('</div>')

        elif etype == "web_search_result":
            parts.append(f'<div class="event">{time_html} <span class="evt-label" style="color:#00695c;">Web Search:</span> "{esc(d.get("query", ""))}"')
            for r in d.get("results", []):
                parts.append(f'<div class="search-result">{esc(r.get("title", ""))} &mdash; <code>{esc(r.get("url", ""))}</code></div>')
            parts.append('</div>')

        elif etype == "synthesize_start":
            parts.append(f'<div class="event">{time_html} <span class="evt-label" style="color:#00838f;">Synthesizing tactics</span> (strategy: {esc(d.get("strategy", "")[:150])})</div>')

        elif etype == "synthesize_thinking":
            parts.append(f'<div class="event">{time_html} <span class="evt-label" style="color:#00838f;">Lean Agent Reasoning:</span>')
            parts.append(f'<div class="thinking">{esc(d.get("reasoning", ""))}</div></div>')

        elif etype == "synthesize_result":
            parts.append(f'<div class="event">{time_html} <span class="evt-label" style="color:#00838f;">Lean Agent Output:</span>')
            parts.append(f'<pre>{esc(d.get("tactics", ""))}</pre></div>')

        elif etype == "repair_start":
            diags = d.get("diagnostics", [])
            parts.append(f'<div class="event">{time_html} <span class="evt-label" style="color:#e65100;">Repair Attempt</span> ({len(diags)} errors)')
            for diag in diags:
                parts.append(f'<div class="diag">{esc(str(diag)[:200])}</div>')
            parts.append('</div>')

        elif etype == "repair_thinking":
            parts.append(f'<div class="event">{time_html} <span class="evt-label" style="color:#e65100;">Repair Reasoning:</span>')
            parts.append(f'<div class="thinking">{esc(d.get("reasoning", ""))}</div></div>')

        elif etype == "repair_result":
            parts.append(f'<div class="event">{time_html} <span class="evt-label" style="color:#e65100;">Repaired Code:</span>')
            parts.append(f'<pre>{esc(d.get("tactics", ""))}</pre></div>')

        elif etype == "verify_start":
            parts.append(f'<div class="event">{time_html} <span class="evt-label">Compiling Lean source...</span>')
            parts.append(f'<pre>{esc(d.get("source", ""))}</pre></div>')

        elif etype == "verify_result":
            ok = d.get("success", False)
            elapsed = d.get("elapsed", "?")
            if ok:
                parts.append(f'<div class="event">{time_html} <span class="evt-label turn-ok">&#10003; Compilation succeeded ({elapsed}s)</span></div>')
            else:
                parts.append(f'<div class="event">{time_html} <span class="evt-label turn-fail">&#10007; Compilation failed ({elapsed}s)</span>')
                for diag in d.get("diagnostics", []):
                    parts.append(f'<div class="diag">{esc(str(diag)[:200])}</div>')
                parts.append('</div>')

        elif etype == "diagnosis":
            parts.append(f'<div class="event">{time_html} <span class="evt-label" style="color:#f57f17;">&#128269; Diagnosis:</span>')
            parts.append('<div class="diagnosis">')
            if d.get("root_cause"):
                parts.append(f'<p><b>Root cause:</b> {esc(d["root_cause"])}</p>')
            if d.get("fix"):
                parts.append(f'<p><b>Fix:</b> {esc(d["fix"])}</p>')
            if d.get("lesson"):
                parts.append(f'<p><b>Lesson:</b> {esc(d["lesson"])}</p>')
            parts.append('</div></div>')

        elif etype == "diagnosis_thinking":
            parts.append(f'<div class="event">{time_html} <span class="evt-label" style="color:#f57f17;">Diagnosis Reasoning:</span>')
            parts.append(f'<div class="thinking">{esc(d.get("reasoning", ""))}</div></div>')

        elif etype == "exact_suggestion":
            parts.append(f'<div class="event">{time_html} <span class="evt-label turn-ok">&#9889; Applied exact? suggestion</span></div>')

        elif etype == "fix_hallucination":
            parts.append(f'<div class="event">{time_html} <span class="evt-label" style="color:#9c27b0;">Fixed hallucinated names:</span>')
            for bad, good in d.get("replacements", {}).items():
                parts.append(f'<div class="search-result"><s style="color:#c62828">{esc(bad)}</s> &rarr; <b style="color:#4caf50">{esc(good)}</b></div>')
            parts.append('</div>')

        elif etype == "decomposition":
            parts.append(f'<div class="event">{time_html} <span class="evt-label" style="color:#ab47bc;">Decomposition Plan:</span>')
            parts.append(f'<div class="creative">{esc(d.get("description", ""))}</div></div>')

        elif etype == "turn_complete":
            result = d.get("result", "?")
            css = "turn-ok" if result == "verified" else "turn-partial" if d.get("promising") else "turn-fail"
            icon = "&#10003;" if result == "verified" else "~" if d.get("promising") else "&#10007;"
            parts.append(f'<div class="event"><span class="evt-label {css}">{icon} Turn {d.get("turn", "?")}: {esc(result)} ({d.get("error_count", 0)} errors)</span></div>')

        elif etype == "lesson_learned":
            parts.append(f'<div class="event">{time_html} <span class="evt-label">Lessons extracted:</span> {esc(d.get("lesson", ""))}</div>')

        elif etype == "error":
            parts.append(f'<div class="event">{time_html} <span class="evt-label turn-fail">Error:</span> {esc(d.get("message", ""))}</div>')

        else:
            # Catch-all for any event type not explicitly handled
            import json as json_mod
            parts.append(f'<div class="event">{time_html} <span class="evt-label">[{esc(etype)}]</span> <code>{esc(json_mod.dumps(d)[:300])}</code></div>')

    # Lessons
    if lessons_list:
        parts.append('<h2>Session Lessons</h2>')
        for l in lessons_list:
            cat = l.get("category", "")
            hits = l.get("hit_count", 0)
            parts.append(f'<div class="lesson">[{esc(cat)}, {hits}x] {esc(l["lesson"][:500])}</div>')

    if global_lessons:
        parts.append('<h2>Global Lessons (applied to all sessions)</h2>')
        for l in global_lessons:
            cat = l.get("category", "")
            hits = l.get("hit_count", 0)
            parts.append(f'<div class="lesson">[{esc(cat)}, {hits}x] {esc(l["lesson"][:500])}</div>')

    parts.append(f'''
<div style="margin-top:40px;color:#999;font-size:11px;border-top:1px solid #ddd;padding-top:8px;">
  Generated by LeanForge &middot; Session {esc(session_id)} &middot; {len(all_events)} events &middot; {s.get("total_turns", 0)} turns
</div>
<div class="no-print" style="margin-top:8px;">
  <button class="print-btn" onclick="window.print()">Print / Save as PDF</button>
</div>
</body></html>''')

    return HTMLResponse("\n".join(parts))


@app.get("/health")
async def health():
    return {"status": "ok", "service": "workbench"}
