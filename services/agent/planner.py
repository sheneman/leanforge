"""Planner: asks the LLM what to try next based on session history.

The planner builds a focused prompt from MongoDB queries (not full history)
and asks the orchestrator LLM to propose the next proof strategy.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx
import structlog

from services.agent.db import build_context

log = structlog.get_logger()

LLM_API_BASE = os.getenv("LLM_API_BASE", "").rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_API_MODEL = os.getenv("LLM_API_MODEL", "")
PLANNER_MODEL = os.getenv("PLANNER_MODEL", "qwen/qwen3.5-122b")
LEANSTRAL_API_MODEL = os.getenv("LEANSTRAL_API_MODEL", "")

# Planner produces STRATEGY ONLY — no Lean code.
# All Lean code generation goes through Leanstral.
PLANNER_SYSTEM_PROMPT = """\
You are a Lean 4 theorem proving strategist. You choose WHAT TO DO NEXT \
and propose proof STRATEGIES in natural language. You do NOT write Lean code.

AVAILABLE ACTIONS (choose ONE):
- PROVE: Search for lemmas, synthesize a proof, and verify it. The default action.
- INVESTIGATE: Look up a specific lemma's exact signature and behavior before trying \
  to use it. Use when you're unsure about a lemma's argument types or return type.
- DECOMPOSE: Split the theorem into sub-lemmas. Describe each sub-lemma and how they \
  combine. Use for complex theorems that can't be solved in one step.
- RESEARCH: Do web search for Lean 4 / Mathlib4 documentation about a specific topic. \
  Use only when FAISS retrieval returned nothing useful.
- SIMPLIFY: Try a minimal proof using just exact?, apply?, or simp? to let Lean search.

RULES:
1. Read the TECHNICAL LESSONS section. These are HARD FACTS.
2. Read the DEAD ENDS section. Do NOT retry those approaches.
3. Propose something NEW that has NOT been tried.
4. If a promising direction exists, BUILD ON IT.
5. Do NOT write Lean tactics or code. Describe the approach in natural language.
6. Be specific about which mathlib lemmas to use and what proof structure to follow.
7. If there's an IMMEDIATE FIX lesson, act on it — it's from the last failed turn.

Respond in EXACTLY this format:

