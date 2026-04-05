"""LLM client abstraction for the orchestrator.

Provides async helpers to call Nemotron (orchestrator planning) and
Leanstral (proof synthesis) via OpenAI-compatible chat-completion endpoints.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import httpx
import structlog

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# Paths to model config files (relative to repo root)
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
_NEMOTRON_CFG_PATH = _REPO_ROOT / "config" / "models" / "nemotron.json"
_LEANSTRAL_CFG_PATH = _REPO_ROOT / "config" / "models" / "leanstral.json"


def _load_json(path: Path) -> dict[str, Any]:
    """Load a JSON config file, returning {} on failure."""
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        log.warning("config_load_failed", path=str(path), error=str(exc))
        return {}


_nemotron_cfg: dict[str, Any] = _load_json(_NEMOTRON_CFG_PATH)
_leanstral_cfg: dict[str, Any] = _load_json(_LEANSTRAL_CFG_PATH)


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def nemotron_is_configured() -> bool:
    """Return True if the Nemotron API key and base URL are set."""
    return bool(
        os.getenv("NEMOTRON_API_KEY", "")
        and os.getenv("NEMOTRON_API_BASE", "")
    )


def leanstral_is_configured() -> bool:
    """Return True if the Leanstral API key and base URL are set."""
    return bool(
        os.getenv("LEANSTRAL_API_KEY", "")
        and os.getenv("LEANSTRAL_API_BASE", "")
    )


# ---------------------------------------------------------------------------
# Default / fallback responses
# ---------------------------------------------------------------------------

_DEFAULT_NEMOTRON_PLAN = json.dumps({
    "next_action": "synthesize",
    "reasoning": "LLM not configured; defaulting to synthesize.",
})

_DEFAULT_LEANSTRAL_TACTICS: list[str] = [
    "exact?",
    "simp",
    "ring",
    "omega",
    "aesop",
    "decide",
    "norm_num",
    "linarith",
]


# ---------------------------------------------------------------------------
# Nemotron (orchestrator planner)
# ---------------------------------------------------------------------------

async def call_nemotron(system_prompt: str, user_prompt: str) -> str:
    """Call the Nemotron orchestrator LLM and return the generated text.

    If the API is not configured, returns a sensible default JSON plan.
    """
    if not nemotron_is_configured():
        log.info("nemotron_not_configured, returning default plan")
        return _DEFAULT_NEMOTRON_PLAN

    api_key = os.environ["NEMOTRON_API_KEY"]
    api_base = os.environ["NEMOTRON_API_BASE"].rstrip("/")
    model_id = _nemotron_cfg.get("model_id", "nvidia/llama-3.3-nemotron-super-49b-v1")
    max_tokens = _nemotron_cfg.get("max_tokens", 4096)
    temperature = _nemotron_cfg.get("temperature", 0.3)
    top_p = _nemotron_cfg.get("top_p", 0.95)

    url = f"{api_base}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
    }

    log.info(
        "nemotron_request",
        url=url,
        model=model_id,
        user_prompt_len=len(user_prompt),
    )

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            log.info("nemotron_response", content_len=len(content))
            return content
    except Exception as exc:
        log.error("nemotron_call_failed", error=str(exc))
        return _DEFAULT_NEMOTRON_PLAN


# ---------------------------------------------------------------------------
# Leanstral (proof synthesis)
# ---------------------------------------------------------------------------

def _parse_candidates(raw: str) -> list[str]:
    """Extract multiple candidate proof bodies from a Leanstral response.

    The model may return proofs separated by ``---``, numbered lists, or
    markdown code fences. We split on common delimiters and return unique,
    non-empty candidates.
    """
    # Strip markdown code fences
    raw = re.sub(r"```lean4?\s*", "", raw)
    raw = re.sub(r"```", "", raw)

    # Try splitting on common delimiters
    # 1. Triple-dash separator
    if "\n---" in raw:
        parts = raw.split("\n---")
    # 2. Numbered candidates like "1." "2." at line start
    elif re.search(r"^\d+\.\s", raw, re.MULTILINE):
        parts = re.split(r"^\d+\.\s", raw, flags=re.MULTILINE)
    # 3. "-- candidate" or "-- proof" headers
    elif re.search(r"^--\s*(candidate|proof|option)", raw, re.MULTILINE | re.IGNORECASE):
        parts = re.split(r"^--\s*(candidate|proof|option)\s*\d*\s*", raw, flags=re.MULTILINE | re.IGNORECASE)
    else:
        # Treat the whole response as a single candidate
        parts = [raw]

    candidates: list[str] = []
    for part in parts:
        # Strip the leading "by" keyword if present (we add it ourselves)
        cleaned = part.strip()
        if cleaned.lower().startswith("by"):
            cleaned = cleaned[2:].strip()
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)

    return candidates if candidates else _DEFAULT_LEANSTRAL_TACTICS[:]


async def call_leanstral(prompt: str) -> list[str]:
    """Call Leanstral for proof synthesis, returning candidate proofs.

    If the API is not configured, returns generic tactic fallbacks.
    """
    if not leanstral_is_configured():
        log.info("leanstral_not_configured, returning default tactics")
        return _DEFAULT_LEANSTRAL_TACTICS[:]

    api_key = os.environ["LEANSTRAL_API_KEY"]
    api_base = os.environ["LEANSTRAL_API_BASE"].rstrip("/")
    model_id = _leanstral_cfg.get("model_id", "leanstral-v1")
    max_tokens = _leanstral_cfg.get("max_tokens", 2048)
    temperature = _leanstral_cfg.get("temperature", 0.6)
    top_p = _leanstral_cfg.get("top_p", 0.95)

    url = f"{api_base}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model_id,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a Lean 4 proof-synthesis assistant. "
                    "Produce tactic-mode proofs. Return multiple candidates "
                    "separated by '---' if you see several viable approaches."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
    }

    log.info(
        "leanstral_request",
        url=url,
        model=model_id,
        prompt_len=len(prompt),
    )

    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            log.info("leanstral_response", content_len=len(content))
            candidates = _parse_candidates(content)
            log.info("leanstral_candidates", count=len(candidates))
            return candidates
    except Exception as exc:
        log.error("leanstral_call_failed", error=str(exc))
        return _DEFAULT_LEANSTRAL_TACTICS[:]
