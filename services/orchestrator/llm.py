"""LLM client abstraction for the orchestrator.

Provides async helpers to call an orchestrator LLM (planning) and
a proof-synthesis LLM (Leanstral) via OpenAI-compatible chat-completion
endpoints. Provider-agnostic — configure via environment variables.

Environment variables:
    LLM_API_KEY         API key for the LLM provider
    LLM_API_BASE        Base URL (e.g. https://api.openai.com/v1)
    LLM_API_MODEL       Model ID for the orchestrator planner
    LEANSTRAL_API_MODEL Model ID for proof synthesis
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
# Paths to model config files (optional overrides)
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
_LLM_CFG_PATH = _REPO_ROOT / "config" / "models" / "nemotron.json"
_LEANSTRAL_CFG_PATH = _REPO_ROOT / "config" / "models" / "leanstral.json"


def _load_json(path: Path) -> dict[str, Any]:
    """Load a JSON config file, returning {} on failure."""
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        log.warning("config_load_failed", path=str(path), error=str(exc))
        return {}


_llm_cfg: dict[str, Any] = _load_json(_LLM_CFG_PATH)
_leanstral_cfg: dict[str, Any] = _load_json(_LEANSTRAL_CFG_PATH)


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def _get_api_key() -> str:
    return os.getenv("LLM_API_KEY", "")


def _get_api_base() -> str:
    return os.getenv("LLM_API_BASE", "").rstrip("/")


def _get_llm_model() -> str:
    """Orchestrator planner model. Env var takes precedence over config file."""
    return os.getenv("LLM_API_MODEL", _llm_cfg.get("model_id", ""))


def _get_leanstral_model() -> str:
    """Proof synthesis model. Env var takes precedence over config file."""
    return os.getenv("LEANSTRAL_API_MODEL", _leanstral_cfg.get("model_id", ""))


def llm_is_configured() -> bool:
    """Return True if the LLM API key, base URL, and orchestrator model are set."""
    return bool(_get_api_key() and _get_api_base() and _get_llm_model())


def leanstral_is_configured() -> bool:
    """Return True if the LLM API key, base URL, and synthesis model are set."""
    return bool(_get_api_key() and _get_api_base() and _get_leanstral_model())


# Keep old names as aliases for backward compatibility in orchestrator/main.py
nemotron_is_configured = llm_is_configured


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
# Orchestrator planner LLM
# ---------------------------------------------------------------------------

async def call_nemotron(system_prompt: str, user_prompt: str) -> str:
    """Call the orchestrator planner LLM and return the generated text.

    If the API is not configured, returns a sensible default JSON plan.
    """
    if not llm_is_configured():
        log.info("llm_not_configured, returning default plan")
        return _DEFAULT_NEMOTRON_PLAN

    api_key = _get_api_key()
    api_base = _get_api_base()
    model_id = _get_llm_model()
    max_tokens = _llm_cfg.get("max_tokens", 4096)
    temperature = _llm_cfg.get("temperature", 0.3)
    top_p = _llm_cfg.get("top_p", 0.95)

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

    log.info("llm_request", url=url, model=model_id, user_prompt_len=len(user_prompt))

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            msg = data["choices"][0]["message"]
            content = msg.get("content") or msg.get("reasoning_content") or ""
            log.info("llm_response", content_len=len(content))
            return content
    except Exception as exc:
        log.error("llm_call_failed", error=str(exc))
        return _DEFAULT_NEMOTRON_PLAN


# ---------------------------------------------------------------------------
# Proof synthesis LLM (Leanstral or any Lean-capable model)
# ---------------------------------------------------------------------------

def _parse_candidates(raw: str) -> list[str]:
    """Extract multiple candidate proof bodies from a synthesis response.

    Handles various LLM output formats: code fences (```lean4, ```tactics,
    ```proof, etc.), full file content (strips imports + theorem declaration),
    numbered lists, and separator-delimited candidates.
    """
    # Strip ALL code fences regardless of label
    raw = re.sub(r"```\w*\s*", "", raw)

    # If the response contains a full theorem declaration, extract just the proof body
    # Pattern: ... := by\n  <tactics>
    by_match = re.search(r":=\s*by\s*\n([\s\S]+)", raw)
    if by_match:
        raw = by_match.group(1)

    # Strip import lines and open statements that Leanstral sometimes includes
    lines = raw.split("\n")
    filtered: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("open "):
            continue
        if re.match(r"^(theorem|lemma|def|example)\s", stripped):
            continue
        if stripped.startswith("#"):  # directives like #check
            continue
        filtered.append(line)
    raw = "\n".join(filtered).strip()

    # Split into multiple candidates
    if "\n---" in raw:
        parts = raw.split("\n---")
    elif re.search(r"^\d+\.\s", raw, re.MULTILINE):
        parts = re.split(r"^\d+\.\s", raw, flags=re.MULTILINE)
    elif re.search(r"^--\s*(candidate|proof|option)", raw, re.MULTILINE | re.IGNORECASE):
        parts = re.split(r"^--\s*(candidate|proof|option)\s*\d*\s*", raw, flags=re.MULTILINE | re.IGNORECASE)
    else:
        parts = [raw]

    candidates: list[str] = []
    for part in parts:
        cleaned = part.strip()
        # Strip leading "by" keyword (we add it ourselves in _build_lean_source)
        if cleaned.lower().startswith("by\n") or cleaned.lower().startswith("by "):
            cleaned = cleaned[2:].strip()
        elif cleaned.lower() == "by":
            continue
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)

    return candidates if candidates else _DEFAULT_LEANSTRAL_TACTICS[:]


async def call_leanstral(prompt: str) -> list[str]:
    """Call the proof-synthesis LLM, returning candidate proofs.

    If the API is not configured, returns generic tactic fallbacks.
    """
    if not leanstral_is_configured():
        log.info("leanstral_not_configured, returning default tactics")
        return _DEFAULT_LEANSTRAL_TACTICS[:]

    api_key = _get_api_key()
    api_base = _get_api_base()
    model_id = _get_leanstral_model()
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

    log.info("leanstral_request", url=url, model=model_id, prompt_len=len(prompt))

    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            msg = data["choices"][0]["message"]
            content = msg.get("content") or msg.get("reasoning_content") or ""
            log.info("leanstral_response", content_len=len(content))
            candidates = _parse_candidates(content)
            log.info("leanstral_candidates", count=len(candidates))
            return candidates
    except Exception as exc:
        log.error("leanstral_call_failed", error=str(exc))
        return _DEFAULT_LEANSTRAL_TACTICS[:]