ACTION: <one of: PROVE, INVESTIGATE, DECOMPOSE, RESEARCH, SIMPLIFY>
STRATEGY: <short name, 3-8 words>
DESCRIPTION: <detailed description of the proof approach — which lemmas to use, \
what case splits to make, what induction scheme to follow, etc. 2-5 sentences.>
SEARCH: <mathlib search query to find relevant lemmas>
WEB_SEARCH: <optional web search query targeting Lean 4 / Mathlib4 docs ONLY, or NONE>
REASONING: <why this might work given past failures, 1-2 sentences>
"""


def _call_leanstral(user: str) -> tuple[str, str]:
    """Call Leanstral using its recommended settings.

    Leanstral is a Lean 4 proof generation model by Mistral. Key settings:
    - temperature=1.0 (recommended by Mistral docs)
    - Full proof output (not constrained to tactics-only)
    - No structured output (let it generate natural Lean code)
    - max_tokens=16384 (proofs can be long)

    Returns (content, reasoning_trace).
    """
    if not LLM_API_BASE or not LLM_API_KEY or not LEANSTRAL_API_MODEL:
        raise RuntimeError("LLM_API_BASE, LLM_API_KEY, and LEANSTRAL_API_MODEL must be configured")

    url = f"{LLM_API_BASE}/chat/completions"
    with httpx.Client(timeout=300) as client:
        resp = client.post(
            url,
            headers={
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": LEANSTRAL_API_MODEL,
                "messages": [
                    {"role": "user", "content": user},
                ],
                "max_tokens": 16384,
                "temperature": 1.0,
            },
        )
        resp.raise_for_status()
        msg = resp.json()["choices"][0]["message"]
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning_content") or ""
        if not content and reasoning:
            content = reasoning
        return content, reasoning


def _call_llm(system: str, user: str, model: str | None = None) -> tuple[str, str]:
    """Call the LLM API and return (content, reasoning_trace).

    Returns both the final output and any reasoning/thinking trace
    so callers can emit both for visibility.
    """
    model = model or LLM_API_MODEL
    if not LLM_API_BASE or not LLM_API_KEY or not model:
        raise RuntimeError("LLM_API_BASE, LLM_API_KEY, and model must be configured")

    url = f"{LLM_API_BASE}/chat/completions"
    with httpx.Client(timeout=300) as client:
        resp = client.post(
            url,
            headers={
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": 65536,
                "temperature": 0.4,
            },
        )
        resp.raise_for_status()
        msg = resp.json()["choices"][0]["message"]
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning_content") or ""
        # If content is empty but reasoning exists, use reasoning as content
        if not content and reasoning:
            content = reasoning
        return content, reasoning


def _format_context_for_prompt(ctx: dict) -> str:
    """Format the DB context into a concise text prompt."""
    lines = [
        f"## Problem\n{ctx['problem']}",
        f"\n## Lean Statement\n{ctx['lean_statement']}",
        f"\n## Total turns so far: {ctx['total_turns']}",
    ]

    # Lessons go FIRST — hard facts
    if ctx.get("lessons"):
        lines.append(f"\n## TECHNICAL LESSONS (ALWAYS apply these — do NOT repeat these mistakes)")
        for lesson in ctx["lessons"]:
            lines.append(f"  - {lesson}")

    if ctx["best_partial_proof"]:
        lines.append(f"\n## Best partial proof so far\n{ctx['best_partial_proof'][:1000]}")

    if ctx["dead_ends"]:
        lines.append(f"\n## DEAD ENDS (DO NOT retry these strategies)")
        for de in ctx["dead_ends"]:
            lines.append(f"  - {de}")

    if ctx["promising_strategies"]:
        lines.append(f"\n## Promising directions (build on these)")
        for ps in ctx["promising_strategies"]:
            lines.append(f"  - {ps['name']}: {ps['description']}")

    if ctx["recent_turns"]:
        lines.append(f"\n## Recent attempts")
        for t in ctx["recent_turns"]:
            status = "✓" if t["result"] == "verified" else "✗" if not t["promising"] else "~"
            diag = "; ".join(t["diagnostics"][:2]) if t["diagnostics"] else "no diagnostics"
            lines.append(f"  [{status}] Turn {t['turn']}: {t['strategy']} → {t['result']} ({diag})")
            if t.get("subgoals"):
                for sg in t["subgoals"][:1]:
                    lines.append(f"      Remaining goal: {sg[:200]}")

    if ctx["lemmas_found"]:
        lines.append(f"\n## Useful lemmas found")
        for l in ctx["lemmas_found"]:
            lines.append(f"  - {l['name']}: {l['statement']}")

    return "\n".join(lines)


def _parse_structured_response(raw: str) -> dict:
    """Parse the planner's structured text response into a dict.

    Handles various formats: the expected STRATEGY/TACTICS format,
    JSON objects, or falls back to extracting what we can from free text.
    """
    # Strip thinking tags
    raw = re.sub(r"<think>[\s\S]*?</think>", "", raw).strip()

    # Try JSON first (in case the model outputs it anyway)
    json_match = re.search(r"\{[\s\S]*\}", raw)
    if json_match:
        try:
            plan = json.loads(json_match.group(0))
            if "strategy_name" in plan or "suggested_tactics" in plan:
                return plan
        except json.JSONDecodeError:
            pass

    # Parse structured text format
    plan: dict[str, Any] = {}

    # Extract ACTION:
    m = re.search(r"ACTION:\s*(.+?)(?:\n|$)", raw)
    action = m.group(1).strip().upper() if m else "PROVE"
    valid_actions = {"PROVE", "INVESTIGATE", "DECOMPOSE", "RESEARCH", "SIMPLIFY"}
    plan["action"] = action if action in valid_actions else "PROVE"

    # Extract STRATEGY:
    m = re.search(r"STRATEGY:\s*(.+?)(?:\n|$)", raw)
    plan["strategy_name"] = m.group(1).strip() if m else ""

    # Extract DESCRIPTION:
    m = re.search(r"DESCRIPTION:\s*(.+?)(?:\n(?:SEARCH|WEB_SEARCH|TACTICS|REASONING):|$)", raw, re.DOTALL)
    plan["strategy_description"] = m.group(1).strip()[:500] if m else ""

    # Extract SEARCH:
    m = re.search(r"SEARCH:\s*(.+?)(?:\n|$)", raw)
    if m:
        q = m.group(1).strip()
        plan["search_queries"] = [q] if q and q.lower() != "none" else []
    else:
        plan["search_queries"] = []

    # Extract WEB_SEARCH:
    m = re.search(r"WEB_SEARCH:\s*(.+?)(?:\n|$)", raw)
    if m:
        q = m.group(1).strip()
        plan["web_search_queries"] = [q] if q and q.lower() != "none" else []
    else:
        plan["web_search_queries"] = []

    # Planner should NOT produce tactics — Leanstral does that.
    # But if the planner wrote code anyway, ignore it.
    plan["suggested_tactics"] = ""

    # Extract REASONING:
    m = re.search(r"REASONING:\s*(.+?)$", raw, re.DOTALL)
    plan["reasoning"] = m.group(1).strip()[:300] if m else ""

    # If we got no strategy name, use a generic one from the description
    if not plan["strategy_name"] and plan["strategy_description"]:
        plan["strategy_name"] = plan["strategy_description"][:50]

    return plan


CREATIVITY_SYSTEM = """\
You are a creative mathematical problem-solver and proof strategist. \
Your job is NOT to write Lean code — it's to think deeply and laterally \
about how a theorem might be proved.

