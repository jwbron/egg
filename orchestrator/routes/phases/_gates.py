"""Conditional-ACK HITL gate plus the unresolved decision / contract-gap
collectors used by complete_phase (#3312 decomposition).
"""

import json
from pathlib import Path

import routes.phases as _pkg
from models import DecisionStatus, Pipeline

from . import logger

# Context marker prefix used on the 3-way conditional-ACK HITL gate
# decision (#2004). The resolve_decision handler matches on this prefix
# to dispatch approve+accept / reject / address-in-pipeline behavior.
CONDITIONAL_ACK_GATE_MARKER = "conditional_ack_gate:"

# User-facing option labels for the conditional-ACK HITL gate. Exposed as
# module-level constants so the resolve_decision handler and tests can
# reference the same strings without duplication (#2004).
CONDITIONAL_ACK_APPROVE = "Approve and accept obligations"
CONDITIONAL_ACK_REJECT = "Reject and force NACK"
CONDITIONAL_ACK_ADDRESS = "Address in-pipeline (invalidate ACK)"
CONDITIONAL_ACK_OPTIONS = [
    CONDITIONAL_ACK_APPROVE,
    CONDITIONAL_ACK_REJECT,
    CONDITIONAL_ACK_ADDRESS,
]


def _existing_conditional_ack_gate(pipeline: Pipeline) -> str | None:
    """Return the id of the pending conditional-ACK gate decision, if any.

    The gate decision is identified by a ``CONDITIONAL_ACK_GATE_MARKER``
    prefix on the decision's context field. Only pending decisions count —
    a previously-resolved gate for a prior conditional ACK does not block
    a new round if the producer later attaches a fresh condition.
    """
    for decision in pipeline.decisions:
        if decision.status != DecisionStatus.PENDING:
            continue
        if (decision.context or "").startswith(CONDITIONAL_ACK_GATE_MARKER):
            return decision.id
    return None


def _ensure_conditional_ack_gate(
    pipeline: Pipeline,
    repo_path: Path,
) -> str | None:
    """Queue the 3-way conditional-ACK HITL gate if conditions are live.

    Looks up the peer-consensus tracker for this pipeline and, if there
    are any active pre-merge conditions, enqueues a ``choice`` HITL
    decision (#2004). The decision's context embeds the conditions as
    JSON so the resolve_decision handler can dispatch without querying
    the tracker (which may have been torn down by then).

    Returns the decision id when a new gate is queued, the existing
    pending-decision id when one is already in flight, or ``None`` when
    no conditions exist (or the tracker is unavailable).
    """
    tracker = _pkg.get_peer_consensus_tracker(pipeline.id)
    if tracker is None:
        return None
    try:
        conditions = tracker.get_pre_merge_conditions()
    except Exception:
        # Never block phase completion on a tracker read failure — the PR
        # body renderer applies the same guard (#1998).
        logger.warning(
            "Failed to read pre-merge conditions from tracker",
            pipeline_id=pipeline.id,
            exc_info=True,
        )
        return None
    if not conditions:
        return None

    existing_id = _existing_conditional_ack_gate(pipeline)
    if existing_id is not None:
        return existing_id

    # Render the conditions into the decision question so humans see the
    # obligation text up front without having to fetch the raw context.
    question_lines = [
        "Reviewer(s) issued a conditional ACK with pre-merge obligations:",
        "",
    ]
    for c in conditions:
        reviewer = c.get("reviewer", "unknown")
        condition = str(c.get("condition", "")).strip()
        if not condition:
            continue
        question_lines.append(f"- {reviewer}: {condition}")
    question_lines.extend(
        [
            "",
            "How should we proceed?",
        ]
    )
    question = "\n".join(question_lines)

    # Embed the conditions list in the context so the resolve handler can
    # act on specific (reviewer, producer) edges without re-querying the
    # tracker. Prefixed with CONDITIONAL_ACK_GATE_MARKER so the handler
    # can detect these decisions unambiguously.
    context_payload = {"conditions": conditions}
    context = CONDITIONAL_ACK_GATE_MARKER + json.dumps(context_payload)

    queue = _pkg.get_decision_queue(pipeline.id, repo_path)
    decision = queue.queue_decision(
        question=question,
        context=context,
        options=list(CONDITIONAL_ACK_OPTIONS),
        decision_type="choice",
        phase=pipeline.current_phase,
    )
    logger.info(
        "Queued conditional-ACK HITL gate",
        pipeline_id=pipeline.id,
        decision_id=decision.id,
        condition_count=len(conditions),
    )
    return decision.id


