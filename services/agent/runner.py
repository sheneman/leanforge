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
from services.agent.planner import plan_next_step, synthesize_tactics, repair_tactics, diagnose_failure, creative_brainstorm

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
                params={"q": f"{query} Lean 4 Mathlib4", "count": count + 3},
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": BRAVE_API_KEY,
                },
            )
            resp.raise_for_status()
            results = resp.json().get("web", {}).get("results", [])
            # Filter out Lean 3 docs — they use incompatible syntax
            lean3_markers = ["mathlib3 docs", "mathlib_docs", "leanprover-community.github.io/mathlib_docs"]
            filtered = []
            for r in results:
                url = r.get("url", "")
                title = r.get("title", "")
                if any(m in url or m in title for m in lean3_markers):
                    log.info("web_search_filtered_lean3", url=url)
                    continue
                filtered.append({
                    "title": title,
                    "url": url,
                    "description": r.get("description", "")[:300],
                })
                if len(filtered) >= count:
                    break
            return filtered
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


def _apply_exact_suggestions(source: str, diagnostics: list[dict]) -> str | None:
    """Extract 'Try this:' suggestions from Lean info diagnostics and apply them.

    When the model writes `exact?`, `apply?`, or `simp?`, Lean returns
    info diagnostics like 'Try this: exact Even.add ha hb'. This function
    finds those suggestions and substitutes them back into the source.

    Returns the modified source, or None if no suggestions were found.
    """
    suggestions = []
    for d in diagnostics:
        if not isinstance(d, dict):
            continue
        msg = d.get("message", "")
        sev = d.get("severity", "")
        line = d.get("line")
        if sev == "info" and "Try this:" in msg:
            # Extract the suggestion after "Try this: "
            m = re.search(r"Try this:\s*(.+?)(?:\n|$)", msg)
            if m and line is not None:
                suggestions.append((int(line), m.group(1).strip()))

    if not suggestions:
        return None

    lines = source.split("\n")
    applied = False
    for line_num, replacement in sorted(suggestions, reverse=True):
        idx = line_num - 1  # Lean uses 1-based line numbers
        if 0 <= idx < len(lines):
            old_line = lines[idx]
            # Preserve indentation
            indent = len(old_line) - len(old_line.lstrip())
            lines[idx] = " " * indent + replacement
            applied = True
            log.info("exact_suggestion_applied",
                      line=line_num, old=old_line.strip()[:50],
                      new=replacement[:80])

    return "\n".join(lines) if applied else None


def _collapse_simple_by_blocks(source: str) -> str:
    """Collapse simple multi-line by-blocks into single lines.

    The LLM constantly writes:
        have h : T := by
          exact foo
        next_tactic   -- often gets wrongly indented inside the by block

    This function collapses single-tactic by-blocks to:
        have h : T := by exact foo

    This prevents the most common indentation bug where the next sibling
    tactic ends up inside the by-block.
    """
    lines = source.split("\n")
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.rstrip()

        # Only collapse `have/let := by` patterns — NOT standalone `by` lines.
        # Standalone `by` (the theorem's proof block opener) must stay on its own line.
        if re.search(r":=\s*by\s*$", stripped):
            # Look at the next non-empty line
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1

            if j < len(lines):
                next_line = lines[j].strip()
                # Check if it's a single simple tactic (no nested blocks)
                is_simple = (
                    next_line and
                    not next_line.startswith("by ") and
                    "by\n" not in next_line and
                    not next_line.endswith(" by") and
                    ":= by" not in next_line and
                    not any(next_line.startswith(kw) for kw in [
                        "match ", "if ", "cases ", "induction ",
                        "calc", "have ", "haveI ", "let ", "letI ",
                        "show ", "suffices ", "obtain ",
                    ])
                )
                # Check if only one tactic line follows before returning to outer indent
                k = j + 1
                while k < len(lines) and not lines[k].strip():
                    k += 1
                one_tactic_only = (
                    k >= len(lines) or
                    (k < len(lines) and len(lines[k]) - len(lines[k].lstrip()) <= len(line) - len(line.lstrip()))
                )

                if is_simple and one_tactic_only:
                    # Collapse: append tactic to the by line
                    collapsed = stripped + " " + next_line
                    result.append(collapsed)
                    i = j + 1  # Skip the tactic line
                    continue

        result.append(line)
        i += 1

    return "\n".join(result)


