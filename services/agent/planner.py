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

# Simpler prompt that doesn't require JSON — just structured text
PLANNER_SYSTEM_PROMPT = """\
You are a Lean 4 theorem proving strategist. You propose proof strategies.

RULES:
1. Read the TECHNICAL LESSONS section. These are HARD FACTS. Do NOT violate them.
2. Read the DEAD ENDS section. Do NOT retry those approaches.
3. Propose something NEW that has NOT been tried.
4. If a promising direction exists, BUILD ON IT.

Respond in EXACTLY this format (keep the labels, fill in the values):

STRATEGY: <short name, 3-8 words>
DESCRIPTION: <what to try and why, 1-3 sentences>
SEARCH: <mathlib search query>
WEB_SEARCH: <optional web search query, or NONE>
TACTICS:
<the tactic-mode proof body — ONLY tactics, no imports, no theorem declaration>
END_TACTICS
REASONING: <why this might work, 1-2 sentences>
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
                "max_tokens": 65536,
                "temperature": 0.4,
            },
        )
        resp.raise_for_status()
        msg = resp.json()["choices"][0]["message"]
        content = msg.get("content") or msg.get("reasoning_content") or ""
        return content


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

    # Extract TACTICS block
    m = re.search(r"TACTICS:\s*\n([\s\S]*?)(?:END_TACTICS|REASONING:|\Z)", raw)
    if m:
        tactics = m.group(1).strip()
        # Strip code fences if model wrapped them
        tactics = re.sub(r"```\w*\s*", "", tactics).strip()
        plan["suggested_tactics"] = tactics
    else:
        # Try to find any code block
        m = re.search(r"```(?:lean4?)?\s*\n([\s\S]*?)```", raw)
        plan["suggested_tactics"] = m.group(1).strip() if m else ""

    # Extract REASONING:
    m = re.search(r"REASONING:\s*(.+?)$", raw, re.DOTALL)
    plan["reasoning"] = m.group(1).strip()[:300] if m else ""

    # If we got nothing useful, try to salvage from free text
    if not plan["strategy_name"] and not plan["suggested_tactics"]:
        # Look for any tactic-like content
        tactic_patterns = [
            r"(by_cases\b[\s\S]*?)(?:\n\n|\Z)",
            r"(intro\b[\s\S]*?)(?:\n\n|\Z)",
            r"(induction\b[\s\S]*?)(?:\n\n|\Z)",
            r"(exact[\s?!]\b[\s\S]*?)(?:\n\n|\Z)",
            r"(simp\b[\s\S]*?)(?:\n\n|\Z)",
        ]
        for pat in tactic_patterns:
            tm = re.search(pat, raw)
            if tm:
                plan["suggested_tactics"] = tm.group(1).strip()[:500]
                plan["strategy_name"] = "extracted_from_text"
                break

    return plan


def plan_next_step(session_id: str) -> dict:
    """Query MongoDB for session context, ask the LLM for the next strategy."""
    ctx = build_context(session_id)

    if ctx["status"] == "verified":
        return {"strategy_name": "DONE", "reasoning": "Proof already verified"}

    prompt = _format_context_for_prompt(ctx)
    log.info("planner_prompt", session_id=session_id, prompt_len=len(prompt))

    raw = _call_llm(PLANNER_SYSTEM_PROMPT, prompt, model=PLANNER_MODEL)
    log.info("planner_response", session_id=session_id, response_len=len(raw))

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
