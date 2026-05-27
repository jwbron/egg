"""
Decision endpoints for HITL integration.

Provides REST endpoints for queuing, polling, and resolving
human-in-the-loop decisions.
"""

import json
import re
import sys
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, jsonify, request

# Add parent directory to path for imports
_parent_path = Path(__file__).parent.parent
if str(_parent_path) not in sys.path:
    sys.path.insert(0, str(_parent_path))

# Add shared directory to path for logging
_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


from decision_queue import (
    DecisionAlreadyResolvedError,
    DecisionNotFoundError,
    get_decision_queue,
)
from events import EventType, emit_event
from lifecycle_auth import require_lifecycle_secret
from models import PipelinePhase
from peer_consensus import get_peer_consensus_tracker
from state_store import InvalidPipelineIdError, PipelineNotFoundError

logger = get_logger("orchestrator.decisions")

decisions_bp = Blueprint("decisions", __name__, url_prefix="/api/v1/pipelines")


def make_error_response(
    message: str,
    status_code: int = 400,
    details: dict[str, Any] | None = None,
) -> tuple[Response, int]:
    """Create an error response."""
    response: dict[str, Any] = {"success": False, "message": message}
    if details:
        response["details"] = details
    return jsonify(response), status_code


def make_success_response(
    message: str,
    data: dict[str, Any] | None = None,
) -> tuple[Response, int]:
    """Create a success response."""
    response: dict[str, Any] = {"success": True, "message": message}
    if data:
        response["data"] = data
    return jsonify(response), 200


from routes import get_state_store_for_pipeline  # noqa: E402 — shared helper


def _handle_restart_agent(pipeline_id: str, question: str) -> None:
    """Stop and respawn a stalled agent container.

    Parses the agent role from the HITL decision question
    (format: ``"Agent <role> issue: ..."``) and uses the Docker client
    to stop the old container.  A ``CONTAINER_STOPPED`` event is emitted
    so the pipeline orchestration loop can decide whether to respawn.

    Args:
        pipeline_id: Pipeline ID.
        question: The decision question text containing the agent role.
    """
    match = re.match(r"Agent\s+(\S+)\s+issue:", question)
    if not match:
        logger.warning(
            "Could not parse agent role from restart decision",
            pipeline_id=pipeline_id,
            question=question[:120],
        )
        return

    agent_role = match.group(1)
    logger.info(
        "Restarting agent via HITL decision",
        pipeline_id=pipeline_id,
        agent_role=agent_role,
    )

    try:
        from docker_client import get_docker_client

        docker_client = get_docker_client()
        containers = docker_client.list_containers(
            all=False,
            labels={"egg.pipeline.id": pipeline_id, "egg.agent.role": agent_role},
        )
        if not containers:
            logger.warning(
                "No running container found for agent",
                pipeline_id=pipeline_id,
                agent_role=agent_role,
            )
            return

        container = containers[0]
        docker_client.stop_container(container.container_id, timeout=10)
        logger.info(
            "Stopped stalled agent container for restart",
            pipeline_id=pipeline_id,
            agent_role=agent_role,
            container_id=container.container_id[:12],
        )

        # Emit event so health monitor / pipeline loop can track the stop
        try:
            emit_event(
                EventType.CONTAINER_STOPPED,
                pipeline_id=pipeline_id,
                data={
                    "container_id": container.container_id,
                    "agent_role": agent_role,
                    "reason": "hitl_restart",
                },
            )
        except Exception:
            logger.debug("Failed to emit CONTAINER_STOPPED event", exc_info=True)

    except Exception:
        logger.warning(
            "Failed to restart agent container",
            pipeline_id=pipeline_id,
            agent_role=agent_role,
            exc_info=True,
        )


