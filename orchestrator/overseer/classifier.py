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
from enum import StrEnum
from typing import Any

from agent_model_resolution import OVERSEER_TIER_MODELS
from egg_agent.client import run_agent_async
from overseer.utils import parse_json_or_fallback as _parse_json_or_fallback

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache (bounded to prevent unbounded memory growth)
# ---------------------------------------------------------------------------

_MAX_CACHE_SIZE = 256
_cache: dict[str, Any] = {}


def _cache_key(*parts: Any) -> str:
    """Compute a deterministic cache key from arbitrary inputs."""
    raw = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _cache_put(key: str, value: Any) -> None:
    """Insert into cache, evicting oldest entries if over max size."""
    if len(_cache) >= _MAX_CACHE_SIZE:
        # Evict oldest ~25% of entries (dict preserves insertion order in 3.7+)
        evict_count = _MAX_CACHE_SIZE // 4
        keys_to_evict = list(_cache.keys())[:evict_count]
        for k in keys_to_evict:
            del _cache[k]
    _cache[key] = value


def clear_cache() -> None:
    """Clear the classifier result cache."""
    _cache.clear()


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

# Classify tier (#2270 §1): the cheap, high-volume Haiku tier, sourced from the
# single overseer-tier table in ``agent_model_resolution`` rather than a local
# literal so all three tiers (classify/routine/adversarial) stay in sync.
HAIKU_MODEL = OVERSEER_TIER_MODELS["classify"]


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


# ---------------------------------------------------------------------------
# Public classifier functions
# ---------------------------------------------------------------------------


async def classify_stall(
    logs: list[dict],
    progress: list[dict],
    consensus: dict | None = None,
    container_logs: str | None = None,
) -> dict:
    """Classify whether an agent is stuck or doing legitimate work.

    Args:
        logs: Recent log entries from the agent.
        progress: Recent progress events from the agent.
        consensus: Optional current BRC consensus status from the orchestrator.
            When provided, the classifier uses this authoritative state instead
            of inferring agent status solely from progress events.
        container_logs: Optional raw Docker container logs for the agent.
            Provides additional runtime context (stderr, tracebacks, etc.)
            that may not appear in structured progress events.

    Returns:
        A dict with keys:
            classification: ``"stuck"`` | ``"working"`` | ``"needs_help"`` | ``"infrastructure_error"``
            confidence: float between 0.0 and 1.0
            reasoning: str explaining the classification
    """
    truncated_container_logs = container_logs[-8000:] if container_logs else None
    key = _cache_key("classify_stall", logs, progress, consensus, truncated_container_logs)
    if key in _cache:
        return _cache[key]  # type: ignore[no-any-return]

    consensus_instruction = ""
    if consensus:
        consensus_instruction = (
            "\n\nIMPORTANT: The 'consensus' field contains the current BRC "
            "consensus status from the orchestrator. This is authoritative — "
            "if an agent has confirmed consensus (confirmed=true), it is NOT "
            "stalled. Progress events may be stale; prefer consensus state.\n"
        )

    container_log_instruction = ""
    if container_logs:
        container_log_instruction = (
            "\n\nThe 'container_logs' field contains recent Docker container "
            "logs for this agent. Look for errors, tracebacks, repeated "
            "failures, OOM kills, or signs of infrastructure problems that "
            "may not appear in structured progress events.\n"
        )

    prompt = (
        "You are a pipeline health classifier. Analyze the following agent "
        "logs and progress events to determine if the agent is stuck, doing "
        "legitimate work, or needs help.\n\n"
        f"{consensus_instruction}"
        f"{container_log_instruction}"
        "Use 'infrastructure_error' when the agent is blocked by an infrastructure "
        "issue it cannot resolve itself (e.g., git failures, permission denied, "
        "EROFS, gateway errors, .gitignore blocking files, 403/500 HTTP errors). "
        "These require human intervention, not agent nudges.\n\n"
        "Respond with ONLY a JSON object (no markdown fences) with these keys:\n"
        '  "classification": one of "stuck", "working", "needs_help", "infrastructure_error"\n'
        '  "confidence": float between 0.0 and 1.0\n'
        '  "reasoning": brief explanation\n'
    )
    context_data: dict[str, Any] = {"logs": logs, "progress": progress}
    if consensus:
        context_data["consensus"] = consensus
    if container_logs:
        # Truncate to last 8000 chars to stay within classifier context limits
        context_data["container_logs"] = container_logs[-8000:]
    context = json.dumps(context_data, default=str)

    raw = await _call_classifier(prompt, context)
    result = _parse_json_or_fallback(
        raw,
        {"classification": "working", "confidence": 0.5, "reasoning": raw},
    )

    _cache_put(key, result)
    return result


