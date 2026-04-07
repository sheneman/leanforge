"""HTML dashboard for monitoring proof agent sessions.

Serves a single-page dashboard that auto-refreshes showing sessions,
strategies, lessons, and recent turns.

Run: uvicorn services.agent.dashboard:app --port 8105
"""
from __future__ import annotations

import html
import os

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pymongo import MongoClient, DESCENDING

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "leanforge")

app = FastAPI(title="LeanForge Dashboard", version="0.1.0")

CSS = """
body { font-family: 'SF Mono', 'Consolas', monospace; background: #0d1117; color: #c9d1d9; margin: 2em; }
h1, h2, h3 { color: #58a6ff; }
a { color: #58a6ff; text-decoration: none; }
a:hover { text-decoration: underline; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; }
th, td { border: 1px solid #30363d; padding: 8px 12px; text-align: left; }
th { background: #161b22; color: #58a6ff; }
tr:hover { background: #161b22; }
.verified { color: #3fb950; font-weight: bold; }
.failed { color: #f85149; }
.partial { color: #d29922; }
.in_progress { color: #58a6ff; }
pre { background: #161b22; padding: 1em; border-radius: 6px; overflow-x: auto; font-size: 13px; }
.lesson { background: #1c2128; padding: 6px 10px; margin: 4px 0; border-left: 3px solid #d29922; font-size: 13px; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 12px; margin: 2px; }
.badge-dead { background: #f8514922; color: #f85149; }
.badge-promising { background: #d2992222; color: #d29922; }
.badge-verified { background: #3fb95022; color: #3fb950; }
.header { display: flex; justify-content: space-between; align-items: center; }
.refresh { color: #484f58; font-size: 12px; }
details { margin: 0.5em 0; }
summary { cursor: pointer; color: #58a6ff; }
"""


def _db():
    return MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)[MONGO_DB]


def _page(title: str, body: str, refresh: int = 15) -> HTMLResponse:
    h = "<!DOCTYPE html><html><head>"
    h += "<title>" + html.escape(title) + "</title>"
    h += '<meta http-equiv="refresh" content="' + str(refresh) + '">'
    h += "<style>" + CSS + "</style>"
    h += "</head><body>"
    h += '<div class="header">'
    h += "<h1>LeanForge Proof Agent</h1>"
    h += '<span class="refresh">Auto-refreshes every ' + str(refresh) + 's</span>'
    h += "</div>"
    h += body
    h += "</body></html>"
    return HTMLResponse(h)


def _esc(s) -> str:
    return html.escape(str(s))


@app.get("/", response_class=HTMLResponse)
async def index():
    db = _db()
    sessions = list(db.sessions.find().sort("updated_at", DESCENDING))

    rows = ""
    for s in sessions:
        sid = s["_id"]
        status = s["status"]
        turns = s["total_turns"]
        problem = _esc(s["problem"][:100])
        updated = str(s.get("updated_at", ""))[:19]
        n_strats = db.strategies.count_documents({"session_id": sid})
        n_lessons = db.lessons.count_documents({"session_id": sid})
        rows += "<tr>"
        rows += '<td><a href="/session/' + _esc(sid) + '">' + _esc(sid) + "</a></td>"
        rows += '<td class="' + status + '">' + status + "</td>"
        rows += "<td>" + str(turns) + "</td>"
        rows += "<td>" + str(n_strats) + "</td>"
        rows += "<td>" + str(n_lessons) + "</td>"
        rows += "<td>" + problem + "</td>"
        rows += "<td>" + updated + "</td>"
        rows += "</tr>"

    body = "<h2>Sessions</h2>"
    body += "<table><tr><th>Session</th><th>Status</th><th>Turns</th><th>Strategies</th><th>Lessons</th><th>Problem</th><th>Updated</th></tr>"
    body += rows if rows else "<tr><td colspan=7>No sessions yet</td></tr>"
    body += "</table>"

    return _page("LeanForge Dashboard", body)