def _handle_hard_reset_recovery_resolution(
    pipeline_id: str,
    context: str,
    resolution: str,
) -> None:
    """Dispatch the hard-reset recovery HITL ack (#2792).

    ``context`` is ``hard_reset_recovery:<phase>``; ``resolution`` is
    one of the two options the HITL exposed (``"Continue with post-reset
    state"`` or ``"Abort pipeline"``).

    Continue → :func:`routes.pipelines.resume_pipeline_after_hard_reset_ack`
    resets the failed phase exec state and spawns a fresh
    ``_run_pipeline`` thread so the populator (and downstream phase
    work) re-runs against the reconciled worktree.

    Abort → :func:`routes.pipelines.abort_pipeline_after_hard_reset_ack`
    transitions the pipeline to CANCELLED.  Cleanup of containers /
    worktrees / other pending decisions still happens via the existing
    PATCH ``update_pipeline`` flow the operator drives next.

    Unknown resolutions are logged at WARN and broadcast as an
    ``OVERSEER_ALERT`` so the operator notices the dispatch was skipped
    — the decision is already marked RESOLVED at this point, so the
    human's intent is preserved in state regardless, but the pipeline
    will stay stuck in ``failed_pending_hitl`` until someone intervenes.
    """
    phase_value = context.removeprefix("hard_reset_recovery:")

    # Local import to avoid circular import at module load
    # (routes.pipelines imports routes.decisions via the blueprint
    # registration path in some test setups).
    from routes.pipelines import (
        abort_pipeline_after_hard_reset_ack,
        resume_pipeline_after_hard_reset_ack,
    )

    if resolution == "Continue with post-reset state":
        ok = resume_pipeline_after_hard_reset_ack(
            pipeline_id,
            phase_value=phase_value,
        )
        if not ok:
            logger.warning(
                "hard_reset_recovery 'Continue' dispatch returned False",
                pipeline_id=pipeline_id,
                phase=phase_value,
            )
    elif resolution == "Abort pipeline":
        ok = abort_pipeline_after_hard_reset_ack(pipeline_id)
        if not ok:
            logger.warning(
                "hard_reset_recovery 'Abort' dispatch returned False",
                pipeline_id=pipeline_id,
            )
    else:
        logger.warning(
            "hard_reset_recovery resolved with unrecognized option",
            pipeline_id=pipeline_id,
            resolution=resolution[:80],
        )
        try:
            try:
                from message_store import Message, get_message_store
            except ImportError:
                from orchestrator.message_store import (  # type: ignore[no-redef]
                    Message,
                    get_message_store,
                )
            msg = Message(
                pipeline_id=pipeline_id,
                from_role="orchestrator",
                to_role="all",
                message_type="OVERSEER_ALERT",
                subject="hard-reset-recovery-unknown-resolution",
                body=(
                    "Hard-reset recovery HITL resolved with an unrecognized "
                    f"option (received: {resolution[:80]!r}). Expected one of "
                    "'Continue with post-reset state' or 'Abort pipeline'. The "
                    "decision is marked RESOLVED but no dispatch ran; the "
                    "pipeline will stay in failed_pending_hitl until the "
                    "operator re-resolves with a valid option."
                ),
                metadata={
                    "anomaly": "hard_reset_recovery_unknown_resolution",
                    "priority": "high",
                    "context": context,
                    "resolution": resolution[:80],
                },
                phase=phase_value or None,
            )
            get_message_store().add_message(msg)
        except Exception:  # noqa: BLE001
            # N5 follow-up: a broadcast failure here means the operator
            # never sees the alert.  Log at WARN so the bus-down path
            # leaves a trace alongside the unknown-resolution warning.
            logger.warning(
                "hard_reset_recovery OVERSEER_ALERT broadcast failed",
                pipeline_id=pipeline_id,
                exc_info=True,
            )