You bring fresh perspectives by:
1. DECOMPOSING: Can the problem be split into simpler sub-problems?
2. ANALOGIES: What similar theorems exist? What proof techniques worked for those?
3. REFRAMING: Can the statement be reformulated in an equivalent but easier-to-prove way?
4. CONNECTIONS: What areas of mathematics connect to this problem? Group theory, \
   number theory, combinatorics — what bridges exist?
5. SIMPLIFICATION: Is there a known one-liner in Mathlib that solves this directly? \
   What's the laziest possible proof?
6. OBSTACLES: Why have previous approaches failed? What's the REAL blocker — is it \
   a type mismatch, a missing conversion, a wrong lemma, or a fundamental approach problem?

Think like a mathematician who has seen thousands of proofs. What patterns apply here?

Respond with 2-4 IDEAS, each in this format:

IDEA: <short title>
INSIGHT: <the key mathematical or technical insight, 2-4 sentences>
SEARCH: <a mathlib search query that might find relevant lemmas, or NONE>
"""


def creative_brainstorm(session_id: str) -> list[dict]:
    """Ask the creativity agent for fresh ideas about the proof problem.

    Called periodically (every N turns) or when the system is stuck.
    Returns a list of ideas that get stored and fed to the planner.
    """
    ctx = build_context(session_id)

    if ctx["status"] == "verified":
        return []

    # Build a richer context for creative thinking
    lines = [
        f"## Theorem to prove\n{ctx['problem']}",
        f"\n## Formal Lean 4 statement\n{ctx['lean_statement']}",
        f"\n## Attempts so far: {ctx['total_turns']} turns",
    ]

    if ctx["dead_ends"]:
        lines.append(f"\n## What has NOT worked ({len(ctx['dead_ends'])} dead ends)")
        for de in ctx["dead_ends"][:10]:
            lines.append(f"  - {de}")

    if ctx.get("lessons"):
        lines.append(f"\n## What we've learned")
        for lesson in ctx["lessons"][:10]:
            lines.append(f"  - {lesson}")

    if ctx["best_partial_proof"]:
        lines.append(f"\n## Closest attempt so far\n{ctx['best_partial_proof'][:800]}")

    if ctx["lemmas_found"]:
        lines.append(f"\n## Lemmas discovered in Mathlib")
        for l in ctx["lemmas_found"][:10]:
            lines.append(f"  - {l['name']}: {l['statement']}")

    if ctx["promising_strategies"]:
        lines.append(f"\n## Promising directions that showed partial success")
        for ps in ctx["promising_strategies"][:5]:
            lines.append(f"  - {ps['name']}: {ps['description']}")

    lines.append(
        f"\n## Your task\n"
        f"Think creatively about how to prove this. The system has tried "
        f"{ctx['total_turns']} approaches and is stuck. What fresh angles, "
        f"connections, or simplifications might break through?"
    )

    prompt = "\n".join(lines)
    log.info("creativity_prompt", session_id=session_id, prompt_len=len(prompt))

    raw, reasoning = _call_llm(CREATIVITY_SYSTEM, prompt, model=PLANNER_MODEL)
    log.info("creativity_response", session_id=session_id, response_len=len(raw))

    if reasoning:
        from services.agent.db import emit_event
        emit_event(session_id, "creativity_thinking", {
            "reasoning": reasoning[:5000],
        })

    # Parse ideas
    raw = re.sub(r"<think>[\s\S]*?</think>", "", raw).strip()
    ideas = []
    idea_blocks = re.split(r"\nIDEA:\s*", "\n" + raw)
    for block in idea_blocks[1:]:  # skip text before first IDEA
        idea: dict = {}
        # Title is the first line
        title_match = re.match(r"(.+?)(?:\n|$)", block)
        idea["title"] = title_match.group(1).strip()[:100] if title_match else "Untitled"

        # Extract INSIGHT
        m = re.search(r"INSIGHT:\s*(.+?)(?:\nSEARCH:|$)", block, re.DOTALL)
        idea["insight"] = m.group(1).strip()[:500] if m else ""

        # Extract SEARCH
        m = re.search(r"SEARCH:\s*(.+?)(?:\n|$)", block)
        q = m.group(1).strip() if m else ""
        idea["search_query"] = q if q.upper() != "NONE" else ""

        if idea["insight"]:
            ideas.append(idea)

    log.info("creativity_ideas", session_id=session_id, count=len(ideas))

    # Store ideas as events and as lessons for the planner
    from services.agent.db import emit_event, log_lesson
    emit_event(session_id, "creativity_ideas", {
        "ideas": [{"title": i["title"], "insight": i["insight"][:200]} for i in ideas],
    })

    for idea in ideas[:4]:
        log_lesson(
            session_id,
            f"Creative idea — {idea['title']}: {idea['insight'][:300]}",
            category="creative",
        )

    return ideas


def plan_next_step(session_id: str) -> dict:
    """Query MongoDB for session context, ask the LLM for the next strategy."""
    ctx = build_context(session_id)

    if ctx["status"] == "verified":
        return {"strategy_name": "DONE", "reasoning": "Proof already verified"}

    prompt = _format_context_for_prompt(ctx)
    log.info("planner_prompt", session_id=session_id, prompt_len=len(prompt))

    raw, reasoning = _call_llm(PLANNER_SYSTEM_PROMPT, prompt, model=PLANNER_MODEL)
    log.info("planner_response", session_id=session_id, response_len=len(raw), reasoning_len=len(reasoning))

    # Emit the reasoning trace so the dashboard can show it
    if reasoning:
        from services.agent.db import emit_event
        emit_event(session_id, "planner_thinking", {
            "reasoning": reasoning[:5000],
        })

    plan = _parse_structured_response(raw)

    # If we still have no strategy or tactics, fall back
    if not plan.get("strategy_name") and not plan.get("suggested_tactics"):
        log.warning("planner_parse_failed", raw=raw[:300])
        plan = {
            "strategy_name": "fallback_generic",
            "strategy_description": "Planner output could not be parsed. Trying generic tactics.",
            "search_queries": [ctx["problem"][:80]],
            "web_search_queries": [],
            "suggested_tactics": "exact?",
            "reasoning": "Fallback due to parse failure",
        }
    else:
        log.info("planner_parsed", strategy=plan.get("strategy_name", "?"))

    return plan


LEAN_AGENT_SYSTEM = """\
You are a Lean 4 proof engineer. You write short, correct Lean 4 tactic proofs.

