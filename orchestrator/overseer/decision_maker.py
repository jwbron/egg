"""Sonnet/Opus-tier decision functions for the overseer agent.

These functions take classifier output and contextual information to
produce actionable decisions.  They use a higher-capability model
(Sonnet or Opus) for nuanced reasoning about corrective actions.
"""

from __future__ import annotations

import json
import logging
import re

from egg_agent.client import run_agent_async
from overseer.utils import parse_json_or_fallback as _parse_json_or_fallback

logger = logging.getLogger(__name__)

# Default model for decision-making tier
DECISION_MODEL = "sonnet"

# Infrastructure error subcategories that are safe to auto-restart.
# Only transient / environmental issues — NOT permission, config, or filesystem errors.
RESTARTABLE_PATTERNS: list[str] = [
    "unresponsive",
    "crashed",
    "oom",
    "timeout",
    "timed out",
    "hung",
    "not responding",
]

# Non-transient error indicators that override RESTARTABLE_PATTERNS.
# If any of these appear in the error text, we escalate to HITL even when
# a restartable keyword is also present (e.g. "Agent crashed after permission error").
NON_RESTARTABLE_PATTERNS: list[str] = [
    "permission",
    "erofs",
    "read-only file system",
    "certificate",
    "config error",
    "configuration invalid",
    "misconfigured",
    "authentication",
    "authorization",
    "credentials",
    "disk full",
    "no space left",
    "quota exceeded",
]

_RESTARTABLE_RE = re.compile(
    "|".join(re.escape(p) for p in RESTARTABLE_PATTERNS),
    re.IGNORECASE,
)

_NON_RESTARTABLE_RE = re.compile(
    "|".join(re.escape(p) for p in NON_RESTARTABLE_PATTERNS),
    re.IGNORECASE,
)


def _is_restartable(error_text: str) -> bool:
    """Return True if *error_text* describes a transient, auto-restartable error.

    The deny-list (NON_RESTARTABLE_PATTERNS) takes priority: if both a
    restartable and a non-restartable keyword are present, the error is
    treated as non-restartable to avoid restart loops on persistent failures.
    """
    if not _RESTARTABLE_RE.search(error_text):
        return False
    if _NON_RESTARTABLE_RE.search(error_text):
        return False
    return True


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


# ---------------------------------------------------------------------------
# Public decision functions
# ---------------------------------------------------------------------------


async def decide_corrective_action(
    classification: dict,
    context: dict,
    *,
    model: str | None = None,
    redirect_history: list[dict] | None = None,
) -> dict:
    """Decide what corrective action to take based on a classification.

    Args:
        classification: Output from a classifier function (e.g. classify_stall).
        context: Additional context (pipeline state, agent history, etc.).
        model: Override the default decision model.
        redirect_history: Prior corrective actions sent to this agent.
            Used by the deterministic post-hoc guard (#2190) to downgrade
            ``restart_agent`` recommendations on first-occurrence
            ``stuck`` classifications when the model disregards the
            prompt's no-restart-on-first-stall guidance.

    Returns:
        A dict with keys:
            action: ``"nudge"`` | ``"redirect"`` | ``"restart_agent"`` | ``"hitl"`` | ``"restart_phase"`` | ``"issue"`` | ``"slack"``
            message: str with the message or action description
            priority: ``"low"`` | ``"medium"`` | ``"high"`` | ``"critical"``
    """
    # Fast-path: infrastructure errors bypass LLM.
    # Restartable subcategories (unresponsive, crashed, OOM, timeout, hung)
    # trigger an automatic restart; everything else escalates to HITL.
    if classification.get("classification") == "infrastructure_error":
        error_details = classification.get("reasoning", "Infrastructure error detected")
        if _is_restartable(error_details):
            return {
                "action": "restart_agent",
                "message": f"Restartable infrastructure error detected: {error_details}",
                "priority": "high",
            }
        return {
            "action": "hitl",
            "message": f"Infrastructure error requiring human intervention: {error_details}",
            "priority": "critical",
        }

    prompt = (
        "You are a pipeline health decision maker. Based on the following "
        "classification result and context, decide what corrective action "
        "to take.\n\n"
        "Available actions (in escalating order):\n"
        '  "nudge" - Send a gentle reminder to the agent\n'
        '  "redirect" - Send directive instructions to change course\n'
        '  "restart_agent" - Restart the stuck/failed agent (preserves worktree)\n'
        '  "hitl" - Escalate to human-in-the-loop decision\n'
        '  "restart_phase" - Restart all agents in the current phase (requires HITL approval)\n'
        '  "issue" - File a diagnostic GitHub issue\n'
        '  "slack" - Send urgent Slack notification\n\n'
        "Recommendation ladder for stall / silent-agent classifications "
        "(issue #2190): inspecting agent state must come before any "
        "container-restart recommendation. An apparently-silent agent is "
        "often mid-tool-call (e.g. a multi-minute pytest). Restarting "
        "would destroy in-flight commits.\n"
        "  - First response: `nudge` or `redirect` whose message body "
        'leads with "Inspect container logs via '
        "`mcp__egg__get_container_logs(task_id=…, agent_role=…)` before "
        'taking destructive action."\n'
        "  - Do NOT recommend `restart_agent` for a first stall alert. "
        "Reserve `restart_agent` for follow-up alerts that fire after a "
        "log inspection has confirmed the agent is genuinely inactive "
        "(no recent commits, no pushes, no tool-call results), or for "
        "infrastructure errors classified separately.\n"
        "  - Never embed `egg-orch container restart <id>` as a first-line "
        "operator action in the message body.\n\n"
        "Respond with ONLY a JSON object (no markdown fences) with these keys:\n"
        '  "action": one of the actions above\n'
        '  "message": string describing the action or message to send\n'
        '  "priority": one of "low", "medium", "high", "critical"\n'
    )
    ctx = json.dumps({"classification": classification, "context": context}, default=str)

    raw = await _call_decision_maker(prompt, ctx, model=model)
    decision = _parse_json_or_fallback(
        raw,
        {"action": "nudge", "message": raw, "priority": "medium"},
    )

    return _enforce_no_first_stall_restart(decision, classification, redirect_history)