def _collect_unresolved_phase_decisions(
    pipeline: Pipeline,
    store_repo_path: Path,
) -> list[str]:
    """Return IDs of decisions scoped to the current phase that are still pending.

    Checks two decision stores:

    - ``pipeline.decisions`` — orchestrator-side HITL queue (e.g. phase_gate
      prompts).
    - The SDLC contract's ``decisions`` list — agent-authored HITL points
      created via ``egg-contract add-decision``.

    Decisions with no phase tag are skipped for backward compatibility — we
    cannot prove they belong to the current phase, and older persisted state
    may predate the contract-side ``phase`` field (added alongside this
    guard).
    """
    current_phase = pipeline.current_phase

    unresolved = [
        d.id
        for d in pipeline.decisions
        if d.status == DecisionStatus.PENDING and d.phase == current_phase
    ]

    # Contract decisions are optional — ISSUE-mode pipelines may reach this
    # endpoint before a contract has been populated.
    if pipeline.has_contract:
        try:
            from egg_contracts.loader import (
                ContractNotFoundError,
                ContractValidationError,
                load_contract,
            )
        except ImportError:
            # egg_contracts not installed — cannot scan contract decisions.
            logger.warning(
                "Failed to scan contract decisions for unresolved entries",
                pipeline_id=pipeline.id,
                exc_info=True,
            )
            return unresolved

        try:
            from routes import resolve_worktree_path
            from routes.pipelines import _pipeline_identifier

            worktree_path = resolve_worktree_path(pipeline.id, store_repo_path)
            contract_id = _pipeline_identifier(pipeline.issue_number, pipeline.id)
            try:
                contract = load_contract(contract_id, worktree_path)
            except ContractNotFoundError:
                contract = None
            if contract is not None:
                unresolved.extend(
                    d.id for d in contract.decisions if not d.resolved and d.phase == current_phase
                )
        except OSError, ValueError, ContractValidationError:
            # OSError covers filesystem failures loading the contract,
            # ValueError covers serialization/validation issues (pydantic V2
            # raises ValueError for invalid data), and
            # ContractValidationError covers corrupt/invalid contract JSON.
            # Programming errors (AttributeError, TypeError, NameError)
            # are left to propagate so they surface during development.
            logger.warning(
                "Failed to scan contract decisions for unresolved entries",
                pipeline_id=pipeline.id,
                exc_info=True,
            )

    return unresolved


def _collect_unresolved_contract_gaps(
    pipeline: Pipeline,
    store_repo_path: Path,
) -> list[str]:
    """Return ``"<task>/<gap>"`` ids for every unresolved ``TaskGap``.

    A tester→coder :class:`TaskGap` left ``resolved == False`` ships into
    the committed contract snapshot and fails ``test_models_gaps.py`` red
    in CI on the already-open PR (#3298 class 4). This scans the contract
    so :func:`complete_phase` can block finalize on open gaps — the same
    way it blocks on unresolved HITL decisions — instead of discovering
    them reactively in CI. See #3300.

    Gaps are not phase-scoped: at finalize every gap should be resolved,
    so the scan spans all tasks (unlike the phase-filtered decision
    scan). Returns an empty list for clean contracts and when the
    contract is absent/unloadable (the gate fails open, mirroring the
    decision scan — a load failure must never strand the pipeline).
    """
    if not pipeline.has_contract:
        return []

    try:
        from egg_contracts.loader import (
            ContractNotFoundError,
            ContractValidationError,
            load_contract,
        )
    except ImportError:
        logger.warning(
            "Failed to scan contract for unresolved gaps",
            pipeline_id=pipeline.id,
            exc_info=True,
        )
        return []

    try:
        from routes import resolve_worktree_path
        from routes.pipelines import _pipeline_identifier

        worktree_path = resolve_worktree_path(pipeline.id, store_repo_path)
        contract_id = _pipeline_identifier(pipeline.issue_number, pipeline.id)
        try:
            contract = load_contract(contract_id, worktree_path)
        except ContractNotFoundError:
            return []
        return [f"{task_id}/{gap.id}" for task_id, gap in contract.unresolved_gaps()]
    except OSError, ValueError, ContractValidationError:
        # Mirror _collect_unresolved_phase_decisions: filesystem /
        # serialization / validation failures fail open and log; only
        # programming errors are left to propagate.
        logger.warning(
            "Failed to scan contract for unresolved gaps",
            pipeline_id=pipeline.id,
            exc_info=True,
        )
        return []
