"""
Decision endpoints for HITL integration.

Provides REST endpoints for queuing, polling, and resolving
human-in-the-loop decisions.
"""

import json
import re
import sys
from datetime import UTC, datetime
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


def _normalize_choice_resolution(resolution: str) -> str:
    """Unwrap a structured ``choice`` envelope to its bare option label (#2978).

    The local SDLC HITL CLI resolves a ``choice`` decision by sending
    ``{"action": "select", "selected": "<option>"}`` (see
    ``sandbox/egg_lib/sdlc_hitl.py``); :func:`resolve_decision`
    JSON-serializes that dict into ``decision.resolution``.  Dispatch
    hooks that compare the resolution against bare option labels must
    unwrap the envelope first — otherwise every structured selection
    reads as an unrecognized option.  Bare-string resolutions (legacy /
    direct-API callers) and any other shape pass through unchanged so
    the caller's existing matching still runs.

    Audit-trail note: this is a dispatch-side unwrap only.  The
    persisted ``decision.resolution`` (and the ``DECISION_RESOLVED``
    event / API response payload) still carries the raw envelope JSON
    as the operator sent it.  Only the in-process value routed to the
    Restart-agent / Continue-without / conditional-ACK / hard-reset
    dispatch helpers — and any subsequent log line that echoes it — is
    the normalized form.
    """
    if not resolution:
        return resolution
    try:
        payload = json.loads(resolution)
    except json.JSONDecodeError:
        return resolution
    if isinstance(payload, dict) and payload.get("action") == "select":
        selected = payload.get("selected")
        if isinstance(selected, str):
            return selected
    return resolution


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

    # #2978: defense-in-depth — unwrap the ``choice`` envelope so a future
    # direct caller bypassing ``resolve_decision``'s dispatch-boundary
    # normalization still sees the bare option label below.  Idempotent on
    # already-unwrapped strings.
    resolution = _normalize_choice_resolution(resolution)

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

        # #2978: normalize the choice envelope once at the dispatch
        # boundary so every dispatch hook below sees the bare option
        # label instead of the ``{"action": "select", "selected": ...}``
        # envelope the SDLC HITL CLI sends.  ``decision.resolution``
        # (persisted on disk, emitted on ``DECISION_RESOLVED``, and
        # returned in the API response) is intentionally unchanged —
        # the audit trail keeps the raw envelope while dispatch routes
        # on the unwrapped label.
        dispatch_resolution = _normalize_choice_resolution(decision.resolution or "")

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
        if dispatch_resolution == "Restart agent":
            _handle_restart_agent(pipeline_id, decision.question)

        # Handle the conditional-ACK 3-way HITL gate (#2004). Context
        # prefix is the discriminator — the question text is arbitrary
        # prose and mustn't be relied on for dispatch.
        if decision.context and dispatch_resolution:
            _handle_conditional_ack_gate(
                pipeline_id,
                decision.context,
                dispatch_resolution,
                store.repo_path,
            )

        # Handle "Continue without" resolution for failed reviewer decisions.
        # The concurrent executor stores "failed_role:<role>" in the decision
        # context when a reviewer crashes. Excuse the reviewer so consensus
        # can proceed without their ACK.
        if dispatch_resolution == "Continue without" and decision.context.startswith(
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

        # #2979: the worktree-divergence reconcile no longer dispatches on
        # resolution.  The in-loop phase-boundary callers block on the
        # reconcile HITL and resume inline once it resolves; the
        # populate_contract route surfaces it as a 409 and the operator
        # re-runs the endpoint after reconciling.  No restart-phase
        # dispatch is needed (and the destructive hard-reset recovery that
        # required one was removed), so resolution just marks the decision
        # RESOLVED.

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
        # Not in the orchestrator queue — fall back to contract-resident
        # decisions (#3071).  Agents register HITL questions on the SDLC
        # contract (``cq-N``, via ``register_open_question`` or the
        # impasse-escalation router); those reach this queue only through
        # the post-gate bridge (``_queue_and_await_contract_decisions``),
        # which runs after phase_gate approval.  A producer blocked
        # *pre-propose* therefore deadlocked with no operator channel:
        # this endpoint 404'd and ``answer_feedback`` covers only
        # ``contract.feedback`` (#3007).  Mirror ``answer_feedback``'s
        # direct contract write-back so the blocked agent unblocks on its
        # next contract poll.
        return _resolve_contract_decision(
            pipeline_id=pipeline_id,
            decision_id=decision_id,
            resolution=resolution,
            pipeline=_pipeline,
        )
    except DecisionAlreadyResolvedError as e:
        return make_error_response(str(e), status_code=409)


def _resolve_contract_decision(
    pipeline_id: str,
    decision_id: str,
    resolution: str,
    pipeline: Any,
) -> tuple[Response, int]:
    """Resolve a contract-resident HITL decision (``cq-N``) directly (#3071).

    Contract decisions are registered by agents
    (``mcp__sdlc__register_open_question``) or by the orchestrator's
    impasse-escalation router (:func:`impasse_routing.route_impasses`);
    they live only in ``.egg-state/contracts/{identifier}.json`` and are
    bridged into the orchestrator decision queue only *after* phase_gate
    approval.  An agent blocked on such a decision before proposing never
    reaches the gate, so the bridge never runs and the operator had no
    resolution channel — the producer respawn loop burned cost with no
    pause (#3071, observed on pipeline-c2faf164).

    This fallback gives the operator a first-class path for that pre-gate
    window, mirroring :func:`answer_feedback` (#3007): write the
    resolution fields straight onto the contract decision so the blocked
    agent unblocks on its next contract poll.  The write-back shape
    (``resolved_by="human"``, raw resolution string) matches the
    post-gate bridge's, so downstream consumers see one format.  The
    caller's route decorator is lifecycle-secret guarded, so an agent
    cannot resolve its own question (parity with queue decisions, #1769).
    """
    # Lazy imports — same seam and rationale as ``answer_feedback``:
    # ``contract_store`` / ``routes.pipelines`` pull in heavy state-store
    # dependencies that must not bind at module import time.
    import contract_store

    try:
        from egg_contracts import (
            ContractNotFoundError,
            ContractValidationError,
            load_contract,
            save_contract,
        )
    except ImportError as exc:
        logger.error(
            "egg_contracts not available; cannot resolve contract decision",
            pipeline_id=pipeline_id,
            decision_id=decision_id,
            error=str(exc),
        )
        return make_error_response(
            "Contract subsystem (egg_contracts) is not available on this host",
            status_code=500,
        )
    from routes.pipelines import _pipeline_identifier

    worktree = contract_store.resolve_pipeline_worktree(pipeline_id)
    if worktree is None:
        return make_error_response(
            f"Decision {decision_id} not found (not in the orchestrator "
            f"queue, and no pipeline worktree exists to check the contract)",
            status_code=404,
        )

    identifier = _pipeline_identifier(getattr(pipeline, "issue_number", None), pipeline_id)

    with contract_store.lock_for(identifier):
        try:
            contract = load_contract(identifier, worktree)
        except ContractNotFoundError:
            return make_error_response(
                f"Decision {decision_id} not found (not in the orchestrator "
                f"queue; no contract exists for {pipeline_id})",
                status_code=404,
            )
        except ContractValidationError as exc:
            return make_error_response(
                f"Contract validation failed: {exc}",
                status_code=500,
            )

        decision = next((d for d in (contract.decisions or []) if d.id == decision_id), None)
        if decision is None:
            return make_error_response(
                f"Decision {decision_id} not found (neither in the "
                f"orchestrator queue nor on the contract)",
                status_code=404,
            )
        if decision.resolved:
            return make_error_response(
                f"Decision {decision_id} has already been resolved",
                status_code=409,
            )

        resolved_at = datetime.now(UTC)
        decision.resolved = True
        decision.resolution = resolution
        decision.resolved_by = "human"
        decision.resolved_at = resolved_at

        try:
            save_contract(contract, worktree)
        except Exception as exc:
            logger.error(
                "Failed to save contract after resolving contract decision",
                pipeline_id=pipeline_id,
                decision_id=decision_id,
                error=str(exc),
            )
            return make_error_response(
                f"Failed to save contract: {exc}",
                status_code=500,
            )

    logger.info(
        "Contract decision resolved by operator",
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
                "resolution": resolution,
                "scope": "contract",
            },
        )
    except Exception:
        logger.warning(
            "Failed to emit DECISION_RESOLVED event",
            pipeline_id=pipeline_id,
            decision_id=decision_id,
            exc_info=True,
        )

    return make_success_response(
        "Decision resolved",
        data={
            "decision": {
                "id": decision_id,
                "status": "resolved",
                "resolution": resolution,
                "resolved_at": resolved_at.isoformat(),
                "scope": "contract",
            }
        },
    )


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


