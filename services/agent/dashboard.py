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
    from services.agent.runner import run_loop
    run_loop(session_id, max_turns=max_turns, delay=10)


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

        # STREAM: poll for new events
        while True:
            await asyncio.sleep(2)
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
}}
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
      html += '<div class="session-item' + active + '" onclick="selectSession(\\''+esc(s.session_id)+'\\')"><div class="sid">'+esc(s.session_id)+'</div>';
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

// --- Events ---
async function loadExistingEvents(sid) {{
  try {{
    const resp = await fetch(PREFIX + '/api/sessions/' + encodeURIComponent(sid) + '/events?limit=200');
    const events = await resp.json();
    const stream = document.getElementById('event-stream');
    for (const evt of events) {{
      stream.appendChild(renderEvent(evt));
    }}
    if (autoScroll) stream.scrollTop = stream.scrollHeight;
  }} catch(e) {{ /* ignore */ }}
}}

function startSSE(sid) {{
  eventSource = new EventSource(PREFIX + '/api/stream/' + encodeURIComponent(sid));
  const stream = document.getElementById('event-stream');

  stream.addEventListener('scroll', function() {{
    autoScroll = (stream.scrollHeight - stream.scrollTop - stream.clientHeight < 50);
  }});

  eventSource.onmessage = function(event) {{
    if (currentSession !== sid) return;
    const evt = JSON.parse(event.data);
    stream.appendChild(renderEvent(evt));
    if (autoScroll) stream.scrollTop = stream.scrollHeight;
  }};

  eventSource.onerror = function() {{
    // Will auto-reconnect
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

    case 'synthesize_thinking':
      div.className = 'evt';
      div.innerHTML = time + '<details><summary style="color:var(--teal);cursor:pointer">Leanstral reasoning (click to expand)</summary><div class="evt-reasoning">' + esc(d.reasoning || '') + '</div></details>';
      break;

    case 'synthesize_start':
      div.className = 'evt evt-synthesize';
      div.innerHTML = time + 'Synthesizing tactics (hints: ' + (d.hints_len || 0) + ' chars)...';
      break;

    case 'synthesize_result':
      div.className = 'evt';
      div.innerHTML = time + 'Leanstral:<div class="evt-tactics">' + esc(d.tactics || '') + '</div>';
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


@app.get("/health")
async def health():
    return {"status": "ok", "service": "workbench"}
