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
You are a Lean 4 theorem proving strategist. You propose proof STRATEGIES \
in natural language. You do NOT write Lean code — a specialized Lean model \
will generate the code based on your strategy.

RULES:
1. Read the TECHNICAL LESSONS section. These are HARD FACTS.
2. Read the DEAD ENDS section. Do NOT retry those approaches.
3. Propose something NEW that has NOT been tried.
4. If a promising direction exists, BUILD ON IT.
5. Do NOT write Lean tactics or code. Describe the approach in natural language.
6. Be specific about which mathlib lemmas to use and what proof structure to follow.

Respond in EXACTLY this format:

STRATEGY: <short name, 3-8 words>
DESCRIPTION: <detailed description of the proof approach — which lemmas to use, \
what case splits to make, what induction scheme to follow, etc. 2-5 sentences.>
SEARCH: <mathlib search query to find relevant lemmas>
WEB_SEARCH: <optional web search query, or NONE>
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


def synthesize_tactics(
    theorem_statement: str,
    strategy: str = "",
    hints: str = "",
    session_id: str = "",
) -> tuple[str, str]:
    """Call Leanstral to generate Lean 4 tactics based on a strategy.

    This is the ONLY place Lean code should be generated. The planner
    proposes strategies in natural language; Leanstral writes the code.
    Returns (tactics, reasoning).
    """
    if not LEANSTRAL_API_MODEL:
        return "exact?", ""

    # Give Leanstral a complete Lean 4 file to complete — this is how it's designed to work
    user = f"Complete the following Lean 4 proof:\n\n"
    user += f"```lean4\nimport Mathlib.Tactic\n\n{theorem_statement} := by\n  sorry\n```\n\n"
    if strategy:
        user += f"Proof strategy: {strategy}\n\n"
    if hints:
        user += f"Relevant mathlib lemmas that may help:\n{hints}\n\n"
    user += "Replace the sorry with a complete tactic proof. Output the full proof as Lean 4 code."

    try:
        content, reasoning = _call_leanstral(user)
        if reasoning and session_id:
            from services.agent.db import emit_event
            emit_event(session_id, "synthesize_thinking", {
                "reasoning": reasoning[:3000],
            })
        # Leanstral returns full Lean code — the runner's _clean_leanstral_output
        # will extract just the tactics from the complete proof
        return content, reasoning
    except Exception as e:
        log.error("synthesize_failed", error=str(e))
        return "exact?", ""


def repair_tactics(
    theorem_statement: str,
    failed_tactics: str,
    diagnostics: list[str],
    session_id: str = "",
) -> tuple[str, str]:
    """Call Leanstral to fix a failed proof based on Lean compiler errors.

    Sends the failed tactics + error messages to Leanstral for repair.
    Returns (repaired_tactics, reasoning).
    """
    if not LEANSTRAL_API_MODEL:
        return "exact?", ""

    # Give Leanstral the failed proof with errors and ask for a complete new proof
    user = f"The following Lean 4 proof failed to compile:\n\n"
    user += f"```lean4\nimport Mathlib.Tactic\n\n{theorem_statement} := by\n"
    for line in failed_tactics[:1500].split("\n"):
        user += f"  {line}\n"
    user += "```\n\n"
    user += "Compiler errors:\n"
    for d in diagnostics[:5]:
        user += f"  - {d}\n"
    user += "\nWrite a corrected complete proof. Fix the errors above. Output the full Lean 4 code."

    try:
        content, reasoning = _call_leanstral(user)
        if reasoning and session_id:
            from services.agent.db import emit_event
            emit_event(session_id, "repair_thinking", {
                "reasoning": reasoning[:3000],
            })
        return content, reasoning
    except Exception as e:
        log.error("repair_failed", error=str(e))
        return "exact?", ""