def _clean_leanstral_output(raw: str) -> str:
    """Aggressively clean Leanstral output to extract only valid Lean 4 tactics.

    Leanstral often produces:
    - Code fences (```lean4, ```tactics, ```)
    - Natural language explanations mixed with code
    - Multiple disconnected code blocks
    - Import statements and theorem declarations
    - Hallucinated lemma names with commentary

    This function extracts the first coherent block of tactic code.
    """
    # Strip code fences
    raw = re.sub(r"```\w*", "", raw)

    # If there's a := by in the output, extract everything after it
    by_match = re.search(r":=\s*by\s*\n([\s\S]+)", raw)
    if by_match:
        raw = by_match.group(1)

    # Split into lines and filter
    lines = raw.split("\n")
    tactic_lines: list[str] = []
    in_tactics = False

    for line in lines:
        stripped = line.strip()

        # Skip empty lines at the start
        if not in_tactics and not stripped:
            continue

        # Skip imports, opens, theorem declarations
        if stripped.startswith("import ") or stripped.startswith("open "):
            continue
        if re.match(r"^(theorem|lemma|def|example|#)\s", stripped):
            continue

        # Skip natural language lines (heuristic: starts with uppercase letter
        # followed by lowercase, contains no Lean-like tokens)
        if (re.match(r"^[A-Z][a-z]", stripped)
            and not any(kw in stripped for kw in [
                "have ", "let ", "show ", "suffices ", "calc ", "match ",
                "cases ", "induction ", "apply ", "exact ", "rw ", "simp ",
                "intro ", "obtain ", "use ", "refine ", "constructor",
                "by_cases ", "omega", "linarith", "ring", "norm_num",
                "sorry", "trivial", "tauto", "aesop", "decide",
            ])):
            # If we already have tactics, this natural language line means
            # the coherent block ended
            if in_tactics and tactic_lines:
                break
            continue

        # Skip lines that are clearly commentary
        if stripped.startswith("--") and not in_tactics:
            continue
        if stripped.startswith("So ") or stripped.startswith("Now ") or stripped.startswith("But "):
            if in_tactics and tactic_lines:
                break
            continue
        if stripped.startswith("Wait") or stripped.startswith("Alternatively") or stripped.startswith("Looking"):
            if in_tactics and tactic_lines:
                break
            continue

        # This looks like a tactic line
        in_tactics = True
        tactic_lines.append(line)

    result = "\n".join(tactic_lines).strip()

    # Strip leading "by" if present
    if result.lower().startswith("by\n") or result.lower().startswith("by "):
        result = result[2:].strip()
    elif result.lower() == "by":
        result = ""

    return result if result else "sorry"


_TOP_LEVEL_TACTICS = re.compile(
    r"^(have|let|show|suffices|obtain|intro|apply|exact|rw|simp|"
    r"cases|rcases|induction|by_cases|constructor|use|refine|"
    r"calc|match|omega|linarith|ring|norm_num|aesop|trivial|"
    r"decide|tauto|sorry|haveI|letI|·|--|\|)\b"
)

_CONTINUATION_STARTERS = re.compile(
    r"^(·|\|)"
)


def _normalize_tactic_indentation(tactics: str, base_indent: int = 2) -> str:
    """Normalize tactic indentation with block tracking and pop detection.

    Uses both keyword detection AND original indentation to determine
    block structure:
    - A 'by' at end of line pushes a new block (depth +1)
    - A keyword line at original indent <= the block opener's indent pops the block
    - Keywords go at the current block's base depth
    - Non-keywords go one level deeper (continuations)
    """
    lines = tactics.split("\n")
    result: list[str] = []

    # Stack entries: (output_depth, original_indent_of_opener)
    block_stack: list[tuple[int, int]] = [(0, 0)]

    for line in lines:
        if not line.strip():
            result.append("")
            continue

        content = line.lstrip()
        original_indent = len(line) - len(line.lstrip())
        is_keyword = bool(_TOP_LEVEL_TACTICS.match(content))

        # Pop blocks: if this is a keyword at or below the opener's indent, pop
        if is_keyword:
            while len(block_stack) > 1:
                _, opener_indent = block_stack[-1]
                if original_indent <= opener_indent:
                    block_stack.pop()
                else:
                    break

        # Determine output indent
        current_depth = block_stack[-1][0]
        if is_keyword:
            indent = base_indent + current_depth * 2
        else:
            indent = base_indent + (current_depth + 1) * 2

        result.append(" " * indent + content)

        # If this line ends with 'by', push a new block
        stripped = content.rstrip()
        if stripped.endswith(" by") or stripped.endswith(":= by") or stripped == "by":
            block_stack.append((current_depth + 1, original_indent))

    return "\n".join(result)