def _enforce_no_first_stall_restart(
    decision: dict,
    classification: dict,
    redirect_history: list[dict] | None,
) -> dict:
    """Deterministically downgrade ``restart_agent`` on first-stall alerts.

    The decision-maker prompt instructs the model not to recommend
    ``restart_agent`` for a first-occurrence stall classification (issue
    #2190 — restarting destroys in-flight commits from agents mid-pytest).
    Prompts are advisory; this guard is the load-bearing enforcement.

    Trigger: ``action == "restart_agent"`` AND classification is
    ``stuck``/``needs_help`` AND no prior intervention of any kind appears
    in ``redirect_history``. In that state we rewrite the decision to a
    ``hitl`` so the operator gets a real decision surface (the original
    recommendation and the model's reasoning are preserved in the
    question text).

    The "no prior intervention" check spans ``nudge``, ``redirect``,
    ``restart_agent``, and ``hitl`` so a previous restart (which may
    itself have been the wrong call) doesn't fast-track the next one
    past the guard. The intent is "ensure at least one non-destructive
    intervention before destruction" — if any kind of corrective action
    has already fired, the guard yields and the model's recommendation
    stands.
    """
    if decision.get("action") != "restart_agent":
        return decision

    cls = classification.get("classification")
    if cls not in {"stuck", "needs_help"}:
        return decision

    history = redirect_history or []
    _PRIOR_INTERVENTIONS = {"nudge", "redirect", "restart_agent", "hitl"}
    if any(h.get("action") in _PRIOR_INTERVENTIONS for h in history):
        return decision

    original_msg = decision.get("message", "")
    original_suffix = f" Model's recommendation: {original_msg}" if original_msg else ""
    return {
        "action": "hitl",
        "message": (
            "Overseer overrode a `restart_agent` recommendation on a "
            "first-occurrence stall. The agent may be mid-tool-call (e.g. "
            "a multi-minute pytest) rather than genuinely stuck; restart "
            "would destroy in-flight commits. Inspect container logs via "
            "`mcp__egg__get_container_logs(task_id=…, agent_role=…)` "
            "before approving a restart." + original_suffix
        ).strip(),
        "priority": decision.get("priority", "medium"),
    }


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
    context: dict | None = None,
    model: str | None = None,
) -> dict:
    """Decide whether to escalate further based on prior redirect attempts.

    Args:
        classification: Latest classifier output.
        redirect_history: List of prior redirects sent to this agent, each
            with keys like ``action``, ``timestamp``, ``outcome``.
        context: Optional additional context (e.g. container logs).
        model: Override the default decision model.

    Returns:
        A dict with keys:
            escalate: bool
            level: ``"redirect"`` | ``"hitl"`` | ``"issue"``
            reasoning: str explaining the decision
    """
    # Fast-path: infrastructure errors bypass LLM.
    # Restartable subcategories can be auto-restarted instead of escalating.
    if classification.get("classification") == "infrastructure_error":
        reasoning = classification.get("reasoning", "requires human intervention")
        if _is_restartable(reasoning):
            return {
                "escalate": True,
                "level": "restart_agent",
                "reasoning": f"Restartable infrastructure error: {reasoning}",
            }
        return {
            "escalate": True,
            "level": "hitl",
            "reasoning": f"Infrastructure error: {reasoning}",
        }

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
    ctx_data: dict = {"classification": classification, "redirect_history": redirect_history}
    if context:
        ctx_data["context"] = context
    ctx = json.dumps(
        ctx_data,
        default=str,
    )

    raw = await _call_decision_maker(prompt, ctx, model=model)
    return _parse_json_or_fallback(
        raw,
        {"escalate": True, "level": "hitl", "reasoning": raw},
    )
