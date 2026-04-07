"""HTML dashboard for monitoring proof agent sessions.

Includes a live terminal view that streams turns via SSE (Server-Sent Events).

Run: uvicorn services.agent.dashboard:app --port 8105
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import time

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from pymongo import MongoClient, DESCENDING

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "leanforge")
PREFIX = os.getenv("DASHBOARD_PREFIX", "/dashboard")

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
#terminal { background: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 1em; height: 600px; overflow-y: auto; font-size: 13px; line-height: 1.6; }
#terminal .turn { margin-bottom: 12px; padding-bottom: 12px; border-bottom: 1px solid #21262d; }
#terminal .turn-num { color: #484f58; }
#terminal .strategy-name { color: #58a6ff; font-weight: bold; }
#terminal .result-verified { color: #3fb950; }
#terminal .result-partial { color: #d29922; }
#terminal .result-failed { color: #f85149; }
#terminal .diag { color: #f85149; font-size: 12px; }
#terminal .tactic { color: #7ee787; font-size: 12px; }
#terminal .search { color: #a5d6ff; font-size: 12px; }
#terminal .reasoning { color: #8b949e; font-size: 12px; font-style: italic; }
.live-dot { display: inline-block; width: 8px; height: 8px; background: #3fb950; border-radius: 50%; margin-right: 6px; animation: pulse 1.5s infinite; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
"""


def _db():
    return MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)[MONGO_DB]


def _esc(s) -> str:
    return html.escape(str(s))


def _page(title, body, refresh=0):
    h = "<!DOCTYPE html><html><head>"
    h += "<title>" + _esc(title) + "</title>"
    if refresh:
        h += '<meta http-equiv="refresh" content="' + str(refresh) + '">'
    h += "<style>" + CSS + "</style></head><body>"
    h += '<div class="header"><h1>LeanForge Proof Agent</h1>'
    if refresh:
        h += '<span class="refresh">Auto-refreshes every ' + str(refresh) + 's</span>'
    h += "</div>" + body + "</body></html>"
    return HTMLResponse(h)


# ── Index ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    db = _db()
    sessions = list(db.sessions.find().sort("updated_at", DESCENDING))
    rows = ""
    for s in sessions:
        sid = s["_id"]
        status = s["status"]
        n_strats = db.strategies.count_documents({"session_id": sid})
        n_lessons = db.lessons.count_documents({"session_id": sid})
        rows += "<tr>"
        rows += '<td><a href="' + PREFIX + '/session/' + _esc(sid) + '">' + _esc(sid) + "</a></td>"
        rows += '<td class="' + status + '">' + status + "</td>"
        rows += "<td>" + str(s["total_turns"]) + "</td>"
        rows += "<td>" + str(n_strats) + "</td>"
        rows += "<td>" + str(n_lessons) + "</td>"
        rows += "<td>" + _esc(s["problem"][:100]) + "</td>"
        rows += "<td>" + str(s.get("updated_at", ""))[:19] + "</td>"
        rows += "</tr>"
    body = "<h2>Sessions</h2><table>"
    body += "<tr><th>Session</th><th>Status</th><th>Turns</th><th>Strategies</th><th>Lessons</th><th>Problem</th><th>Updated</th></tr>"
    body += rows if rows else "<tr><td colspan=7>No sessions yet</td></tr>"
    body += "</table>"
    return _page("LeanForge Dashboard", body, refresh=15)


# ── Session detail ────────────────────────────────────────────────────────