def build_lean_source(lean_statement: str, imports: list[str], tactics: str, preamble: str = "") -> str:
    """Assemble a complete Lean source file."""
    import_lines = "\n".join(f"import {i}" for i in imports)
    # Normalize the theorem statement
    stmt = lean_statement.strip()
    if not re.match(r"^(theorem|lemma|def|example)\s", stmt):
        import hashlib
        name = "auto_" + hashlib.md5(stmt.encode()).hexdigest()[:8]
        stmt = f"theorem {name} : {stmt}"

    # Clean the tactics through the aggressive filter
    tactics = _clean_leanstral_output(tactics)

    parts = [import_lines, ""]
    if preamble:
        parts.append(preamble)
        parts.append("")
    parts.append(f"{stmt} := by")
    # Add tactics with minimal indent (formatter will fix structure)
    for line in tactics.split("\n"):
        if line.strip():
            parts.append(f"  {line.lstrip()}" if not line.startswith("  ") else line)
        else:
            parts.append("")
    parts.append("")
    source = "\n".join(parts)

    # Format with our Lean-aware formatter (fixes sibling indentation)
    from scripts.lean_format import format_lean_source
    source = format_lean_source(source)

    # Run lean-fmt as a final pass if available (normalizes spacing, operators)
    source = _run_lean_fmt(source)

    return source


def _fix_hallucinated_names(source: str, diagnostics: list[dict], session_id: str = "") -> str:
    """Fix hallucinated lemma names by searching retrieval for the closest real match.

    When Lean says 'unknown identifier X' or 'unknown constant X', search
    mathlib for the closest real name and substitute it in the source.
    """
    replacements: dict[str, str] = {}

    for diag in diagnostics:
        msg = diag.get("message", "") if isinstance(diag, dict) else str(diag)

        # Extract unknown identifiers/constants
        for pattern in [
            r"Unknown (?:identifier|constant) [`'](\S+)[`']",
            r"unknown identifier [`'](\S+)[`']",
            r"unknown constant [`'](\S+)[`']",
        ]:
            m = re.search(pattern, msg, re.IGNORECASE)
            if m:
                bad_name = m.group(1).strip("'`")
                if bad_name in replacements or len(bad_name) < 3:
                    continue

                # Search retrieval for closest match
                results = search_mathlib(bad_name, top_k=3)
                if results:
                    best = results[0]
                    real_name = best.get("name", "")
                    if real_name and real_name != bad_name:
                        replacements[bad_name] = real_name
                        log.info("fix_hallucination",
                                 bad=bad_name, real=real_name,
                                 score=best.get("score", 0))

    if not replacements:
        return source

    # Apply substitutions
    fixed = source
    for bad, good in replacements.items():
        fixed = fixed.replace(bad, good)

    if session_id and replacements:
        db.emit_event(session_id, "fix_hallucination", {
            "replacements": {k: v for k, v in list(replacements.items())[:5]},
        })
        # Learn this as a global lesson
        for bad, good in replacements.items():
            db.log_lesson(
                session_id,
                f"'{bad}' does not exist in mathlib. Use '{good}' instead.",
                category="api",
                global_lesson=True,
            )

    return fixed


