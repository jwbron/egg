"""concurrent-phase lifted helpers helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _phase_bail_reason_impl(*, store, pipeline_id, run_epoch) -> str | None:
    """Why this poll loop must exit without escalating, or ``None`` to keep polling.

    Two independent conditions, resolved from a single pipeline load:

    ``superseded_by_restart``
        A newer ``run_epoch`` means another ``_run_pipeline`` thread owns
        this pipeline (#3315). Mirrors the post-return epoch check (#1638)
        but runs *inside* the poll loop so a superseded thread stops
        polling before it can fire stale escalations.

    ``pipeline_cancelled``
        The operator cancelled the run (#3633). The cancel route stops this
        phase's event loop directly, but nothing stops *this* thread, and
        before this check it kept polling — admitting the next slice,
        creating its integration branch, and spawning agents against a
        pipeline the operator believes is stopped. Re-reading the persisted
        status bounds a missed stop signal to one poll interval.

    FAILED is deliberately NOT a bail condition: ``container_monitor``
    reconciliation can mark a live pipeline FAILED while this loop polls,
    and the consensus-complete branch recovers it to RUNNING (#1273).
    Bailing here would turn that recoverable transient into a real failure.

    Best-effort: a missing store or a load failure returns ``None`` so a
    transient store hiccup never tears down a legitimately-running phase.
    """
    if store is None:
        return None
    try:
        _pip = store.load_pipeline(pipeline_id)
    except Exception as _err:  # noqa: BLE001 — never wedge the caller
        _pkg.logger.debug(
            "Phase bail check failed; continuing",
            pipeline_id=pipeline_id,
            error=str(_err),
        )
        return None
    if _pip.status == _pkg.PipelineStatus.CANCELLED:
        return "pipeline_cancelled"
    if run_epoch is not None and (_pip.run_epoch or _pip.created_at) != run_epoch:
        return "superseded_by_restart"
    return None


def _record_container_exit_impl(
    exec_info,
    final_info,
    *,
    docker_client,
    _logs_lock,
    has_failures,
    all_logs,
    store,
    phase_str,
    pipeline_id,
) -> None:
    from models import AgentExecutionStatus as StateAgentStatus

    """Capture logs and update pipeline state for an exited container."""
    container_logs = ""
    if final_info.exit_code != 0:
        try:
            container_logs = docker_client.get_container_logs(
                exec_info.container_id,
                tail=200,
            )
        except Exception:
            pass

    with _logs_lock:
        # 143 (SIGTERM) is orchestrator-initiated teardown, not a
        # failure — match the K8s monitor's classifier (#2210) so
        # the two layers don't disagree about what 143 means.
        if final_info.exit_code not in (0, 143):
            has_failures[0] = True
        all_logs.append(
            f"--- {exec_info.role.value} (exit={final_info.exit_code}) ---\n{container_logs}"
        )

    if store is not None:
        try:
            with _pkg.get_pipeline_state_lock(pipeline_id):
                pip = store.load_pipeline(pipeline_id)
                pe = pip.get_phase_execution(_pkg.PipelinePhase(phase_str))

                for ci in pe.containers:
                    if ci.container_id == exec_info.container_id:
                        ci.status = final_info.status
                        ci.exited_at = final_info.exited_at
                        ci.exit_code = final_info.exit_code
                        break

                for agent in pe.agents:
                    if agent.container_id == exec_info.container_id:
                        agent.completed_at = _pkg.datetime.now(_pkg.UTC)
                        if final_info.exit_code in (0, 143):
                            agent.status = StateAgentStatus.COMPLETE
                        else:
                            agent.status = StateAgentStatus.FAILED
                            agent.error = f"Container exited with code {final_info.exit_code}"
                        break

                # Cap each tail line at 4096 chars: containers that print
                # large JSON blobs on one line could otherwise persist
                # multi-MB lines into pipeline state on every chatty exit.
                last_lines = (
                    [ln[:4096] for ln in container_logs.splitlines()[-200:]]
                    if container_logs
                    else []
                )
                pe.agent_exits.append(
                    _pkg.AgentExitInfo(
                        role=exec_info.role,
                        exit_code=final_info.exit_code,
                        last_lines=last_lines,
                        terminated_at=_pkg.datetime.now(_pkg.UTC),
                        container_id=exec_info.container_id,
                    )
                )

                store.save_pipeline(pip)
        except Exception as track_err:
            _pkg.logger.warning(
                "Failed to update concurrent agent state",
                container_id=exec_info.container_id,
                error=str(track_err),
            )


def _stop_running_containers_impl(*, active_executions, exited_containers, docker_client) -> None:
    """Gracefully stop all containers that haven't exited yet."""
    for e in active_executions:
        if e.container_id not in exited_containers:
            try:
                docker_client.stop_container(e.container_id, timeout=30)
            except Exception:
                pass


def _latest_proposal_ts_impl(_pid, _sid):
    _get_brc_tracker = None
    try:
        from peer_consensus import get_peer_consensus_tracker as _get_brc_tracker
    except ImportError:
        from ..peer_consensus import (
            get_peer_consensus_tracker as _get_brc_tracker,  # type: ignore[no-redef]
        )

    """Return the latest CONSENSUS_PROPOSE timestamp from the BRC tracker.

    Used by the post-consensus-timeout poll loop (#2245) to rebaseline
    the per-iteration budget on producer progress.  Returns ``None`` if
    the tracker is unavailable, has no proposals, or any lookup raises —
    callers treat ``None`` as "no progress signal yet" and proceed
    without a rebaseline.
    """
    if _get_brc_tracker is None:
        return None
    try:
        _t = _get_brc_tracker(_pid, _sid)
    except Exception:
        return None
    if _t is None:
        return None
    try:
        return _t.get_latest_proposal_timestamp()
    except Exception:
        return None


def _update_agents_complete_impl(*, store, phase_str, pipeline_id, slice_id) -> None:
    from models import AgentExecutionStatus as StateAgentStatus

    _get_brc_tracker = None
    try:
        from peer_consensus import get_peer_consensus_tracker as _get_brc_tracker
    except ImportError:
        from ..peer_consensus import (
            get_peer_consensus_tracker as _get_brc_tracker,  # type: ignore[no-redef]
        )

    """Mark all running agents as COMPLETE in pipeline state (consensus path)."""
    if store is None:
        return
    try:
        with _pkg.get_pipeline_state_lock(pipeline_id):
            pip = store.load_pipeline(pipeline_id)
            pe = pip.get_phase_execution(_pkg.PipelinePhase(phase_str))
            completed_container_ids: set[str] = set()

            # Look up proposal commit SHAs from the BRC tracker so we can
            # populate agent.commit (issue #1691). The lookup is slice-
            # aware (#2137) — when ``slice_id`` is set the tracker key
            # is the nested ``{pipeline_id}/{slice_id}`` form.
            _brc = None
            if _get_brc_tracker is not None:
                try:
                    _brc = _get_brc_tracker(pipeline_id, slice_id)
                except TypeError:
                    # Older tracker import-shim without slice_id support.
                    try:
                        _brc = _get_brc_tracker(pipeline_id)
                    except Exception:
                        pass
                except Exception:
                    pass

            # Filter to this slice's agents — without the filter, slice-2
            # BRC completing flips slice-3's still-running agents to
            # COMPLETE because they share ``pe.agents`` (#2422). For
            # pipeline-level (non-sliced) phases ``slice_id`` is ``None``
            # and we still match all agents whose ``slice_id`` is ``None``.
            for agent in pe.agents:
                if getattr(agent, "slice_id", None) != slice_id:
                    continue
                if agent.status in (StateAgentStatus.RUNNING, StateAgentStatus.FAILED):
                    agent.status = StateAgentStatus.COMPLETE
                    agent.completed_at = _pkg.datetime.now(_pkg.UTC)
                    if agent.container_id:
                        completed_container_ids.add(agent.container_id)
                # Populate commit SHA from the consensus tracker's proposal
                # records.  Only producers have SHAs; reviewers get "".
                if _brc is not None and not agent.commit:
                    sha = _brc.get_proposal_commit_sha(agent.role.value)
                    if sha and sha != "RECONSTRUCTED_NO_SHA":
                        agent.commit = sha
                    elif sha is None or sha == "RECONSTRUCTED_NO_SHA":
                        # Diagnostic only (#1911): log when the BRC
                        # tracker returns null or the
                        # RECONSTRUCTED_NO_SHA sentinel for a role
                        # so we can see on real runs whether the
                        # three-role implement phase
                        # (coder/tester/documenter) wiring misses
                        # SHAs.  Deliberately no auto-fallback —
                        # that would mask the real bug.  Empty
                        # string is the expected reviewer default
                        # (reviewers never propose) — do NOT warn
                        # for that case or the signal drowns in
                        # noise.
                        _pkg.logger.warning(
                            "BRC tracker returned no commit sha for completed agent",
                            pipeline_id=pipeline_id,
                            phase=phase_str,
                            role=agent.role.value,
                            brc_value=sha,
                        )

            # Also mark containers as exited so the container monitor
            # doesn't find stale RUNNING entries and mark pipeline FAILED.
            # See issue #1294.
            for ci in pe.containers:
                if (
                    ci.container_id in completed_container_ids
                    and ci.status == _pkg.ContainerStatus.RUNNING
                ):
                    ci.status = _pkg.ContainerStatus.EXITED
                    # Synthetic: container will be stopped next, but 0
                    # reflects successful consensus completion.
                    ci.exit_code = 0
                    ci.exited_at = _pkg.datetime.now(_pkg.UTC)

            # Auto-withdraw any stale consensus-timeout HITL a superseded
            # thread opened before this phase converged (#3315 facet c).
            # Folded into this already-locked load→save so it costs no
            # extra lock and rides every consensus-success path.
            _withdrawn = _pkg._cancel_consensus_timeout_decisions(pip)
            if _withdrawn:
                _pkg.logger.info(
                    "Auto-withdrew stale consensus-timeout HITL decision(s) on convergence",
                    pipeline_id=pipeline_id,
                    phase=phase_str,
                    withdrawn=_withdrawn,
                )

            store.save_pipeline(pip)
    except Exception as track_err:
        _pkg.logger.warning(
            "Failed to update agents to COMPLETE after consensus",
            pipeline_id=pipeline_id,
            error=str(track_err),
        )


def _record_spawned_agents_impl(executions, *, store, pipeline_id, phase_str, slice_id) -> None:
    from models import (
        AgentExecution as StateAgentExecution,
    )
    from models import (
        AgentExecutionStatus as StateAgentStatus,
    )

    if store is None:
        return
    try:
        with _pkg.get_pipeline_state_lock(pipeline_id):
            pip = store.load_pipeline(pipeline_id)
            phase_execution = pip.get_phase_execution(_pkg.PipelinePhase(phase_str))
            for exec_info in executions:
                if exec_info.container_id:
                    spawn_info = exec_info.container_info
                    if spawn_info is not None:
                        # Preserve backend-specific fields (pod_name,
                        # namespace, job_name on k8s) from the spawner
                        # while overriding the live bookkeeping fields.
                        container_info = spawn_info.model_copy(
                            update={
                                "status": _pkg.ContainerStatus.RUNNING,
                                "started_at": _pkg.datetime.now(_pkg.UTC),
                                "agent_role": exec_info.role,
                            }
                        )
                    else:
                        container_info = _pkg.ContainerInfo(
                            container_id=exec_info.container_id,
                            container_name=f"{pipeline_id}-{exec_info.role.value}",
                            status=_pkg.ContainerStatus.RUNNING,
                            started_at=_pkg.datetime.now(_pkg.UTC),
                            agent_role=exec_info.role,
                        )
                    phase_execution.containers.append(container_info)

                agent_state = StateAgentExecution(
                    role=exec_info.role,
                    status=(
                        StateAgentStatus.RUNNING
                        if exec_info.status == StateAgentStatus.RUNNING
                        else StateAgentStatus.FAILED
                    ),
                    container_id=exec_info.container_id,
                    started_at=_pkg.datetime.now(_pkg.UTC),
                    slice_id=slice_id,
                    # Carry the per-agent resolved model through the
                    # reconstruction (#3174). ``_spawn_agent`` stamps this on
                    # the in-memory execution, but the persisted record is
                    # rebuilt from scratch here — without this copy the field
                    # dead-ends at None and both operator confirmation
                    # channels (get_status, list_containers), which read from
                    # persisted state, surface ``resolved_model: null`` for
                    # every concurrent-phase agent (initial spawn and
                    # restart_phase respawn alike).
                    resolved_model=exec_info.resolved_model,
                )
                phase_execution.agents.append(agent_state)
            store.save_pipeline(pip)
    except Exception as track_err:
        _pkg.logger.warning(
            "Failed to record concurrent agents in pipeline state",
            pipeline_id=pipeline_id,
            error=str(track_err),
        )


def _retry_transient_spawn_failures_impl(
    executions,
    *,
    pipeline,
    executor,
    agent_prompts,
    spawner,
    pipeline_id,
    phase_str,
):
    """Phase-level respawn of transiently-failed roles; returns the
    updated executions list (verbatim extraction from _run_concurrent_phase)."""
    try:
        from concurrent_executor import _is_transient_agent_error
    except ImportError:
        from ..concurrent_executor import _is_transient_agent_error  # type: ignore

    # Phase-level retry for transient spawn failures (#1879).  Per-role
    # retries in kubernetes_spawner handle short blips (~7s budget); this
    # outer budget bridges longer outages like a gateway cold start by
    # respawning only the failed roles while survivors wait idle.  BRC can
    # not start without the full cohort anyway, so leaving survivors alone
    # during the retry window does not risk correctness.
    phase_max_retries = getattr(pipeline.config, "phase_spawn_max_retries", 2)
    phase_initial_backoff = getattr(
        pipeline.config, "phase_spawn_retry_initial_backoff_seconds", 30.0
    )
    _PHASE_RETRY_BACKOFF_MULTIPLIER = 3.0
    for attempt in range(phase_max_retries):
        failed = [e for e in executions if e.status.value == "failed"]
        if not failed:
            break
        transient_failed = [e for e in failed if _is_transient_agent_error(e.error)]
        if not transient_failed:
            # All remaining failures are permanent — retrying would just
            # burn the budget for no benefit.
            break

        delay = phase_initial_backoff * (_PHASE_RETRY_BACKOFF_MULTIPLIER**attempt)
        failed_roles = [e.role for e in failed]
        _pkg.logger.warning(
            "Phase-level spawn retry scheduled",
            pipeline_id=pipeline_id,
            phase=phase_str,
            attempt=attempt + 1,
            max_attempts=phase_max_retries,
            delay_seconds=delay,
            failed_roles=[r.value for r in failed_roles],
            transient_roles=[e.role.value for e in transient_failed],
        )
        _pkg.time.sleep(delay)

        # Clear any half-created gateway worktree state for failed roles
        # so the retry sees a clean slate.  Survivors' worktrees use
        # different container_ids and are untouched.
        for role in failed_roles:
            agent_worktree_id = f"{pipeline_id}-{role.value}"
            try:
                spawner.gateway.delete_worktrees(
                    container_id=agent_worktree_id,
                    force=True,
                )
            except Exception as clear_err:
                _pkg.logger.warning(
                    "Failed to clear partial worktree before retry",
                    pipeline_id=pipeline_id,
                    agent_worktree_id=agent_worktree_id,
                    error=str(clear_err),
                )

        retry_executions = executor.spawn_specific_roles(failed_roles, agent_prompts=agent_prompts)
        by_role = {e.role: e for e in retry_executions}
        executions = [
            by_role.get(e.role, e) if e.status.value == "failed" else e for e in executions
        ]

        still_failed = [e for e in executions if e.status.value == "failed"]
        _pkg.logger.info(
            "Phase-level spawn retry outcome",
            pipeline_id=pipeline_id,
            phase=phase_str,
            attempt=attempt + 1,
            recovered_roles=[
                r.value for r in failed_roles if r not in {e.role for e in still_failed}
            ],
            still_failed_roles=[e.role.value for e in still_failed],
        )

    return executions
