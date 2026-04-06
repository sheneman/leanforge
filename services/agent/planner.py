"""Planner: asks the LLM what to try next based on session history.

The planner builds a focused prompt from MongoDB queries (not full history)
and asks the orchestrator LLM to propose the next proof strategy. It then
calls Leanstral for tactic synthesis.
"""
from __future__ import annotations

import json
import os
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

PLANNER_SYSTEM_PROMPT = """\
You are a Lean 4 theorem proving strategist. You are working on a proof that \
has not been solved yet. You have access to the history of what has been tried.

Your job:
1. Read the TECHNICAL LESSONS carefully — these are hard facts. Do NOT violate them.
2. Analyze what has been tried and what failed
3. Propose a NEW strategy that has NOT been tried before
4. Be specific: name exact mathlib lemmas, tactic sequences, or proof structures
5. If a promising direction exists, build on it rather than starting over
6. If many approaches have failed, try something fundamentally different
7. If you need external information (proof techniques, Lean 4 API docs, similar \
proofs, mathlib conventions), add web_search_queries to research it

Output a JSON object with these fields:
{
  "strategy_name": "short name for this approach",
  "strategy_description": "detailed description of what to try and why",
  "search_queries": ["mathlib search query 1", "query 2"],
  "web_search_queries": ["optional web search if you need external info, e.g. 'Lean 4 Nat.iterate unfold tactic'"],
  "suggested_tactics": "the tactic-mode proof body to try (just tactics, no imports/theorem)",
  "reasoning": "why this might work given past failures"
}

The web_search_queries field is OPTIONAL — only use it when:
- You need to find Lean 4 API documentation or syntax help
- You want to see how similar proofs are done in mathlib or other Lean projects
- Local mathlib search is not returning useful results
- You need proof strategy ideas from papers or discussions

Output ONLY the JSON object, no markdown fences, no explanation before or after.\
"""


def _call_llm(system: str, user: str, model: str | None = None) -> str:
    """Call the LLM API and return the response text."""
    model = model or LLM_API_MODEL
    if not LLM_API_BASE or not LLM_API_KEY or not model:
        raise RuntimeError("LLM_API_BASE, LLM_API_KEY, and model must be configured")

    url = f"{LLM_API_BASE}/chat/completions"
    with httpx.Client(timeout=90) as client:
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
                "max_tokens": 4096,
                "temperature": 0.4,
            },
        )
        resp.raise_for_status()
        msg = resp.json()["choices"][0]["message"]
        # Some models (e.g. Nemotron) put output in reasoning_content with content=None
        content = msg.get("content") or msg.get("reasoning_content") or ""
        return content


def _format_context_for_prompt(ctx: dict) -> str:
    """Format the DB context into a concise text prompt."""
    lines = [
        f"## Problem\n{ctx['problem']}",
        f"\n## Lean Statement\n{ctx['lean_statement']}",
        f"\n## Total turns so far: {ctx['total_turns']}",
    ]

    # Lessons go FIRST — these are hard facts the model must respect
    if ctx.get("lessons"):
        lines.append(f"\n## TECHNICAL LESSONS (ALWAYS apply these — do NOT repeat these mistakes)")
        for lesson in ctx["lessons"]:
            lines.append(f"  - {lesson}")

    if ctx["best_partial_proof"]:
        lines.append(f"\n## Best partial proof so far\n{ctx['best_partial_proof'][:1000]}")

    if ctx["dead_ends"]:
        lines.append(f"\n## Dead ends (DO NOT retry these)")
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


def plan_next_step(session_id: str) -> dict:
    """Query MongoDB for session context, ask the LLM for the next strategy.

    Returns a dict with: strategy_name, strategy_description, search_queries,
    suggested_tactics, reasoning.
    """
    ctx = build_context(session_id)

    if ctx["status"] == "verified":
        return {"strategy_name": "DONE", "reasoning": "Proof already verified"}

    prompt = _format_context_for_prompt(ctx)
    log.info("planner_prompt", session_id=session_id, prompt_len=len(prompt))

    raw = _call_llm(PLANNER_SYSTEM_PROMPT, prompt, model=PLANNER_MODEL)
    log.info("planner_response", session_id=session_id, response_len=len(raw))

    # Parse JSON from response — handle thinking tags, code fences, truncation
    raw = raw.strip()
    # Strip <think>...</think> blocks from thinking models
    import re as _re
    raw = _re.sub(r"<think>[\s\S]*?</think>", "", raw).strip()
    # Strip code fences
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
    if raw.endswith("```"):
        raw = raw[: raw.rfind("```")]
    raw = raw.strip()
    # Try to find JSON object even if there's surrounding text
    json_match = _re.search(r"\{[\s\S]*\}", raw)
    if json_match:
        raw = json_match.group(0)

    try:
        plan = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("planner_json_parse_failed", raw=raw[:500])
        plan = {
            "strategy_name": "fallback_generic",
            "strategy_description": "Try generic tactics: exact?, simp, ring, omega, aesop",
            "search_queries": [ctx["problem"][:100]],
            "suggested_tactics": "exact?",
            "reasoning": "LLM response was not valid JSON; falling back to generic tactics",
        }

    return plan


def synthesize_tactics(theorem_statement: str, hints: str = "") -> str:
    """Call Leanstral for tactic suggestions."""
    if not LEANSTRAL_API_MODEL:
        return "exact?"

    user = f"Prove: {theorem_statement}"
    if hints:
        user += f"\n\nRelevant lemmas and context:\n{hints}"
    user += "\n\nReturn ONLY the tactic body. No imports, no theorem declaration, no code fences."

    system = (
        "You are a Lean 4 proof-synthesis assistant. "
        "Produce tactic-mode proofs. Return ONLY the tactics after := by. "
        "No imports, no theorem declaration, no code fences, no explanation."
    )

    try:
        return _call_llm(system, user, model=LEANSTRAL_API_MODEL)
    except Exception as e:
        log.error("synthesize_failed", error=str(e))
        return "exact?"