RULES:
1. Use ONLY lemmas listed in the comments — they are real and exist in Mathlib.
2. Use the EXACT fully-qualified name from the comments (e.g., `IsPGroup.commutative_of_card_eq_prime_sq`, NOT `commutative_of_card_eq_prime_sq`).
3. Prefer short proofs (1-5 lines) using exact/apply with existing lemmas.
4. Do NOT reprove what Mathlib already provides.
5. Do NOT use introN (not valid in Lean 4). Use `intro a b c`.
6. Output ONLY the complete Lean 4 file. No explanation, no markdown.
7. Add specific imports if lemmas come from modules beyond Mathlib.Tactic (e.g., `import Mathlib.GroupTheory.PGroup`).
8. You CAN use `exact?` as a tactic when unsure of the exact term. Lean will search for a matching lemma. This is especially useful for the final step.

CRITICAL INDENTATION RULES:
- For simple have statements, use ONE LINE: `have h : T := by rw [x]; exact y`
- Do NOT use multi-line by blocks for simple conversions.
- If you must use a multi-line by block, the NEXT tactic after it must be BACK at 2-space indent.
- WRONG (causes 'No goals to be solved'):
    have h : T := by
      exact foo
      exact bar   -- THIS IS INSIDE THE BY BLOCK — WRONG
  - CORRECT:
    have h : T := by exact foo
    exact bar     -- THIS IS A SIBLING — CORRECT
  - ALSO CORRECT:
    have h : T := by
      exact foo
    exact bar     -- BACK TO 2-SPACE INDENT