def _handle_conditional_ack_gate(
    pipeline_id: str,
    context: str,
    resolution: str,
    repo_path: Path,
) -> None:
    """Dispatch the 3-way conditional-ACK HITL gate resolution (#2004).

    ``context`` is the decision's raw context field, prefixed with
    ``CONDITIONAL_ACK_GATE_MARKER`` and followed by a JSON payload whose
    ``conditions`` entry is a list of ``{reviewer, producer, condition,
    version}`` dicts. ``resolution`` is the human's choice — one of the
    three option strings defined in ``routes.phases``.

    Dispatch:

    - **approve+accept**: write one line per condition to
      ``contract.pr.deferred_actions`` so obligations survive tracker
      teardown between phase close and PR creation (#2003 shipped the
      tracker-backed PR render; this is the durable path).
    - **reject**: call ``tracker.handle_nack`` on each (reviewer, producer)
      edge carrying a condition. Producer returns to WORKING; the caller
      must restart the phase to re-run consensus.
    - **address-in-pipeline**: call ``matrix.invalidate_ack`` on each
      conditioning edge. The ACK drops back to PENDING; the producer
      must re-propose before the phase can complete.

    Silently returns on malformed context or unknown resolution — the
    resolve_decision endpoint still records the resolution so the human's
    intent is preserved. Recovery relies on ``_ensure_conditional_ack_gate``
    re-queuing a new gate on the next ``complete_phase`` call when the
    tracker still has live conditions (the unresolved-decisions guard only
    checks ``PENDING`` decisions, so a resolved-but-failed gate would not
    be caught by that guard alone).
    """
    from routes.phases import (  # local import — avoid circular
        CONDITIONAL_ACK_ADDRESS,
        CONDITIONAL_ACK_APPROVE,
        CONDITIONAL_ACK_GATE_MARKER,
        CONDITIONAL_ACK_REJECT,
    )

    if not context.startswith(CONDITIONAL_ACK_GATE_MARKER):
        return
    payload_str = context[len(CONDITIONAL_ACK_GATE_MARKER) :]
    try:
        payload = json.loads(payload_str)
    except json.JSONDecodeError:
        logger.warning(
            "Conditional-ACK gate context is not valid JSON",
            pipeline_id=pipeline_id,
        )
        return

    conditions = payload.get("conditions") or []
    if not isinstance(conditions, list):
        return

    if resolution == CONDITIONAL_ACK_APPROVE:
        _persist_deferred_actions(pipeline_id, conditions, repo_path)
    elif resolution == CONDITIONAL_ACK_REJECT:
        _force_nack_conditional_edges(pipeline_id, conditions)
    elif resolution == CONDITIONAL_ACK_ADDRESS:
        _invalidate_conditional_acks(pipeline_id, conditions)
    else:
        logger.info(
            "Conditional-ACK gate resolved with unrecognized option",
            pipeline_id=pipeline_id,
            resolution=resolution[:80],
        )


def _persist_deferred_actions(
    pipeline_id: str,
    conditions: list[dict[str, Any]],
    repo_path: Path,
) -> None:
    """Write conditions to ``contract.pr.deferred_actions`` (#2004).

    Loads the contract from the pipeline's worktree, appends one formatted
    line per condition, and saves. Writes are deduplicated against any
    existing entries so resolving the same gate twice (or re-queuing after
    tracker-state changes) doesn't duplicate bullets on the PR.
    """
    try:
        from egg_contracts.loader import (
            ContractNotFoundError,
            ContractValidationError,
            load_contract,
            save_contract,
        )
        from egg_contracts.models import DeferredAction, PRMetadata
    except ImportError:
        logger.warning(
            "egg_contracts unavailable; cannot persist deferred_actions",
            pipeline_id=pipeline_id,
        )
        return

    try:
        from routes import resolve_worktree_path
        from routes.pipelines import _pipeline_identifier
        from state_store import get_state_store

        store = get_state_store(repo_path)
        pipeline = store.load_pipeline(pipeline_id)
        worktree_path = resolve_worktree_path(pipeline_id, repo_path)
        contract_id = _pipeline_identifier(pipeline.issue_number, pipeline.id)
        contract = load_contract(contract_id, worktree_path)
    except ContractNotFoundError, OSError, ValueError, ContractValidationError:
        logger.warning(
            "Cannot load contract to persist deferred_actions",
            pipeline_id=pipeline_id,
            exc_info=True,
        )
        return

    new_actions: list[DeferredAction] = []
    for c in conditions:
        reviewer = str(c.get("reviewer", "")).strip() or "unknown"
        condition = str(c.get("condition", "")).strip()
        if not condition:
            continue
        resolved_in_diff = str(c.get("resolved_in_diff", "")).strip()
        new_actions.append(
            DeferredAction(
                reviewer=reviewer,
                condition=condition,
                resolved_in_diff=resolved_in_diff,
            )
        )
    if not new_actions:
        return

    # PR metadata may be absent on ISSUE-mode pipelines that haven't yet hit
    # the PR-finalization writeback (unlikely here since the gate requires a
    # tracker, but defensive). Create a minimal stub using the issue title if so.
    if contract.pr is None:
        contract.pr = PRMetadata(
            title=(contract.issue.title if contract.issue else "Pipeline deferred actions"),
        )

    # Dedupe by (reviewer, condition) so re-resolving the same gate doesn't
    # double-list. A later call that adds a ``resolved_in_diff`` for an
    # already-persisted obligation upgrades the existing entry in place.
    #
    # Dedup is intentionally one-way (#2336 review):
    #   - SHA-replaces-SHA: an existing resolution is *not* overwritten
    #     by a later resolution for the same ``(reviewer, condition)``.
    #     Once a reviewer marks an obligation resolved, the recorded SHA
    #     is sticky.
    #   - Resolved → open downgrade: a later open-only entry does *not*
    #     clear ``resolved_in_diff``. This matches the pre-existing
    #     append-only design of contract-persisted obligations (#2004).
    # Both cases are edge cases driven by NACK / re-propose / reviewer
    # re-ACK cycles; the live tracker remains the source of truth for
    # in-flight state and is preferred by the renderer at Tier 2.
    merged: list[DeferredAction] = list(contract.pr.deferred_actions)
    by_key: dict[tuple[str, str], DeferredAction] = {(a.reviewer, a.condition): a for a in merged}
    for action in new_actions:
        key = (action.reviewer, action.condition)
        existing = by_key.get(key)
        if existing is None:
            merged.append(action)
            by_key[key] = action
        elif action.resolved_in_diff and not existing.resolved_in_diff:
            # Upgrade open → resolved; preserve list ordering.
            existing.resolved_in_diff = action.resolved_in_diff
    contract.pr.deferred_actions = merged

    try:
        save_contract(contract, worktree_path)
    except OSError, ValueError:
        # The gate decision is already resolved, so the human's intent is
        # recorded. Recovery depends on the tracker surviving until the
        # next complete_phase call, where _ensure_conditional_ack_gate
        # re-queues a new gate. If the tracker is torn down before that
        # (e.g. orchestrator restart), the obligations are silently lost.
        logger.warning(
            "Failed to save contract with deferred_actions",
            pipeline_id=pipeline_id,
            exc_info=True,
        )
        return

    logger.info(
        "Persisted pre-merge obligations to contract",
        pipeline_id=pipeline_id,
        deferred_action_count=len(merged),
    )


