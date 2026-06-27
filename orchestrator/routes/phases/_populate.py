"""populate_contract: build the SDLC contract from the plan draft, with
pre-populate worktree-divergence reconcile gating (#3312 decomposition).
"""

import routes.phases as _pkg
from flask import Response, jsonify
from state_store import InvalidPipelineIdError, PipelineNotFoundError

from . import logger
from ._responses import make_error_response, make_success_response


def populate_contract(pipeline_id: str) -> tuple[Response, int]:
    """
    Populate a pipeline's SDLC contract from its plan draft.

    Reads the plan document from the pipeline's worktree, extracts task
    structure, and writes tasks and acceptance criteria to the contract.
    On the ``POPULATED`` outcome the route also commits the contract to
    the orchestrator's local worktree and pushes the work branch to
    origin so fresh agent spawns (``restart_phase``, ``restart_agent``,
    post-cancel restart) pull the populated state on respawn (#2629).

    URL params:
        pipeline_id: Pipeline ID

    Response (200 — ``POPULATED``):
        {
            "success": true,
            "message": "Contract populated from plan",
            "data": {
                "phase_count": 2,
                "task_count": 6,
                "pushed_to_origin": true
            }
        }

    ``pushed_to_origin`` is the operator's signal for whether agents
    will see the populated state on respawn. ``True`` iff
    ``push_worktree_branch`` reported success (a no-op fast-forward push
    counts; a no-op commit alone does not). ``False`` means the commit
    or push failed (or the push was not attempted because
    ``pipeline.branch`` is unset or the worktree resolves to
    ``store.repo_path``) and the operator must commit and push
    themselves before respawning.

    Pre-populate sync (#2792, non-destructive since #2979): the route
    runs :func:`_sync_worktree_with_remote` before reading the draft.
    When the rebase autoresolve cannot reconcile a divergence the sync
    is non-destructive — it leaves the worktree at the local HEAD
    (committed work intact) and reports ``diverged_unreconciled``.  This
    route cannot block on the operator, so it pauses the pipeline
    (``AWAITING_HUMAN``, not ``FAILED``) via
    :func:`_emit_divergence_reconcile_hitl` and returns HTTP 409 with
    ``reason="divergence_reconcile_unacked"`` and ``backup_ref`` /
    ``local_only_commit_shas`` in ``details``.  The operator reconciles
    the orchestrator-side worktree, acks the reconcile HITL (visible in
    ``/sdlc``), and re-runs this endpoint — refusing to populate against
    an un-reconciled worktree matches the #2337 / #2627 fail-loud posture
    without discarding work.

    Error responses include a machine-readable ``reason`` code (#1939,
    #2627):

    - 400 ``invalid_pipeline_id``
    - 404 ``pipeline_not_found`` / ``draft_missing`` / ``no_draft_path``
    - 409 ``divergence_reconcile_unacked`` (#2792, #2979)
    - 422 ``parse_failed`` / ``empty_result`` / forest violations
      (structured body)
    - 500 ``contract_load_failed`` / ``egg_contracts_unavailable`` /
      ``unexpected_exception`` / ``populate_contract_failed``
    """
    try:
        store, pipeline = _pkg.get_state_store_for_pipeline(pipeline_id)

        # Resolve worktree path for contract/draft access
        from routes import resolve_worktree_path

        worktree_path = resolve_worktree_path(pipeline_id, store.repo_path)

        # Import and call the populate function
        from routes.pipelines import (
            PopulateOutcome,
            _commit_statefiles_to_worktree,
            _compute_gateway_mode,
            _emit_divergence_reconcile_hitl,
            _find_pending_divergence_reconcile_decision,
            _get_spawner,
            _pipeline_identifier,
            _populate_contract_from_plan,
            _sync_worktree_with_remote,
        )

        # #2979 follow-up: if a prior populate_contract already paused the
        # pipeline on a reconcile HITL and the operator has not yet
        # resolved it, surface the existing decision instead of re-running
        # the sync and appending a duplicate HITL.  Re-emit would still be
        # correctness-safe (the abort path resolves the most recent
        # decision), but each retry would bloat ``pipeline.decisions`` and
        # confuse /sdlc.  Returning 409 here keeps the route idempotent
        # under operator retries against an already-paused pipeline.
        existing_reconcile_decision = _find_pending_divergence_reconcile_decision(pipeline)
        if existing_reconcile_decision is not None:
            logger.info(
                "populate_contract: pipeline already paused on reconcile HITL; "
                "skipping re-emit and returning 409",
                pipeline_id=pipeline_id,
                decision_id=existing_reconcile_decision.id,
            )
            return make_error_response(
                "Pipeline is already paused on a worktree-reconcile HITL from a "
                "prior populate_contract call. Reconcile the worktree, resolve "
                "the existing decision (visible in /sdlc), and re-run "
                "populate_contract.",
                status_code=409,
                reason="divergence_reconcile_unacked",
                details={
                    "diverged_unreconciled": True,
                    "decision_id": existing_reconcile_decision.id,
                    "already_paused": True,
                },
            )

        # #2792/#2979: reconcile the worktree before reading the draft so
        # a divergence here is surfaced the same way the phase-boundary
        # sync does.  Since #2979 the sync is non-destructive: when the
        # rebase autoresolve can't reconcile a divergence it leaves the
        # worktree at the local HEAD (committed work intact) and reports
        # ``diverged_unreconciled``.  This route cannot block on the
        # operator the way the in-loop phase-boundary callers do, so it
        # pauses the pipeline (AWAITING_HUMAN, NOT FAILED), emits the
        # reconcile HITL, and returns 409 — refusing to populate against
        # an un-reconciled worktree.  The operator reconciles the worktree,
        # resolves the HITL, and re-runs populate_contract.
        populate_sync_outcome = None
        if pipeline.branch and worktree_path != store.repo_path:
            gateway_mode_for_sync, _ = _compute_gateway_mode(pipeline)
            try:
                populate_sync_outcome = _sync_worktree_with_remote(
                    _get_spawner(),
                    pipeline_id,
                    worktree_path,
                    gateway_mode=gateway_mode_for_sync,
                    base_branch=pipeline.base_branch,
                    pipeline_branch=pipeline.branch,
                )
            except Exception as sync_err:  # noqa: BLE001
                logger.warning(
                    "populate_contract: pre-populate sync raised (continuing)",
                    pipeline_id=pipeline_id,
                    error=str(sync_err),
                )

        diverged_unreconciled = bool(
            populate_sync_outcome and populate_sync_outcome.diverged_unreconciled
        )
        divergence_backup_ref = populate_sync_outcome.backup_ref if populate_sync_outcome else None
        divergence_local_only = (
            list(populate_sync_outcome.local_only_commit_shas) if populate_sync_outcome else []
        )

        if diverged_unreconciled:
            # Do NOT run the populator against an un-reconciled worktree.
            # Pause the pipeline+phase (AWAITING_HUMAN — nothing was
            # discarded; the divergence is recoverable), emit the reconcile
            # HITL, and return 409.  The operator reconciles the
            # orchestrator-side worktree, resolves the HITL, and re-runs
            # populate_contract against the reconciled state (#2979).
            logger.error(
                "populate_contract: refusing to populate over unreconciled divergence",
                pipeline_id=pipeline_id,
                backup_ref=divergence_backup_ref,
                local_only_commit_count=len(divergence_local_only),
            )
            _emit_divergence_reconcile_hitl(
                pipeline_id,
                store,
                phase=pipeline.current_phase,
                backup_ref=divergence_backup_ref,
                local_only_commit_shas=divergence_local_only,
            )
            return make_error_response(
                "Worktree diverged from origin during pre-populate sync and "
                "could not be auto-reconciled; the pipeline is paused. Operator "
                "must reconcile the worktree and ack the reconcile HITL (visible "
                "in /sdlc), then re-run populate_contract.",
                status_code=409,
                reason="divergence_reconcile_unacked",
                details={
                    "diverged_unreconciled": True,
                    "backup_ref": divergence_backup_ref,
                    "local_only_commit_shas": divergence_local_only,
                },
            )

        _populate_endpoint_result = _populate_contract_from_plan(
            repo_path=worktree_path,
            pipeline_id=pipeline_id,
            pipeline_mode=pipeline.mode.value if pipeline.mode else "issue",
            issue_number=pipeline.issue_number,
        )

        # #2627 follow-up: surface non-POPULATED outcomes as 4xx/5xx so
        # callers don't get a misleading 200 when the populate step
        # silently produced an empty contract.  Forest-violation is
        # handled separately by the existing class-name branch in the
        # outer ``except`` (HTTP 422 with structured errors).
        _outcome = _populate_endpoint_result.outcome
        if _outcome == PopulateOutcome.POPULATED:
            # Persist the populated contract back to origin so fresh
            # agent spawns (restart_phase, restart_agent, post-cancel
            # restart) pull the populated state on respawn rather than
            # the empty contract on origin.  Without this, the route
            # mutates only the orchestrator's local worktree and is
            # unusable as a recovery primitive — the implement-start
            # guard would refuse to demote to monolithic and the
            # pipeline would wedge.  See #2629.
            #
            # Failures here are fail-soft: ``pushed_to_origin`` in the
            # response data tells the caller whether the contract is
            # visible to agents.  ``False`` means the operator must
            # commit and push themselves before respawning.
            pushed_to_origin = False
            if pipeline.branch and worktree_path != store.repo_path:
                try:
                    identifier = _pipeline_identifier(pipeline.issue_number, pipeline_id)
                    _commit_statefiles_to_worktree(
                        worktree_path,
                        f"Populate contract for {identifier} (#2629)",
                        pipeline_identifier=identifier,
                        pipeline_id=pipeline_id,
                    )
                    # Push unconditionally — a no-op commit does NOT
                    # imply origin matches local.  The per-pipeline
                    # worktree is long-lived (see
                    # ``resolve_worktree_path``) and may carry commits
                    # ahead of origin from a prior failed push.
                    # Pushing unconditionally fast-forwards in the safe
                    # case (origin already matches → no-op push) and
                    # delivers the un-pushed commit in the dangerous
                    # one (the exact wedge #2629 was opened against).
                    # The ``populate_contract`` route is an
                    # operator-initiated recovery primitive, not a hot
                    # loop, so the gateway round-trip is cheap relative
                    # to the correctness benefit.
                    gateway_mode, _ = _compute_gateway_mode(pipeline)
                    push_result = _get_spawner().gateway.push_worktree_branch(
                        pipeline_id=pipeline_id,
                        repo_path=str(worktree_path),
                        branch=pipeline.branch,
                        mode=gateway_mode,
                        base_branch=pipeline.base_branch,
                    )
                    pushed_to_origin = bool(push_result)
                    if not pushed_to_origin:
                        logger.warning(
                            "populate_contract: push failed (continuing)",
                            pipeline_id=pipeline_id,
                            detail=push_result.describe(),
                        )
                except Exception as persist_err:  # noqa: BLE001
                    logger.warning(
                        "populate_contract: persist to origin failed (continuing)",
                        pipeline_id=pipeline_id,
                        error=str(persist_err),
                    )

            return make_success_response(
                "Contract populated from plan",
                data={
                    "phase_count": _populate_endpoint_result.slice_count,
                    "task_count": _populate_endpoint_result.task_count,
                    "pushed_to_origin": pushed_to_origin,
                },
            )
        if _outcome in {PopulateOutcome.DRAFT_MISSING, PopulateOutcome.NO_DRAFT_PATH}:
            return make_error_response(
                f"Plan draft not available ({_outcome.value})",
                status_code=404,
                reason=_outcome.value,
            )
        if _outcome in {PopulateOutcome.PARSE_FAILED, PopulateOutcome.EMPTY_RESULT}:
            return make_error_response(
                f"Plan populate produced non-POPULATED outcome ({_outcome.value})",
                status_code=422,
                reason=_outcome.value,
            )
        # CONTRACT_LOAD_FAILED / EGG_CONTRACTS_UNAVAILABLE /
        # UNEXPECTED_EXCEPTION — server-side failure.
        return make_error_response(
            f"Plan populate failed ({_outcome.value})",
            status_code=500,
            reason=_outcome.value,
        )

    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
            reason="invalid_pipeline_id",
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
            reason="pipeline_not_found",
        )
    except Exception as e:
        # #2137 TASK-2-2: forest-violation errors are routed as 422 with
        # the inlined structured-error body so the plan reviewer can
        # cite them verbatim. We branch on the class name to avoid an
        # import cycle (routes/pipelines.py imports from routes/phases.py).
        if e.__class__.__name__ == "ForestValidationError":
            try:
                body, status = e.to_response()  # type: ignore[attr-defined]
                # #3046 — ForestValidationError covers both the forest-shape
                # and file-overlap-ordering rejections. Use a
                # discriminator-agnostic event name and include
                # ``reason=`` so operators grepping the audit log catch
                # both cases.
                logger.warning(
                    "contract_populate_dag_rejection",
                    pipeline_id=pipeline_id,
                    reason=getattr(e, "reason", None),
                    errors=getattr(e, "errors", None),
                )
                return jsonify(body), status
            except Exception:  # noqa: BLE001
                # Fall through to the generic 500 path if to_response
                # is missing or shaped unexpectedly.
                pass
        logger.error(
            "contract_populate_endpoint_failed",
            pipeline_id=pipeline_id,
            error=str(e),
            exc_info=True,
        )
        return make_error_response(
            f"Failed to populate contract: {e}",
            status_code=500,
            reason="populate_contract_failed",
        )