def _run_lean_fmt(source: str) -> str:
    """Run lean-fmt on the source if available. Returns original if lean-fmt fails."""
    import subprocess
    import tempfile
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".lean", delete=False) as f:
            f.write(source)
            f.flush()
            result = subprocess.run(
                ["lean-fmt", f.name],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        pass
    return source


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

    # 0. Creativity agent — runs every 5 turns or on turn 1 to inject fresh ideas
    if turn_number == 1 or turn_number % 5 == 0:
        try:
            db.emit_event(session_id, "creativity_start", {"turn": turn_number})
            ideas = creative_brainstorm(session_id)
            # Feed any search queries from creative ideas into the turn
            creative_searches = [
                i["search_query"] for i in ideas
                if i.get("search_query")
            ]
            log.info("creativity_done", session_id=session_id,
                     ideas=len(ideas), searches=len(creative_searches))
        except Exception as e:
            log.warning("creativity_failed", error=str(e))
            creative_searches = []
    else:
        creative_searches = []

    # 1. Plan next step
    db.emit_event(session_id, "planner_start", {"turn": turn_number})
    plan = plan_next_step(session_id)
    strategy = plan.get("strategy_name", "unknown")
    log.info("plan", session_id=session_id, strategy=strategy)

    action = plan.get("action", "PROVE")
    db.emit_event(session_id, "planner_result", {
        "action": action,
        "strategy": strategy,
        "reasoning": plan.get("reasoning", "")[:300],
        "suggested_tactics": plan.get("suggested_tactics", "")[:3000],
    })

    if strategy == "DONE":
        return {"result": "already_verified"}

    # --- INVESTIGATE action: just search and log, don't synthesize ---
    if action == "INVESTIGATE":
        all_lemmas = []
        for query in plan.get("search_queries", [])[:3]:
            db.emit_event(session_id, "search_start", {"query": query})
            results = search_mathlib(query, top_k=10)
            for r in results:
                db.log_lemma(session_id, r["name"], r.get("statement", ""), r.get("module", ""))
                all_lemmas.append(r)
            db.emit_event(session_id, "search_result", {
                "query": query,
                "results": [{"name": r["name"], "statement": r.get("statement", "")} for r in results[:5]],
            })
        # Log findings as a lesson so the planner sees them next turn
        if all_lemmas:
            findings = "; ".join(f"{l['name']}: {l.get('statement', '')[:100]}" for l in all_lemmas[:5])
            db.log_lesson(session_id, f"Investigation found: {findings[:500]}", category="investigation")
        db.log_turn(
            session_id=session_id, turn_number=turn_number, strategy=strategy,
            tactics_tried=[], lean_source="", result="investigation",
            diagnostics=[], promising=True, notes=f"Investigated: {plan.get('strategy_description', '')[:200]}",
        )
        db.emit_event(session_id, "turn_complete", {
            "turn": turn_number, "result": "investigation", "promising": True, "error_count": 0,
        })
        return {"result": "investigation", "turn": turn_number, "strategy": strategy}

    # --- RESEARCH action: web search only ---
    if action == "RESEARCH":
        for query in plan.get("web_search_queries", plan.get("search_queries", []))[:2]:
            results = web_search(query, count=5)
            db.emit_event(session_id, "web_search_result", {
                "query": query,
                "results": [{"title": r.get("title", ""), "url": r.get("url", "")} for r in results[:5]],
            })
            if results:
                refs = "; ".join(f"{r['title'][:60]} ({r['url'][:80]})" for r in results[:3])
                db.log_lesson(session_id, f"Research: {refs}", category="web_reference")
        db.log_turn(
            session_id=session_id, turn_number=turn_number, strategy=strategy,
            tactics_tried=[], lean_source="", result="research",
            diagnostics=[], promising=True, notes=f"Researched: {plan.get('strategy_description', '')[:200]}",
        )
        db.emit_event(session_id, "turn_complete", {
            "turn": turn_number, "result": "research", "promising": True, "error_count": 0,
        })
        return {"result": "research", "turn": turn_number, "strategy": strategy}

    # --- DECOMPOSE action: log sub-problems for future turns ---
    if action == "DECOMPOSE":
        desc = plan.get("strategy_description", "")
        db.log_lesson(session_id, f"Decomposition plan: {desc[:500]}", category="decomposition")
        db.emit_event(session_id, "decomposition", {"description": desc[:500]})
        db.log_turn(
            session_id=session_id, turn_number=turn_number, strategy=strategy,
            tactics_tried=[], lean_source="", result="decomposition",
            diagnostics=[], promising=True, notes=desc[:500],
        )
        db.emit_event(session_id, "turn_complete", {
            "turn": turn_number, "result": "decomposition", "promising": True, "error_count": 0,
        })
        return {"result": "decomposition", "turn": turn_number, "strategy": strategy}

    # --- SIMPLIFY action: try minimal proof with search tactics ---
    if action == "SIMPLIFY":
        # Build a minimal proof using exact?, apply?, simp?
        session_lessons = [l["lesson"] for l in db.get_lessons(session_id)]
        simple_tactics, _ = synthesize_tactics(
            session["lean_statement"],
            strategy="Try the simplest possible proof: exact?, apply?, simp?, or decide. One line if possible.",
            session_id=session_id,
            lessons=session_lessons,
        )
        # Fall through to verification — skip search and synthesis
        plan["strategy_description"] = "Simplify: try minimal search tactics"

    # --- PROVE / SIMPLIFY: search + synthesize + verify ---
    all_lemmas = []

    # 2. Search mathlib (PROVE only — SIMPLIFY skips search)
    if action == "PROVE":
        search_queries = plan.get("search_queries", [])[:3]
        if creative_searches:
            search_queries = search_queries + creative_searches[:2]
        for query in search_queries:
            db.emit_event(session_id, "search_start", {"query": query})
            results = search_mathlib(query, top_k=5)
            for r in results:
                db.log_lemma(session_id, r["name"], r.get("statement", ""), r.get("module", ""))
                all_lemmas.append(r)
            db.emit_event(session_id, "search_result", {
                "query": query,
                "results": [{"name": r["name"], "statement": r.get("statement", "")[:150]} for r in results[:3]],
            })

        # 2b. Web search (if planner requested it)
        web_results = []
        for query in plan.get("web_search_queries", [])[:2]:
            results = web_search(query, count=3)
            web_results.extend(results)
            log.info("web_search", session_id=session_id, query=query, results=len(results))
            db.emit_event(session_id, "web_search_result", {
                "query": query,
                "results": [{"title": r.get("title", ""), "url": r.get("url", "")} for r in results[:3]],
            })
        if web_results:
            refs = "; ".join(f"{r['title'][:60]} ({r['url'][:80]})" for r in web_results[:3])
            db.log_lesson(
                session_id,
                f"Web refs for '{plan.get('web_search_queries', ['?'])[0][:50]}': {refs}",
                category="web_reference",
            )

    # 3. Synthesize tactics
    strategy_desc = plan.get("strategy_description", "")
    session_lessons = [l["lesson"] for l in db.get_lessons(session_id)]

    if action == "SIMPLIFY":
        # SIMPLIFY already created simple_tactics above
        leanstral_tactics = simple_tactics
        db.emit_event(session_id, "synthesize_result", {"tactics": leanstral_tactics[:3000]})
    else:
        # PROVE: full synthesis with lemma hints
        lemma_hints = ""
        if all_lemmas:
            lemma_hints = "\n".join(
                f"  {l['name']}: {l.get('statement', '')[:150]}" for l in all_lemmas[:10]
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
            lemmas=all_lemmas,
            lessons=session_lessons,
        )

    db.emit_event(session_id, "synthesize_result", {
        "tactics": leanstral_tactics[:3000],
    })

    # 4. Verify Leanstral's output
    # Leanstral returns a complete Lean file — use it directly if it has imports + theorem
    # Otherwise fall back to build_lean_source
    best_result = None
    best_tactics = leanstral_tactics
    best_source = ""

    # Check if Leanstral returned a complete file (has import and theorem)
    cleaned = re.sub(r"```\w*", "", leanstral_tactics).strip()
    # Remove comment-only lines at the start (strategy comments)
    lean_lines = [l for l in cleaned.split("\n") if not l.strip().startswith("--") or "import" in l.lower()]
    has_import = any("import " in l for l in cleaned.split("\n")[:5])
    has_theorem = any(re.match(r"^(theorem|lemma|def)\s", l.strip()) for l in cleaned.split("\n"))

    if has_import and has_theorem:
        # Use Leanstral's complete file directly — just strip code fences
        source = cleaned
        # Run formatter to fix indentation
        from scripts.lean_format import format_lean_source
        source = format_lean_source(source)
        source = _run_lean_fmt(source)
    else:
        # Leanstral returned just tactics — wrap them
        source = build_lean_source(
            session["lean_statement"],
            session["imports"],
            leanstral_tactics,
        )
    # Collapse simple by-blocks to single lines to prevent indentation bugs
    source = _collapse_simple_by_blocks(source)
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

    # 4a2. If Lean returned "Try this:" suggestions (from exact?, apply?, simp?),
    # apply them and re-verify. This lets the model use search tactics.
    suggested_source = _apply_exact_suggestions(best_source, verify_diags)
    if suggested_source:
        db.emit_event(session_id, "exact_suggestion", {
            "original": best_source[:1000],
            "suggested": suggested_source[:1000],
        })
        db.emit_event(session_id, "verify_start", {"source": suggested_source[:3000]})
        t0 = time.time()
        result = verify_lean(suggested_source)
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
            best_source = suggested_source

    # 4b. If failed with unknown identifiers, try fixing hallucinated names first
    if not result.get("success"):
        has_unknown = any(
            "unknown" in (d.get("message", "") if isinstance(d, dict) else str(d)).lower()
            for d in verify_diags
        )
        if has_unknown:
            fixed_source = _fix_hallucinated_names(source, verify_diags, session_id)
            if fixed_source != source:
                db.emit_event(session_id, "verify_start", {"source": fixed_source[:3000]})
                t0 = time.time()
                result = verify_lean(fixed_source)
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
                    best_source = fixed_source
                    best_tactics = leanstral_tactics  # tactics didn't change, source did

    # 4c. If still failed, try Leanstral REPAIR
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
            strategy=plan.get("strategy_description", ""),
            lessons=session_lessons,
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

    # Extract remaining subgoals from diagnostics (e.g., "unsolved goals" messages)
    subgoals = []
    for d in diags:
        msg = d.get("message", "") if isinstance(d, dict) else str(d)
        if "unsolved goals" in msg.lower():
            # Lean prints the goal state after "unsolved goals"
            subgoals.append(msg[:500])
        elif "expected type" in msg.lower() or "has type" in msg.lower():
            subgoals.append(msg[:300])

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
        subgoals_remaining=subgoals[:3],
    )

    # Update strategy tracking
    outcome = "promising" if promising else "dead_end"
    db.log_strategy(session_id, strategy, plan.get("strategy_description", ""), outcome, [turn_number])

    # Tag lemmas with outcomes so retrieval can learn what's useful
    db.tag_lemmas_used(session_id, best_source, result_label)

    # Only update best partial proof if genuinely promising
    if promising and best_source:
        db.update_session(session_id, best_partial_proof=best_source[:5000])

    # Auto-extract lessons from repeated error patterns
    if not promising:
        new_lessons = db.auto_extract_lessons(session_id)
        if new_lessons:
            log.info("lessons_extracted", session_id=session_id, count=new_lessons)

    # Diagnostic analysis — ask the LLM to reason about WHY the proof failed.
    # This is the agent's self-debugging: instead of blindly retrying, it
    # understands the root cause and logs an actionable lesson.
    if not promising and error_count > 0 and error_count <= 5:
        diagnosis = diagnose_failure(
            lean_source=best_source,
            diagnostics=diag_messages,
            lemma_signatures=all_lemmas[:5] if all_lemmas else None,
            session_id=session_id,
            strategy=plan.get("strategy_description", ""),
        )
        db.emit_event(session_id, "diagnosis", {
            "root_cause": diagnosis.get("root_cause", ""),
            "fix": diagnosis.get("fix", ""),
            "lesson": diagnosis.get("lesson", ""),
        })
        # Log the lesson if it's actionable (not "NONE" or empty)
        lesson_text = diagnosis.get("lesson", "")
        if lesson_text and lesson_text.upper() != "NONE" and len(lesson_text) > 20:
            db.log_lesson(session_id, lesson_text, category="diagnosis")
            log.info("diagnosis_lesson", session_id=session_id, lesson=lesson_text[:80])

        # Also log the specific fix as a high-priority lesson so the planner
        # acts on it immediately next turn
        fix_text = diagnosis.get("fix", "")
        if fix_text and fix_text.upper() != "NONE" and len(fix_text) > 20:
            db.log_lesson(
                session_id,
                f"IMMEDIATE FIX (from last turn): {fix_text[:300]}",
                category="diagnosis",
            )

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
        # Clean up: strip fences, thinking tags
        raw = re.sub(r"<think>[\s\S]*?</think>", "", raw).strip()
        raw = re.sub(r"```\w*", "", raw).strip()

        # Find the theorem/lemma declaration — may span multiple lines
        lines = raw.split("\n")
        stmt_lines: list[str] = []
        collecting = False
        for line in lines:
            stripped = line.strip()
            if not collecting and re.match(r"^(theorem|lemma)\s", stripped):
                collecting = True
                stmt_lines.append(stripped)
            elif collecting:
                # Continuation: indented line or line with : or )
                if stripped.startswith("(") or stripped.startswith("[") or stripped.startswith(":") or (line.startswith(" ") and stripped):
                    stmt_lines.append(stripped)
                else:
                    break  # End of theorem statement

        if stmt_lines:
            lean_stmt = " ".join(stmt_lines)
        else:
            lean_stmt = lines[0].strip() if lines else ""

        # Remove trailing := by ... if the model added it
        lean_stmt = re.sub(r"\s*:=\s*by.*$", "", lean_stmt).strip()
        # Remove trailing := sorry
        lean_stmt = re.sub(r"\s*:=\s*sorry.*$", "", lean_stmt).strip()

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

    # Verify the theorem statement compiles before starting the proof loop.
    # Iterate up to 5 times: compile "statement := by sorry", if errors,
    # ask the LLM to fix the statement with error feedback, repeat.
    from services.agent.planner import _call_llm, PLANNER_MODEL
    MAX_FORMALIZE_RETRIES = 5

    for attempt in range(MAX_FORMALIZE_RETRIES + 1):
        check_source = f"import Mathlib.Tactic\n\n{session['lean_statement']} := by sorry\n"
        check_result = verify_lean(check_source)
        check_diags = check_result.get("diagnostics", [])
        real_errors = [
            d for d in check_diags
            if (d.get("severity") if isinstance(d, dict) else "") == "error"
            and "sorry" not in (d.get("message", "") if isinstance(d, dict) else str(d)).lower()
        ]

        if not real_errors:
            if attempt > 0:
                db.emit_event(session_id, "formalize_result", {
                    "lean_statement": session["lean_statement"],
                    "attempt": attempt + 1,
                })
                log.info("statement_validated", session_id=session_id, attempts=attempt + 1)
            break  # Statement compiles — proceed to proof loop

        error_msgs = [d.get("message", "")[:200] if isinstance(d, dict) else str(d)[:200] for d in real_errors[:3]]
        log.warning("statement_invalid", session_id=session_id, attempt=attempt + 1, errors=error_msgs)
        db.emit_event(session_id, "error", {
            "message": f"Statement doesn't compile (attempt {attempt + 1}): {'; '.join(error_msgs)}"
        })

        if attempt >= MAX_FORMALIZE_RETRIES:
            db.emit_event(session_id, "error", {
                "message": f"Could not produce a valid theorem statement after {MAX_FORMALIZE_RETRIES + 1} attempts. Please provide one manually."
            })
            return

        # Ask the LLM to fix the statement
        db.emit_event(session_id, "formalize_start", {
            "problem": f"Fix attempt {attempt + 1}: {session['problem'][:200]}"
        })
        retry_raw, _ = _call_llm(
            "You are a Lean 4 formalization expert. Fix this theorem statement so it compiles. "
            "Output ONLY the corrected theorem statement. No imports, no proof, no code fences. "
            "The statement must work with 'import Mathlib.Tactic'.",
            f"This Lean 4 theorem statement has errors:\n\n{session['lean_statement']}\n\n"
            f"Compiler errors: {'; '.join(error_msgs)}\n\nFix it.",
            model=PLANNER_MODEL,
        )
        retry_raw = re.sub(r"<think>[\s\S]*?</think>", "", retry_raw).strip()
        retry_raw = re.sub(r"```\w*", "", retry_raw).strip()
        retry_raw = re.sub(r"\s*:=\s*by.*$", "", retry_raw).strip()
        retry_raw = re.sub(r"\s*:=\s*sorry.*$", "", retry_raw).strip()
        if retry_raw and re.search(r"^(theorem|lemma)\s", retry_raw):
            db.update_session(session_id, lean_statement=retry_raw)
            session["lean_statement"] = retry_raw
            log.info("statement_retry", session_id=session_id, attempt=attempt + 1, statement=retry_raw[:200])
        else:
            db.emit_event(session_id, "error", {
                "message": f"LLM retry {attempt + 1} did not produce a valid theorem statement."
            })
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