def _force_nack_conditional_edges(
    pipeline_id: str,
    conditions: list[dict[str, Any]],
) -> None:
    """Force-NACK each (reviewer, producer) edge carrying a condition (#2004).

    The human has rejected the obligation. This is not a reviewer-
    authored NACK — there's no proposal artifact to cite, and the
    ReviewPayload schema rightly rejects empty artifact_references.
    Instead, drive the approval matrix + producer-phase state directly
    so the end state matches a normal NACK: edge NACKED at current
    version, condition cleared, producer back in WORKING.
    """
    from peer_consensus import ConsensusPhase

    tracker = get_peer_consensus_tracker(pipeline_id)
    if tracker is None:
        logger.warning(
            "No active tracker to force-NACK conditional ACK",
            pipeline_id=pipeline_id,
        )
        return

    synthetic_reason = "human rejected conditional ACK"
    nacked: list[tuple[str, str]] = []
    with tracker._lock:
        for c in conditions:
            reviewer = str(c.get("reviewer", "")).strip()
            producer = str(c.get("producer", "")).strip()
            if not reviewer or not producer:
                continue
            try:
                version = tracker.matrix.get_proposal_version(producer)
                tracker.matrix.record_nack(
                    reviewer,
                    producer,
                    version,
                    reason=synthetic_reason,
                    artifact_refs=[],
                )
                tracker._producer_phases[producer] = ConsensusPhase.WORKING
                nacked.append((reviewer, producer))
            except Exception:
                logger.warning(
                    "Failed to force-NACK conditional edge",
                    pipeline_id=pipeline_id,
                    reviewer=reviewer,
                    producer=producer,
                    exc_info=True,
                )
    if nacked:
        logger.info(
            "Force-NACKed conditional ACK edges",
            pipeline_id=pipeline_id,
            edges=nacked,
        )