@app.get("/session/{session_id}", response_class=HTMLResponse)
async def session_detail(session_id: str):
    db = _db()
    s = db.sessions.find_one({"_id": session_id})
    if not s:
        return _page("Not Found", "<p>Session not found</p>")

    body = '<p><a href="' + PREFIX + '/">&larr; All Sessions</a>'
    body += ' | <a href="' + PREFIX + '/live/' + _esc(session_id) + '"><span class="live-dot"></span>Live View</a></p>'
    body += "<h2>" + _esc(session_id) + "</h2>"

    body += "<table>"
    body += '<tr><th>Status</th><td class="' + s["status"] + '">' + s["status"] + "</td></tr>"
    body += "<tr><th>Problem</th><td>" + _esc(s["problem"]) + "</td></tr>"
    body += "<tr><th>Lean</th><td><code>" + _esc(s["lean_statement"]) + "</code></td></tr>"
    body += "<tr><th>Turns</th><td>" + str(s["total_turns"]) + "</td></tr>"
    body += "<tr><th>Updated</th><td>" + str(s.get("updated_at", ""))[:19] + "</td></tr>"
    body += "</table>"

    if s.get("verified_proof"):
        body += '<h3 class="verified">Verified Proof</h3><pre>' + _esc(s["verified_proof"]) + "</pre>"
    elif s.get("best_partial_proof"):
        body += "<h3>Best Partial Proof</h3><pre>" + _esc(s["best_partial_proof"][:2000]) + "</pre>"

    lesson_docs = list(db.lessons.find({"session_id": session_id}).sort("hit_count", DESCENDING).limit(25))
    if lesson_docs:
        body += "<h3>Technical Lessons (" + str(len(lesson_docs)) + ")</h3>"
        for l in lesson_docs:
            body += '<div class="lesson"><b>[' + _esc(l.get("category", "")) + "]</b> " + _esc(l["lesson"])
            body += ' <span style="color:#484f58">(hits: ' + str(l.get("hit_count", 0)) + ")</span></div>"

    strats = list(db.strategies.find({"session_id": session_id}))
    dead = [x for x in strats if x.get("outcome") == "dead_end"]
    promising = [x for x in strats if x.get("outcome") in ("promising", "partial")]
    verified_s = [x for x in strats if x.get("outcome") == "verified"]
    body += "<h3>Strategies (" + str(len(strats)) + " total)</h3><p>"
    body += '<span class="badge badge-verified">' + str(len(verified_s)) + " verified</span> "
    body += '<span class="badge badge-promising">' + str(len(promising)) + " promising</span> "
    body += '<span class="badge badge-dead">' + str(len(dead)) + " dead ends</span></p>"

    if promising:
        body += "<details open><summary>Promising (" + str(len(promising)) + ")</summary><ul>"
        for p in promising[-15:]:
            body += "<li><b>" + _esc(p["name"]) + "</b>: " + _esc(p.get("description", "")[:150]) + "</li>"
        body += "</ul></details>"
    if dead:
        body += "<details><summary>Dead ends (" + str(len(dead)) + ")</summary><ul>"
        for d in dead[:20]:
            body += "<li>" + _esc(d["name"]) + "</li>"
        body += "</ul></details>"

    turn_docs = list(db.turns.find({"session_id": session_id}).sort("turn", DESCENDING).limit(30))
    if turn_docs:
        body += "<h3>Recent Turns</h3><table>"
        body += "<tr><th>Turn</th><th>Strategy</th><th>Result</th><th>Diagnostics</th></tr>"
        for t in turn_docs:
            cls = "verified" if t["result"] == "verified" else "partial" if t.get("promising") else "failed"
            diags = _esc("; ".join(t.get("diagnostics", [])[:2])[:150])
            body += "<tr><td>" + str(t["turn"]) + "</td>"
            body += "<td>" + _esc(t["strategy"][:50]) + "</td>"
            body += '<td class="' + cls + '">' + t["result"] + "</td>"
            body += "<td>" + diags + "</td></tr>"
        body += "</table>"

    return _page("Session: " + session_id, body, refresh=10)


# ── Live terminal view ────────────────────────────────────────────────────

LIVE_PAGE_JS = """
<script>
const evtSource = new EventSource(PREFIX + "/stream/" + SESSION_ID);
const terminal = document.getElementById("terminal");
let autoScroll = true;

terminal.addEventListener("scroll", function() {
    autoScroll = (terminal.scrollHeight - terminal.scrollTop - terminal.clientHeight < 50);
});

evtSource.onmessage = function(event) {
    const data = JSON.parse(event.data);
    const div = document.createElement("div");
    div.className = "turn";

    let resultClass = "result-" + data.result;
    let icon = data.result === "verified" ? "&#x2713;" : data.promising ? "~" : "&#x2717;";

    let h = '<span class="turn-num">Turn ' + data.turn + '</span> ';
    h += '<span class="strategy-name">' + data.strategy + '</span> ';
    h += '<span class="' + resultClass + '">' + icon + ' ' + data.result + '</span>';

    if (data.reasoning) {
        h += '<br><span class="reasoning">' + data.reasoning + '</span>';
    }
    if (data.lean_source) {
        h += '<br><details><summary style="color:#7ee787;font-size:12px;cursor:pointer">Lean source</summary>';
        h += '<pre style="font-size:11px;margin:4px 0;max-height:300px;overflow:auto">' + data.lean_source + '</pre></details>';
    }
    if (data.diagnostics && data.diagnostics.length > 0) {
        for (let d of data.diagnostics) {
            h += '<br><span class="diag">&gt; ' + d + '</span>';
        }
    }

    div.innerHTML = h;
    terminal.appendChild(div);

    // Update counters
    document.getElementById("turn-count").textContent = data.turn;
    document.getElementById("status").textContent = data.session_status || "in_progress";

    if (autoScroll) {
        terminal.scrollTop = terminal.scrollHeight;
    }
};

evtSource.onerror = function() {
    const div = document.createElement("div");
    div.style.color = "#484f58";
    div.textContent = "[connection lost — will retry...]";
    terminal.appendChild(div);
};
</script>
"""