async def classify_error(
    error_context: dict,
    container_logs: str | None = None,
) -> dict:
    """Classify the severity and type of an error.

    Args:
        error_context: Dict describing the error (message, code, traceback, etc.).
        container_logs: Optional raw Docker container logs for the agent.
            Provides additional runtime context for diagnosis.

    Returns:
        A dict with keys:
            error_type: str describing the category of error
            severity: ``"low"`` | ``"medium"`` | ``"high"`` | ``"critical"``
            recommended_action: str with a suggested next step
    """
    truncated_container_logs = container_logs[-8000:] if container_logs else None
    key = _cache_key("classify_error", error_context, truncated_container_logs)
    if key in _cache:
        return _cache[key]  # type: ignore[no-any-return]

    container_log_instruction = ""
    if container_logs:
        container_log_instruction = (
            "\n\nThe 'container_logs' field in the context contains recent "
            "Docker container logs. Use these to identify root causes such "
            "as OOM kills, segfaults, network errors, or repeated failures.\n"
        )

    prompt = (
        "You are a pipeline error classifier. Analyze the following error "
        "context and classify its severity and type.\n\n"
        f"{container_log_instruction}"
        "Classify as 'infrastructure_error' when the error is caused by "
        "infrastructure the agent cannot fix (git failures, permission denied, "
        "EROFS, gateway errors, .gitignore issues, 403/500 HTTP errors). "
        "For infrastructure errors, recommended_action should be 'escalate_hitl'.\n\n"
        "Respond with ONLY a JSON object (no markdown fences) with these keys:\n"
        '  "error_type": string category (e.g. "timeout", "oom", "auth_failure", '
        '"test_failure", "infrastructure_error")\n'
        '  "severity": one of "low", "medium", "high", "critical"\n'
        '  "recommended_action": brief suggestion for remediation\n'
    )
    enriched_context = dict(error_context)
    if container_logs:
        enriched_context["container_logs"] = container_logs[-8000:]
    context = json.dumps(enriched_context, default=str)

    raw = await _call_classifier(prompt, context)
    result = _parse_json_or_fallback(
        raw,
        {"error_type": "unknown", "severity": "medium", "recommended_action": raw},
    )

    _cache_put(key, result)
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

    _cache_put(key, result)
    return result


# ---------------------------------------------------------------------------
# Activity-pattern classification (#2059 / #2132, #2270 §2, task-7-4).
#
# ``detect_loop`` answers a single yes/no ("is the agent in a repetitive
# loop?"). #2059 / #2132 asked for the *failure modes* an agent can exhibit to
# be first-class, testable verdicts rather than an undifferentiated "stuck":
#
#   * ``thrashing``         — rapidly switching between tasks / files /
#                             approaches without finishing any (lots of context
#                             switching, repeated undo/redo, no convergence).
#   * ``spinning``          — repeating the same action (or a tight cycle of a
#                             few actions) with no state change / no progress.
#   * ``improper_tool_use`` — repeated tool errors: malformed arguments, the
#                             wrong tool for the job, ignoring tool results and
#                             re-issuing the same failing call.
#   * ``productive``        — making forward progress (the healthy default).
#
# Keeping these as an enum lets detectors and tests assert on a stable
# vocabulary instead of free-text reasoning.
# ---------------------------------------------------------------------------


class ActivityPattern(StrEnum):
    """First-class agent activity-pattern verdicts (#2059 / #2132)."""

    PRODUCTIVE = "productive"
    THRASHING = "thrashing"
    SPINNING = "spinning"
    IMPROPER_TOOL_USE = "improper_tool_use"