"""


def synthesize_tactics(
    theorem_statement: str,
    strategy: str = "",
    hints: str = "",
    session_id: str = "",
    lemmas: list[dict] | None = None,
    lessons: list[str] | None = None,
) -> tuple[str, str]:
    """Call qwen3.5-122b (the lean agent) to write a proof.

    Builds a Lean file with imports + commented lemma signatures from
    retrieval + theorem with sorry. The model reads the lemma signatures
    and uses them directly. Session lessons are included so the model
    doesn't repeat known mistakes.
    """
    # Build the Lean file with context
    # Start with Mathlib.Tactic only — the model adds specific imports if needed.
    # Don't auto-add from retrieval modules — some may not be built in the container.
    lines = ["import Mathlib.Tactic", ""]
    if lessons:
        lines.append("-- CONSTRAINTS (hard rules from previous attempts — OBEY THESE):")
        for lesson in lessons[:8]:
            lines.append(f"-- ! {lesson[:200]}")
        lines.append("")
    if strategy:
        lines.append(f"-- Proof strategy: {strategy}")
        lines.append("")
    if lemmas:
        lines.append("-- The following lemmas exist in Mathlib and SHOULD be used:")
        lines.append("-- Use the EXACT fully-qualified names shown here.")
        lines.append("-- Add 'import <module>' if needed (module shown in brackets).")
        for lem in (lemmas or [])[:10]:
            name = lem.get("name", "")
            stmt = lem.get("statement", "")  # Full signature, no truncation
            mod = lem.get("module", "")
            mod_note = f"  [from {mod}]" if mod else ""
            lines.append(f"-- {name} : {stmt}{mod_note}")
        lines.append("")
    lines.append(f"{theorem_statement} := by")
    lines.append("  sorry")
    lines.append("")
    lean_file = "\n".join(lines)

    user = (
        f"Replace the sorry with a correct Lean 4 proof. "
        f"Use the lemmas listed in the comments. Keep it short.\n\n"
        f"{lean_file}"
    )

    try:
        content, reasoning = _call_llm(LEAN_AGENT_SYSTEM, user, model=PLANNER_MODEL)
        if reasoning and session_id:
            from services.agent.db import emit_event
            emit_event(session_id, "synthesize_thinking", {
                "reasoning": reasoning[:5000],
            })
        return content, reasoning
    except Exception as e:
        log.error("synthesize_failed", error=str(e))
        return "exact?", ""


def repair_tactics(
    theorem_statement: str,
    failed_tactics: str,
    diagnostics: list[str],
    session_id: str = "",
    strategy: str = "",
    lessons: list[str] | None = None,
) -> tuple[str, str]:
    """Call qwen3.5-122b to fix a failed proof based on Lean compiler errors.

    Sends the failed tactics + error messages + strategy context + lessons
    so the repair agent can make targeted fixes instead of blind rewrites.
    Returns (repaired_tactics, reasoning).
    """
    if not LEANSTRAL_API_MODEL:
        return "exact?", ""

    # Give Leanstral the failed file with full context
    lines = [
        "import Mathlib.Tactic",
        "",
    ]
    if lessons:
        lines.append("-- CONSTRAINTS (hard rules — OBEY THESE):")
        for lesson in (lessons or [])[:6]:
            lines.append(f"-- ! {lesson[:200]}")
        lines.append("")
    if strategy:
        lines.append(f"-- Intended strategy: {strategy[:300]}")
        lines.append("")
    lines.append("-- COMPILER ERRORS from previous attempt:")
    for d in diagnostics[:5]:
        lines.append(f"-- ERROR: {d}")
    lines.append("")
    lines.append(f"{theorem_statement} := by")
    # Put the failed tactics as commented-out reference
    lines.append("  -- Previous attempt (failed):")
    for tac_line in failed_tactics[:1000].split("\n")[:15]:
        if tac_line.strip():
            lines.append(f"  -- {tac_line.strip()}")
    lines.append("  sorry  -- Replace with corrected proof")
    lines.append("")
    lean_file = "\n".join(lines)

    user = (
        "The sorry needs a corrected proof. The previous attempt and its errors "
        "are shown as comments. Fix the errors and replace sorry. "
        "Keep the intended strategy in mind. "
        "Output ONLY the complete Lean 4 file.\n\n"
        f"{lean_file}"
    )

    try:
        content, reasoning = _call_llm(LEAN_AGENT_SYSTEM, user, model=PLANNER_MODEL)
        if reasoning and session_id:
            from services.agent.db import emit_event
            emit_event(session_id, "repair_thinking", {
                "reasoning": reasoning[:5000],
            })
        return content, reasoning
    except Exception as e:
        log.error("repair_failed", error=str(e))
        return "exact?", ""


DIAGNOSIS_SYSTEM = """\
You are a Lean 4 debugging expert. You analyze failed proof attempts to \
understand WHY they failed and extract actionable lessons.

