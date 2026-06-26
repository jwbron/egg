"""Sonnet/Opus-tier decision functions for the overseer agent.

These functions take classifier output and contextual information to
produce actionable decisions.  They use a higher-capability model
(Sonnet or Opus) for nuanced reasoning about corrective actions.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from agent_model_resolution import OVERSEER_TIER_MODELS
from egg_agent.client import run_agent_async
from overseer.utils import parse_json_or_fallback as _parse_json_or_fallback

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Adjudication (#2270 slice-4) — the on-demand OVERSEER's structured verdict.
#
# A detection-plane Finding with ``requires_adjudication=True`` spawns a NORMAL
# on-demand OVERSEER agent (slice-3 spawn path, Opus via the slice-2 resolver).
# That agent ADVISES only: it returns one of the bounded corrective-vocabulary
# recommendations (the slice-6 authority plane is what actually executes). This
# module owns the verdict schema + parsing so both the spawn site
# (routes/pipelines) and the monitor's on-demand entry point agree on the shape.
# ---------------------------------------------------------------------------

# The closed advisory vocabulary the adjudicator may recommend. Mirrors the
# slice-6 CorrectiveExecutor's actions, plus ``none`` (no action / false alarm).
# The adjudicator only advises; it never executes.
ADJUDICATION_ACTIONS: frozenset[str] = frozenset(
    {"none", "nudge_agent", "respawn_cohort", "open_operator_hitl"}
)


@dataclass(frozen=True)
class AdjudicationVerdict:
    """Structured verdict returned by the on-demand OVERSEER adjudicator.

    Attributes:
        confirmed: Whether the adjudicator agrees the finding is a real problem
            (``False`` means the deterministic detector over-fired — a calibration
            data point, §2).
        recommended_action: One of :data:`ADJUDICATION_ACTIONS`. Advisory only;
            the slice-6 authority plane decides whether/how to execute it.
        severity: The adjudicator's severity assessment (may differ from the
            detector's).
        reasoning: Human-facing explanation.
        finding_class: The finding class this verdict adjudicates (for routing).
        raw: The raw verdict payload, retained for audit.
    """

    confirmed: bool
    recommended_action: str
    severity: str = "medium"
    reasoning: str = ""
    finding_class: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "confirmed": self.confirmed,
            "recommended_action": self.recommended_action,
            "severity": self.severity,
            "reasoning": self.reasoning,
            "finding_class": self.finding_class,
        }


def build_adjudication_prompt(finding: Any) -> str:
    """Build the one-shot adjudication prompt for a single finding.

    The adjudicator is a NORMAL overseer agent invoked on-demand for ONE
    finding; it is not a standing watcher. It must return ONLY a JSON verdict.
    """
    finding_dict = finding.to_dict() if hasattr(finding, "to_dict") else dict(finding)
    return (
        "You are the on-demand overseer ADJUDICATOR. A deterministic detector "
        "on the orchestrator flagged a possible problem and asked for your "
        "judgement before any corrective action is taken. You ADVISE only — you "
        "do not execute anything.\n\n"
        "Finding under adjudication (JSON):\n"
        f"{json.dumps(finding_dict, default=str, indent=2)}\n\n"
        "Decide whether this is a genuine problem or a false alarm (the detector "
        "over-firing). Be conservative: a false confirmation trains operators to "
        "ignore the overseer.\n\n"
        "Respond with ONLY a JSON object (no markdown fences):\n"
        '  "confirmed": boolean — is this a real problem?\n'
        '  "recommended_action": one of "none", "nudge_agent", '
        '"respawn_cohort", "open_operator_hitl"\n'
        '  "severity": one of "info", "low", "medium", "high"\n'
        '  "reasoning": brief explanation\n'
    )


def parse_adjudication_verdict(raw: Any, *, finding: Any = None) -> AdjudicationVerdict:
    """Parse an adjudicator response (raw text or dict) into a verdict.

    Defensive: an unparseable / malformed response degrades to a conservative
    *unconfirmed* verdict recommending ``open_operator_hitl`` only when the
    detector itself demanded adjudication, so a broken adjudicator never
    silently swallows a genuine deadlock.
    """
    finding_class = ""
    if finding is not None:
        finding_class = str(getattr(finding, "finding_class", "") or "")

    if isinstance(raw, dict):
        payload = raw
    else:
        payload = _parse_json_or_fallback(str(raw), {})

    if not isinstance(payload, dict) or not payload:
        # Conservative fallback: defer to a human rather than drop the finding.
        return AdjudicationVerdict(
            confirmed=False,
            recommended_action="open_operator_hitl",
            severity=str(getattr(finding, "severity", "medium") or "medium"),
            reasoning="Adjudicator returned no parseable verdict; deferring to operator.",
            finding_class=finding_class,
            raw={"unparseable": str(raw)[:500]},
        )

    action = str(payload.get("recommended_action", "none"))
    if action not in ADJUDICATION_ACTIONS:
        action = "open_operator_hitl"
    return AdjudicationVerdict(
        confirmed=bool(payload.get("confirmed", False)),
        recommended_action=action,
        severity=str(payload.get("severity", "medium")),
        reasoning=str(payload.get("reasoning", "")),
        finding_class=finding_class or str(payload.get("finding_class", "")),
        raw=dict(payload),
    )


# Routine tier (#2270 §1): routine corrective decisions run on Sonnet, sourced
# from the single overseer-tier table in ``agent_model_resolution`` so the
# classify/routine/adversarial tiers stay in sync. Adversarial / high-stakes
# adjudication is a separate, costlier tier (the overseer agent's own resolved
# ``opus`` model) and is not driven from here.
DECISION_MODEL = OVERSEER_TIER_MODELS["routine"]

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

# Spans every action in the decision-maker's vocabulary so any prior
# corrective intervention (destructive or not) bypasses the
# first-stall restart guard. See ``_enforce_no_first_stall_restart``.
_PRIOR_INTERVENTIONS: frozenset[str] = frozenset(
    {"nudge", "redirect", "issue", "slack", "restart_agent", "restart_phase", "hitl"}
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

    The "no prior intervention" check spans every action in the
    decision-maker's vocabulary — ``nudge``, ``redirect``, ``issue``,
    ``slack``, ``restart_agent``, ``restart_phase``, and ``hitl``. The
    intent is "ensure at least one corrective action of any kind has
    fired before destruction": if the operator (or the overseer) has
    already had a chance to respond to the agent's state via any
    intervention type, the guard yields and the model's recommendation
    stands. A previous ``restart_agent`` (which may itself have been
    the wrong call) likewise bypasses the guard rather than fast-track
    the next restart through it.
    """
    if decision.get("action") != "restart_agent":
        return decision

    cls = classification.get("classification")
    if cls not in {"stuck", "needs_help"}:
        return decision

    history = redirect_history or []
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