@app.get("/live/{session_id}", response_class=HTMLResponse)
async def live_view(session_id: str):
    db = _db()
    s = db.sessions.find_one({"_id": session_id})
    if not s:
        return _page("Not Found", "<p>Session not found</p>")

    body = '<p><a href="' + PREFIX + '/session/' + _esc(session_id) + '">&larr; Session Detail</a></p>'
    body += '<h2><span class="live-dot"></span>' + _esc(session_id) + ' — Live</h2>'
    body += '<p>Status: <span id="status" class="' + s["status"] + '">' + s["status"] + '</span>'
    body += ' | Turns: <span id="turn-count">' + str(s["total_turns"]) + '</span></p>'
    body += '<div id="terminal">'

    # Seed with last 20 turns
    turn_docs = list(db.turns.find({"session_id": session_id}).sort("turn", DESCENDING).limit(20))
    for t in reversed(turn_docs):
        cls = "result-verified" if t["result"] == "verified" else "result-partial" if t.get("promising") else "result-failed"
        icon = "&#x2713;" if t["result"] == "verified" else "~" if t.get("promising") else "&#x2717;"
        diags = t.get("diagnostics", [])
        body += '<div class="turn">'
        body += '<span class="turn-num">Turn ' + str(t["turn"]) + '</span> '
        body += '<span class="strategy-name">' + _esc(t["strategy"]) + '</span> '
        body += '<span class="' + cls + '">' + icon + ' ' + t["result"] + '</span>'
        if t.get("notes"):
            body += '<br><span class="reasoning">' + _esc(t["notes"][:200]) + '</span>'
        if t.get("lean_source"):
            body += '<br><details><summary style="color:#7ee787;font-size:12px;cursor:pointer">Lean source</summary>'
            body += '<pre style="font-size:11px;margin:4px 0;max-height:300px;overflow:auto">' + _esc(t["lean_source"][:2000]) + '</pre></details>'
        for d in diags[:3]:
            body += '<br><span class="diag">&gt; ' + _esc(d[:200]) + '</span>'
        body += '</div>'

    body += '</div>'

    # Inject JS with correct prefix and session ID
    js = LIVE_PAGE_JS.replace("PREFIX", '"' + PREFIX + '"').replace("SESSION_ID", '"' + session_id + '"')
    body += js

    return _page("Live: " + session_id, body, refresh=0)


# ── SSE stream ────────────────────────────────────────────────────────────

@app.get("/stream/{session_id}")
async def stream_turns(session_id: str):
    """Server-Sent Events endpoint that polls MongoDB for new turns."""
    async def event_generator():
        db = _db()
        # Start from the latest turn
        last_turn = 0
        latest = db.turns.find_one({"session_id": session_id}, sort=[("turn", DESCENDING)])
        if latest:
            last_turn = latest["turn"]

        while True:
            await asyncio.sleep(3)
            new_turns = list(
                db.turns.find({"session_id": session_id, "turn": {"$gt": last_turn}})
                .sort("turn", 1)
                .limit(10)
            )
            for t in new_turns:
                last_turn = t["turn"]
                s = db.sessions.find_one({"_id": session_id})
                data = {
                    "turn": t["turn"],
                    "strategy": t["strategy"],
                    "result": t["result"],
                    "promising": t.get("promising", False),
                    "diagnostics": t.get("diagnostics", [])[:5],
                    "lean_source": t.get("lean_source", "")[:3000],
                    "reasoning": t.get("notes", "")[:300],
                    "session_status": s["status"] if s else "unknown",
                }
                yield "data: " + json.dumps(data) + "\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@app.get("/health")
async def health():
    return {"status": "ok", "service": "dashboard"}
