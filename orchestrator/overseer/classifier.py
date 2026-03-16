"""Haiku-tier classification functions for the overseer agent.

Each function builds a focused prompt, calls the LLM via
``egg_agent.client.run_agent_async``, and parses the response into a
structured result dict.  Results are cached by input hash to avoid
re-analyzing identical data.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from egg_agent.client import run_agent_async

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_cache: dict[str, Any] = {}


def _cache_key(*parts: Any) -> str:
    """Compute a deterministic cache key from arbitrary inputs."""
    raw = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def clear_cache() -> None:
    """Clear the classifier result cache."""
    _cache.clear()


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

HAIKU_MODEL = "haiku"


async def _call_classifier(prompt: str, context: str) -> str:
    """Call the LLM classifier and return the raw response text.

    Uses Haiku with ``max_turns=1`` for fast, single-shot classification.
    The caller is responsible for parsing the response.
    """
    full_prompt = f"{prompt}\n\n---\nContext:\n{context}"
    result = await run_agent_async(
        full_prompt,
        model=HAIKU_MODEL,
        max_turns=1,
    )
    if not result.success:
        raise RuntimeError(f"Classifier call failed: {result.error}")
    return result.stdout.strip()


def _parse_json_or_fallback(raw: str, fallback: dict[str, Any]) -> dict[str, Any]:
    """Try to parse *raw* as JSON; return *fallback* on failure."""
    try:
        return json.loads(raw)  # type: ignore[no-any-return]
    except (json.JSONDecodeError, TypeError):
        pass
    # Try extracting a JSON block from markdown fences
    if "```" in raw:
        for block in raw.split("```"):
            block = block.strip()
            if block.startswith("json"):
                block = block[4:].strip()
            if block.startswith("{"):
                try:
                    return json.loads(block)  # type: ignore[no-any-return]
                except (json.JSONDecodeError, TypeError):
                    pass
    return fallback


# ---------------------------------------------------------------------------
# Public classifier functions
# ---------------------------------------------------------------------------


async def classify_stall(logs: list[dict], progress: list[dict]) -> dict:
    """Classify whether an agent is stuck or doing legitimate work.

    Args:
        logs: Recent log entries from the agent.
        progress: Recent progress events from the agent.

    Returns:
        A dict with keys:
            classification: ``"stuck"`` | ``"working"`` | ``"needs_help"``
            confidence: float between 0.0 and 1.0
            reasoning: str explaining the classification
    """
    key = _cache_key("classify_stall", logs, progress)
    if key in _cache:
        return _cache[key]  # type: ignore[no-any-return]

    prompt = (
        "You are a pipeline health classifier. Analyze the following agent "
        "logs and progress events to determine if the agent is stuck, doing "
        "legitimate work, or needs help.\n\n"
        "Respond with ONLY a JSON object (no markdown fences) with these keys:\n"
        '  "classification": one of "stuck", "working", "needs_help"\n'
        '  "confidence": float between 0.0 and 1.0\n'
        '  "reasoning": brief explanation\n'
    )
    context = json.dumps({"logs": logs, "progress": progress}, default=str)

    raw = await _call_classifier(prompt, context)
    result = _parse_json_or_fallback(
        raw,
        {"classification": "working", "confidence": 0.5, "reasoning": raw},
    )

    _cache[key] = result
    return result


async def classify_error(error_context: dict) -> dict:
    """Classify the severity and type of an error.

    Args:
        error_context: Dict describing the error (message, code, traceback, etc.).

    Returns:
        A dict with keys:
            error_type: str describing the category of error
            severity: ``"low"`` | ``"medium"`` | ``"high"`` | ``"critical"``
            recommended_action: str with a suggested next step
    """
    key = _cache_key("classify_error", error_context)
    if key in _cache:
        return _cache[key]  # type: ignore[no-any-return]

    prompt = (
        "You are a pipeline error classifier. Analyze the following error "
        "context and classify its severity and type.\n\n"
        "Respond with ONLY a JSON object (no markdown fences) with these keys:\n"
        '  "error_type": string category (e.g. "timeout", "oom", "auth_failure", "test_failure")\n'
        '  "severity": one of "low", "medium", "high", "critical"\n'
        '  "recommended_action": brief suggestion for remediation\n'
    )
    context = json.dumps(error_context, default=str)

    raw = await _call_classifier(prompt, context)
    result = _parse_json_or_fallback(
        raw,
        {"error_type": "unknown", "severity": "medium", "recommended_action": raw},
    )

    _cache[key] = result
    return result


async def detect_loop(recent_actions: list[dict]) -> dict:
    """Detect if an agent is in a repetitive action loop.

    Args:
        recent_actions: List of recent actions/tool calls from the agent.

    Returns:
        A dict with keys:
            is_loop: bool
            loop_pattern: str | None describing the repeated pattern
            confidence: float between 0.0 and 1.0
    """
    key = _cache_key("detect_loop", recent_actions)
    if key in _cache:
        return _cache[key]  # type: ignore[no-any-return]

    prompt = (
        "You are a pipeline action analyzer. Examine the following sequence "
        "of agent actions and determine if the agent is stuck in a "
        "repetitive loop.\n\n"
        "Respond with ONLY a JSON object (no markdown fences) with these keys:\n"
        '  "is_loop": boolean\n'
        '  "loop_pattern": string describing the repeated pattern, or null if no loop\n'
        '  "confidence": float between 0.0 and 1.0\n'
    )
    context = json.dumps({"recent_actions": recent_actions}, default=str)

    raw = await _call_classifier(prompt, context)
    result = _parse_json_or_fallback(
        raw,
        {"is_loop": False, "loop_pattern": None, "confidence": 0.5},
    )
    # Normalize is_loop to bool
    if isinstance(result.get("is_loop"), str):
        result["is_loop"] = result["is_loop"].lower() in ("true", "yes", "1")

    _cache[key] = result
    return result


async def check_alignment(activity: list[dict], contract: dict) -> dict:
    """Check if agent activity aligns with assigned contract tasks.

    Args:
        activity: List of recent agent activities (tool calls, file edits, etc.).
        contract: The contract or task description the agent is assigned to.

    Returns:
        A dict with keys:
            aligned: bool
            concerns: list[str] of alignment concerns
            suggested_redirect: str | None with a suggested course correction
    """
    key = _cache_key("check_alignment", activity, contract)
    if key in _cache:
        return _cache[key]  # type: ignore[no-any-return]

    prompt = (
        "You are a pipeline task alignment checker. Compare the agent's "
        "recent activity against its assigned contract/tasks and determine "
        "if the agent is working on the right things.\n\n"
        "Respond with ONLY a JSON object (no markdown fences) with these keys:\n"
        '  "aligned": boolean\n'
        '  "concerns": list of strings describing any alignment concerns\n'
        '  "suggested_redirect": string with a course correction suggestion, or null\n'
    )
    context = json.dumps({"activity": activity, "contract": contract}, default=str)

    raw = await _call_classifier(prompt, context)
    result = _parse_json_or_fallback(
        raw,
        {"aligned": True, "concerns": [], "suggested_redirect": None},
    )

    _cache[key] = result
    return result