def _invalidate_conditional_acks(
    pipeline_id: str,
    conditions: list[dict[str, Any]],
) -> None:
    """Invalidate each conditioning ACK edge so the producer re-proposes (#2004).

    Unlike NACK, invalidation doesn't bump the revision count — the ACK
    just drops back to PENDING. The producer phase state is reset to
    WORKING so it can re-propose with the condition folded into its
    next proposal's scope.
    """
    from peer_consensus import ConsensusPhase

    tracker = get_peer_consensus_tracker(pipeline_id)
    if tracker is None:
        logger.warning(
            "No active tracker to invalidate conditional ACK",
            pipeline_id=pipeline_id,
        )
        return

    invalidated: list[tuple[str, str]] = []
    with tracker._lock:
        for c in conditions:
            reviewer = str(c.get("reviewer", "")).strip()
            producer = str(c.get("producer", "")).strip()
            if not reviewer or not producer:
                continue
            try:
                did_invalidate = tracker.matrix.invalidate_ack(reviewer, producer)
            except Exception:
                logger.warning(
                    "Failed to invalidate conditional ACK",
                    pipeline_id=pipeline_id,
                    reviewer=reviewer,
                    producer=producer,
                    exc_info=True,
                )
                continue
            if did_invalidate:
                tracker._producer_phases[producer] = ConsensusPhase.WORKING
                invalidated.append((reviewer, producer))
    if invalidated:
        logger.info(
            "Invalidated conditional ACK edges for in-pipeline address",
            pipeline_id=pipeline_id,
            edges=invalidated,
        )


@decisions_bp.route("/<pipeline_id>/decisions", methods=["GET"])
def list_decisions(pipeline_id: str) -> tuple[Response, int]:
    """
    List decisions for a pipeline.

    URL params:
        pipeline_id: Pipeline ID

    Query params:
        pending_only: Only return pending decisions (default: false)

    Response:
        {
            "success": true,
            "data": {
                "decisions": [
                    {
                        "id": "decision-1",
                        "question": "...",
                        "status": "pending",
                        ...
                    }
                ]
            }
        }
    """
    try:
        store, _pipeline = get_state_store_for_pipeline(pipeline_id)
    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )

    pending_only = request.args.get("pending_only", "false").lower() == "true"

    try:
        queue = get_decision_queue(pipeline_id, store.repo_path)

        if pending_only:
            decisions = queue.get_pending_decisions()
        else:
            pipeline = queue._load_pipeline()
            decisions = pipeline.decisions

        decision_data = [
            {
                "id": d.id,
                "question": d.question,
                "context": d.context,
                "options": d.options,
                "decision_type": d.decision_type,
                "questions": d.questions,
                "status": d.status.value,
                "created_at": d.created_at.isoformat(),
                "resolved_at": d.resolved_at.isoformat() if d.resolved_at else None,
                "resolution": d.resolution,
                "phase": d.phase.value if d.phase else None,
                "content_changed": d.content_changed,
            }
            for d in decisions
        ]

        return make_success_response(
            f"Found {len(decisions)} decision(s)",
            data={"decisions": decision_data},
        )

    except Exception as e:
        logger.error("Failed to list decisions", pipeline_id=pipeline_id, error=str(e))
        return make_error_response(f"Failed to list decisions: {e}", status_code=500)


@decisions_bp.route("/<pipeline_id>/decisions", methods=["POST"])
def queue_decision(pipeline_id: str) -> tuple[Response, int]:
    """
    Queue a new decision for human review.

    URL params:
        pipeline_id: Pipeline ID

    Request body:
        {
            "question": "Which approach should we use?",
            "context": "Additional context...",  // optional
            "options": ["Option A", "Option B"]  // optional
        }

    Response:
        {
            "success": true,
            "data": {
                "decision": {...}
            }
        }
    """
    raw = request.get_json()
    if raw is not None and not isinstance(raw, dict):
        return make_error_response("Request body must be a JSON object")
    data = raw if raw is not None else {}

    question = data.get("question")
    if not question:
        return make_error_response("Missing question")

    VALID_DECISION_TYPES = ("phase_gate", "choice", "feedback")
    decision_type = data.get("decision_type", "choice")
    if decision_type not in VALID_DECISION_TYPES:
        return make_error_response(
            f"Invalid decision_type '{decision_type}'. Must be one of: {', '.join(VALID_DECISION_TYPES)}"
        )

    phase_str = data.get("phase")
    phase = None
    if phase_str is not None:
        try:
            phase = PipelinePhase(phase_str)
        except ValueError:
            valid_phases = [p.value for p in PipelinePhase]
            return make_error_response(
                f"Invalid phase '{phase_str}'. Must be one of: {', '.join(valid_phases)}"
            )

    try:
        store, _pipeline = get_state_store_for_pipeline(pipeline_id)
    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )

    try:
        queue = get_decision_queue(pipeline_id, store.repo_path)
        decision = queue.queue_decision(
            question=question,
            context=data.get("context", ""),
            options=data.get("options"),
            decision_type=decision_type,
            questions=data.get("questions"),
            phase=phase,
        )

        logger.info(
            "Decision queued",
            pipeline_id=pipeline_id,
            decision_id=decision.id,
        )

        return make_success_response(
            "Decision queued",
            data={
                "decision": {
                    "id": decision.id,
                    "question": decision.question,
                    "decision_type": decision.decision_type,
                    "questions": decision.questions,
                    "status": decision.status.value,
                    "created_at": decision.created_at.isoformat(),
                    "phase": decision.phase.value if decision.phase else None,
                }
            },
        )

    except Exception as e:
        logger.error("Failed to queue decision", pipeline_id=pipeline_id, error=str(e))
        return make_error_response(f"Failed to queue decision: {e}", status_code=500)


