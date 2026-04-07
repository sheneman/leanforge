"""Autonomous proof search agent.

Runs an indefinite loop for a given session: plan → search → synthesize →
verify → log → repeat. Reads/writes state to MongoDB so context window
stays small even after thousands of turns.

Usage:
    # Start a new session
    python -m services.agent.runner \\
        --session collatz_descent \\
        --problem "For every n > 1, ∃ k, Collatz^k(n) < n" \\
        --lean-statement "theorem collatz_below (n : ℕ) (hn : 1 < n) : ∃ k : ℕ, (Nat.iterate collatzStep k n) < n" \\
        --max-turns 1000

    # Resume an existing session
    python -m services.agent.runner --session collatz_descent --resume

    # Check status
    python -m services.agent.runner --session collatz_descent --status
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

import httpx
import structlog
from dotenv import load_dotenv

load_dotenv()

# Must import after load_dotenv so env vars are available
from services.agent import db
from services.agent.planner import plan_next_step, synthesize_tactics, repair_tactics

log = structlog.get_logger()

LEAN_ENV_URL = os.getenv("LEAN_ENV_URL", "http://leanforge-lean-env:8101").rstrip("/")
RETRIEVAL_URL = os.getenv("RETRIEVAL_URL", "http://leanforge-retrieval:8103").rstrip("/")
BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")
TURN_DELAY_SECS = int(os.getenv("TURN_DELAY_SECS", "5"))


# ---------------------------------------------------------------------------
# Tool calls (search, verify, web search)
# ---------------------------------------------------------------------------

def search_mathlib(query: str, top_k: int = 10) -> list[dict]:
    """Search retrieval service for relevant lemmas."""
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{RETRIEVAL_URL}/search",
                json={"query": query, "top_k": top_k},
            )
            resp.raise_for_status()
            return resp.json().get("results", [])
    except Exception as e:
        log.warning("search_failed", error=str(e))
        return []


def web_search(query: str, count: int = 5) -> list[dict]:
    """Search the web via Brave Search API. Used as last resort for research."""
    if not BRAVE_API_KEY:
        log.warning("web_search_skipped", reason="no BRAVE_API_KEY")
        return []
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": count},
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": BRAVE_API_KEY,
                },
            )
            resp.raise_for_status()
            results = resp.json().get("web", {}).get("results", [])
            return [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "description": r.get("description", "")[:300],
                }
                for r in results[:count]
            ]
    except Exception as e:
        log.warning("web_search_failed", error=str(e))
        return []


def verify_lean(source: str) -> dict:
    """Compile Lean source via lean_env service."""
    try:
        with httpx.Client(timeout=300) as client:
            resp = client.post(
                f"{LEAN_ENV_URL}/compile",
                json={"source": source},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        log.error("verify_failed", error=str(e))
        return {"success": False, "diagnostics": [{"message": str(e)}]}


def build_lean_source(lean_statement: str, imports: list[str], tactics: str, preamble: str = "") -> str:
    """Assemble a complete Lean source file."""
    import_lines = "\n".join(f"import {i}" for i in imports)
    # Normalize the theorem statement
    stmt = lean_statement.strip()
    if not re.match(r"^(theorem|lemma|def|example)\s", stmt):
        import hashlib
        name = "auto_" + hashlib.md5(stmt.encode()).hexdigest()[:8]
        stmt = f"theorem {name} : {stmt}"

    # Clean tactics: strip ALL code fences (opening and closing), imports, declarations
    tactics = re.sub(r"```\w*", "", tactics)  # strip ```lean4, ```tactics, ```, etc.
    lines = []
    for line in tactics.split("\n"):
        s = line.strip()
        if s.startswith("import ") or s.startswith("open ") or re.match(r"^(theorem|lemma|def)\s", s):
            continue
        lines.append(line)
    tactics = "\n".join(lines).strip()
    if tactics.lower().startswith("by\n") or tactics.lower().startswith("by "):
        tactics = tactics[2:].strip()

    parts = [import_lines, ""]
    if preamble:
        parts.append(preamble)
        parts.append("")
    parts.append(f"{stmt} := by")
    parts.append(f"  {tactics}")
    parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# One turn of the agent
# ---------------------------------------------------------------------------

def run_turn(session_id: str) -> dict:
    """Execute one turn of the proof search agent.

    1. Query MongoDB for session context
    2. Ask planner LLM for next strategy
    3. Search mathlib based on plan
    4. Synthesize tactics (Leanstral or from plan)
    5. Verify with Lean
    6. Log results to MongoDB

    Returns the turn result dict.
    """
    session = db.get_session(session_id)
    if not session:
        raise ValueError(f"Session {session_id} not found")

    if session["status"] == "verified":
        log.info("session_already_verified", session_id=session_id)
        return {"result": "already_verified"}

    turn_number = db.get_turn_count(session_id) + 1
    log.info("turn_start", session_id=session_id, turn=turn_number)

    # Emit turn_start event
    db.emit_event(session_id, "turn_start", {"turn": turn_number})

    # 1. Plan next step
    db.emit_event(session_id, "planner_start", {"turn": turn_number})
    plan = plan_next_step(session_id)
    strategy = plan.get("strategy_name", "unknown")
    log.info("plan", session_id=session_id, strategy=strategy)

    db.emit_event(session_id, "planner_result", {
        "strategy": strategy,
        "reasoning": plan.get("reasoning", "")[:300],
        "suggested_tactics": plan.get("suggested_tactics", "")[:3000],
    })

    if strategy == "DONE":
        return {"result": "already_verified"}

    # 2. Search mathlib
    all_lemmas = []
    for query in plan.get("search_queries", [])[:3]:
        db.emit_event(session_id, "search_start", {"query": query})
        results = search_mathlib(query, top_k=5)
        for r in results:
            db.log_lemma(session_id, r["name"], r.get("statement", ""), r.get("module", ""))
            all_lemmas.append(r)
        db.emit_event(session_id, "search_result", {
            "query": query,
            "results": [{"name": r["name"], "statement": r.get("statement", "")[:150]} for r in results[:3]],
        })

    # 2b. Web search (if planner requested it — last resort for research)
    web_results = []
    for query in plan.get("web_search_queries", [])[:2]:
        results = web_search(query, count=3)
        web_results.extend(results)
        log.info("web_search", session_id=session_id, query=query, results=len(results))
        db.emit_event(session_id, "web_search_result", {
            "query": query,
            "results": [{"title": r.get("title", ""), "url": r.get("url", "")} for r in results[:3]],
        })
    # Log useful web findings as lessons
    for wr in web_results[:3]:
        db.log_lesson(
            session_id,
            f"Web research: {wr['title'][:100]} — {wr['description'][:150]}",
            category="web_research",
        )

    # 3. Synthesize tactics via Leanstral (ALL code generation goes through Leanstral)
    strategy_desc = plan.get("strategy_description", "")
    lemma_hints = ""
    if all_lemmas:
        lemma_hints = "\n".join(
            f"  {l['name']}: {l.get('statement', '')[:150]}" for l in all_lemmas[:10]
        )
    if web_results:
        lemma_hints += "\nWeb research:\n" + "\n".join(
            f"  {wr['title'][:80]}: {wr['description'][:150]}" for wr in web_results[:3]
        )

    db.emit_event(session_id, "synthesize_start", {
        "strategy": strategy_desc[:300],
        "hints_len": len(lemma_hints),
    })
    leanstral_tactics, _ = synthesize_tactics(
        session["lean_statement"],
        strategy=strategy_desc,
        hints=lemma_hints,
        session_id=session_id,
    )
    db.emit_event(session_id, "synthesize_result", {
        "tactics": leanstral_tactics[:3000],
    })

    # 4. Verify Leanstral's output
    best_result = None
    best_tactics = leanstral_tactics
    best_source = ""

    source = build_lean_source(
        session["lean_statement"],
        session["imports"],
        leanstral_tactics,
    )
    best_source = source

    db.emit_event(session_id, "verify_start", {"source": source[:3000]})
    t0 = time.time()
    result = verify_lean(source)
    elapsed = round(time.time() - t0, 2)

    verify_diags = result.get("diagnostics", [])
    db.emit_event(session_id, "verify_result", {
        "success": result.get("success", False),
        "diagnostics": [
            (d.get("message", "")[:200] if isinstance(d, dict) else str(d)[:200])
            for d in verify_diags[:5]
        ],
        "elapsed": elapsed,
    })

    # 4b. If failed, try Leanstral REPAIR (send errors back to Leanstral)
    if not result.get("success"):
        diag_msgs = [
            (d.get("message", "")[:200] if isinstance(d, dict) else str(d)[:200])
            for d in verify_diags[:5]
        ]
        db.emit_event(session_id, "repair_start", {
            "diagnostics": diag_msgs,
        })
        repaired_tactics, _ = repair_tactics(
            session["lean_statement"],
            leanstral_tactics,
            diag_msgs,
            session_id=session_id,
        )
        db.emit_event(session_id, "repair_result", {
            "tactics": repaired_tactics[:3000],
        })

        # Verify the repaired version
        source = build_lean_source(
            session["lean_statement"],
            session["imports"],
            repaired_tactics,
        )
        db.emit_event(session_id, "verify_start", {"source": source[:3000]})
        t0 = time.time()
        result = verify_lean(source)
        elapsed = round(time.time() - t0, 2)

        verify_diags = result.get("diagnostics", [])
        db.emit_event(session_id, "verify_result", {
            "success": result.get("success", False),
            "diagnostics": [
                (d.get("message", "")[:200] if isinstance(d, dict) else str(d)[:200])
                for d in verify_diags[:5]
            ],
            "elapsed": elapsed,
        })

        if result.get("success"):
            best_tactics = repaired_tactics
            best_source = source
        else:
            # Learn from repair failure immediately
            repair_diag_msgs = [
                (d.get("message", "")[:200] if isinstance(d, dict) else str(d)[:200])
                for d in verify_diags[:5]
            ]
            db.learn_from_repair_failure(
                session_id, leanstral_tactics, repaired_tactics,
                diag_msgs, repair_diag_msgs,
            )
            # Use whichever had fewer errors
            repair_errors = sum(1 for d in verify_diags if (d.get("severity") if isinstance(d, dict) else "") == "error")
            orig_errors = best_result.get("_error_count", 999) if best_result else 999
            if repair_errors < orig_errors:
                best_tactics = repaired_tactics
                best_source = source

    # Check for verified proof (from either initial or repaired attempt)
    if result.get("success"):
        has_sorry = any(
            "sorry" in (d.get("message", "") if isinstance(d, dict) else str(d))
            for d in result.get("diagnostics", [])
        )
        if not has_sorry:
            # VERIFIED!
            log.info("VERIFIED", session_id=session_id, turn=turn_number)
            db.update_session(session_id, status="verified", verified_proof=best_source)
            db.log_turn(
                session_id=session_id,
                turn_number=turn_number,
                strategy=strategy,
                tactics_tried=[best_tactics],
                lean_source=best_source,
                result="verified",
                diagnostics=[],
                promising=True,
                notes=f"VERIFIED! {plan.get('reasoning', '')}",
            )
            db.log_strategy(session_id, strategy, plan.get("strategy_description", ""), "verified", [turn_number])
            db.emit_event(session_id, "turn_complete", {
                "turn": turn_number,
                "result": "verified",
                "promising": True,
                "error_count": 0,
            })
            return {"result": "verified", "proof": best_source, "turn": turn_number}

    # Track error count from final result
    best_result = result
    diags = result.get("diagnostics", [])
    error_count = sum(1 for d in diags if (d.get("severity") if isinstance(d, dict) else "") == "error")
    best_result["_error_count"] = error_count

    # 5. Evaluate result — real progress detection, not just error counting
    diags = best_result.get("diagnostics", []) if best_result else []
    diag_messages = [
        (d.get("message", "")[:200] if isinstance(d, dict) else str(d)[:200])
        for d in diags[:5]
    ]
    error_count = best_result.get("_error_count", 0) if best_result else 0

    # Compute a signature of the errors to detect repeats
    error_sig = "|".join(sorted(set(
        (d.get("category", "") or d.get("message", "")[:40]) if isinstance(d, dict) else str(d)[:40]
        for d in diags if (d.get("severity") if isinstance(d, dict) else "") == "error"
    )))

    # Check if this is genuinely new or just repeating recent failures
    recent = db.get_recent_turns(session_id, limit=5)
    recent_sigs = set()
    recent_strategies = set()
    for t in recent:
        t_diags = t.get("diagnostics", [])
        sig = "|".join(sorted(set(d[:40] for d in t_diags)))
        recent_sigs.add(sig)
        recent_strategies.add(t.get("strategy", ""))

    # Determine if this is actually promising
    is_repeat_error = error_sig in recent_sigs and error_sig != ""
    is_repeat_strategy = strategy in recent_strategies
    is_syntax_error = any(
        kw in error_sig.lower()
        for kw in ["introN", "unexpected token", "expected command", "unknown tactic"]
    )

    if error_count == 0:
        promising = True  # No errors = genuinely promising
    elif is_repeat_error and is_repeat_strategy:
        promising = False  # Same strategy, same errors = dead end
    elif is_syntax_error:
        promising = False  # Syntax errors are never promising
    elif error_count == 1 and not is_repeat_error:
        promising = True  # One new error type = might be close
    else:
        promising = False  # Multiple errors or repeated = not promising

    result_label = "partial" if promising else "failed"

    db.log_turn(
        session_id=session_id,
        turn_number=turn_number,
        strategy=strategy,
        tactics_tried=[best_tactics],
        lean_source=best_source[:3000],
        result=result_label,
        diagnostics=diag_messages,
        promising=promising,
        notes=plan.get("reasoning", ""),
    )

    # Update strategy tracking
    outcome = "promising" if promising else "dead_end"
    db.log_strategy(session_id, strategy, plan.get("strategy_description", ""), outcome, [turn_number])

    # Only update best partial proof if genuinely promising
    if promising and best_source:
        db.update_session(session_id, best_partial_proof=best_source[:5000])

    # Auto-extract lessons after every failed turn (not just every 10)
    if not promising:
        new_lessons = db.auto_extract_lessons(session_id)
        if new_lessons:
            log.info("lessons_extracted", session_id=session_id, count=new_lessons)
            db.emit_event(session_id, "lesson_learned", {
                "lesson": f"Extracted {new_lessons} lesson(s) from errors",
                "count": new_lessons,
            })

    # Emit turn_complete event
    db.emit_event(session_id, "turn_complete", {
        "turn": turn_number,
        "result": result_label,
        "promising": promising,
        "error_count": error_count,
    })

    log.info(
        "turn_done",
        session_id=session_id,
        turn=turn_number,
        result="partial" if promising else "failed",
        errors=error_count,
        strategy=strategy,
    )

    return {
        "result": "partial" if promising else "failed",
        "turn": turn_number,
        "strategy": strategy,
        "errors": error_count,
        "diagnostics": diag_messages,
    }


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _auto_formalize(session_id: str, problem: str) -> str:
    """Use the LLM to translate a natural language problem into a Lean 4 theorem statement."""
    from services.agent.planner import _call_llm, PLANNER_MODEL

    db.emit_event(session_id, "formalize_start", {"problem": problem[:300]})

    system = (
        "You are a Lean 4 formalization expert. "
        "Given a natural language math statement, output ONLY the Lean 4 theorem statement. "
        "Include the theorem keyword, name, arguments with types, and the proposition. "
        "Do NOT include 'by', tactics, imports, or proof. "
        "Do NOT wrap in code fences. "
        "Example: theorem even_add (a b : Nat) (ha : Even a) (hb : Even b) : Even (a + b)"
    )
    user = f"Formalize this as a Lean 4 theorem statement:\n\n{problem}"

    try:
        raw, reasoning = _call_llm(system, user, model=PLANNER_MODEL)
        if reasoning:
            db.emit_event(session_id, "formalize_thinking", {"reasoning": reasoning[:3000]})
        # Clean up: strip fences, thinking tags, pick first line that starts with theorem/lemma
        raw = re.sub(r"<think>[\s\S]*?</think>", "", raw).strip()
        raw = re.sub(r"```\w*\s*", "", raw).strip()
        for line in raw.split("\n"):
            line = line.strip()
            if re.match(r"^(theorem|lemma)\s", line):
                lean_stmt = line
                break
        else:
            lean_stmt = raw.split("\n")[0].strip()

        # Remove trailing := by ... if the model added it
        lean_stmt = re.sub(r"\s*:=\s*by.*$", "", lean_stmt).strip()

        log.info("auto_formalized", session_id=session_id, statement=lean_stmt[:200])
        db.emit_event(session_id, "formalize_result", {"lean_statement": lean_stmt})
        return lean_stmt
    except Exception as e:
        log.error("auto_formalize_failed", error=str(e))
        db.emit_event(session_id, "error", {"message": f"Auto-formalization failed: {e}"})
        return ""


def run_loop(session_id: str, max_turns: int = 1000, delay: int = TURN_DELAY_SECS) -> None:
    """Run the proof search loop until verified or max_turns reached."""
    session = db.get_session(session_id)
    if not session:
        log.error("session_not_found", session_id=session_id)
        sys.exit(1)

    # Auto-formalize if no lean_statement provided
    if not session.get("lean_statement"):
        lean_stmt = _auto_formalize(session_id, session["problem"])
        if lean_stmt:
            db.update_session(session_id, lean_statement=lean_stmt)
            session["lean_statement"] = lean_stmt
        else:
            log.error("no_lean_statement", session_id=session_id)
            db.emit_event(session_id, "error", {"message": "Could not formalize the problem into Lean. Please provide a Lean theorem statement."})
            return

    log.info("loop_start", session_id=session_id, max_turns=max_turns, problem=session["problem"][:100])

    for i in range(max_turns):
        session = db.get_session(session_id)
        if session["status"] == "verified":
            log.info("loop_done_verified", session_id=session_id, turns=session["total_turns"])
            print(f"\n✓ VERIFIED after {session['total_turns']} turns!")
            print(f"Proof:\n{session.get('verified_proof', '')}")
            return

        if session["status"] in ("stuck", "abandoned"):
            log.info("loop_stopped", session_id=session_id, status=session["status"])
            print(f"\nSession {session_id} is {session['status']}. Use --resume to restart.")
            return

        try:
            result = run_turn(session_id)
        except Exception as e:
            log.error("turn_error", session_id=session_id, error=str(e))
            db.emit_event(session_id, "error", {"message": str(e)[:500]})
            time.sleep(delay * 2)
            continue

        if result.get("result") == "verified":
            print(f"\n✓ VERIFIED after {result['turn']} turns!")
            print(f"Proof:\n{result.get('proof', '')}")
            return

        # Progress report every 10 turns
        turn = result.get("turn", 0)
        if turn % 10 == 0:
            strats = db.get_strategies(session_id)
            dead = sum(1 for s in strats if s.get("outcome") == "dead_end")
            promising = sum(1 for s in strats if s.get("outcome") in ("promising", "partial"))
            print(f"  Turn {turn}: {result.get('result', '?')} | "
                  f"strategy={result.get('strategy', '?')} | "
                  f"dead_ends={dead} promising={promising}")

        time.sleep(delay)

    log.info("loop_max_turns", session_id=session_id, max_turns=max_turns)
    print(f"\nReached max turns ({max_turns}). Session {session_id} still in progress.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Autonomous proof search agent")
    parser.add_argument("--session", required=True, help="Session ID")
    parser.add_argument("--problem", help="Natural language problem description")
    parser.add_argument("--lean-statement", help="Lean theorem statement")
    parser.add_argument("--imports", nargs="*", default=["Mathlib.Tactic"], help="Lean imports")
    parser.add_argument("--preamble", default="", help="Lean preamble (definitions before theorem)")
    parser.add_argument("--max-turns", type=int, default=1000, help="Maximum turns")
    parser.add_argument("--delay", type=int, default=TURN_DELAY_SECS, help="Seconds between turns")
    parser.add_argument("--resume", action="store_true", help="Resume existing session")
    parser.add_argument("--status", action="store_true", help="Show session status and exit")
    args = parser.parse_args()

    if args.status:
        session = db.get_session(args.session)
        if not session:
            print(f"Session {args.session} not found")
            sys.exit(1)
        turns = db.get_turn_count(args.session)
        strats = db.get_strategies(args.session)
        dead = sum(1 for s in strats if s.get("outcome") == "dead_end")
        promising = sum(1 for s in strats if s.get("outcome") in ("promising", "partial"))
        recent = db.get_recent_turns(args.session, limit=3)
        print(f"Session: {args.session}")
        print(f"Status: {session['status']}")
        print(f"Problem: {session['problem'][:100]}")
        print(f"Turns: {turns}")
        print(f"Strategies: {len(strats)} ({dead} dead ends, {promising} promising)")
        if recent:
            print(f"Last 3 turns:")
            for t in recent:
                print(f"  Turn {t['turn']}: {t['strategy']} → {t['result']}")
        if session.get("verified_proof"):
            print(f"Verified proof:\n{session['verified_proof']}")
        return

    if args.resume:
        session = db.get_session(args.session)
        if not session:
            print(f"Session {args.session} not found. Use --problem and --lean-statement to create it.")
            sys.exit(1)
        if session["status"] in ("stuck", "abandoned"):
            db.update_session(args.session, status="in_progress")
        run_loop(args.session, max_turns=args.max_turns, delay=args.delay)
        return

    # Create new session
    if not args.problem or not args.lean_statement:
        print("ERROR: --problem and --lean-statement are required for new sessions")
        sys.exit(1)

    existing = db.get_session(args.session)
    if existing:
        print(f"Session {args.session} already exists. Use --resume to continue.")
        sys.exit(1)

    db.create_session(
        session_id=args.session,
        problem=args.problem,
        lean_statement=args.lean_statement,
        imports=args.imports,
        metadata={"preamble": args.preamble} if args.preamble else {},
    )
    print(f"Created session: {args.session}")
    run_loop(args.session, max_turns=args.max_turns, delay=args.delay)


if __name__ == "__main__":
    main()