@app.get("/session/{session_id}", response_class=HTMLResponse)
async def session_detail(session_id: str):
    db = _db()
    s = db.sessions.find_one({"_id": session_id})
    if not s:
        return _page("Not Found", "<p>Session not found</p>")

    body = '<p><a href="/">&larr; All Sessions</a></p>'
    body += "<h2>" + _esc(session_id) + "</h2>"

    # Info table
    body += "<table>"
    body += '<tr><th>Status</th><td class="' + s["status"] + '">' + s["status"] + "</td></tr>"
    body += "<tr><th>Problem</th><td>" + _esc(s["problem"]) + "</td></tr>"
    body += "<tr><th>Lean</th><td><code>" + _esc(s["lean_statement"]) + "</code></td></tr>"
    body += "<tr><th>Turns</th><td>" + str(s["total_turns"]) + "</td></tr>"
    body += "<tr><th>Updated</th><td>" + str(s.get("updated_at", ""))[:19] + "</td></tr>"
    body += "</table>"

    # Verified proof
    if s.get("verified_proof"):
        body += '<h3 class="verified">Verified Proof</h3>'
        body += "<pre>" + _esc(s["verified_proof"]) + "</pre>"

    # Best partial
    elif s.get("best_partial_proof"):
        body += "<h3>Best Partial Proof</h3>"
        body += "<pre>" + _esc(s["best_partial_proof"][:2000]) + "</pre>"

    # Lessons
    lesson_docs = list(db.lessons.find({"session_id": session_id}).sort("hit_count", DESCENDING).limit(25))
    if lesson_docs:
        body += "<h3>Technical Lessons (" + str(len(lesson_docs)) + ")</h3>"
        for l in lesson_docs:
            cat = _esc(l.get("category", ""))
            hits = l.get("hit_count", 0)
            body += '<div class="lesson"><b>[' + cat + "]</b> " + _esc(l["lesson"])
            body += ' <span style="color:#484f58">(hits: ' + str(hits) + ")</span></div>"

    # Strategies
    strats = list(db.strategies.find({"session_id": session_id}))
    dead = [x for x in strats if x.get("outcome") == "dead_end"]
    promising = [x for x in strats if x.get("outcome") in ("promising", "partial")]
    verified = [x for x in strats if x.get("outcome") == "verified"]

    body += "<h3>Strategies (" + str(len(strats)) + " total)</h3><p>"
    body += '<span class="badge badge-verified">' + str(len(verified)) + " verified</span> "
    body += '<span class="badge badge-promising">' + str(len(promising)) + " promising</span> "
    body += '<span class="badge badge-dead">' + str(len(dead)) + " dead ends</span></p>"

    if promising:
        body += "<details open><summary>Promising (" + str(len(promising)) + ")</summary><ul>"
        for p in promising[-15:]:
            desc = _esc(p.get("description", "")[:150])
            body += "<li><b>" + _esc(p["name"]) + "</b>: " + desc + "</li>"
        body += "</ul></details>"

    if dead:
        body += "<details><summary>Dead ends (" + str(len(dead)) + ")</summary><ul>"
        for d in dead[:20]:
            body += "<li>" + _esc(d["name"]) + "</li>"
        body += "</ul></details>"

    # Recent turns
    turn_docs = list(db.turns.find({"session_id": session_id}).sort("turn", DESCENDING).limit(30))
    if turn_docs:
        body += "<h3>Recent Turns</h3><table>"
        body += "<tr><th>Turn</th><th>Strategy</th><th>Result</th><th>Diagnostics</th></tr>"
        for t in turn_docs:
            result = t["result"]
            cls = "verified" if result == "verified" else "partial" if t.get("promising") else "failed"
            diags = _esc("; ".join(t.get("diagnostics", [])[:2])[:150])
            body += "<tr>"
            body += "<td>" + str(t["turn"]) + "</td>"
            body += "<td>" + _esc(t["strategy"][:50]) + "</td>"
            body += '<td class="' + cls + '">' + result + "</td>"
            body += "<td>" + diags + "</td>"
            body += "</tr>"
        body += "</table>"

    return _page("Session: " + session_id, body, refresh=10)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "dashboard"}