@decisions_bp.route("/<pipeline_id>/decisions/<decision_id>", methods=["GET"])
def get_decision(pipeline_id: str, decision_id: str) -> tuple[Response, int]:
    """
    Get a specific decision.

    URL params:
        pipeline_id: Pipeline ID
        decision_id: Decision ID

    Response:
        {
            "success": true,
            "data": {
                "decision": {...}
            }
        }
    """
    try:
        store, _pipeline = get_state_store_for_pipeline(pipeline_id)
    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )

    try:
        queue = get_decision_queue(pipeline_id, store.repo_path)
        decision = queue.get_decision(decision_id)

        return make_success_response(
            "Decision retrieved",
            data={
                "decision": {
                    "id": decision.id,
                    "question": decision.question,
                    "context": decision.context,
                    "options": decision.options,
                    "decision_type": decision.decision_type,
                    "questions": decision.questions,
                    "status": decision.status.value,
                    "created_at": decision.created_at.isoformat(),
                    "resolved_at": decision.resolved_at.isoformat()
                    if decision.resolved_at
                    else None,
                    "resolution": decision.resolution,
                    "phase": decision.phase.value if decision.phase else None,
                }
            },
        )

    except DecisionNotFoundError:
        return make_error_response(
            f"Decision {decision_id} not found",
            status_code=404,
        )


