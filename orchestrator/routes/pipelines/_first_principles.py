"""first-principles redirect + refine restart helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def apply_first_principles_redirect(
    pipeline_id: str,
    new_task_description: str,
    *,
    reason: str,
) -> list[str]:
    """Adopt a first-principles redirect: rewrite the seed and re-run refine.

    Called in-process from the decision-resolve hook when an operator adopts a
    redirect raised by the ``first_principles_reviewer``. Two durable steps:

    1. **Rewrite the seed** via the operator-grade
       ``rewrite_task_description_as_operator`` (audited, ``Role.HUMAN``), then
       commit+push the worktree to the work branch so the refine restart's
       re-fork (which forks fresh worktrees from ``origin/<branch>``) sees the
       rewritten ``task_description`` rather than the old one.
    2. **Re-run refine** via :func:`_restart_refine_phase`.

    Returns the role values respawned. Raises on failure; the caller logs and
    leaves the decision resolved (the operator's intent is recorded regardless).
    """
    from operator_actions import rewrite_task_description_as_operator

    repo_path = _pkg.get_repo_path()
    store, pipeline = _pkg._resolve_pipeline(pipeline_id, repo_path)
    issue_number = getattr(pipeline, "issue_number", None)
    gateway_mode, _ = _pkg._compute_gateway_mode(pipeline)
    spawner = _pkg._get_spawner()

    rewrite = rewrite_task_description_as_operator(
        pipeline_id,
        new_task_description,
        reason=reason,
        actor="operator:first-principles-redirect",
        issue_number=issue_number,
    )

    # Durably land the rewritten seed on the work branch. The refine restart
    # below deletes per-agent worktrees and re-forks fresh ones from
    # ``origin/<branch>``; without this push the re-fork would re-materialise
    # the OLD seed and the redirect would be silently lost (#3080 re-fork
    # semantics).
    worktree = _pkg.Path(rewrite["worktree"])
    identifier = _pkg._pipeline_identifier(issue_number, pipeline_id)
    try:
        committed = _pkg._commit_statefiles_to_worktree(
            worktree,
            f"first-principles redirect: rewrite seed — {reason}"[:200],
            identifier,
            pipeline_id=pipeline_id,
        )
        if committed and pipeline.branch:
            spawner.gateway.push_worktree_branch(
                pipeline_id=pipeline_id,
                repo_path=str(worktree),
                branch=pipeline.branch,
                mode=gateway_mode,
                base_branch=pipeline.base_branch,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort; restart still proceeds
        _pkg.logger.warning(
            "Failed to push rewritten seed to work branch; refine restart may "
            "re-fork the prior seed (first-principles redirect)",
            pipeline_id=pipeline_id,
            error=str(exc),
        )

    return _pkg._restart_refine_phase(
        pipeline_id, store, reason=reason, spawner=spawner, gateway_mode=gateway_mode
    )


def _restart_refine_phase(
    pipeline_id: str,
    store: _pkg.Any,
    *,
    reason: str,
    spawner: _pkg.Any,
    gateway_mode: str,
) -> list[str]:
    """Re-run the refine phase in-process (non-route sibling of ``restart_phase``).

    Mirrors ``restart_phase``'s essential steps for the refine phase so the
    first-principles accept-path can re-run refine from the decision-resolve
    hook (no Flask request). Refine has no slices, so the per-slice tracker
    loop in ``restart_phase`` is intentionally omitted. Raises ``ValueError``
    if the pipeline is not currently parked at the refine phase.
    """
    phase = _pkg.PipelinePhase.REFINE.value
    lock = _pkg.get_pipeline_state_lock(pipeline_id)
    with lock:
        pipeline = store.load_pipeline(pipeline_id)
        if pipeline.current_phase.value != phase:
            raise ValueError(
                f"_restart_refine_phase: pipeline {pipeline_id} is not at the "
                f"refine phase (current: {pipeline.current_phase.value})"
            )
        phase_exec = pipeline.phases.get(phase)
        if phase_exec is None:
            raise ValueError(f"Refine phase not found in pipeline {pipeline_id}")

        agent_roles: list[_pkg.AgentRole] = []
        for agent in phase_exec.agents:
            if hasattr(agent, "role"):
                role = (
                    agent.role
                    if isinstance(agent.role, _pkg.AgentRole)
                    else _pkg.AgentRole(agent.role)
                )
                agent_roles.append(role)
        if not agent_roles:
            from egg_contracts.agent_roles import get_roles_for_phase as _grfp

            for r in _grfp(
                phase,
                include_reviewers=True,
                repo=pipeline.repo,
                has_contract=getattr(pipeline, "has_contract", True),
            ):
                try:
                    agent_roles.append(_pkg.AgentRole(r.value))
                except ValueError:
                    continue

        old_container_ids = [c.container_id for c in phase_exec.containers]
        phase_exec.containers = []
        phase_exec.agents = []
        phase_exec.review_cycles = 0
        phase_exec.hitl_review_cycles = 0
        phase_exec.status = _pkg.PipelineStatus.PENDING
        phase_exec.started_at = None
        phase_exec.work_started_at = None
        phase_exec.completed_at = None
        phase_exec.error = None
        phase_exec.cycle_timings = []
        pipeline.status = _pkg.PipelineStatus.RUNNING
        pipeline.error = None
        pipeline.run_epoch = _pkg.datetime.now(_pkg.UTC)
        store.update_pipeline(pipeline_id, pipeline.model_dump(mode="json"))

    # --- Outside the lock: slow, idempotent, best-effort teardown ---
    for container_id in old_container_ids:
        try:
            spawner.stop_agent_container(container_id, cleanup_session=True)
        except Exception as e:  # noqa: BLE001
            _pkg.logger.warning(
                "Failed to stop container during refine redirect restart",
                container_id=container_id[:12] if container_id else "?",
                error=str(e),
            )
        try:
            spawner.remove_agent_container(container_id, force=True, cleanup_session=False)
        except Exception as e:  # noqa: BLE001
            _pkg.logger.warning(
                "Failed to remove container during refine redirect restart",
                container_id=container_id[:12] if container_id else "?",
                error=str(e),
            )

    restart_role_values = {role.value for role in agent_roles}
    try:
        all_worktrees = _pkg.agent_salvage.enumerate_agent_worktrees(
            pipeline_id, validate_git=False
        )
    except (OSError, ImportError, RuntimeError) as e:
        _pkg.logger.warning(
            "Failed to enumerate per-agent worktrees during refine redirect restart",
            pipeline_id=pipeline_id,
            error=str(e),
        )
        all_worktrees = []
    worktrees_to_delete = [wt for wt in all_worktrees if wt.agent_role in restart_role_values]
    if worktrees_to_delete:
        try:
            _pkg.agent_salvage.auto_salvage_pipeline(
                spawner.gateway,
                pipeline_id,
                worktree_filter={wt.worktree_id for wt in worktrees_to_delete},
                mode=gateway_mode,
                base_branch=pipeline.base_branch,
            )
        except Exception as e:  # noqa: BLE001
            _pkg.logger.warning(
                "Auto-salvage failed during refine redirect restart; proceeding",
                pipeline_id=pipeline_id,
                error=str(e),
            )
    for wt in worktrees_to_delete:
        try:
            spawner.gateway.delete_worktrees(container_id=wt.worktree_id, force=True)
        except Exception as e:  # noqa: BLE001
            _pkg.logger.warning(
                "Failed to delete per-agent worktree during refine redirect restart",
                agent_worktree_id=wt.worktree_id,
                pipeline_id=pipeline_id,
                error=str(e),
            )

    try:
        from peer_consensus import get_peer_consensus_tracker

        tracker = get_peer_consensus_tracker(pipeline_id)
        if tracker:
            tracker.clear()
    except Exception as e:  # noqa: BLE001
        _pkg.logger.warning(
            "Failed to clear peer consensus during refine redirect restart",
            pipeline_id=pipeline_id,
            error=str(e),
        )

    spawner.reset_restart_counts(pipeline_id)
    try:
        from health_monitor import get_health_monitor

        _hm = get_health_monitor()
        if _hm is not None:
            for role in agent_roles:
                _hm.reset_agent(role.value)
    except Exception as e:  # noqa: BLE001
        _pkg.logger.warning(
            "Failed to reset health-monitor state during refine redirect restart",
            pipeline_id=pipeline_id,
            error=str(e),
        )

    _pkg._spawn_pipeline_run_thread(pipeline_id, store.repo_path, pipeline.run_epoch)
    agents_to_restart = [role.value for role in agent_roles]
    _pkg.logger.info(
        "Refine phase re-run for first-principles redirect",
        pipeline_id=pipeline_id,
        reason=reason,
        agents_to_restart=agents_to_restart,
    )
    return agents_to_restart