Given a failed Lean 4 proof and its compiler errors, you must:
1. Identify the ROOT CAUSE — not just restate the error message.
2. Check if a lemma was used with wrong types (return type vs goal type).
3. Check if an import is missing or invalid.
4. Check if indentation caused scope errors.
5. Suggest a SPECIFIC fix.

Respond in EXACTLY this format:
ROOT_CAUSE: <one sentence explaining why this specific proof failed>
FIX: <specific actionable fix, e.g. "use commGroupOfCardEqPrimeSq instead of \
commutative_of_card_eq_prime_sq because the goal is IsAbelian G, not ∀ a b, a * b = b * a">
LESSON: <a reusable lesson for future attempts, or NONE if this was a one-off typo>
"""


def diagnose_failure(
    lean_source: str,
    diagnostics: list[str],
    lemma_signatures: list[dict] | None = None,
    session_id: str = "",
    strategy: str = "",
) -> dict:
    """Ask the LLM to analyze WHY a proof attempt failed.

    Returns a dict with root_cause, fix, and lesson fields.
    This is the agent's ability to understand its own errors rather
    than blindly retrying.
    """
    lines = []
    if strategy:
        lines.append(f"## Intended strategy\n{strategy[:300]}")
        lines.append("")
    lines.extend([
        "## Failed Lean 4 proof",
        lean_source[:2000],
        "",
        "## Compiler errors",
    ])
    for d in diagnostics[:5]:
        lines.append(f"  - {d}")

    if lemma_signatures:
        lines.append("")
        lines.append("## Available lemma signatures (from Mathlib)")
        for lem in lemma_signatures[:5]:
            name = lem.get("name", "")
            stmt = lem.get("statement", "")
            lines.append(f"  {name} : {stmt}")

    user = "\n".join(lines)

    try:
        raw, reasoning = _call_llm(DIAGNOSIS_SYSTEM, user, model=PLANNER_MODEL)

        if reasoning and session_id:
            from services.agent.db import emit_event
            emit_event(session_id, "diagnosis_thinking", {
                "reasoning": reasoning[:3000],
            })

        # Parse structured response
        result = {}
        for field in ["ROOT_CAUSE", "FIX", "LESSON"]:
            m = re.search(rf"{field}:\s*(.+?)(?:\n[A-Z_]+:|$)", raw, re.DOTALL)
            result[field.lower()] = m.group(1).strip()[:300] if m else ""

        log.info("diagnosis_complete",
                 session_id=session_id,
                 root_cause=result.get("root_cause", "")[:80])
        return result

    except Exception as e:
        log.error("diagnosis_failed", error=str(e))
        return {"root_cause": "", "fix": "", "lesson": ""}
