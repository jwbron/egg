"""impasse-retry wrapper for the concurrent-phase runner (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _run_concurrent_phase_with_impasse_retry(
    pipeline_id: str,
    pipeline: _pkg.Pipeline,
    phase: str,
    spawner,
    repo_volumes: dict[str, str],
    gateway_mode: str,
    repos: list[str],
    sandbox_env: dict[str, str],
    store,
    certs_volume: str | None,
    worktree_repo_path: _pkg.Path,
    review_feedback: str | None = None,
    slice_id: str | None = None,
    operator_directives: list[_pkg.OperatorDirective] | None = None,
    iteration_history: list[_pkg.IterationSummary] | None = None,
    run_epoch: _pkg.datetime | None = None,
) -> tuple[int, str]:
    """Run a concurrent phase, auto-delegating impasses once before HITL.

    Wraps :func:`_run_concurrent_phase` with the runtime escape-hatch
    introduced in #2529:

    1. Run the BRC cycle as usual.
    2. After it exits, scan each producer's ``AgentOutput`` for a typed
       :class:`egg_contracts.Impasse`.
    3. For ``WRONG_ROLE`` impasses with a single eligible alternative
       producer role and ``task.delegation_attempts == 0``, mutate
       ``task.role`` to the suggested role and re-run the BRC cycle
       once. The new spawn picks up the role flip when
       ``_build_agent_prompt`` re-reads the contract.
    4. For everything else (second impasse, non-WRONG_ROLE category,
       no eligible alternative role, unresolvable task_id) the helper
       creates a HITL decision on the contract and the slice exits
       so the operator can choose between cancel / re-plan / manual
       resolution. ``feedback_no_auto_hitl.md``: the orchestrator
       creates the decision; surfacing to the user is the operator
       layer's job.

    Pipeline-level (non-sliced) callers can pass ``slice_id=None``;
    the routing helper falls back to a contract-wide search for the
    impassed task.
    """
    try:
        from orchestrator.impasse_routing import (
            ImpasseAction,
            collect_impasses,
            route_impasses,
        )
    except ImportError:
        from impasse_routing import (  # type: ignore[no-redef]
            ImpasseAction,
            collect_impasses,
            route_impasses,
        )

    try:
        from egg_contracts.agent_roles import AgentRole as ContractAgentRoleEnum
    except ImportError:  # pragma: no cover - import seam parity
        from shared.egg_contracts.agent_roles import (  # type: ignore[no-redef]
            AgentRole as ContractAgentRoleEnum,
        )
    # Two attempts max: original + at most one delegated retry. The
    # ``delegation_attempts`` counter on the contract task enforces the
    # same bound when the slice is restarted out-of-band by an
    # operator, so a long-lived pipeline can never escape this gate.
    MAX_IMPASSE_ATTEMPTS = 2

    # Producer roles only — impasses are a producer concept; reviewers
    # don't author tasks. Mirrors the producer trio in
    # ``shared/egg_restrictions/patterns.py``.
    producer_roles = [
        ContractAgentRoleEnum.CODER,
        ContractAgentRoleEnum.TESTER,
        ContractAgentRoleEnum.DOCUMENTER,
    ]

    last_exit = 0
    last_logs = ""
    for attempt in range(MAX_IMPASSE_ATTEMPTS):
        is_terminal = attempt + 1 == MAX_IMPASSE_ATTEMPTS

        last_exit, last_logs = _pkg._run_concurrent_phase(
            pipeline_id=pipeline_id,
            pipeline=pipeline,
            phase=phase,
            spawner=spawner,
            repo_volumes=repo_volumes,
            gateway_mode=gateway_mode,
            repos=repos,
            sandbox_env=sandbox_env,
            store=store,
            certs_volume=certs_volume,
            worktree_repo_path=worktree_repo_path,
            review_feedback=review_feedback,
            slice_id=slice_id,
            operator_directives=operator_directives,
            iteration_history=iteration_history,
            run_epoch=run_epoch,
        )

        try:
            impasses = collect_impasses(
                _pkg.Path(worktree_repo_path),
                pipeline_id,
                producer_roles,
            )
        except Exception as scan_err:  # noqa: BLE001
            _pkg.logger.warning(
                "Impasse scan raised; continuing without delegation",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                error=str(scan_err),
            )
            return last_exit, last_logs

        if not impasses:
            return last_exit, last_logs

        # Defense-in-depth (#3315 facet a, slice path): if a restart bumped
        # ``run_epoch`` while this thread was running, a stale producer-written
        # impasse file could otherwise drive ``route_impasses`` into a HITL
        # against the freshly-restarted phase. The poll loop in
        # ``_run_concurrent_phase`` already bails on supersession before any
        # escalation; mirror that here so the "no escalation when superseded"
        # property holds on the slice path too — return the (superseded) result
        # without routing.
        if _pkg._pipeline_superseded_by_restart(store, pipeline_id, run_epoch):
            _pkg.logger.info(
                "Restart superseded this thread before impasse routing; "
                "skipping route_impasses to avoid escalating against a "
                "freshly-restarted phase",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
            )
            return last_exit, last_logs

        try:
            # On the terminal iteration we have no remaining BRC cycle
            # to respawn with a new role, so a delegation made here
            # would silently dangle (review feedback #2 on PR #2553).
            # Force every impasse onto the escalate path instead.
            decisions = route_impasses(
                repo_path=_pkg.Path(worktree_repo_path),
                pipeline_id=pipeline_id,
                contract_identifier=pipeline_id,
                impasses=impasses,
                slice_id=slice_id,
                force_escalate=is_terminal,
            )
        except Exception as route_err:  # noqa: BLE001
            _pkg.logger.error(
                "Impasse routing raised; surfacing slice failure",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                error=str(route_err),
            )
            return last_exit, last_logs

        all_delegated = decisions and all(d.action == ImpasseAction.DELEGATE for d in decisions)
        if not all_delegated:
            # Any escalation, or an empty decision list, means the
            # operator gates the next move. Don't auto-retry.
            for d in decisions:
                _pkg.logger.info(
                    "Impasse decision",
                    pipeline_id=pipeline_id,
                    slice_id=slice_id,
                    action=d.action.value,
                    role=d.role,
                    task_id=d.task_id,
                    new_role=d.new_role,
                    reason=d.reason,
                    hitl_decision_id=d.hitl_decision_id,
                )
            return last_exit, last_logs

        # All impasses delegated cleanly — the contract has been
        # mutated, log the swap and let the loop respawn with the new
        # roles. Last attempt falls through and returns whatever the
        # second BRC cycle produced.
        for d in decisions:
            _pkg.logger.info(
                "Impasse delegated; retrying slice with new role",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                attempt=attempt + 1,
                from_role=d.role,
                to_role=d.new_role,
                task_id=d.task_id,
            )

        # Drop the now-routed impasse signals before the next BRC
        # cycle, so a producer that crashes pre-handoff in iter-N+1
        # cannot resurrect this iteration's impasse via a stale file.
        _pkg._clear_stale_impasses_for_producers(
            _pkg.Path(worktree_repo_path),
            pipeline_id,
            producer_roles,
            cleanup_reason="post-delegation cleanup",
        )

    return last_exit, last_logs
