"""Phase-transition state machine: the valid-transition table, the transition
validator, and concurrent-state teardown on phase change (#3312 decomposition).
"""

from models import PipelinePhase

from . import logger

# Valid phase transitions.
#
# Issue #1557 — Jira-epic SDLC support: ``PLAN`` gains ``APPLY`` as a
# valid successor, and the new ``APPLY`` phase advances only to
# ``IMPLEMENT``. The orchestrator-side scheduler in
# :func:`orchestrator.routes.pipelines._next_phases_for_epic` picks
# ``APPLY`` only when ``Pipeline.is_epic`` is true; non-epic pipelines
# continue to advance ``PLAN → IMPLEMENT`` directly (``IMPLEMENT`` is
# listed before ``APPLY`` so the default ``next_phases[0]`` semantics
# preserve the pre-#1557 behaviour for callers that don't go through
# the epic-aware helper).
PHASE_TRANSITIONS = {
    PipelinePhase.REFINE: [PipelinePhase.PLAN, PipelinePhase.IMPLEMENT],
    PipelinePhase.PLAN: [PipelinePhase.IMPLEMENT, PipelinePhase.APPLY],
    PipelinePhase.APPLY: [PipelinePhase.IMPLEMENT],
    # IMPLEMENT is now terminal — the PR phase was removed in #2777 (cq-4).
    # The context PR opens up-front at the plan→implement boundary via
    # ``_open_context_pr_at_implement_start``; slice PRs stack on it.
    PipelinePhase.IMPLEMENT: [],
}


def validate_phase_transition(
    current_phase: PipelinePhase,
    target_phase: PipelinePhase,
) -> tuple[bool, str]:
    """Validate a phase transition.

    Args:
        current_phase: Current pipeline phase
        target_phase: Target phase to transition to

    Returns:
        Tuple of (is_valid, error_message)
    """
    transitions = PHASE_TRANSITIONS
    if target_phase not in transitions.get(current_phase, []):
        valid_targets = transitions.get(current_phase, [])
        if not valid_targets:
            return False, f"Phase {current_phase.value} is terminal"
        return False, (
            f"Cannot transition from {current_phase.value} to {target_phase.value}. "
            f"Valid transitions: {[p.value for p in valid_targets]}"
        )
    return True, ""


def _clear_concurrent_state(pipeline_id: str) -> None:
    """Clear ephemeral message store and consensus state on phase transition."""
    try:
        from message_store import get_message_store
    except ImportError:
        from ..message_store import get_message_store  # type: ignore[no-redef]

    cleared = get_message_store().clear(pipeline_id)

    # Clear BRC tracker if it exists. The legacy ConsensusEvaluator was
    # removed in cq-5 of #2777; the BRC tracker is the only consensus
    # state that needs clearing on a phase transition.
    try:
        from peer_consensus import remove_peer_consensus_tracker

        remove_peer_consensus_tracker(pipeline_id)
    except ImportError:
        pass

    if cleared:
        logger.debug(
            "Cleared concurrent state on phase transition",
            pipeline_id=pipeline_id,
            messages_cleared=cleared,
        )
