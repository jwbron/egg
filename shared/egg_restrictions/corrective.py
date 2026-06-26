"""Control-plane authorization for the overseer corrective-action vocabulary.

Issue #2270 slice-6 (§4 — Authority). The overseer **advises**; the orchestrator
control plane **executes**. The corrective vocabulary is CLOSED — exactly three
actions — and it may be invoked ONLY by the orchestrator control-plane identity.

Every agent role, *including the overseer itself*, is denied. An agent that could
open an operator HITL decision, nudge a peer, or respawn a cohort directly would
be an authority-escalation — exactly the unmediated-action vector §4 closes. The
overseer's reach stops at returning an :class:`AdjudicationVerdict`; the
orchestrator's :class:`CorrectiveExecutor` is the only thing that acts on it.

This predicate is the RBAC gate. It lives in the shared package — with no
dependency on :mod:`egg_restrictions.patterns` or :mod:`egg_contracts` — so the
gateway (``gateway/agent_restrictions.py``, the named enforcement surface) and
the orchestrator-side executor enforce the *same* rule from one source of truth,
with no import cycle.
"""

from __future__ import annotations

# The closed corrective vocabulary. Mirrors
# ``overseer.decision_maker.ADJUDICATION_ACTIONS`` minus ``none`` (which is the
# adjudicator's "no action" recommendation, never an executable action). The
# executor exposes exactly these three.
CORRECTIVE_ACTIONS: frozenset[str] = frozenset(
    {
        "open_operator_hitl",
        "nudge_agent",
        "respawn_cohort",
    }
)

# The single authorized identity: the orchestrator control plane. This is NOT an
# ``AgentRole`` — it is the in-process control plane that runs the executor. It
# mirrors the ``from_role="orchestrator"`` / ``actor="orchestrator-*"`` identity
# used for orchestrator-authored messages and contract mutations.
ORCHESTRATOR_CONTROL_PLANE_IDENTITY = "orchestrator"


def corrective_action_authorized(identity: str | None, action: str) -> tuple[bool, str]:
    """Authorize a corrective action for ``identity`` (deny-by-default).

    Args:
        identity: The caller identity. Authorized only when it is the
            orchestrator control plane (:data:`ORCHESTRATOR_CONTROL_PLANE_IDENTITY`).
            Any agent role — ``overseer``, ``coder``, ``reviewer_*``, … — is
            denied.
        action: The requested action. Must be a member of
            :data:`CORRECTIVE_ACTIONS`; anything else is rejected before the
            identity check (the vocabulary is closed).

    Returns:
        ``(allowed, reason)``. ``reason`` is always populated so callers can
        audit-log the verdict verbatim.
    """
    if action not in CORRECTIVE_ACTIONS:
        return False, (
            f"unknown corrective action {action!r}; the vocabulary is closed to "
            f"{sorted(CORRECTIVE_ACTIONS)}"
        )

    normalized = (identity or "").strip().lower()
    if normalized == ORCHESTRATOR_CONTROL_PLANE_IDENTITY:
        return True, "orchestrator control-plane authorized"

    return False, (
        f"corrective actions are control-plane only; caller {identity!r} is "
        "denied (agents — including the overseer, which only advises — may not "
        "execute corrective actions directly)"
    )


__all__ = [
    "CORRECTIVE_ACTIONS",
    "ORCHESTRATOR_CONTROL_PLANE_IDENTITY",
    "corrective_action_authorized",
]