_ACTIVITY_PATTERN_VALUES = frozenset(p.value for p in ActivityPattern)


async def classify_activity_pattern(recent_actions: list[dict]) -> dict:
    """Classify an agent's recent activity into a first-class pattern verdict.

    Args:
        recent_actions: List of recent actions / tool calls (and their results)
            from the agent.

    Returns:
        A dict with keys:
            pattern: one of :class:`ActivityPattern`
                (``"productive"`` | ``"thrashing"`` | ``"spinning"`` |
                ``"improper_tool_use"``)
            confidence: float between 0.0 and 1.0
            reasoning: brief explanation
    """
    key = _cache_key("classify_activity_pattern", recent_actions)
    if key in _cache:
        return _cache[key]  # type: ignore[no-any-return]

    prompt = (
        "You are a pipeline activity-pattern classifier. Examine the agent's "
        "recent actions (and their results) and classify the dominant pattern "
        "into EXACTLY ONE of these first-class verdicts:\n"
        '  - "productive": making forward progress toward the task; varied, '
        "purposeful actions that build on each other.\n"
        '  - "thrashing": rapidly switching between different tasks/files/'
        "approaches without finishing any; repeated undo/redo; no "
        "convergence.\n"
        '  - "spinning": repeating the same action or a tight cycle of a few '
        "actions with no state change and no progress.\n"
        '  - "improper_tool_use": repeated tool errors — malformed arguments, '
        "wrong tool for the job, or re-issuing the same failing call while "
        "ignoring the error result.\n\n"
        "Respond with ONLY a JSON object (no markdown fences) with these keys:\n"
        '  "pattern": one of "productive", "thrashing", "spinning", '
        '"improper_tool_use"\n'
        '  "confidence": float between 0.0 and 1.0\n'
        '  "reasoning": brief explanation\n'
    )
    context = json.dumps({"recent_actions": recent_actions}, default=str)

    raw = await _call_classifier(prompt, context)
    result = _parse_json_or_fallback(
        raw,
        {
            "pattern": ActivityPattern.PRODUCTIVE.value,
            "confidence": 0.5,
            "reasoning": raw,
        },
    )
    # Coerce any out-of-vocab pattern to the safe default so the verdict set
    # stays closed and testable.
    pattern = str(result.get("pattern", "")).strip().lower()
    if pattern not in _ACTIVITY_PATTERN_VALUES:
        result["pattern"] = ActivityPattern.PRODUCTIVE.value
    else:
        result["pattern"] = pattern

    _cache_put(key, result)
    return result


async def check_decision_consistency(
    phase_output: dict,
    prior_decisions: list[dict],
) -> dict:
    """Check if phase output respects and references prior HITL decisions.

    Args:
        phase_output: Current phase contract state or output summary.
        prior_decisions: Resolved HITL decisions from prior phases.

    Returns:
        A dict with keys:
            consistent: bool — whether the output honours prior decisions
            concerns: list[str] of consistency concerns
            confidence: float between 0.0 and 1.0
    """
    key = _cache_key("check_decision_consistency", phase_output, prior_decisions)
    if key in _cache:
        return _cache[key]  # type: ignore[no-any-return]

    prompt = (
        "You are a pipeline decision-consistency checker. Compare the current "
        "phase output against prior resolved HITL decisions and determine "
        "whether the output respects and incorporates those decisions.\n\n"
        "Respond with ONLY a JSON object (no markdown fences) with these keys:\n"
        '  "consistent": boolean — true if the phase output honours all prior decisions\n'
        '  "concerns": list of strings describing any consistency issues\n'
        '  "confidence": float between 0.0 and 1.0\n'
    )
    context = json.dumps(
        {"phase_output": phase_output, "prior_decisions": prior_decisions},
        default=str,
    )

    raw = await _call_classifier(prompt, context)
    fallback = {"consistent": True, "concerns": [], "confidence": 0.5}
    result = _parse_json_or_fallback(raw, fallback)
    if result is fallback:
        logger.warning(
            "check_decision_consistency: LLM returned unparseable response, "
            "using fail-open fallback (consistent=True, confidence=0.5)"
        )

    _cache_put(key, result)
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

    _cache_put(key, result)
    return result
