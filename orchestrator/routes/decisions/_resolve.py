"""resolve_decision + contract-decision fallback (#3312 decomposition)."""

import json
from datetime import UTC, datetime
from typing import Any

import routes.decisions as _pkg
from decision_queue import DecisionAlreadyResolvedError, DecisionNotFoundError
from events import EventType
from flask import Response, request
from state_store import InvalidPipelineIdError, PipelineNotFoundError

from . import logger
from ._responses import make_error_response, make_success_response


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
        store, _pipeline = _pkg.get_state_store_for_pipeline(pipeline_id)
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
        queue = _pkg.get_decision_queue(pipeline_id, store.repo_path)
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
        dispatch_resolution = _pkg._normalize_choice_resolution(decision.resolution or "")

        try:
            _pkg.emit_event(
                EventType.DECISION_RESOLVED,
                pipeline_id=pipeline_id,
                data={
                    "decision_id": decision_id,
                    "resolution": decision.resolution,
                    "scope": "queue",
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
            _pkg._handle_restart_agent(pipeline_id, decision.question)

        # Handle the conditional-ACK 3-way HITL gate (#2004). Context
        # prefix is the discriminator — the question text is arbitrary
        # prose and mustn't be relied on for dispatch.
        if decision.context and dispatch_resolution:
            _pkg._handle_conditional_ack_gate(
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
            tracker = _pkg.get_peer_consensus_tracker(pipeline_id)
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

        # Executable task-completion option (#3124): "Mark task <id>
        # complete" performs the audited operator mutation instead of
        # leaving the operator to impersonate an agent role via pod exec.
        executed_action = _pkg._maybe_complete_task_from_resolution(
            pipeline_id, decision_id, dispatch_resolution
        )

        # #3233: if the orchestrator restarted while this pipeline was parked
        # AWAITING_HUMAN, the in-memory _run_pipeline driver that polls
        # wait_for_decision is gone — this resolution would otherwise be
        # recorded with no consumer and the pipeline hangs silently. Revive
        # the driver via start_pipeline's AWAITING_HUMAN recovery once the
        # queue has no remaining pending decisions. No-ops when a live driver
        # is already polling (the normal in-process path consumes it).
        #
        # Gate strictly on the *resolved decision* being a phase_gate.
        # start_pipeline's AWAITING_HUMAN recovery assumes the park is a
        # phase gate (it advances/resets the phase from the latest resolved
        # phase_gate resolution); AWAITING_HUMAN is *not* synonymous with
        # "parked at a phase gate". Other driverless AWAITING_HUMAN parks —
        # the worktree-divergence reconcile HITL (#2979), which parks with a
        # deliberately inert ``choice`` decision the operator resolves before
        # re-running populate_contract — would otherwise be force-advanced
        # spuriously: with no fresh phase_gate resolution, _parse_resolution
        # treats the absent resolution as approval and marks the current
        # phase complete. Only a phase_gate resolution is meant to drive the
        # phase forward, so only it triggers the revival.
        if decision.decision_type == "phase_gate":
            try:
                from routes.pipelines import maybe_revive_orphaned_awaiting_human_driver

                maybe_revive_orphaned_awaiting_human_driver(pipeline_id, store.repo_path)
            except Exception:
                logger.warning(
                    "Orphaned-driver revival check failed after decision resolve "
                    "(decision is still resolved) (#3233)",
                    pipeline_id=pipeline_id,
                    decision_id=decision_id,
                    exc_info=True,
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
                    "scope": "queue",
                },
                **({"executed_action": executed_action} if executed_action else {}),
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
        return _pkg._resolve_contract_decision(
            pipeline_id=pipeline_id,
            decision_id=decision_id,
            resolution=resolution,
            pipeline=_pipeline,
            queue=queue,
        )
    except DecisionAlreadyResolvedError as e:
        return make_error_response(str(e), status_code=409)


def _resolve_contract_decision(
    pipeline_id: str,
    decision_id: str,
    resolution: str,
    pipeline: Any,
    queue: Any,
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
    (``resolved_by="human"``, stripped resolution string) matches the
    post-gate bridge's (``_queue_and_await_contract_decisions``), so
    downstream consumers see one format.  The caller's route decorator is
    lifecycle-secret guarded, so an agent cannot resolve its own question
    (parity with queue decisions, #1769).

    **Post-gate guard.** Once the bridge has mirrored ``cq-N`` to a fresh
    ``decision-M``, the pipeline thread is blocked on
    ``wait_for_decision(decision-M)`` (no timeout — see
    ``orchestrator/decision_queue.py``).  Resolving the contract ``cq-N``
    here would unblock the agent on its next poll but strand the bridge
    thread indefinitely.  Before falling back, scan pending queue
    decisions for the bridge's context-string fingerprint
    (``"Open contract question {cq-id},"``) and 409 with a pointer to
    the mirror id so the operator resolves the queue-side decision
    instead.
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
        from egg_contracts.models import DecisionType
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

    # Post-gate guard: if the bridge has already mirrored this ``cq-N`` into
    # the orchestrator queue, resolving the contract side here would leave
    # the bridge's ``wait_for_decision`` polling indefinitely (no timeout,
    # no automatic recovery on DECISION_RESOLVED).  Detect the mirror via
    # the bridge's context-string fingerprint and steer the operator to
    # the queue id instead.
    bridge_context_prefix = f"Open contract question {decision_id},"
    try:
        for pending in queue.get_pending_decisions():
            if pending.context and pending.context.startswith(bridge_context_prefix):
                return make_error_response(
                    f"Decision {decision_id} has been bridged into the "
                    f"orchestrator queue as {pending.id}; resolve that id "
                    f"instead so the pipeline thread blocked on the bridge's "
                    f"wait_for_decision is also unblocked.",
                    status_code=409,
                )
    except Exception:
        logger.warning(
            "Failed to scan queue for bridged contract decision; proceeding with fallback",
            pipeline_id=pipeline_id,
            decision_id=decision_id,
            exc_info=True,
        )

    worktree = contract_store.resolve_pipeline_worktree(pipeline_id)
    if worktree is None:
        return make_error_response(
            f"Decision {decision_id} not found (not in the orchestrator "
            f"queue, and no pipeline worktree exists to check the contract)",
            status_code=404,
        )

    identifier = _pipeline_identifier(getattr(pipeline, "issue_number", None), pipeline_id)

    # Normalize for one-format parity with the post-gate bridge's
    # ``(resolved.resolution or "").strip()`` (#3071 review): with
    # free-form ``Other (explain in reply)``-style answers, an unstripped
    # write here would diverge from the bridge's on-disk shape.
    resolution = resolution.strip()

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
        # Defense-in-depth: only HITL decisions are operator-resolvable
        # (#3071 review).  ``AUTO`` decisions are auto-resolved by the
        # orchestrator; nothing creates them outside the orchestrator
        # today, but reject explicitly so a future code path that does
        # cannot accidentally let the operator overwrite the auto-resolution.
        if decision.type != DecisionType.HITL:
            return make_error_response(
                f"Decision {decision_id} is not operator-resolvable "
                f"(type={getattr(decision.type, 'value', decision.type)}; "
                f"only HITL decisions can be resolved via this endpoint)",
                status_code=400,
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
        _pkg.emit_event(
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

    # Executable task-completion option (#3124) — same dispatch as the
    # queue path, so a contract-resident (``cq-N``) decision resolved
    # pre-bridge also executes instead of only recording the choice.
    executed_action = _pkg._maybe_complete_task_from_resolution(
        pipeline_id, decision_id, _pkg._normalize_choice_resolution(resolution)
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
            },
            **({"executed_action": executed_action} if executed_action else {}),
        },
    )
