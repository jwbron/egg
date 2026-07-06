"""concurrent-phase runner + impasse-retry wrapper helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _run_concurrent_phase(
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
    """Run a phase using concurrent all-agents-at-once execution.

    Creates a ConcurrentPhaseExecutor that spawns all agents simultaneously,
    all sharing the pipeline branch. Each container receives a role-specific
    prompt built via ``_build_agent_prompt``. After spawning, waits for all
    containers to exit and records their state in the pipeline store.

    Returns:
        (exit_code, logs) — 0 on success.

    Raises:
        SpawnFailureError: If any agent fails to spawn. Survivors are stopped
            and their pipeline-state records are marked FAILED before the
            exception propagates. Distinguishes spawn failures from container
            exits so the outer caller's ``pipeline.error`` is accurate.
    """
    from models import (
        AgentExecutionStatus as StateAgentStatus,
    )
    from models import (
        ContainerInfo,
        ContainerStatus,
        PipelinePhase,
        resolve_consensus_timeout_minutes,
    )

    try:
        from concurrent_executor import ConcurrentPhaseExecutor
    except ImportError:
        from ..concurrent_executor import ConcurrentPhaseExecutor  # type: ignore

    phase_str = phase if isinstance(phase, str) else phase.value
    pipeline_mode = "issue" if pipeline.issue_number is not None else "prompt"

    # Slice-aware sandbox env (#2137 TASK-4-3 / #2403): when running a
    # per-slice team, the spawner exposes the slice id via
    # ``EGG_SLICE_ID`` and leaves ``EGG_PIPELINE_ID`` as the bare
    # pipeline id. An earlier shape encoded the slice into
    # ``EGG_PIPELINE_ID`` itself (``{pipeline_id}/{slice_id}``) so the
    # orchestrator's ``_tracker_key`` would route CONSENSUS_* to the
    # slice tracker without an extra signal-level field. That broke
    # every agent → orchestrator round-trip:
    #
    #   * the orchestrator-side ``PIPELINE_ID_PATTERN`` and the agent
    #     handler validator (``[a-zA-Z0-9_-]+``) both reject the slash,
    #   * Flask's default URL converter doesn't allow ``/``, so every
    #     ``POST /api/v1/pipelines/{pid}/...`` route 404s — i.e. all
    #     of progress, BRC, heartbeat, message, phase, decision, etc.
    #
    # Slice routing is plumbed explicitly instead: the BRC handlers
    # pull ``EGG_SLICE_ID`` and forward it on the signal payload, and
    # the orchestrator's signal handlers feed it into
    # ``get_peer_consensus_tracker(pipeline_id, slice_id)``. CONSENSUS_*
    # isolation is preserved; HEARTBEAT and OVERSEER_ALERT are not
    # tracker-scoped at all — ``handle_heartbeat_signal`` is a no-op
    # ACK with no tracker lookup, and OVERSEER_ALERT flows through the
    # message bus (``MessageType.OVERSEER_ALERT``) rather than the
    # consensus tracker. So per-slice scoping doesn't apply to either,
    # and operator telemetry stays pipeline-wide as before. The
    # pipeline-level fan-out for OVERSEER_ALERT mentioned in earlier
    # comments here is tracked alongside the per-slice MCP control
    # verbs in #2199.
    #
    # Single source of truth (#2410 v2 review): ``EGG_SLICE_ID`` is
    # injected by ``KubernetesSpawner.spawn_agent_job`` from the same
    # ``slice_id`` parameter that drives Job naming and worktree id, so
    # there is no need to also stuff it into ``sandbox_env`` here. The
    # key is in ``_PROTECTED_ENV_KEYS`` so any future caller that does
    # supply a value via ``extra_env`` is logged and overridden.

    # Build per-role prompts for concurrent phase execution.
    from egg_contracts.agent_roles import get_roles_for_phase as _get_roles_for_phase

    roles: list[_pkg.AgentRole] = []
    for r in _get_roles_for_phase(
        phase_str,
        include_reviewers=True,
        repo=pipeline.repo,
        has_contract=getattr(pipeline, "has_contract", True),
    ):
        try:
            roles.append(_pkg.AgentRole(r.value))
        except ValueError:
            # New roles not yet in orchestrator AgentRole — skip
            continue

    # Build a review graph filtered to only active roles so consensus
    # tracking doesn't wait for unspawned agents.
    from review_graph import ReviewGraph
    from review_graph import get_review_graph_for_phase as _get_graph

    full_graph = _get_graph(phase_str, repo=pipeline.repo)
    active_role_names = {r.value for r in roles}
    filtered_edges = [
        e
        for e in full_graph.edges
        if e.reviewer_role in active_role_names and e.producer_role in active_role_names
    ]
    filtered_graph = ReviewGraph(filtered_edges)

    # Scope the per-slice team to the slice's repo (#3393 task-6-1).
    #
    # Every slice maps to exactly one repo (slice ↔ repo, 1:1). For a
    # multi-repo pipeline the slice's work, worktree, test gate, reviewer
    # diff and PR all live in ITS repo — not necessarily the pipeline
    # primary. We resolve the slice's repo via ``resolve_slice_repo`` and
    # thread the slice-scoped repo / worktree / base-branch into the agent
    # prompts (which drive ``get_repo_checks`` for the tester's configured
    # checks, the file-boundary patterns, and the reviewer's
    # ``git diff origin/<base>...HEAD``) and the spawn (via ``base_branch``
    # → ``EGG_BASE_BRANCH`` and a slice-primary-first ``repos`` ordering so
    # the spawner sets the agent cwd / ``EGG_REPO_PATH`` to the slice's
    # repo worktree).
    #
    # N=1 stays byte-identical: a single-repo pipeline has one RepoSpec, so
    # the block below is skipped entirely (``len(pipeline.repos) <= 1``),
    # leaving ``slice_repo == pipeline.repo``, ``worktree_repo_path``, and
    # the pipeline base branch exactly as before — no extra contract read.
    slice_repo = pipeline.repo
    slice_repo_path = worktree_repo_path
    slice_repos = repos
    slice_base_branch: str | None = None
    if slice_id and len(getattr(pipeline, "repos", None) or []) > 1:
        from egg_contracts.loader import load_contract

        slice_obj = None
        try:
            _contract = load_contract(pipeline_id, worktree_repo_path)
            slice_obj = next((s for s in _contract.slices if s.id == slice_id), None)
        except Exception as contract_err:  # noqa: BLE001
            # Best-effort: a contract load/parse failure degrades to the
            # pipeline-primary repo (today's behaviour), it does not block
            # the spawn. The slice still runs, just against the primary.
            _pkg.logger.warning(
                "Slice-repo scoping: contract load failed; using pipeline primary repo (#3393)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                error=str(contract_err),
            )

        # Single gate-repo accessor (shared with the tester's task-6-2
        # TestSliceGateRepoAccessor): the repo the whole slice team scopes to.
        resolved = _pkg._resolve_slice_gate_repo(slice_obj, pipeline) if slice_obj else None
        if resolved and resolved != pipeline.repo:
            slice_repo = resolved
            slice_repo_path = _pkg._resolve_slice_worktree_path(
                pipeline, resolved, worktree_repo_path
            )
            # Per-repo base branch from the pipeline's RepoSpec list.
            for spec in pipeline.repos or []:
                if getattr(spec, "repo", None) == resolved:
                    slice_base_branch = getattr(spec, "base_branch", None)
                    break
            # Order the slice's repo first so the spawner treats it as the
            # effective repo for this per-slice team (cwd / EGG_REPO_PATH).
            # ``repo_volumes`` already carries every repo owner/repo-keyed
            # (slice-3), so only the ordering changes here.
            slice_repos = [resolved, *[r for r in repos if r != resolved]]
            _pkg.logger.info(
                "Slice scoped to secondary repo (#3393 task-6-1)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                slice_repo=slice_repo,
                slice_worktree=str(slice_repo_path),
            )

    # Resolve base branch for diff commands in agent prompts. Prefer the
    # slice repo's own base (its RepoSpec.base_branch) over the pipeline
    # singleton, then fall back to auto-detecting the default branch in the
    # slice's worktree (#3393 task-6-1). For N=1 this is the pipeline base /
    # pipeline worktree exactly as before.
    _resolved_base_branch = slice_base_branch or pipeline.base_branch
    if not _resolved_base_branch:
        try:
            _resolved_base_branch = _pkg.get_default_branch(slice_repo_path)
        except Exception:
            _resolved_base_branch = None

    # A producer with no work in this slice is no longer pre-seeded (#3027
    # retired the #2581 pre-seed). It stays spawned and, if it finds it has
    # nothing to contribute, submits a generic no-op propose
    # (``no_changes_needed=true``) — the prompts below tell every producer
    # about that path. The consensus protocol accepts the no-op durably, so
    # no orchestrator-side roster pre-classification is needed.
    agent_prompts: dict[_pkg.AgentRole, str] = {}
    for role in roles:
        prompt = _pkg._build_agent_prompt(
            role_value=role.value,
            phase=phase_str,
            pipeline_id=pipeline_id,
            pipeline_mode=pipeline_mode,
            prompt=pipeline.prompt,
            issue_number=pipeline.issue_number,
            # Slice-scoped repo / worktree (#3393 task-6-1): drives the
            # tester's ``get_repo_checks`` (per-repo configured checks),
            # the role file-boundary patterns, and the reviewer diff base —
            # all resolve from the slice's repo, not the pipeline primary.
            # N=1 ⇒ these equal ``pipeline.repo`` / ``worktree_repo_path``.
            repo=slice_repo,
            branch=pipeline.branch,
            base_branch=_resolved_base_branch,
            repo_path=str(slice_repo_path),
            concurrent=True,
            review_feedback=review_feedback,
            network_mode=gateway_mode,
            operator_directives=operator_directives,
            iteration_history=iteration_history,
        )
        agent_prompts[role] = prompt

    # Create spawn function and executor.
    spawn_fn = spawner.create_concurrent_spawn_fn(
        pipeline_id=pipeline_id,
        issue_number=pipeline.issue_number,
        repo_volumes=repo_volumes,
        mode=gateway_mode,
        # Slice's repo first (#3393 task-6-1): the spawner derives the agent
        # cwd / EGG_REPO_PATH from the primary (first) repo, so ordering the
        # slice's repo first sets the working directory to that repo's
        # worktree. N=1 / primary-repo slices leave ``repos`` unchanged.
        repos=slice_repos,
        phase=phase_str,
        sandbox_env=sandbox_env,
        certs_volume=certs_volume,
        # Pass the *resolved* base branch (above) rather than the raw
        # ``pipeline.base_branch`` so a ``None`` (auto-detect) base still
        # reaches the spawner as a concrete branch name. The spawner exports
        # it as ``EGG_BASE_BRANCH`` for the BRC event-pump's per-producer
        # ``git log --not origin/<base>`` delta (#2967); without a concrete
        # value the wrapper + composer fall back to ``origin/main`` and the
        # delta errors out on every non-``main`` repo. Worktree creation is
        # unaffected: the gateway resolves the same default branch when handed
        # ``None``, so resolving one layer up here is equivalent.
        base_branch=_resolved_base_branch,
        spawn_max_retries=pipeline.config.spawn_max_retries,
        spawn_retry_initial_backoff_seconds=pipeline.config.spawn_retry_initial_backoff_seconds,
        slice_id=slice_id,
    )

    max_concurrent = getattr(pipeline.config, "max_concurrent_agents", 6)
    # #3064 slice-3: in orchestrator-ownership mode the event loop watches
    # one-shot Job termination to drive failure supervision (backoff /
    # respawn / OVERSEER_ALERT). Hand it a Job-status observer when the
    # spawner can provide one (the kubernetes spawner); spawners without it
    # leave supervision observation dormant (pod mode is unaffected either way).
    event_status_view = None
    _make_status_view = getattr(spawner, "create_event_job_status_view", None)
    if callable(_make_status_view):
        event_status_view = _make_status_view()
    executor = ConcurrentPhaseExecutor(
        pipeline=pipeline,
        spawn_fn=spawn_fn,
        max_concurrent=max_concurrent,
        review_graph=filtered_graph,
        roles=roles,
        slice_id=slice_id,
        event_status_view=event_status_view,
    )

    # Spawn all agents with their prompts.
    executions = executor.spawn_all(agent_prompts=agent_prompts)

    # Phase-level retry for transient spawn failures (#1879).
    executions = _pkg._retry_transient_spawn_failures_impl(
        executions,
        pipeline=pipeline,
        executor=executor,
        agent_prompts=agent_prompts,
        spawner=spawner,
        pipeline_id=pipeline_id,
        phase_str=phase_str,
    )
    # Record spawned containers/agents in pipeline state.
    _pkg._record_spawned_agents_impl(
        executions,
        store=store,
        pipeline_id=pipeline_id,
        phase_str=phase_str,
        slice_id=slice_id,
    )

    # Check for spawn failures before waiting.  Stop successfully-spawned
    # containers so they don't continue running after the phase is aborted,
    # then write their terminal status back to pipeline state so get_status
    # agrees with list_containers (kubernetes_monitor won't reconcile a
    # non-RUNNING pipeline, so we must finalize here).
    spawn_failures = [e for e in executions if e.status.value == "failed"]
    if spawn_failures:
        survivor_container_ids: set[str] = set()
        for e in executions:
            if e.container_id and e.status.value != "failed":
                survivor_container_ids.add(e.container_id)
                try:
                    spawner.backend.stop_container(e.container_id, timeout=10)
                except Exception:
                    pass

        if store is not None:
            try:
                with _pkg.get_pipeline_state_lock(pipeline_id):
                    pip = store.load_pipeline(pipeline_id)
                    phase_execution = pip.get_phase_execution(PipelinePhase(phase_str))
                    abort_error = "Aborted during spawn-failure cleanup"
                    now = _pkg.datetime.now(_pkg.UTC)
                    for agent_state in phase_execution.agents:
                        if (
                            agent_state.container_id in survivor_container_ids
                            and agent_state.status == StateAgentStatus.RUNNING
                        ):
                            agent_state.status = StateAgentStatus.FAILED
                            agent_state.error = abort_error
                            agent_state.completed_at = now
                    for container_info in phase_execution.containers:
                        if (
                            container_info.container_id in survivor_container_ids
                            and container_info.status == ContainerStatus.RUNNING
                        ):
                            container_info.status = ContainerStatus.FAILED
                            container_info.exited_at = now
                    store.save_pipeline(pip)
            except Exception as cleanup_err:
                _pkg.logger.warning(
                    "Failed to record spawn-failure cleanup in pipeline state",
                    pipeline_id=pipeline_id,
                    error=str(cleanup_err),
                )

        raise _pkg.SpawnFailureError([(e.role.value, e.error) for e in spawn_failures])

    # Consensus-driven polling loop with container-exit fallback.
    #
    # The loop periodically checks consensus via executor.check_consensus().
    # When all agents signal READY, the phase completes immediately without
    # waiting for containers to exit.  If consensus is never reached (timeout
    # or all containers exit first), fall back to exit-code-based completion.
    active_executions = [e for e in executions if e.container_id]
    docker_client = spawner.backend
    all_logs: list[str] = []
    has_failures = [False]  # Mutable container for closure access
    # Lock kept for forward-compat; the polling loop is single-threaded
    # after the #1921 refactor but _record_container_exit uses the lock
    # and is called from multiple code paths.
    _logs_lock = _pkg.threading.Lock()

    poll_interval = 5  # seconds
    raw_timeout = resolve_consensus_timeout_minutes(pipeline.config, phase_str)
    consensus_timeout = max(raw_timeout, 1) * 60  # minimum 1 minute
    start_time = _pkg.time.monotonic()
    objection_decision_created = False

    # ``run_epoch`` is the authoritative epoch the owning ``_run_pipeline``
    # thread captured at start (#1638). The poll loop uses it to detect a
    # ``restart_phase`` (or any restart that bumps ``run_epoch``) that
    # superseded this thread (#3315). ``start_time`` is a fresh monotonic
    # clock per call, but a parked-then-restarted phase leaves the *old*
    # ``_run_concurrent_phase`` thread alive in its poll loop with a
    # ``start_time`` from the original phase start; once its ``elapsed``
    # crosses ``consensus_timeout`` it would fire a spurious consensus-timeout
    # OVERSEER_ALERT + HITL decision against the freshly-restarted phase. The
    # new ``_run_pipeline`` thread owns the pipeline now, so this stale thread
    # must bail before escalating. When ``run_epoch`` is not supplied (legacy
    # / direct-call callers) the guard is dormant — behaviour is unchanged.

    _superseded_by_restart = _pkg.functools.partial(
        _pkg._superseded_by_restart_impl,
        store=store,
        pipeline_id=pipeline_id,
        run_epoch=run_epoch,
    )

    # Track which containers have exited and their results.
    exited_containers: dict[str, ContainerInfo] = {}

    _record_container_exit = _pkg.functools.partial(
        _pkg._record_container_exit_impl,
        docker_client=docker_client,
        _logs_lock=_logs_lock,
        has_failures=has_failures,
        all_logs=all_logs,
        store=store,
        phase_str=phase_str,
        pipeline_id=pipeline_id,
    )

    _stop_running_containers = _pkg.functools.partial(
        _pkg._stop_running_containers_impl,
        active_executions=active_executions,
        exited_containers=exited_containers,
        docker_client=docker_client,
    )

    _latest_proposal_ts = _pkg._latest_proposal_ts_impl

    _update_agents_complete = _pkg.functools.partial(
        _pkg._update_agents_complete_impl,
        store=store,
        phase_str=phase_str,
        pipeline_id=pipeline_id,
        slice_id=slice_id,
    )

    _demoted_agents: set[str] = set()

    # #2243 progress-gate state: log on first defer + first un-defer only
    # so the polling loop doesn't spam at every iteration once we cross
    # ``consensus_timeout``.
    _progress_gate_deferring = False

    # #3426 HITL-gate state: same log-once discipline for the
    # operator-gated suspension of the consensus timeout.
    _hitl_gate_deferring = False

    while True:
        elapsed = _pkg.time.monotonic() - start_time

        # 0. Bail if a restart superseded this thread (#3315). A parked phase
        #    that is restarted after the consensus-timeout budget elapsed
        #    leaves this old thread polling with a stale ``start_time``; the
        #    new ``_run_pipeline`` thread already owns the pipeline. Exit
        #    cleanly — stop this executor's event loop so it stops requesting
        #    one-shot spawns — WITHOUT firing the timeout escalation. Return a
        #    NON-zero exit so the caller never mistakes this for success and
        #    advances the phase; the post-return epoch check (#1638) at the
        #    call site re-confirms the restart and exits the old thread without
        #    marking the phase FAILED.
        if _superseded_by_restart():
            _pkg.logger.info(
                "Phase superseded by restart (run_epoch changed) — exiting stale "
                "_run_concurrent_phase thread without escalation",
                pipeline_id=pipeline_id,
                phase=phase,
                slice_id=slice_id,
            )
            executor.stop_event_loop()
            return 1, "Phase superseded by restart; stale monitor thread exited."

        # 1. Check consensus
        try:
            consensus = executor.check_consensus()
        except Exception as e:
            _pkg.logger.warning(
                "Consensus check failed, continuing poll",
                pipeline_id=pipeline_id,
                error=str(e),
            )
            consensus = {"is_complete": False, "has_objections": False, "blocking_agents": []}

        # 2. Consensus reached — stop containers and return
        if consensus.get("is_complete"):
            # Recover pipeline if externally marked FAILED (issue #1273).
            # The container_monitor reconciliation thread may have marked the
            # pipeline FAILED while we were polling.  Now that consensus is
            # confirmed complete, restore the pipeline to RUNNING so stored
            # state matches the successful outcome.
            #
            # NOTE: consensus staleness is acceptable here.  The `consensus`
            # dict was fetched earlier in this loop iteration and is not
            # re-evaluated under the lock.  If consensus regressed between
            # the outer check and lock acquisition (extremely unlikely), the
            # next iteration of this monitoring loop will re-evaluate and
            # self-correct.
            if store is not None:
                try:
                    _current_pip = store.load_pipeline(pipeline_id)
                    if _current_pip.status == _pkg.PipelineStatus.FAILED:
                        _pkg.logger.warning(
                            "Pipeline externally marked FAILED but consensus is complete — recovering",
                            pipeline_id=pipeline_id,
                        )
                        with _pkg.get_pipeline_state_lock(pipeline_id):
                            _current_pip = store.load_pipeline(pipeline_id)
                            if _current_pip.status == _pkg.PipelineStatus.FAILED:
                                _current_pip.status = _pkg.PipelineStatus.RUNNING
                                _current_pip.error = None
                                store.save_pipeline(_current_pip)
                except Exception as recovery_err:
                    _pkg.logger.warning(
                        "External FAILED recovery check failed",
                        pipeline_id=pipeline_id,
                        error=str(recovery_err),
                    )

            if _pkg._emit_event is not None:
                _pkg._emit_event(
                    _pkg.EventType.CONSENSUS_REACHED,
                    pipeline_id,
                    data={"elapsed_seconds": elapsed},
                )
            _pkg.logger.info(
                "Consensus reached, stopping containers",
                pipeline_id=pipeline_id,
                elapsed_seconds=round(elapsed, 1),
                has_failures=has_failures[0],
            )
            _update_agents_complete()
            _stop_running_containers()
            combined_logs = (
                "\n".join(all_logs) if all_logs else "Consensus reached; phase complete."
            )
            # Consensus is the authoritative success signal.  When all agents
            # have confirmed (is_complete=True), container-level failures
            # (e.g. OOM kills that happened *before* the surviving agents
            # reached agreement) should not override the consensus result.
            # Any pending HITL decisions from handle_agent_failure remain
            # active for human review, but the pipeline itself succeeds.
            if has_failures[0]:
                _pkg.logger.warning(
                    "Container failures detected but consensus is complete — treating as success",
                    pipeline_id=pipeline_id,
                    has_failures=has_failures[0],
                )
            # Orchestrator mode (#3064): tear down the BRC event loop now that
            # the slice has converged so it stops requesting one-shot spawns.
            # No-op in pod mode.
            executor.stop_event_loop()
            return 0, combined_logs

        # 3. Handle objections (create HITL decision once).
        #    The decision is fire-and-forget: resolution is processed by the
        #    orchestrator's decision queue (outside this function).  If the
        #    human selects "Override objections", the orchestrator updates
        #    agent readiness, which is picked up by check_consensus() on
        #    the next poll iteration.  "Abort phase" triggers pipeline
        #    cancellation via a separate control path.
        if consensus.get("has_objections") and not objection_decision_created:
            decision = _pkg._persist_hitl_decision(
                pipeline_id,
                pipeline,
                store,
                question="Agent(s) objecting to phase completion. How to proceed?",
                options=["Override objections", "Wait for resolution", "Abort phase"],
                phase=pipeline.current_phase,
            )
            if decision is not None:
                objection_decision_created = True
                _pkg.logger.info(
                    "Objection detected, HITL decision created",
                    pipeline_id=pipeline_id,
                    blocking_agents=consensus.get("blocking_agents", []),
                )

        # 3b. RC3: Stall demotion for dual-role agents.
        # If a dual-role agent has missed heartbeats for 5+ minutes,
        # demote its reviewer edges to ADVISORY so other agents can proceed.
        try:
            from health_monitor import get_health_monitor

            _hm = get_health_monitor()
            if _hm is not None:
                try:
                    from peer_consensus import get_peer_consensus_tracker
                except ImportError:
                    from ..peer_consensus import (
                        get_peer_consensus_tracker,  # type: ignore[no-redef]
                    )

                # Slice-aware tracker lookup (#2137): per-slice trackers
                # are namespaced ``{pipeline_id}/{slice_id}`` so the
                # stall-demotion check fires against the correct scope.
                try:
                    _brc_tracker = get_peer_consensus_tracker(pipeline_id, slice_id)
                except TypeError:
                    _brc_tracker = get_peer_consensus_tracker(pipeline_id)
                if _brc_tracker is not None:
                    heartbeat_actions = _hm.check_heartbeats()
                    for hb_action in heartbeat_actions:
                        stalled_agent = hb_action.get("agent_id", "")
                        stall_elapsed = hb_action.get("elapsed_seconds", 0)
                        if (
                            stall_elapsed >= 300
                            and stalled_agent not in _demoted_agents
                            and _brc_tracker.graph.is_dual_role(stalled_agent)
                        ):
                            try:
                                _brc_tracker.handle_stall_demotion(
                                    stalled_agent,
                                    reason=f"Missed heartbeats for {stall_elapsed}s",
                                )
                                _demoted_agents.add(stalled_agent)
                            except Exception as demote_err:
                                _pkg.logger.debug(
                                    "Stall demotion skipped",
                                    agent=stalled_agent,
                                    error=str(demote_err),
                                )
        except Exception as stall_err:
            _pkg.logger.debug(
                "Stall demotion check failed",
                pipeline_id=pipeline_id,
                error=str(stall_err),
            )

        # 4. Non-blocking check for exited containers
        for exec_info in active_executions:
            if exec_info.container_id in exited_containers:
                continue
            try:
                info = docker_client.get_container_info(exec_info.container_id)
            except (
                _pkg.ContainerNotFoundError,
                _pkg.ContainerOperationError,
                _pkg.PodNotFoundError,
                _pkg.JobOperationError,
            ) as e:
                _pkg.logger.warning(
                    "Container lost during poll",
                    container_id=exec_info.container_id,
                    role=exec_info.role.value,
                    error=str(e),
                )
                info = ContainerInfo(
                    container_id=exec_info.container_id,
                    container_name=f"{pipeline_id}-{exec_info.role.value}",
                    status=ContainerStatus.FAILED,
                    exit_code=-1,
                    exited_at=_pkg.datetime.now(_pkg.UTC),
                )

            if info.status in (
                ContainerStatus.EXITED,
                ContainerStatus.FAILED,
                ContainerStatus.REMOVED,
            ):
                exited_containers[exec_info.container_id] = info
                _record_container_exit(exec_info, info)

                # Handle non-clean exit as agent failure.  0 = normal,
                # 143 = orchestrator-initiated SIGTERM (#2210) — both
                # are classified as clean here to match the K8s monitor's
                # _classify_exit, so the two layers can't race to write
                # contradictory agent.status values.
                if info.exit_code not in (0, 143):
                    # Issue #2806 (Option A): a producer's consensus-wrapper
                    # exhausting its retry budget is unrecoverable — the
                    # slice state machine cannot replace a permanently dead
                    # producer, and the surviving reviewers will heartbeat
                    # forever waiting on a proposal that will never come.
                    # Detect this case and short-circuit the polling loop
                    # with a non-zero return so the caller transitions the
                    # pipeline (or slice) to FAILED. Reviewer-only deaths
                    # still flow through ``handle_agent_failure`` because
                    # peer-review redistribution can recover them.
                    role_value = exec_info.role.value
                    if filtered_graph.is_producer(role_value):
                        # Race window guard: a producer can legitimately
                        # exit non-zero after CONFIRMED (wrapper cleanup
                        # crash) — between step 1 (consensus check) and
                        # step 4 (exit detection) the producer could have
                        # written CONFIRMED and then died. Re-query
                        # consensus before hard-failing; if it has
                        # completed, fall through and let the next
                        # iteration's step 1/2 return success.
                        try:
                            recheck = executor.check_consensus()
                        except Exception as recheck_err:
                            _pkg.logger.warning(
                                "Producer-death consensus recheck failed",
                                pipeline_id=pipeline_id,
                                role=role_value,
                                error=str(recheck_err),
                            )
                            recheck = {"is_complete": False}
                        if recheck.get("is_complete"):
                            _pkg.logger.info(
                                "Producer container exited non-zero but consensus already complete — skipping hard-fail",
                                pipeline_id=pipeline_id,
                                role=role_value,
                                exit_code=info.exit_code,
                            )
                            # Consensus completed in the race window before
                            # the producer's wrapper-cleanup crash. Step 5
                            # (or the next iteration's step 1/2) will return
                            # success; skip handle_agent_failure (reviewer
                            # recovery path, not applicable to producers).
                            continue
                        _pkg._emit_producer_death_alert(
                            pipeline_id=pipeline_id,
                            role=role_value,
                            phase=phase_str,
                            slice_id=slice_id,
                            exit_code=info.exit_code,
                        )
                        _pkg.logger.error(
                            "Producer agent died permanently — failing phase",
                            pipeline_id=pipeline_id,
                            phase=phase_str,
                            slice_id=slice_id,
                            role=role_value,
                            exit_code=info.exit_code,
                        )
                        _stop_running_containers()
                        combined_logs = "\n".join(
                            all_logs
                            + [
                                "--- PRODUCER PERMANENT DEATH ---",
                                (
                                    f"Producer '{role_value}' container exited with code "
                                    f"{info.exit_code} after the consensus-wrapper exhausted "
                                    f"its retry budget. Pipeline failing (issue #2806)."
                                ),
                            ]
                        )
                        return 1, combined_logs
                    try:
                        executor.handle_agent_failure(
                            role=role_value,
                            error=f"Container exited with code {info.exit_code}",
                        )
                    except Exception as e:
                        _pkg.logger.warning(
                            "handle_agent_failure error",
                            role=role_value,
                            error=str(e),
                        )
                else:
                    # Clean exit (0 or 143): the consensus wrapper inside
                    # the container handles restarts if the agent didn't
                    # signal READY. We do NOT auto-register READY here —
                    # agents must explicitly participate in consensus.
                    _pkg.logger.info(
                        "Container exited cleanly, wrapper handles consensus",
                        pipeline_id=pipeline_id,
                        role=exec_info.role.value,
                        exit_code=info.exit_code,
                    )

        # 5. All containers exited — fall back to exit-code-based result.
        #
        # Guarded on a non-empty ``active_executions`` so an empty set is
        # never misread as "everything exited" (``0 >= 0``).  In orchestrator
        # mode (#3064) ``spawn_all`` returns ``[]`` by design — the
        # orchestrator owns the BRC loop and spawns one-shot pods per event,
        # so there are no up-front containers to track.  Completion is driven
        # purely off ``check_consensus()`` (step 2) and the consensus timeout
        # (step 6); a zero-container fallback here would otherwise fail the
        # phase on the first poll, before any event-driven pod ran.
        if active_executions and len(exited_containers) >= len(active_executions):
            combined_logs = "\n".join(all_logs)
            if has_failures[0]:
                # Final consensus recheck: consensus may have completed between
                # the step-2 check and now (race window while containers were
                # shutting down).  Re-query before giving up.
                try:
                    final_consensus = executor.check_consensus()
                except Exception as e:
                    _pkg.logger.warning(
                        "Final consensus recheck failed, treating as incomplete",
                        pipeline_id=pipeline_id,
                        error=str(e),
                    )
                    final_consensus = {"is_complete": False}

                if final_consensus.get("is_complete"):
                    # Guard: consensus may be "complete" by quorum but still
                    # have unresolved NACKs — mirror the step 5 no-failure
                    # NACK check and the timeout path NACK check.
                    if final_consensus.get("has_unresolved_nacks"):
                        nack_details = final_consensus.get("unresolved_nacks", [])
                        nack_summary = _pkg._format_nack_summary(nack_details)
                        _pkg.logger.warning(
                            "Consensus complete on final recheck but unresolved NACKs remain (has_failures path)",
                            pipeline_id=pipeline_id,
                            nack_count=len(nack_details),
                            nack_summary=nack_summary,
                        )
                        # Tag with the consensus-timeout context so "Retry
                        # phase" dispatches through restart_phase on resolve
                        # (#3421), for symmetry with the incomplete-consensus
                        # sites below.  This question is hand-built and does not
                        # promise restart copy, but restart_phase is the correct
                        # "Retry phase" action regardless.  Like its siblings
                        # this pod-mode path is unreachable today (spawn_all
                        # returns [] post-#3164, so active_executions is always
                        # empty); tagging keeps the dispatch honest if pod mode
                        # is ever revived.
                        _pkg._persist_hitl_decision(
                            pipeline_id,
                            pipeline,
                            store,
                            question=(
                                f"Consensus reached but {len(nack_details)} NACK(s) "
                                f"remain unresolved: {nack_summary}. How to proceed?"
                            ),
                            options=["Retry phase", "Accept current state", "Abort phase"],
                            phase=pipeline.current_phase,
                            context=_pkg._CONSENSUS_TIMEOUT_HITL_CONTEXT,
                        )
                        combined_logs += (
                            f"\n--- UNRESOLVED NACKs ({len(nack_details)}) ---\n{nack_summary}"
                        )
                        return 1, combined_logs

                    # Consensus reached after all — recover pipeline if needed
                    if store is not None:
                        try:
                            _current_pip = store.load_pipeline(pipeline_id)
                            if _current_pip.status == _pkg.PipelineStatus.FAILED:
                                _pkg.logger.warning(
                                    "Pipeline externally marked FAILED but consensus is complete — recovering",
                                    pipeline_id=pipeline_id,
                                )
                                with _pkg.get_pipeline_state_lock(pipeline_id):
                                    _current_pip = store.load_pipeline(pipeline_id)
                                    if _current_pip.status == _pkg.PipelineStatus.FAILED:
                                        _current_pip.status = _pkg.PipelineStatus.RUNNING
                                        _current_pip.error = None
                                        store.save_pipeline(_current_pip)
                        except Exception as recovery_err:
                            _pkg.logger.warning(
                                "External FAILED recovery check failed",
                                pipeline_id=pipeline_id,
                                error=str(recovery_err),
                            )

                    _elapsed_final = _pkg.time.monotonic() - start_time
                    if _pkg._emit_event is not None:
                        _pkg._emit_event(
                            _pkg.EventType.CONSENSUS_REACHED,
                            pipeline_id,
                            data={"elapsed_seconds": _elapsed_final},
                        )
                    _pkg.logger.info(
                        "Consensus reached on final recheck, stopping containers",
                        pipeline_id=pipeline_id,
                        elapsed_seconds=round(_elapsed_final, 1),
                        has_failures=has_failures[0],
                    )
                    _update_agents_complete()
                    _stop_running_containers()
                    return 0, combined_logs

                # Incomplete consensus + container failures: surface an HITL
                # decision so the operator can drive recovery (issue #2203).
                # Without this, the phase fails terminally with no signal —
                # the agent's committed work is still on the per-role branch
                # and `restart_phase` would recover, but the operator has no
                # way to know that without out-of-band investigation.
                #
                # If an objection HITL was created earlier in the polling loop
                # this is intentionally a *second* pending decision: it
                # carries different options ("Retry phase" / "Accept current
                # state" / "Abort phase" vs the objection set) and conveys a
                # different operator action.  The test
                # `test_objection_dedup_distinct_from_incomplete_consensus_hitl`
                # locks in the two-decision UX.
                failure_count = sum(1 for info in exited_containers.values() if info.exit_code != 0)
                question, log_suffix = _pkg._incomplete_consensus_decision_text(
                    final_consensus, container_failure_count=failure_count
                )
                _pkg.logger.warning(
                    "Incomplete consensus with container failures — escalating to HITL",
                    pipeline_id=pipeline_id,
                    failure_count=failure_count,
                    blocking_agents=final_consensus.get("blocking_agents", []),
                    nack_count=len(final_consensus.get("unresolved_nacks", []) or []),
                )
                # Tag with the consensus-timeout context so "Retry phase"
                # dispatches through restart_phase on resolve (#3421), matching
                # the restart semantics `_incomplete_consensus_decision_text`
                # promises.  This pod-mode container-exit path is unreachable
                # today (spawn_all returns [] post-#3164, so active_executions
                # is always empty), but tagging keeps the copy honest if pod
                # mode is ever revived.
                _pkg._persist_hitl_decision(
                    pipeline_id,
                    pipeline,
                    store,
                    question=question,
                    options=["Retry phase", "Accept current state", "Abort phase"],
                    phase=pipeline.current_phase,
                    context=_pkg._CONSENSUS_TIMEOUT_HITL_CONTEXT,
                )
                combined_logs += log_suffix
                return 1, combined_logs

            # Before returning success, check the BRC approval matrix for
            # unresolved NACKs.  If reviewers NACKed but producers exited
            # without iterating, we must NOT report success — escalate to
            # HITL so a human can decide how to proceed.
            if consensus.get("has_unresolved_nacks"):
                nack_details = consensus.get("unresolved_nacks", [])
                nack_summary = _pkg._format_nack_summary(nack_details)
                _pkg.logger.warning(
                    "All containers exited with unresolved NACKs",
                    pipeline_id=pipeline_id,
                    nack_count=len(nack_details),
                    nack_summary=nack_summary,
                )
                # Same as the unresolved-NACK site above: tag with the
                # consensus-timeout context so "Retry phase" dispatches through
                # restart_phase (#3421) for symmetry.  Hand-built question, no
                # restart copy, but restart_phase is the right action here too.
                # Dead pod-mode path today; tagging is cheap insurance.
                _pkg._persist_hitl_decision(
                    pipeline_id,
                    pipeline,
                    store,
                    question=(
                        f"All agents exited but {len(nack_details)} NACK(s) remain "
                        f"unresolved: {nack_summary}. How to proceed?"
                    ),
                    options=["Retry phase", "Accept current state", "Abort phase"],
                    phase=pipeline.current_phase,
                    context=_pkg._CONSENSUS_TIMEOUT_HITL_CONTEXT,
                )
                combined_logs += f"\n--- UNRESOLVED NACKs ({len(nack_details)}) ---\n{nack_summary}"
                return 1, combined_logs

            # Final consensus completeness check: all containers exited
            # cleanly (no failures, no NACKs) but consensus may not have
            # been reached.  Mirror the has_failures branch pattern to
            # prevent advancing without confirmed BRC consensus.
            try:
                final_consensus = executor.check_consensus()
            except Exception as e:
                _pkg.logger.warning(
                    "Final consensus recheck failed on clean exit, treating as incomplete",
                    pipeline_id=pipeline_id,
                    error=str(e),
                )
                final_consensus = {"is_complete": False}

            if not final_consensus.get("is_complete"):
                # Symmetric to the has_failures path: clean exits with no
                # consensus also need an HITL decision so the operator can
                # drive recovery (issue #2203).
                question, log_suffix = _pkg._incomplete_consensus_decision_text(
                    final_consensus, container_failure_count=0
                )
                _pkg.logger.warning(
                    "All containers exited cleanly but consensus not reached — escalating to HITL",
                    pipeline_id=pipeline_id,
                    elapsed_seconds=round(elapsed, 1),
                    blocking_agents=final_consensus.get("blocking_agents", []),
                )
                # Same as the container-failure path above: tag with the
                # consensus-timeout context so "Retry phase" dispatches through
                # restart_phase (#3421) and honors the restart copy.  Also a
                # dead pod-mode path today; tagging is cheap insurance.
                _pkg._persist_hitl_decision(
                    pipeline_id,
                    pipeline,
                    store,
                    question=question,
                    options=["Retry phase", "Accept current state", "Abort phase"],
                    phase=pipeline.current_phase,
                    context=_pkg._CONSENSUS_TIMEOUT_HITL_CONTEXT,
                )
                combined_logs += log_suffix
                return 1, combined_logs

            # Consensus confirmed on clean exit — mirror the has_failures
            # success path: emit event, update agent state, stop containers.
            if _pkg._emit_event is not None:
                _pkg._emit_event(
                    _pkg.EventType.CONSENSUS_REACHED,
                    pipeline_id,
                    data={"elapsed_seconds": elapsed},
                )
            _pkg.logger.info(
                "Consensus reached on final recheck, stopping containers",
                pipeline_id=pipeline_id,
                elapsed_seconds=round(elapsed, 1),
                has_failures=has_failures[0],
            )
            _update_agents_complete()
            _stop_running_containers()
            return 0, combined_logs

        # 6. Consensus timeout
        if elapsed >= consensus_timeout:
            # #3426 HITL gate: while an unresolved operator HITL decision
            # (contract ``cq-N``) gates the running phase, the slice is
            # provably operator-gated — a reviewer withholding its ACK
            # pending a human ruling is the system working as designed, not
            # a convergence failure. Suspend the timeout (keep polling, no
            # alert, no failure) until the operator answers. On release,
            # reset the convergence clock so the agents folding in the
            # resolution get a full fresh window instead of a clock that
            # already expired while the human was thinking.
            _hitl_ids = _pkg._unresolved_contract_hitl_ids(pipeline_id, pipeline, phase_str)
            if _hitl_ids:
                if not _hitl_gate_deferring:
                    _pkg.logger.info(
                        "Consensus timeout suspended — phase is operator-gated "
                        "on unresolved HITL decision(s)",
                        pipeline_id=pipeline_id,
                        slice_id=slice_id,
                        elapsed_seconds=round(elapsed, 1),
                        decision_ids=_hitl_ids,
                    )
                    _hitl_gate_deferring = True
                _pkg.time.sleep(poll_interval)
                continue
            if _hitl_gate_deferring:
                _hitl_gate_deferring = False
                start_time = _pkg.time.monotonic()
                _pkg.logger.info(
                    "Consensus timeout clock reset — operator HITL decision(s) resolved",
                    pipeline_id=pipeline_id,
                    slice_id=slice_id,
                    suspended_after_seconds=round(elapsed, 1),
                )
                continue

            # #2243 progress gate: keep polling instead of publishing
            # the consensus-timeout alert while producer/reviewer
            # activity is still live on the BRC bus or in container
            # heartbeats. Without this gate, the historical decision-15
            # / decision-17 misfires on ``issue-1557-v2`` (now
            # ``OVERSEER_ALERT`` post-#2264) fired minutes before the
            # next commit landed.
            _gate_seconds = max(
                0,
                int(getattr(pipeline.config, "brc_consensus_progress_gate_seconds", 300)),
            )
            _gate_defer, _gate_reason = _pkg._check_brc_progress_gate(
                pipeline_id,
                slice_id,
                [e.role.value for e in active_executions],
                _gate_seconds,
            )
            if _gate_defer:
                if not _progress_gate_deferring:
                    _pkg.logger.info(
                        "Consensus timeout deferred by progress gate",
                        pipeline_id=pipeline_id,
                        elapsed_seconds=round(elapsed, 1),
                        gate_seconds=_gate_seconds,
                        reason=_gate_reason,
                    )
                    _progress_gate_deferring = True
                _pkg.time.sleep(poll_interval)
                continue
            if _progress_gate_deferring:
                _pkg.logger.info(
                    "Consensus timeout proceeding — progress gate window elapsed",
                    pipeline_id=pipeline_id,
                    elapsed_seconds=round(elapsed, 1),
                    gate_seconds=_gate_seconds,
                )
                _progress_gate_deferring = False

            # #3490 live-widening gate: re-resolve the budget from freshly
            # loaded config so a PATCH /config update of
            # ``consensus_timeout_minutes*`` takes effect any time before the
            # wall fires; an operator watching a giant slice can widen the
            # window without letting the slice fail and restarting. Checked
            # here, after the HITL and progress gates, so the load only
            # happens once per firing rather than on every deferred poll. A
            # load failure keeps the current budget: a transient store hiccup
            # must never widen or shrink the window on its own.
            if store is not None:
                _fresh_minutes: int | None = None
                try:
                    _fresh_config = store.load_pipeline(pipeline_id).config
                    _fresh_minutes = resolve_consensus_timeout_minutes(_fresh_config, phase_str)
                except Exception as _reresolve_err:
                    _pkg.logger.warning(
                        "Consensus-timeout config re-resolve failed; keeping current budget",
                        pipeline_id=pipeline_id,
                        error=str(_reresolve_err),
                    )
                # The isinstance guard keeps a malformed store payload (or a
                # test double) from replacing the numeric budget.
                if isinstance(_fresh_minutes, int) and not isinstance(_fresh_minutes, bool):
                    _fresh_timeout = max(_fresh_minutes, 1) * 60
                    if _fresh_timeout != consensus_timeout:
                        _pkg.logger.info(
                            "Consensus timeout budget updated from live config",
                            pipeline_id=pipeline_id,
                            slice_id=slice_id,
                            old_timeout_minutes=consensus_timeout / 60,
                            new_timeout_minutes=_fresh_timeout / 60,
                        )
                        consensus_timeout = _fresh_timeout
                        if elapsed < consensus_timeout:
                            _pkg.time.sleep(poll_interval)
                            continue

            _pkg.logger.warning(
                "Consensus timeout reached, falling back to container exit",
                pipeline_id=pipeline_id,
                timeout_minutes=consensus_timeout / 60,
            )
            # Orchestrator mode (#3064): we are giving up on convergence, so
            # stop the BRC event loop before the fallback wait so it does not
            # keep spawning one-shot pods past the deadline.  No-op in pod
            # mode.  (The progress-gate ``continue`` above is taken before
            # this point, so a deferral never reaches here and the loop keeps
            # running across the deferral window.)
            executor.stop_event_loop()
            _pkg._handle_brc_consensus_timeout(
                pipeline,
                pipeline_id,
                consensus_timeout,
                consensus.get("blocking_agents", []),
                store,
                slice_id=slice_id,
                active_role_names=[e.role.value for e in active_executions],
            )

            # Fall back: event-driven wait for remaining containers.
            #
            # Issue #1921: the previous implementation used a
            # ThreadPoolExecutor with a blocking
            # wait_for_container(timeout=3600) per container.  During
            # that hour the polling loop was blind to BRC progress —
            # a NACK → re-propose → ACK cycle completing in the final
            # minute could still be force-killed.  Now we poll
            # container status in short steps and re-check consensus
            # between steps, early-returning on completion before
            # force-killing anything.
            #
            # Issue #2245: the per-iteration budget rebaselines on
            # producer progress.  Each new CONSENSUS_PROPOSE (initial
            # or NACK→re-propose) resets ``last_progress_at`` so the
            # producer's next iteration gets a clean clock instead of
            # inheriting the prior iterations' wall-clock spend.  An
            # absolute cap (``post_consensus_max_total_seconds``)
            # bounds the total wait so an unbounded propose churn
            # can't stall the pipeline indefinitely.
            remaining = [e for e in active_executions if e.container_id not in exited_containers]
            if remaining:
                post_timeout_iteration_budget = (
                    pipeline.config.post_consensus_iteration_budget_seconds
                )
                post_timeout_max_total = pipeline.config.post_consensus_max_total_seconds
                post_timeout_poll_interval = 30  # seconds between checks
                post_timeout_start = _pkg.time.monotonic()
                last_progress_at = post_timeout_start

                # Snapshot the latest proposal timestamp at entry so we
                # only count *new* proposals as progress signals.  ``None``
                # is fine: the rebaseline check at the bottom of the loop
                # short-circuits on ``last_seen_proposal_ts is None``
                # before any datetime comparison runs.
                last_seen_proposal_ts = _latest_proposal_ts(pipeline_id, slice_id)

                while remaining:
                    now_monotonic = _pkg.time.monotonic()
                    total_elapsed = now_monotonic - post_timeout_start
                    iteration_elapsed = now_monotonic - last_progress_at
                    if total_elapsed >= post_timeout_max_total:
                        _pkg.logger.warning(
                            "Post-consensus-timeout absolute cap reached",
                            pipeline_id=pipeline_id,
                            total_elapsed_seconds=round(total_elapsed, 1),
                            max_total_seconds=post_timeout_max_total,
                        )
                        break
                    if iteration_elapsed >= post_timeout_iteration_budget:
                        _pkg.logger.warning(
                            "Post-consensus-timeout iteration budget exhausted",
                            pipeline_id=pipeline_id,
                            iteration_elapsed_seconds=round(iteration_elapsed, 1),
                            iteration_budget_seconds=post_timeout_iteration_budget,
                            total_elapsed_seconds=round(total_elapsed, 1),
                        )
                        break

                    # A. Re-check consensus; if agents converged during
                    # the wait, stop containers and return success
                    # before force-killing them.
                    try:
                        _wait_consensus = executor.check_consensus()
                    except Exception as _wait_consensus_err:
                        _pkg.logger.warning(
                            "Consensus recheck during post-timeout wait failed",
                            pipeline_id=pipeline_id,
                            error=str(_wait_consensus_err),
                        )
                        _wait_consensus = None

                    if (
                        _wait_consensus
                        and _wait_consensus.get("is_complete")
                        and not _wait_consensus.get("has_unresolved_nacks")
                    ):
                        combined_logs = "\n".join(all_logs)
                        _total_elapsed = _pkg.time.monotonic() - start_time
                        if _pkg._emit_event is not None:
                            _pkg._emit_event(
                                _pkg.EventType.CONSENSUS_REACHED,
                                pipeline_id,
                                data={"elapsed_seconds": _total_elapsed},
                            )
                        _pkg.logger.info(
                            "Consensus reached during post-timeout wait",
                            pipeline_id=pipeline_id,
                            elapsed_post_timeout_seconds=round(total_elapsed, 1),
                            total_elapsed_seconds=round(_total_elapsed, 1),
                        )
                        _update_agents_complete()
                        _stop_running_containers()
                        return 0, combined_logs

                    # A'. Rebaseline the iteration clock on producer
                    # progress (#2245).  A fresh CONSENSUS_PROPOSE
                    # timestamp means a producer just landed work
                    # (initial propose or NACK→re-propose) — the next
                    # round of reviews deserves its own iteration
                    # budget, not whatever's left of the prior round's.
                    current_proposal_ts = _latest_proposal_ts(pipeline_id, slice_id)
                    if current_proposal_ts is not None and (
                        last_seen_proposal_ts is None or current_proposal_ts > last_seen_proposal_ts
                    ):
                        _pkg.logger.info(
                            "Post-consensus-timeout clock rebaselined on producer progress",
                            pipeline_id=pipeline_id,
                            iteration_elapsed_seconds=round(iteration_elapsed, 1),
                            total_elapsed_seconds=round(total_elapsed, 1),
                            proposal_timestamp=current_proposal_ts.isoformat(),
                        )
                        last_seen_proposal_ts = current_proposal_ts
                        last_progress_at = _pkg.time.monotonic()

                    # B. Non-blocking container status check; record
                    # any that have exited naturally.
                    still_running = []
                    for exec_info in remaining:
                        try:
                            info = docker_client.get_container_info(exec_info.container_id)
                        except (
                            _pkg.ContainerNotFoundError,
                            _pkg.ContainerOperationError,
                            _pkg.PodNotFoundError,
                            _pkg.JobOperationError,
                        ) as _wait_status_err:
                            _pkg.logger.warning(
                                "Container lost during post-timeout wait",
                                container_id=exec_info.container_id,
                                role=exec_info.role.value,
                                error=str(_wait_status_err),
                            )
                            info = ContainerInfo(
                                container_id=exec_info.container_id,
                                container_name=f"{pipeline_id}-{exec_info.role.value}",
                                status=ContainerStatus.FAILED,
                                exit_code=-1,
                                exited_at=_pkg.datetime.now(_pkg.UTC),
                            )

                        if info.status in (
                            ContainerStatus.EXITED,
                            ContainerStatus.FAILED,
                            ContainerStatus.REMOVED,
                        ):
                            exited_containers[exec_info.container_id] = info
                            _record_container_exit(exec_info, info)
                        else:
                            still_running.append(exec_info)

                    remaining = still_running
                    if not remaining:
                        break

                    _pkg.time.sleep(post_timeout_poll_interval)

                # Budget exhausted with containers still running —
                # force-kill so they don't orphan (issue #1691).
                for exec_info in remaining:
                    try:
                        docker_client.stop_container(exec_info.container_id, timeout=30)
                    except Exception:
                        pass
                    final_info = ContainerInfo(
                        container_id=exec_info.container_id,
                        container_name=f"{pipeline_id}-{exec_info.role.value}",
                        status=ContainerStatus.FAILED,
                        exit_code=-1,
                        exited_at=_pkg.datetime.now(_pkg.UTC),
                    )
                    exited_containers[exec_info.container_id] = final_info
                    _record_container_exit(exec_info, final_info)

            combined_logs = "\n".join(all_logs)
            if has_failures[0]:
                # Consensus recheck: consensus may have completed right as the
                # post-timeout budget elapsed and containers were force-killed
                # (issue #1691).  The in-loop consensus check covers the common
                # case; this recheck catches the narrow race where consensus
                # completed between the last in-loop check and force-kill.
                try:
                    _timeout_consensus = executor.check_consensus()
                except Exception as e:
                    _pkg.logger.warning(
                        "Consensus recheck after timeout failed, treating as incomplete",
                        pipeline_id=pipeline_id,
                        error=str(e),
                    )
                    _timeout_consensus = {"is_complete": False}

                if _timeout_consensus.get("is_complete"):
                    # Guard: consensus may be "complete" by quorum but still
                    # have unresolved NACKs — mirror the step 5 NACK check.
                    if _timeout_consensus.get("has_unresolved_nacks"):
                        nack_details = _timeout_consensus.get("unresolved_nacks", [])
                        nack_summary = _pkg._format_nack_summary(nack_details)
                        _pkg.logger.warning(
                            "Consensus complete on timeout recheck but unresolved NACKs remain",
                            pipeline_id=pipeline_id,
                            nack_count=len(nack_details),
                            nack_summary=nack_summary,
                        )
                        # Tag with the consensus-timeout context so "Retry
                        # phase" dispatches through restart_phase on resolve
                        # (#3421), for symmetry with the incomplete-consensus
                        # sites above.  This question is hand-built and does not
                        # promise restart copy, but restart_phase is the correct
                        # "Retry phase" action regardless.  Like its siblings
                        # this pod-mode path is unreachable today: has_failures[0]
                        # is only set in _record_container_exit, called solely for
                        # active_executions / remaining members, which are always
                        # empty in orchestrator mode (spawn_all returns []
                        # post-#3164).  Tagging keeps the dispatch honest if pod
                        # mode is ever revived.
                        _pkg._persist_hitl_decision(
                            pipeline_id,
                            pipeline,
                            store,
                            question=(
                                f"Consensus reached after timeout but {len(nack_details)} NACK(s) "
                                f"remain unresolved: {nack_summary}. How to proceed?"
                            ),
                            options=["Retry phase", "Accept current state", "Abort phase"],
                            phase=pipeline.current_phase,
                            context=_pkg._CONSENSUS_TIMEOUT_HITL_CONTEXT,
                        )
                        combined_logs += (
                            f"\n--- UNRESOLVED NACKs ({len(nack_details)}) ---\n{nack_summary}"
                        )
                        return 1, combined_logs

                    # Consensus reached during the wait — recover pipeline
                    if store is not None:
                        try:
                            _current_pip = store.load_pipeline(pipeline_id)
                            if _current_pip.status == _pkg.PipelineStatus.FAILED:
                                _pkg.logger.warning(
                                    "Pipeline externally marked FAILED but consensus is complete — recovering (timeout path)",
                                    pipeline_id=pipeline_id,
                                )
                                with _pkg.get_pipeline_state_lock(pipeline_id):
                                    _current_pip = store.load_pipeline(pipeline_id)
                                    if _current_pip.status == _pkg.PipelineStatus.FAILED:
                                        _current_pip.status = _pkg.PipelineStatus.RUNNING
                                        _current_pip.error = None
                                        store.save_pipeline(_current_pip)
                        except Exception as recovery_err:
                            _pkg.logger.warning(
                                "External FAILED recovery check failed (timeout path)",
                                pipeline_id=pipeline_id,
                                error=str(recovery_err),
                            )

                    _elapsed_timeout = _pkg.time.monotonic() - start_time
                    if _pkg._emit_event is not None:
                        _pkg._emit_event(
                            _pkg.EventType.CONSENSUS_REACHED,
                            pipeline_id,
                            data={"elapsed_seconds": _elapsed_timeout},
                        )
                    _pkg.logger.info(
                        "Consensus reached on recheck after timeout, treating as success",
                        pipeline_id=pipeline_id,
                        elapsed_seconds=round(_elapsed_timeout, 1),
                        has_failures=has_failures[0],
                    )
                    _update_agents_complete()
                    _stop_running_containers()
                    return 0, combined_logs

                # Consensus not complete on recheck.  Mirror the non-failure
                # branch's NACK summary so operators see which reviewer edges
                # are still blocking, even when containers had non-zero exits.
                if _timeout_consensus.get("has_unresolved_nacks"):
                    nack_details = _timeout_consensus.get("unresolved_nacks", [])
                    nack_summary = _pkg._format_nack_summary(nack_details)
                    _pkg.logger.warning(
                        "Timeout with unresolved NACKs (has_failures path)",
                        pipeline_id=pipeline_id,
                        nack_count=len(nack_details),
                    )
                    combined_logs += (
                        f"\n--- UNRESOLVED NACKs ({len(nack_details)}) ---\n{nack_summary}"
                    )
                return 1, combined_logs

            # After timeout, check the BRC approval matrix for unresolved
            # NACKs before declaring success.  Producers that exited without
            # addressing reviewer feedback should not be treated as passing.
            try:
                _final_consensus = executor.check_consensus()
            except Exception:
                _pkg.logger.warning("Failed to check consensus at timeout", exc_info=True)
                _final_consensus = {}
            if _final_consensus.get("has_unresolved_nacks"):
                nack_details = _final_consensus.get("unresolved_nacks", [])
                nack_summary = _pkg._format_nack_summary(nack_details)
                _pkg.logger.warning(
                    "Timeout with unresolved NACKs — returning failure",
                    pipeline_id=pipeline_id,
                    nack_count=len(nack_details),
                )
                combined_logs += f"\n--- UNRESOLVED NACKs ({len(nack_details)}) ---\n{nack_summary}"
                return 1, combined_logs

            # Orchestrator-owned event loop: this timeout fallthrough is the
            # dominant non-convergence terminal.  spawn_all returns [] by
            # design, so step 5's "all containers exited" path is guarded off
            # (it requires a non-empty active set) and a slice that never
            # converged — producer never proposed, a reviewer pod failed to
            # ACK, reviews pending with no NACK — lands here with no NACKs.
            # Unlike pod mode, where a clean all-exited phase already routed
            # through step 5's is_complete check, nothing upstream has verified
            # consensus completeness on this path.  Mirror step 5: when the
            # orchestrator owns the loop and consensus is incomplete, escalate
            # an HITL and fail rather than reporting a non-converged slice as
            # success (a bare `return 0` here would advance the phase toward PR
            # creation past the BRC consensus gate).
            if executor.owns_event_loop() and not _final_consensus.get("is_complete"):
                question, log_suffix = _pkg._incomplete_consensus_decision_text(
                    _final_consensus, container_failure_count=0, orchestrator_mode=True
                )
                _pkg.logger.warning(
                    "Consensus timed out and is incomplete (orchestrator-owned loop) — escalating to HITL",
                    pipeline_id=pipeline_id,
                    blocking_agents=_final_consensus.get("blocking_agents", []),
                )
                _pkg._persist_hitl_decision(
                    pipeline_id,
                    pipeline,
                    store,
                    question=question,
                    options=["Retry phase", "Accept current state", "Abort phase"],
                    phase=pipeline.current_phase,
                    context=_pkg._CONSENSUS_TIMEOUT_HITL_CONTEXT,
                )
                combined_logs += log_suffix
                return 1, combined_logs

            return 0, combined_logs

        # 7. Sleep before next poll
        _pkg.time.sleep(poll_interval)
