"""Sonnet/Opus-tier decision functions for the overseer agent.

These functions take classifier output and contextual information to
produce actionable decisions.  They use a higher-capability model
(Sonnet or Opus) for nuanced reasoning about corrective actions.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from egg_agent.client import run_agent_async

logger = logging.getLogger(__name__)

# Default model for decision-making tier
DECISION_MODEL = "sonnet"


async def _call_decision_maker(prompt: str, context: str, *, model: str | None = None) -> str:
    """Call the decision-making LLM and return the raw response text.

    Uses Sonnet (or configured model) with ``max_turns=1``.
    """
    full_prompt = f"{prompt}\n\n---\nContext:\n{context}"
    result = await run_agent_async(
        full_prompt,
        model=model or DECISION_MODEL,
        max_turns=1,
    )
    if not result.success:
        raise RuntimeError(f"Decision maker call failed: {result.error}")
    return result.stdout.strip()


def _parse_json_or_fallback(raw: str, fallback: dict[str, Any]) -> dict[str, Any]:
    """Try to parse *raw* as JSON; return *fallback* on failure."""
    try:
        return json.loads(raw)  # type: ignore[no-any-return]
    except (json.JSONDecodeError, TypeError):
        pass
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
# Public decision functions
# ---------------------------------------------------------------------------


async def decide_corrective_action(
    classification: dict, context: dict, *, model: str | None = None
) -> dict:
    """Decide what corrective action to take based on a classification.

    Args:
        classification: Output from a classifier function (e.g. classify_stall).
        context: Additional context (pipeline state, agent history, etc.).
        model: Override the default decision model.

    Returns:
        A dict with keys:
            action: ``"nudge"`` | ``"redirect"`` | ``"hitl"`` | ``"issue"`` | ``"slack"``
            message: str with the message or action description
            priority: ``"low"`` | ``"medium"`` | ``"high"`` | ``"critical"``
    """
    prompt = (
        "You are a pipeline health decision maker. Based on the following "
        "classification result and context, decide what corrective action "
        "to take.\n\n"
        "Available actions (in escalating order):\n"
        '  "nudge" - Send a gentle reminder to the agent\n'
        '  "redirect" - Send directive instructions to change course\n'
        '  "hitl" - Escalate to human-in-the-loop decision\n'
        '  "issue" - File a diagnostic GitHub issue\n'
        '  "slack" - Send urgent Slack notification\n\n'
        "Respond with ONLY a JSON object (no markdown fences) with these keys:\n"
        '  "action": one of the actions above\n'
        '  "message": string describing the action or message to send\n'
        '  "priority": one of "low", "medium", "high", "critical"\n'
    )
    ctx = json.dumps({"classification": classification, "context": context}, default=str)

    raw = await _call_decision_maker(prompt, ctx, model=model)
    return _parse_json_or_fallback(
        raw,
        {"action": "nudge", "message": raw, "priority": "medium"},
    )


async def compose_redirect_message(
    agent_role: str, issue: str, context: dict, *, model: str | None = None
) -> str:
    """Compose a redirect message for a misbehaving or stuck agent.

    Args:
        agent_role: The role of the agent to redirect (e.g. ``"coder"``).
        issue: Description of the problem that triggered the redirect.
        context: Additional context (contract tasks, recent activity, etc.).
        model: Override the default decision model.

    Returns:
        A string containing the redirect message to send to the agent.
    """
    prompt = (
        f"You are composing a redirect message for a {agent_role} agent that "
        f"is experiencing the following issue: {issue}\n\n"
        "Write a concise, actionable message that:\n"
        "1. Briefly describes what the agent is doing wrong\n"
        "2. Clearly states what the agent should do instead\n"
        "3. References specific contract tasks or files if applicable\n\n"
        "Respond with ONLY the redirect message text (no JSON, no markdown fences)."
    )
    ctx = json.dumps(context, default=str)

    return await _call_decision_maker(prompt, ctx, model=model)


async def decide_escalation_level(
    classification: dict,
    redirect_history: list[dict],
    *,
    model: str | None = None,
) -> dict:
    """Decide whether to escalate further based on prior redirect attempts.

    Args:
        classification: Latest classifier output.
        redirect_history: List of prior redirects sent to this agent, each
            with keys like ``action``, ``timestamp``, ``outcome``.
        model: Override the default decision model.

    Returns:
        A dict with keys:
            escalate: bool
            level: ``"redirect"`` | ``"hitl"`` | ``"issue"``
            reasoning: str explaining the decision
    """
    prompt = (
        "You are a pipeline health escalation decision maker. Based on the "
        "latest classification and the history of prior redirect attempts, "
        "decide whether to escalate and to what level.\n\n"
        "Escalation levels (in order):\n"
        '  "redirect" - Try another redirect message\n'
        '  "hitl" - Escalate to human-in-the-loop\n'
        '  "issue" - File a diagnostic GitHub issue\n\n'
        "Respond with ONLY a JSON object (no markdown fences) with these keys:\n"
        '  "escalate": boolean\n'
        '  "level": one of the levels above\n'
        '  "reasoning": brief explanation of why\n'
    )
    ctx = json.dumps(
        {"classification": classification, "redirect_history": redirect_history},
        default=str,
    )

    raw = await _call_decision_maker(prompt, ctx, model=model)
    return _parse_json_or_fallback(
        raw,
        {"escalate": True, "level": "hitl", "reasoning": raw},
    )