@decisions_bp.route("/<pipeline_id>/decisions/<decision_id>/resolve", methods=["POST"])
@require_lifecycle_secret
def resolve_decision(pipeline_id: str, decision_id: str) -> tuple[Response, int]:
    """
    Resolve a pending decision.

    URL params:
        pipeline_id: Pipeline ID
        decision_id: Decision ID

    Request body:
        {
            "resolution": "Selected option or free-form response"
        }

    Response:
        {
            "success": true,
            "data": {
                "decision": {...}
            }
        }
    """
    raw = request.get_json()
    if raw is not None and not isinstance(raw, dict):
        return make_error_response("Request body must be a JSON object")
    data = raw if raw is not None else {}

    resolution = data.get("resolution")
    if not resolution:
        return make_error_response("Missing resolution")

    # Ensure resolution is always a JSON string — callers may send a dict
    # instead of a pre-serialized string (#1635).
    if not isinstance(resolution, str):
        resolution = json.dumps(resolution)

    try:
        store, _pipeline = get_state_store_for_pipeline(pipeline_id)
    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )

    try:
        queue = get_decision_queue(pipeline_id, store.repo_path)
        decision = queue.resolve_decision(decision_id, resolution)

        logger.info(
            "Decision resolved",
            pipeline_id=pipeline_id,
            decision_id=decision_id,
            source=getattr(request, "egg_source", "unknown"),
        )

        try:
            emit_event(
                EventType.DECISION_RESOLVED,
                pipeline_id=pipeline_id,
                data={
                    "decision_id": decision_id,
                    "resolution": decision.resolution,
                },
            )
        except Exception:
            logger.warning(
                "Failed to emit DECISION_RESOLVED event",
                pipeline_id=pipeline_id,
                decision_id=decision_id,
                exc_info=True,
            )

        # Handle "Restart agent" resolution (#1428).
        # The overseer creates decisions with question format:
        #   "Agent <role> issue: <message>"
        # When the human resolves with "Restart agent", stop the old
        # container and respawn a replacement.
        if decision.resolution == "Restart agent":
            _handle_restart_agent(pipeline_id, decision.question)

        # Handle the conditional-ACK 3-way HITL gate (#2004). Context
        # prefix is the discriminator — the question text is arbitrary
        # prose and mustn't be relied on for dispatch.
        if decision.context and decision.resolution:
            _handle_conditional_ack_gate(
                pipeline_id,
                decision.context,
                decision.resolution,
                store.repo_path,
            )

        # Handle "Continue without" resolution for failed reviewer decisions.
        # The concurrent executor stores "failed_role:<role>" in the decision
        # context when a reviewer crashes. Excuse the reviewer so consensus
        # can proceed without their ACK.
        if decision.resolution == "Continue without" and decision.context.startswith(
            "failed_role:"
        ):
            failed_role = decision.context.removeprefix("failed_role:")
            tracker = get_peer_consensus_tracker(pipeline_id)
            if tracker:
                try:
                    excuse_result = tracker.excuse_reviewer(failed_role)
                    logger.info(
                        "Excused reviewer after 'Continue without' decision",
                        pipeline_id=pipeline_id,
                        failed_role=failed_role,
                        affected_producers=excuse_result.get("affected_producers"),
                    )
                except Exception:
                    logger.warning(
                        "Failed to excuse reviewer",
                        pipeline_id=pipeline_id,
                        failed_role=failed_role,
                        exc_info=True,
                    )

        # #2792: hard-reset recovery ack. ``context`` is
        # ``hard_reset_recovery:<phase>``; dispatch on the prefix so the
        # branch is independent of the prose-y question text.
        if decision.context and decision.context.startswith("hard_reset_recovery:"):
            _handle_hard_reset_recovery_resolution(
                pipeline_id,
                decision.context,
                decision.resolution or "",
            )

        return make_success_response(
            "Decision resolved",
            data={
                "decision": {
                    "id": decision.id,
                    "status": decision.status.value,
                    "resolution": decision.resolution,
                    "resolved_at": decision.resolved_at.isoformat()
                    if decision.resolved_at
                    else None,
                }
            },
        )

    except DecisionNotFoundError:
        return make_error_response(
            f"Decision {decision_id} not found",
            status_code=404,
        )
    except DecisionAlreadyResolvedError as e:
        return make_error_response(str(e), status_code=409)


@decisions_bp.route("/<pipeline_id>/decisions/<decision_id>/cancel", methods=["POST"])
@require_lifecycle_secret
def cancel_decision(pipeline_id: str, decision_id: str) -> tuple[Response, int]:
    """
    Cancel a pending decision.

    URL params:
        pipeline_id: Pipeline ID
        decision_id: Decision ID

    Response:
        {
            "success": true,
            "message": "Decision cancelled"
        }
    """
    try:
        store, _pipeline = get_state_store_for_pipeline(pipeline_id)
    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )

    try:
        queue = get_decision_queue(pipeline_id, store.repo_path)
        decision = queue.cancel_decision(decision_id)

        logger.info(
            "Decision cancelled",
            pipeline_id=pipeline_id,
            decision_id=decision_id,
            source=getattr(request, "egg_source", "unknown"),
        )

        return make_success_response(
            "Decision cancelled",
            data={
                "decision": {
                    "id": decision.id,
                    "status": decision.status.value,
                }
            },
        )

    except DecisionNotFoundError:
        return make_error_response(
            f"Decision {decision_id} not found",
            status_code=404,
        )


@decisions_bp.route("/<pipeline_id>/decisions/status", methods=["GET"])
def get_queue_status(pipeline_id: str) -> tuple[Response, int]:
    """
    Get decision queue status.

    URL params:
        pipeline_id: Pipeline ID

    Response:
        {
            "success": true,
            "data": {
                "total_decisions": 5,
                "pending": 2,
                "resolved": 3,
                "pending_decisions": [...]
            }
        }
    """
    try:
        store, _pipeline = get_state_store_for_pipeline(pipeline_id)
    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )

    try:
        queue = get_decision_queue(pipeline_id, store.repo_path)
        status = queue.get_queue_status()

        return make_success_response("Status retrieved", data=status)

    except Exception as e:
        logger.error("Failed to get queue status", pipeline_id=pipeline_id, error=str(e))
        return make_error_response(f"Failed to get status: {e}", status_code=500)