@decisions_bp.route("/<pipeline_id>/feedback/answer", methods=["POST"])
@require_lifecycle_secret
def answer_feedback(pipeline_id: str) -> tuple[Response, int]:
    """Answer a contract-scoped feedback request from the host operator.

    Agents register open-ended feedback via ``register_feedback_request``
    (``mcp__sdlc__request_feedback``).  Pre-proposal feedback — e.g. a
    refiner asking for a goal on an empty contract — is written only to
    the gateway-backed contract as ``contract.feedback`` (id
    ``feedback-N``).  It is **never** queued as an orchestrator decision
    until *after* the phase_gate is approved by the post-gate bridge
    (:func:`routes.pipelines._queue_and_await_contract_decisions`), so a
    refiner that blocks on the answer before producing any proposal
    deadlocks the pipeline: ``provide_input`` 404s (no such decision in
    the queue) and the only documented answer path
    (``egg-contract``) runs inside agent containers, unreachable from
    the host (#3007).

    This endpoint gives the host operator a first-class answer path. It
    writes the answers straight into the contract and marks the feedback
    submitted — mirroring the write-back the post-gate bridge performs —
    so the waiting agent unblocks on its next contract poll. It is
    lifecycle-secret guarded: only the operator/MCP carry the secret, so
    an agent cannot answer its own feedback (parity with decision
    resolve; see #1769).

    URL params:
        pipeline_id: Pipeline ID

    Request body:
        {
            "answers": {"Q1": "...", "Q2": "..."},   # question id -> answer
            "feedback_id": "feedback-1"                # optional guard
        }
    """
    raw = request.get_json(silent=True)
    if raw is not None and not isinstance(raw, dict):
        return make_error_response("Request body must be a JSON object")
    data = raw if raw is not None else {}

    raw_answers = data.get("answers")
    if not isinstance(raw_answers, dict) or not raw_answers:
        return make_error_response("Missing 'answers' (object mapping question id to answer text)")
    non_string = sorted(str(k) for k, v in raw_answers.items() if not isinstance(v, str))
    if non_string:
        return make_error_response(
            "Answer values must be strings; non-string values for question id(s) "
            f"{non_string}. To leave a question unanswered, omit its id from 'answers'.",
        )
    answers = {str(k): v for k, v in raw_answers.items()}
    requested_feedback_id = data.get("feedback_id")

    try:
        _store, pipeline = get_state_store_for_pipeline(pipeline_id)
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

    # Lazy imports: ``contract_store`` and ``routes.pipelines`` pull in
    # heavy state-store / docker dependencies; importing them at module
    # top would couple decisions.py to initialisation order. Same pattern
    # as contracts.py's ``_branch_read_contract``. The ``egg_contracts``
    # try/except mirrors the post-gate bridge in
    # ``_queue_and_await_contract_decisions`` so grepping for
    # ``egg_contracts`` ImportError handling finds both sites.
    import contract_store

    try:
        from egg_contracts import (
            ContractNotFoundError,
            ContractValidationError,
            load_contract,
            save_contract,
        )
    except ImportError as exc:
        logger.error(
            "egg_contracts not available; cannot answer contract feedback",
            pipeline_id=pipeline_id,
            error=str(exc),
        )
        return make_error_response(
            "Contract subsystem (egg_contracts) is not available on this host",
            status_code=500,
        )
    from routes.pipelines import _pipeline_identifier

    worktree = contract_store.resolve_pipeline_worktree(pipeline_id)
    if worktree is None:
        return make_error_response(
            f"Pipeline worktree not found for {pipeline_id}",
            status_code=404,
        )

    identifier = _pipeline_identifier(getattr(pipeline, "issue_number", None), pipeline_id)

    with contract_store.lock_for(identifier):
        try:
            contract = load_contract(identifier, worktree)
        except ContractNotFoundError:
            return make_error_response(
                f"Contract for {pipeline_id} not found",
                status_code=404,
            )
        except ContractValidationError as exc:
            return make_error_response(
                f"Contract validation failed: {exc}",
                status_code=500,
            )

        feedback = contract.feedback
        if feedback is None:
            return make_error_response(
                "No feedback request is pending on this contract",
                status_code=404,
            )
        if requested_feedback_id and feedback.id != requested_feedback_id:
            return make_error_response(
                f"Feedback {requested_feedback_id} not found "
                f"(pending feedback on this contract is {feedback.id})",
                status_code=404,
            )
        if feedback.submitted:
            return make_error_response(
                f"Feedback {feedback.id} has already been submitted",
                status_code=409,
            )

        valid_ids = {q.id for q in feedback.questions}
        unknown_ids = sorted(qid for qid in answers if qid not in valid_ids)
        if unknown_ids:
            return make_error_response(
                f"Unknown question id(s) {unknown_ids}; "
                f"valid ids for {feedback.id}: {sorted(valid_ids)}",
                status_code=400,
            )

        for question in feedback.questions:
            if question.id in answers:
                question.answer = answers[question.id]
        # Mark submitted after applying answers — even a partial answer
        # set counts as the human responding, matching the bridge's
        # write-back so the agent isn't re-prompted.
        feedback.submitted = True
        feedback.submitted_by = "human"
        feedback.submitted_at = datetime.now(UTC)

        try:
            save_contract(contract, worktree)
        except Exception as exc:
            logger.error(
                "Failed to save contract after answering feedback",
                pipeline_id=pipeline_id,
                feedback_id=feedback.id,
                error=str(exc),
            )
            return make_error_response(
                f"Failed to save contract: {exc}",
                status_code=500,
            )

        answered_feedback = {
            "id": feedback.id,
            "submitted": feedback.submitted,
            "questions": [
                {"id": q.id, "question": q.question, "answer": q.answer} for q in feedback.questions
            ],
        }

    logger.info(
        "Contract feedback answered by operator",
        pipeline_id=pipeline_id,
        feedback_id=answered_feedback["id"],
        answered_questions=sorted(answers.keys()),
        source=getattr(request, "egg_source", "unknown"),
    )

    return make_success_response(
        "Feedback answered",
        data={"feedback": answered_feedback},
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
