"""pipeline per-phase driver loop helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import driver_heartbeat
import routes.pipelines as _pkg  # noqa: E402,F401


def _run_pipeline(
    pipeline_id: str,
    repo_path: _pkg.Path,
    _respawn_attempt: int = 0,
) -> None:
    """Run a pipeline by spawning containers for each phase.

    This runs in a background thread. For each phase it:
    1. Spawns agent containers via concurrent BRC execution
       (_run_concurrent_phase) for all phases.
    2. For reviewed phases (refine, implement, plan): reviewers participate
       in the BRC consensus protocol alongside workers, then the phase
       loops back with feedback if revision is needed.
    3. Advances to the next phase once approved.

    Args:
        pipeline_id: Pipeline ID
        repo_path: Path to repository
        _respawn_attempt: Internal — counts how many times this thread
            has been respawned by the spurious-PNFE recovery path.
            Bounded by ``_PNFE_RESPAWN_MAX_ATTEMPTS`` to prevent a
            persistent transient from cascading into an unbounded
            thread/overseer/commit storm.
    """
    from routes.phases import PHASE_TRANSITIONS

    # Track which run of the pipeline this thread owns.  If the pipeline
    # is deleted and recreated with the same ID while we're still running,
    # the new run creates its own worktrees under the same path.  Without
    # this guard, our finally block would delete the *new* run's worktrees.
    run_epoch: _pkg.datetime | None = None
    overseer_container_id: str | None = None
    phase_overseer_active: bool = False
    overseer_lock = _pkg.threading.Lock()
    health_monitor_instance = None
    health_monitor_timer: _pkg.threading.Event | None = None
    poll_thread: _pkg.threading.Thread | None = None

    try:
        store = _pkg.get_state_store(repo_path)
        spawner = _pkg._get_spawner()
        pipeline = store.load_pipeline(pipeline_id)
        run_epoch = pipeline.run_epoch or pipeline.created_at
        pipeline_mode = "issue" if pipeline.issue_number is not None else "prompt"
        transitions = PHASE_TRANSITIONS

        def _make_overseer_teardown_hook(
            *,
            reason: str,
            container_id: str | None,
            phase: _pkg.PipelinePhase,
        ) -> _pkg.Callable[[], None]:
            """Build a pre_event_hook that tears down the per-phase overseer.

            ``container_id`` and ``phase`` are snapshotted as function
            parameters (frozen per-call), so the returned closure binds
            the loop-iteration values that were current when the
            post-phase cleanup branch fired — late binding would race a
            subsequent loop iteration.  ``reason`` differs between the
            doubly-failed and hard-reset-recovered call sites and is
            forwarded to :func:`_teardown_phase_overseer`.

            #2797 follow-up: collapses the two duplicated closure
            definitions at the two post-phase hard-reset emission sites
            into one shared factory.  The closure remains inside
            ``_run_pipeline`` because the ``phase_overseer_active``
            bool is a local nonlocal of this function.
            """

            def _hook() -> None:
                nonlocal phase_overseer_active
                with overseer_lock:
                    if container_id and phase_overseer_active:
                        phase_overseer_active = False
                        _pkg._teardown_phase_overseer(
                            spawner,
                            container_id,
                            pipeline_id,
                            phase_label=str(phase),
                            reason=reason,
                        )

            return _hook

        # Map pipeline to gateway session mode.
        gateway_mode, detected_visibility = _pkg._compute_gateway_mode(pipeline)
        if not pipeline.network_mode and pipeline.repo:
            if detected_visibility is not None:
                _pkg.logger.info(
                    "Auto-detected network mode from repo visibility",
                    repo=pipeline.repo,
                    visibility=detected_visibility,
                    gateway_mode=gateway_mode,
                )
            else:
                _pkg.logger.warning(
                    "Could not detect repo visibility, defaulting to public mode",
                    repo=pipeline.repo,
                )

        # Parse host repo map for volume mounts.  When the orchestrator
        # runs inside Docker, EGG_REPO_PATH is the *container* path but
        # volume mounts need *host* paths (since the Docker socket
        # operates on the host daemon).  EGG_HOST_REPO_MAP provides a
        # JSON mapping of repo_name -> host_path, auto-generated from
        # repositories.yaml by the egg launcher.
        host_repo_map_raw = _pkg.os.environ.get("EGG_HOST_REPO_MAP", "{}")
        try:
            host_repo_map: dict[str, str] = _pkg.json.loads(host_repo_map_raw)
        except _pkg.json.JSONDecodeError as exc:
            _pkg.logger.error(
                "Failed to parse EGG_HOST_REPO_MAP — no repos will be mounted in sandbox containers",
                raw_value=host_repo_map_raw,
            )
            raise ValueError(
                f"EGG_HOST_REPO_MAP contains invalid JSON: {host_repo_map_raw!r}"
            ) from exc

        # Create a pipeline-level worktree via the gateway.  This worktree
        # is used by the orchestrator for reading/writing contracts, drafts,
        # and state files.  Individual agents get their own per-agent
        # worktrees at spawn time (created in container_spawner.py) so
        # concurrent agents cannot stomp on each other's uncommitted work.
        # See #1481 for the per-agent worktree isolation design.
        #
        # We use the pipeline_id as the worktree container_id for the
        # orchestrator-side worktree.  Agent worktrees use
        # "{pipeline_id}-{role}" as their container_id.
        worktree_id = pipeline_id
        repo_volumes: dict[str, str] = {}
        worktree_repo_path = repo_path  # default; overridden when worktrees exist
        host_uid = int(_pkg.os.environ.get("HOST_UID", 1000))
        host_gid = int(_pkg.os.environ.get("HOST_GID", 1000))
        pipeline_repos = [pipeline.repo] if pipeline.repo else []

        repo_volumes, worktree_repo_path = _pkg._map_host_repos(
            pipeline,
            host_gid=host_gid,
            host_repo_map=host_repo_map,
            host_uid=host_uid,
            pipeline_id=pipeline_id,
            pipeline_repos=pipeline_repos,
            spawner=spawner,
            worktree_id=worktree_id,
            repo_volumes=repo_volumes,
            worktree_repo_path=worktree_repo_path,
        )

        if not repo_volumes:
            raise RuntimeError(
                f"No repo volumes available for pipeline {pipeline_id} — "
                f"worktree creation is required"
            )

        # Sync worktree with remote before starting pipeline phases.  After an
        # orchestrator restart, the local worktree branch may be behind origin:
        # commits pushed by agents in previous phases (contracts, drafts,
        # statefiles) exist on the remote but not in the local checkout.
        # Fetching and resetting ensures downstream code (contract loading,
        # draft reading) sees the full pipeline state from prior phases.
        pipeline, _worktree_done = _pkg._resolve_worktree_repo(
            pipeline,
            gateway_mode=gateway_mode,
            pipeline_id=pipeline_id,
            repo_path=repo_path,
            spawner=spawner,
            store=store,
            worktree_repo_path=worktree_repo_path,
        )
        if _worktree_done:
            return

        # Resolve the certs named volume for gateway CA trust.
        # The docker-compose stack creates ${COMPOSE_PROJECT_NAME:-egg}-certs.
        certs_volume_raw = _pkg.os.environ.get(
            "EGG_CERTS_VOLUME",
            _pkg.os.environ.get("COMPOSE_PROJECT_NAME", "egg") + "-certs",
        )
        # Validate volume name: Docker allows [a-zA-Z0-9][a-zA-Z0-9_.-]*
        # We use a permissive check that rejects obvious shell metacharacters.
        if not _pkg.re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$", certs_volume_raw):
            _pkg.logger.warning(
                "Invalid certs volume name, using default",
                raw_name=certs_volume_raw,
            )
            certs_volume = "egg-certs"
        else:
            certs_volume = certs_volume_raw

        # Capture source_branch before _read_source_branch_artifacts clears
        # it on success — the contract-pull path below (#2035) runs inside
        # the contract_synced block and otherwise wouldn't see the value.
        source_branch_for_contract_pull = pipeline.source_branch

        # Read artifacts from source branch if specified and inline values
        # were not provided.  This populates pipeline.plan and
        # pipeline.analysis so the contract creation block below can use them.
        _pkg._sync_source_branch_drafts(
            gateway_mode=gateway_mode,
            pipeline=pipeline,
            pipeline_id=pipeline_id,
            spawner=spawner,
            store=store,
            worktree_repo_path=worktree_repo_path,
        )

        # Create companion contract in the worktree (deferred from pipeline
        # creation so it doesn't pollute the main repo working directory).
        pipeline, _contract_setup_done = _pkg._sync_contract_setup(
            pipeline,
            gateway_mode=gateway_mode,
            pipeline_id=pipeline_id,
            pipeline_mode=pipeline_mode,
            repo_path=repo_path,
            source_branch_for_contract_pull=source_branch_for_contract_pull,
            spawner=spawner,
            store=store,
            worktree_repo_path=worktree_repo_path,
        )
        if _contract_setup_done:
            return

        # Safety net: when start_phase=implement, the plan phase is
        # skipped so the plan-completion hook at the end of the phase loop
        # never fires.  The inline-plan path above calls
        # _populate_contract_from_plan inside the contract_synced block,
        # but that block is skipped on pipeline restarts (contract already
        # synced) and when _read_source_branch_artifacts writes the draft
        # file to the worktree without going through the inline-plan
        # branch.  This catch-all ensures the contract has phases and
        # tasks before agents spawn when the plan phase was skipped.
        # When start_phase=plan, the plan phase runs normally and the
        # plan-completion hook populates the contract, so no safety net
        # is needed.
        pipeline, _start_phase_done = _pkg._start_phase_setup(
            pipeline,
            pipeline_id=pipeline_id,
            pipeline_mode=pipeline_mode,
            store=store,
            worktree_repo_path=worktree_repo_path,
        )
        if _start_phase_done:
            return

        # Operator directives + prior iteration history are persisted on
        # ``PhaseExecution`` and accumulate across HITL kickbacks (#2795).
        # They are read directly off the phase below each loop iteration —
        # no separate "read once and clear" stash is needed.

        # Initialize the Tier 1 health monitor so deterministic tripwires
        # (heartbeat timeout, container exit, repeated errors, message rate,
        # progress stall) fire during pipeline execution.  The monitor
        # subscribes to EventBus events reactively, but check_heartbeats()
        # and check_progress() need periodic polling.
        try:
            from events import get_event_bus
            from health_monitor import init_health_monitor

            health_monitor_instance = init_health_monitor(
                get_event_bus(), pipeline_id, pipeline.config
            )
            # Sync the phase-aware threshold with the current pipeline phase
            health_monitor_instance.set_current_phase(pipeline.current_phase.value)

            # Wake stuck producers directly when check_brc_progress fires
            # so the deterministic detector actually drives remediation
            # instead of relying on the overseer agent's discretion (#2079).
            # The closure reads the monitor's current phase at fire time so
            # the message records the phase the producer is actually in.
            _on_health_escalation = _pkg.functools.partial(
                _pkg._on_health_escalation_impl,
                health_monitor_instance=health_monitor_instance,
                pipeline_id=pipeline_id,
            )

            health_monitor_instance.on_escalation(_on_health_escalation)

            # Start a background polling thread for time-based tripwires
            health_monitor_timer = _pkg.threading.Event()

            # SHAs we've already raised a branch-divergence alert for
            # (#2224 PR 3).  Per-pipeline dedupe so we fire once per
            # offending commit, not once per 30s tick.
            divergence_alerted_shas: set[str] = set()

            _health_monitor_poll = _pkg.functools.partial(
                _pkg._health_monitor_poll_impl,
                pipeline_id=pipeline_id,
                worktree_repo_path=worktree_repo_path,
                store=store,
                divergence_alerted_shas=divergence_alerted_shas,
            )

            poll_thread = _pkg.threading.Thread(
                target=_health_monitor_poll,
                args=(health_monitor_instance, health_monitor_timer),
                daemon=True,
                name=f"health-monitor-{pipeline_id[:8]}",
            )
            poll_thread.start()
            _pkg.logger.info(
                "Health monitor initialized",
                pipeline_id=pipeline_id,
            )
        except Exception as hm_err:
            # Non-fatal: pipeline can run without Tier 1 monitoring
            _pkg.logger.warning(
                "Failed to initialize health monitor (continuing without Tier 1 monitoring)",
                pipeline_id=pipeline_id,
                error=str(hm_err),
            )

        while True:
            driver_heartbeat.record_tick(pipeline_id)  # #3540 liveness tick
            try:
                pipeline = store.load_pipeline(pipeline_id)
            except Exception:
                # Pipeline was deleted — exit quietly
                _pkg.logger.info(
                    "Pipeline no longer exists, exiting thread",
                    pipeline_id=pipeline_id,
                )
                return

            # Detect recreation/restart: another run now owns this pipeline ID
            _current_epoch = pipeline.run_epoch or pipeline.created_at
            if _current_epoch != run_epoch:
                _pkg.logger.info(
                    "Pipeline was recreated, exiting old thread",
                    pipeline_id=pipeline_id,
                )
                return

            if pipeline.status in (_pkg.PipelineStatus.FAILED, _pkg.PipelineStatus.CANCELLED):
                _pkg.logger.info(
                    "Pipeline stopped", pipeline_id=pipeline_id, status=pipeline.status.value
                )
                break

            current_phase = pipeline.current_phase

            # Start the current phase
            phase_execution = pipeline.get_phase_execution(current_phase)
            pipeline, phase_execution = _pkg._run_pending_phase_init(
                pipeline,
                phase_execution,
                current_phase=current_phase,
                pipeline_id=pipeline_id,
                repo_path=repo_path,
                store=store,
                worktree_repo_path=worktree_repo_path,
            )

            # Spawn overseer container for this phase's health monitoring.
            # The overseer is phase-scoped: spawned at phase start and torn
            # down at phase completion/advance/failure.  Each phase gets a
            # fresh overseer instance with no accumulated state.
            #
            # #2270 slice-5: gate overseer presence on "agents actually
            # running". During a zero-agent HITL park the pipeline has no phase
            # agents in flight, so spawning an overseer there is pure churn
            # (§3). The respawn loop that used to keep it alive across such
            # parks was removed; this gate stops the phase-start spawn from
            # doing the same thing. The agent count is the deterministic phase
            # roster the concurrent executor itself consults — the cohort this
            # phase is about to run.
            _phase_agent_count = _pkg._count_phase_agents(pipeline, current_phase)
            if pipeline.config.overseer_enabled and _pkg._overseer_should_be_present(
                running_agent_count=_phase_agent_count,
                pipeline_status=pipeline.status,
            ):
                try:
                    overseer_result = _pkg._spawn_overseer_agent(
                        spawner=spawner,
                        pipeline_id=pipeline_id,
                        issue_number=pipeline.issue_number,
                        gateway_mode=gateway_mode,
                        pipeline_repos=pipeline_repos if pipeline_repos else None,
                        max_turns=pipeline.config.overseer_max_turns,
                        decision_model=pipeline.config.overseer_decision_maker_model,
                    )
                    with overseer_lock:
                        overseer_container_id = overseer_result.container_info.container_id
                        phase_overseer_active = True
                    _pkg.logger.info(
                        "Overseer container spawned for phase",
                        pipeline_id=pipeline_id,
                        phase=current_phase.value,
                        container_id=overseer_container_id[:12],
                    )
                except (_pkg.ContainerSpawnError, _pkg.KubernetesSpawnError) as e:
                    # Non-fatal: pipeline can run without overseer monitoring
                    _pkg.logger.warning(
                        "Failed to spawn overseer container (continuing without monitoring)",
                        pipeline_id=pipeline_id,
                        phase=current_phase.value,
                        error=str(e),
                    )

            # Common sandbox environment for all containers in this phase.
            # GATEWAY_URL, RUNTIME_UID/GID, proxy vars, DNS lockdown, and
            # extra_hosts are now handled by the shared build_sandbox_config()
            # inside spawn_agent_container().  Only pipeline-specific vars go here.
            if gateway_mode == "private":
                orchestrator_ip = _pkg.ORCHESTRATOR_ISOLATED_IP
            else:
                orchestrator_ip = _pkg.ORCHESTRATOR_EXTERNAL_IP
            orchestrator_url = f"http://{orchestrator_ip}:{_pkg.ORCHESTRATOR_PORT}"
            sandbox_env: dict[str, str] = {
                "EGG_PIPELINE_ID": pipeline_id,
                "EGG_PIPELINE_PHASE": current_phase.value,
                "EGG_PIPELINE_MODE": pipeline_mode,
                "EGG_ORCHESTRATOR_URL": orchestrator_url,
                "EGG_ORCHESTRATOR_MODE": "distributed",
            }
            # ``EGG_BRANCH`` is intentionally NOT set here. The spawner
            # is the single source of truth for the agent's assigned
            # branch (#2428): ``KubernetesSpawner.spawn_agent_job``
            # derives ``EGG_BRANCH`` from its ``branch`` parameter,
            # which the slice scheduler populates with the slice
            # integration branch via
            # ``ConcurrentPhaseExecutor.get_worktree_branch``. Stuffing
            # ``pipeline.branch`` into ``sandbox_env`` here used to be
            # threaded through ``extra_env``, where the spawner's
            # override loop runs after the default-from-``branch``
            # assignment — deterministic precedence, not a race — so
            # the pipeline-level value silently won and slice agents
            # were downgraded to the pipeline tip, breaking every
            # slice-coder push. The branch persistence below is the
            # only side-effect the run loop still needs.
            if not pipeline.branch:
                generated_branch = f"egg/{pipeline_id}/work"
                # Persist the generated branch so the PR phase can use it
                with _pkg.get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    if not pipeline.branch:
                        pipeline.branch = generated_branch
                        store.save_pipeline(pipeline)
                        _pkg.logger.info(
                            "Recorded generated branch on pipeline",
                            pipeline_id=pipeline_id,
                            branch=generated_branch,
                        )
            if pipeline.prompt:
                sandbox_env["EGG_PIPELINE_PROMPT"] = pipeline.prompt

            if pipeline.repo:
                repos = [pipeline.repo]
                sandbox_env["EGG_REPO"] = pipeline.repo
            else:
                repos = []

            # Jira ticket advisory env vars (issue #1556).  These give sandbox
            # agents a stable handle for the ticket the pipeline is working
            # against (``jira ticket get "$EGG_JIRA_TICKET"``) without
            # hard-coding the key.  They are ADVISORY — the gateway's project
            # allowlist is the only hard boundary, and we never export
            # Atlassian credentials (JIRA_BASE_URL / JIRA_USERNAME /
            # JIRA_API_TOKEN) to the sandbox.  An empty string is exported
            # when no ticket is configured so agent wrappers can rely on
            # variable presence.
            jira_ticket_value = getattr(pipeline, "jira_ticket", None) or ""
            sandbox_env["EGG_JIRA_TICKET"] = jira_ticket_value
            if jira_ticket_value and "-" in jira_ticket_value:
                sandbox_env["EGG_JIRA_PROJECT"] = jira_ticket_value.split("-", 1)[0]
            else:
                sandbox_env["EGG_JIRA_PROJECT"] = ""

            # Jira-epic SDLC support (issue #1557). Export ``EGG_IS_EPIC``
            # (bool-string) and ``EGG_EPIC_MODE`` (one of
            # 'epic-fresh', 'epic-reassess', 'ticket', 'github_issue')
            # so the refiner / task-planner / applier prompts can select
            # the right mode block. Mapping is derived via
            # ``prompt_loader.derive_pipeline_mode`` so the orchestrator
            # and any auxiliary callers agree on the canonical rule.
            #
            # Note: ``EGG_PIPELINE_MODE`` is already taken (PipelineMode:
            # 'issue' — set above at L19349).
            # ``EGG_EPIC_MODE`` is the orthogonal Jira-epic dimension.
            try:
                from prompt_loader import derive_pipeline_mode
            except ImportError:  # pragma: no cover - defensive
                derive_pipeline_mode = None  # type: ignore[assignment]
            _is_epic_flag = bool(getattr(pipeline, "is_epic", False))
            _pipeline_mode_attr = getattr(pipeline, "pipeline_mode", None)
            sandbox_env["EGG_IS_EPIC"] = "true" if _is_epic_flag else "false"
            if derive_pipeline_mode is not None:
                sandbox_env["EGG_EPIC_MODE"] = derive_pipeline_mode(
                    is_epic=_is_epic_flag,
                    pipeline_mode=_pipeline_mode_attr,
                    jira_ticket=jira_ticket_value or None,
                )
            else:
                sandbox_env["EGG_EPIC_MODE"] = "github_issue" if not jira_ticket_value else "ticket"

            # Issue #1557 reviewer_code v1 finding #4: run the reassess
            # sweep before the planner / applier spawn on reassess-mode
            # epic pipelines so the task-planner prompt's ``[mode: epic-
            # reassess]`` branch and the applier's in-flight refusal
            # have the children classification on disk.  The sweep
            # writes two JSON files under ``.egg-state/agent-outputs/``;
            # we export both paths into the sandbox env so the prompts
            # read them by env var rather than re-querying the gateway.
            # Fail-open: a sweep failure logs a warning but never aborts
            # the phase — the planner falls back to fresh-mode treatment
            # of the children (which is safe because every action carries
            # an explicit ``jira_action`` and the applier's in-flight
            # refusal hinges on the sweep file's presence).
            if (
                _is_epic_flag
                and _pipeline_mode_attr == "reassess"
                and current_phase.value in ("plan", "apply")
                and jira_ticket_value
            ):
                try:
                    from jira_reassess import (
                        run_reassess_sweep,
                        serialise_sweep_to_disk,
                    )
                except ImportError:  # pragma: no cover - defensive
                    run_reassess_sweep = None  # type: ignore[assignment]
                    serialise_sweep_to_disk = None  # type: ignore[assignment]
                if run_reassess_sweep is not None and serialise_sweep_to_disk is not None:
                    try:
                        sweep_result = run_reassess_sweep(
                            epic_key=jira_ticket_value,
                            state_store=store,
                        )
                        agent_outputs_dir = (
                            _pkg.Path(worktree_repo_path) / ".egg-state" / "agent-outputs"
                        )
                        sweep_path, done_path = serialise_sweep_to_disk(
                            result=sweep_result,
                            agent_outputs_dir=agent_outputs_dir,
                            pipeline_id=pipeline_id,
                        )
                        sandbox_env["EGG_REASSESS_SWEEP_PATH"] = str(sweep_path)
                        sandbox_env["EGG_DONE_CHILDREN_PATH"] = str(done_path)
                        _pkg.logger.info(
                            "Reassess sweep complete",
                            pipeline_id=pipeline_id,
                            epic_key=jira_ticket_value,
                            child_count=len(sweep_result.children),
                            done_count=len(sweep_result.done),
                            warnings=sweep_result.warnings,
                        )
                    except Exception as sweep_err:  # noqa: BLE001 — fail-open
                        _pkg.logger.warning(
                            "Reassess sweep failed (continuing without sweep handoff)",
                            pipeline_id=pipeline_id,
                            epic_key=jira_ticket_value,
                            error=str(sweep_err),
                        )

            phase_failed = False

            # --- Inner review cycle ---
            # NOTE: the legacy PR phase (and its auto-PR / slice-DAG-skip
            # branches) was deleted in #2777 (cq-4 / TASK-2-2). The context
            # PR now opens up-front via ``_open_context_pr_at_implement_start``
            # at the plan→implement boundary, slice PRs stack on it, and
            # IMPLEMENT is the terminal phase — no per-phase auto-PR creation
            # logic is reachable here for ``current_phase.value == "pr"``.
            pipeline, phase_execution, phase_failed, _phase_exec_action = _pkg._run_phase_execution(
                pipeline,
                phase_execution,
                phase_failed,
                certs_volume=certs_volume,
                current_phase=current_phase,
                gateway_mode=gateway_mode,
                pipeline_id=pipeline_id,
                pipeline_mode=pipeline_mode,
                repo_volumes=repo_volumes,
                repos=repos,
                run_epoch=run_epoch,
                sandbox_env=sandbox_env,
                spawner=spawner,
                store=store,
                worktree_repo_path=worktree_repo_path,
            )
            if _phase_exec_action == "return":
                return
            if _phase_exec_action == "break":
                break

            # If the phase failed, emit the failure event so the SSE stream
            # terminates, then break out of the outer loop.
            if phase_failed:
                # Stop the phase-scoped overseer on failure.
                # Hold the lock to prevent the poll thread from seeing the
                # container as EXITED and respawning it.
                with overseer_lock:
                    if overseer_container_id and phase_overseer_active:
                        phase_overseer_active = False
                        _pkg._teardown_phase_overseer(
                            spawner,
                            overseer_container_id,
                            pipeline_id,
                            phase_label=str(current_phase),
                            reason="phase failed",
                        )

                # report_pipeline_status is a stub (no-op) unless status_reporter
                # is installed.  The actual SSE emission is _emit_pipeline_event
                # below.  Kept for consistency with the except block at the
                # bottom of this function.
                _pkg.report_pipeline_status(
                    pipeline,
                    event_type="pipeline.failed",
                    message=f"Pipeline failed: {(pipeline.error or 'unknown')[:100]}",
                )
                _pkg._emit_pipeline_event(pipeline, "pipeline.failed")

                # Best-effort: push worktree branch to remote so work is backed up
                if pipeline.branch and worktree_repo_path != repo_path:
                    try:
                        spawner.gateway.push_worktree_branch(
                            pipeline_id=pipeline_id,
                            repo_path=str(worktree_repo_path),
                            branch=pipeline.branch,
                            mode=gateway_mode,
                            base_branch=pipeline.base_branch,
                        )
                    except Exception as push_err:
                        _pkg.logger.warning(
                            "Best-effort push on failure failed",
                            pipeline_id=pipeline_id,
                            error=str(push_err),
                        )

                break

            # Phase succeeded — mark complete and advance
            with _pkg.get_pipeline_state_lock(pipeline_id):
                pipeline = store.load_pipeline(pipeline_id)
                phase_execution = pipeline.get_phase_execution(current_phase)
                phase_execution.status = _pkg.PipelineStatus.COMPLETE
                phase_execution.completed_at = _pkg.datetime.now(_pkg.UTC)

                store.save_pipeline(pipeline)  # Persist phase completion before HITL gate

            # Report phase completion to collaborator
            _pkg.report_pipeline_status(
                pipeline,
                event_type="phase.completed",
                message=f"Phase {current_phase.value} completed",
            )
            _pkg._emit_pipeline_event(pipeline, "phase.completed")

            # Commit any uncommitted ``.egg-state/`` writes the agents
            # made during the phase BEFORE the worktree sync runs.
            # ``register_open_question`` / ``request_feedback`` mutate the
            # contract live in the shared pipeline worktree (see
            # ``orchestrator/contract_store.py``); those writes are
            # uncommitted on disk.  The ``git reset --hard`` step inside
            # ``_sync_worktree_with_remote`` discards them, leaving the
            # bridge below with an empty ``contract.decisions`` and
            # silently dropping the operator-bound questions (#2488).
            # Committing first lets the sync's rebase reconcile them
            # against agent-pushed drafts cleanly.
            try:
                _pkg._commit_statefiles_to_worktree(
                    worktree_repo_path,
                    f"Persist agent statefile writes before {current_phase.value} sync",
                    pipeline_identifier=_pkg._pipeline_identifier(
                        pipeline.issue_number, pipeline_id
                    ),
                    pipeline_id=pipeline_id,
                )
            except Exception as git_err:
                _pkg.logger.warning(
                    "Failed to commit pre-sync agent statefiles (continuing)",
                    pipeline_id=pipeline_id,
                    phase=current_phase.value,
                    error=str(git_err),
                )

            # Sync worktree with remote before post-phase modifications
            # so that agent-pushed commits (including plan drafts) are
            # incorporated.  This must run BEFORE _populate_contract_from_plan
            # and _sync_pipeline_decisions_to_contract so the autoresolve
            # rebase inside _sync_worktree_with_remote lands the remote
            # state before the populate step reads ``.egg-state/`` —
            # otherwise populate would read a stale local view and either
            # produce an empty contract or overwrite agent-pushed drafts
            # that only exist on origin.  (Before #2979 the helper also
            # issued ``git reset --hard`` on a doubly-failed divergence,
            # which would have reverted local on-disk modifications; that
            # destructive path is gone, so the modern rationale is purely
            # about the autoresolve rebase, not a hard reset.)
            post_phase_sync_outcome: _pkg.WorktreeSyncOutcome | None = None
            post_phase_sync_aborted = False
            if pipeline.branch and worktree_repo_path != repo_path:
                # Best-effort for transient failures: a sync failure must
                # not strand the auto-advance.  Without this guard, a
                # gateway HTTP error or git subprocess failure inside the
                # helper propagates to the outer Exception handler and (if
                # marking FAILED also fails) leaves the pipeline wedged with
                # phase COMPLETE but no successor (#2219).
                #
                # #2979: on an unreconciled divergence the helper pauses
                # (AWAITING_HUMAN) on a reconcile HITL and blocks until the
                # operator acks, then re-runs the sync — nothing is
                # discarded and the pipeline is NOT failed for a recoverable
                # post-consensus sync.  Only an operator abort (or an
                # exhausted reconcile budget) returns aborted=True.
                try:
                    post_phase_sync_outcome, post_phase_sync_aborted = (
                        _pkg._sync_worktree_reconciling_divergence(
                            spawner,
                            pipeline_id,
                            store,
                            repo_path,
                            worktree_repo_path=worktree_repo_path,
                            phase=current_phase,
                            gateway_mode=gateway_mode,
                            base_branch=pipeline.base_branch,
                            pipeline_branch=pipeline.branch,
                        )
                    )
                except Exception as sync_err:
                    _pkg.logger.warning(
                        "Failed to sync worktree with remote after phase (continuing)",
                        pipeline_id=pipeline_id,
                        phase=current_phase.value,
                        error=str(sync_err),
                    )

            # #2979: operator aborted the manual reconcile (or the pause
            # budget was exhausted).  Fail the pipeline; nothing was
            # discarded — the local commits remain pinned under the backup
            # ref for offline recovery.  ``pre_event_hook`` tears down the
            # per-phase overseer under its own lock before the public
            # ``pipeline.failed`` event, matching the prior ordering.
            if post_phase_sync_aborted and post_phase_sync_outcome is not None:
                _pkg._fail_pipeline_after_divergence_abort(
                    pipeline_id,
                    store,
                    phase=current_phase,
                    backup_ref=post_phase_sync_outcome.backup_ref,
                    local_only_commit_shas=post_phase_sync_outcome.local_only_commit_shas,
                    pre_event_hook=_make_overseer_teardown_hook(
                        reason="worktree divergence reconcile aborted",
                        container_id=overseer_container_id,
                        phase=current_phase,
                    ),
                )
                break

            # After plan phase: populate contract with task structure.
            # NOTE: worktree_repo_path is used for both draft reads and
            # contract load/save inside _populate_contract_from_plan.
            # The contract was created at worktree_repo_path above, so
            # both operations must use the same path.
            # Called on every successful plan completion (including after
            # HITL revision) so the contract reflects the latest approved
            # plan, not a previously rejected draft.
            #
            # Routed through _populate_contract_from_plan_safe so a raised
            # exception here cannot skip the HITL gate below (#1890).  The
            # same helper is invoked from advance_phase so force-advances
            # out of plan see the same populate step (#1941).
            #
            # ``source="plan_complete"`` makes the wrapper raise:
            #   * PlanDraftMissingOnLocalError — draft missing on local
            #     but present on origin (#2337 silent demotion).
            #   * PlanDraftMissingOnLocalAndOriginError — draft missing on BOTH local
            #     and origin (#2627 silent advance to empty contract).
            # We catch either below and mark the pipeline FAILED so the
            # operator can intervene rather than implement silently
            # shipping slice-1 alone (#2337) or strand 8 agents on an
            # empty contract (#2627).
            pipeline, phase_overseer_active, _loopblk_action = _pkg._run_plan_advance(
                pipeline,
                phase_overseer_active,
                current_phase=current_phase,
                gateway_mode=gateway_mode,
                overseer_container_id=overseer_container_id,
                overseer_lock=overseer_lock,
                pipeline_id=pipeline_id,
                pipeline_mode=pipeline_mode,
                repo_path=repo_path,
                spawner=spawner,
                store=store,
                worktree_repo_path=worktree_repo_path,
            )
            if _loopblk_action == "break":
                break

            # After refine and plan phases: sync substantive HITL decisions
            # (non-phase-gate) to the contract so implement-phase agents
            # can see what was decided.  Called for both refine and plan
            # phases — refine decisions inform the plan, plan decisions
            # inform the implementation.
            if current_phase.value in _pkg._HITL_GATE_PHASES:
                try:
                    _pkg._sync_pipeline_decisions_to_contract(
                        repo_path,
                        worktree_repo_path,
                        pipeline_id,
                    )
                except Exception as sync_err:
                    _pkg.logger.warning(
                        "Failed to sync pipeline decisions to contract (continuing)",
                        pipeline_id=pipeline_id,
                        phase=current_phase.value,
                        error=str(sync_err),
                    )

            # Write BRC consensus history for this phase before committing
            # statefiles so the history file is included in the commit.
            try:
                _pkg._write_brc_history(
                    worktree_repo_path,
                    pipeline_id,
                    current_phase.value,
                    _pkg._brc_history_identifier(pipeline),
                    # Per-slice implement-phase files are owned by each
                    # slice's integration branch; committing them onto
                    # ``work`` here would conflict with the slice
                    # branches' add of the same paths and break slice
                    # PR merges (#2755). The parameter is a no-op for
                    # non-implement phases.
                    write_per_slice=False,
                )
            except Exception as brc_err:
                _pkg.logger.debug(
                    "Failed to write BRC history (continuing)",
                    pipeline_id=pipeline_id,
                    phase=current_phase,
                    error=str(brc_err),
                )

            # Commit any .egg-state/ files produced during this phase
            # (drafts, reviews, check results, contract updates).  Mirrors
            # the GHA workflow's `git add .egg-state/` at phase boundaries.
            try:
                _pkg._commit_statefiles_to_worktree(
                    worktree_repo_path,
                    f"Persist statefiles after {current_phase.value} phase",
                    pipeline_identifier=_pkg._pipeline_identifier(
                        pipeline.issue_number, pipeline_id
                    ),
                    pipeline_id=pipeline_id,
                )
            except Exception as git_err:
                # Catch broadly: the helper does ``subprocess.run(check=True,
                # timeout=30)`` which can raise ``TimeoutExpired`` (not a
                # CalledProcessError) and ``glob.glob`` which can raise
                # ``OSError``.  A narrow ``except`` here let either escape
                # to the outer handler and stranded the pipeline (#2219).
                _pkg.logger.warning(
                    "Failed to commit statefiles after phase (continuing)",
                    pipeline_id=pipeline_id,
                    phase=current_phase,
                    error=str(git_err),
                )

            # Push statefiles to remote so the next phase's agents
            # don't have unpushed .egg-state/ files in their diff.
            if pipeline.branch and worktree_repo_path != repo_path:
                try:
                    spawner.gateway.push_worktree_branch(
                        pipeline_id=pipeline_id,
                        repo_path=str(worktree_repo_path),
                        branch=pipeline.branch,
                        mode=gateway_mode,
                        base_branch=pipeline.base_branch,
                    )
                except Exception as push_err:
                    _pkg.logger.warning(
                        "Failed to push statefiles after phase (continuing)",
                        pipeline_id=pipeline_id,
                        phase=current_phase,
                        error=str(push_err),
                    )

            # --- Unresolved-gap gate (#3300) ---
            # Block finalize while the contract carries an unresolved
            # tester→coder TaskGap. Runs after the worktree sync above so
            # the contract reflects the agents' final writes, and BEFORE
            # the phase_gate / advance / finalize below so the gap can't
            # ship into the committed contract (which would fail
            # test_models_gaps.py red in CI on the already-open PR —
            # #3298 class 4). Scoped to IMPLEMENT, where gaps are written;
            # no-ops on a clean contract. On a fully-autonomous pipeline
            # (hitl_gates=False) the gate surfaces the escalation but does
            # not block — both options need a human, so blocking would
            # stall the pipeline indefinitely; the reactive CI check stays
            # the backstop there.
            pipeline = _pkg._run_implement_advance(
                pipeline,
                current_phase=current_phase,
                gateway_mode=gateway_mode,
                pipeline_id=pipeline_id,
                repo_path=repo_path,
                spawner=spawner,
                store=store,
                worktree_repo_path=worktree_repo_path,
            )

            # --- HITL gate: pause for human approval ---
            # Refine/plan are gated by the converge-before-advance loop
            # (#3392): it resolves decisions with a human each round, which is
            # what lets us drop the force-advance backstop — a human is present
            # to resolve and approve.
            #
            # But a fully-autonomous pipeline (``hitl_gates is False``) has no
            # human to resolve or approve, and ``wait_for_decision`` polls
            # indefinitely — so unconditionally gating here would convert that
            # explicitly-chosen, first-class config into an indefinite hang
            # with no operator-facing signal that the flag was ignored. Mirror
            # the unresolved-gap gate's autonomous escape (#3300): when
            # ``hitl_gates is False`` we *surface* the gate (event + loud
            # warning) but do not block, advancing autonomously instead.
            # ``hitl_gates`` therefore still governs refine/plan, but only by
            # toggling between the human-gated converge loop and an autonomous
            # advance — never an indefinite stall.
            pipeline, _hitl_gate_action = _pkg._run_hitl_gate_converge(
                pipeline,
                current_phase=current_phase,
                gateway_mode=gateway_mode,
                pipeline_id=pipeline_id,
                repo_path=repo_path,
                spawner=spawner,
                store=store,
                worktree_repo_path=worktree_repo_path,
            )
            if _hitl_gate_action == "continue":
                continue

            # ----------------------------------------------------------
            # #2777 (cq-4, TASK-1-2) — inline ``_run_pipeline``
            # auto-advance plan→implement transition. Calls the new
            # idempotent ``_open_context_pr_at_implement_start``
            # opener directly; auto-advance does NOT route through
            # ``routes/phases.py:advance_phase``, so without this call
            # site a natural plan-exit (no operator REST call) would
            # never get a context PR opened, leaving the slice stack
            # stranded on ``egg/<id>/work`` (the #2593 / #2769
            # symptom). reviewer_code_holistic blocker 1 fix:
            # restored after v1's incorrect "single canonical site"
            # deletion. The opener's ``gh pr list`` pre-flight makes
            # a redundant call from any other transition path a one-
            # round-trip no-op.
            # ----------------------------------------------------------
            if current_phase.value == "plan":
                try:
                    _pkg._open_context_pr_at_implement_start(pipeline_id, repo_path=repo_path)
                except _pkg.ContextPrCreationError as ctx_err:
                    _pkg.logger.warning(
                        "Context PR opener: _run_pipeline auto-advance "
                        "failed (continuing — hard-require enforced at "
                        "advance_phase and the implement-start plan "
                        "pre-flight gate) (#2777, #3100)",
                        pipeline_id=pipeline_id,
                        reason=ctx_err.reason,
                        error=str(ctx_err),
                    )
                except Exception as autoadvance_err:  # noqa: BLE001
                    _pkg.logger.warning(
                        "Context PR opener: _run_pipeline auto-advance "
                        "outer wrapper raised (continuing) (#2777)",
                        pipeline_id=pipeline_id,
                        error=str(autoadvance_err),
                    )

            # Tear down the phase-scoped overseer before advancing.
            # Each phase gets a fresh overseer instance — no state carries
            # over between phases.
            # Hold the lock to prevent the poll thread from seeing the
            # container as EXITED and respawning it.
            with overseer_lock:
                if overseer_container_id and phase_overseer_active:
                    phase_overseer_active = False
                    _pkg._teardown_phase_overseer(
                        spawner,
                        overseer_container_id,
                        pipeline_id,
                        phase_label=current_phase.value,
                        reason="phase ended",
                    )

            # Determine next phase.  Issue #1557: epic-mode pipelines
            # route through the new APPLY phase between PLAN and
            # IMPLEMENT so the APPLIER role can drive Jira mutations on
            # HITL approval.  ``_next_phases_for_epic`` returns
            # ``transitions.get(current_phase, [])`` unchanged for
            # non-epic pipelines so the pre-#1557 scheduling is
            # preserved bit-for-bit.
            next_phases = _pkg._next_phases_for_epic(
                pipeline,
                current_phase,
                transitions.get(current_phase, []),
            )

            if not next_phases:
                # Terminal phase — pipeline complete
                with _pkg.get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    pipeline.status = _pkg.PipelineStatus.COMPLETE
                    store.save_pipeline(pipeline)

                # Report pipeline completion to collaborator
                _pkg.report_pipeline_status(
                    pipeline,
                    event_type="pipeline.completed",
                    message="Pipeline completed successfully",
                )
                _pkg._emit_pipeline_event(pipeline, "pipeline.completed")
                _pkg.logger.info(
                    "Pipeline complete",
                    pipeline_id=pipeline_id,
                )
                break

            # TEST_MARKER: auto_advance_block (load-bearing: brackets the
            # block for TestAutoAdvanceRespawnsThread; do not remove without
            # updating that test class).
            # Advance to next phase by respawning a fresh _run_pipeline
            # thread, mirroring advance_phase (#2165).  Bumping run_epoch
            # makes this thread's finally cleanup detect itself as superseded
            # and skip worktree teardown; the new thread drives the next
            # phase from clean local state.  Without this, any exception in
            # the new phase's first iteration takes the whole pipeline down.
            next_phase = next_phases[0]

            # Issue #1557: when the just-completed phase is PLAN and the
            # pipeline is_epic, we are advancing into APPLY.  Write the
            # applier handoff JSON now (before respawning the driver
            # thread) so the APPLIER container can read it on its
            # first wakeup.  ``approved_phase='plan'`` so the applier
            # drives plan-apply (Task.jira_action walk → child create /
            # edit / link, Won't-Do handoff for the orchestrator drain).
            if (
                getattr(pipeline, "is_epic", False)
                and current_phase == _pkg.PipelinePhase.PLAN
                and next_phase == _pkg.PipelinePhase.APPLY
            ):
                _pkg._write_apply_phase_handoff(
                    pipeline,
                    worktree_repo_path,
                    approved_phase="plan",
                )

            # Issue #1557 task-2-7: when the just-completed phase is
            # APPLY (BRC consensus confirmed), drain the Won't-Do
            # handoff JSON before advancing to IMPLEMENT.  The drain
            # runs out-of-band from the HITL approve POST so a slow
            # Jira API never extends that handler's latency.
            if current_phase == _pkg.PipelinePhase.APPLY:
                _pkg._drain_wontdo_batch_after_apply(pipeline, worktree_repo_path)
            with _pkg.get_pipeline_state_lock(pipeline_id):
                pipeline = store.load_pipeline(pipeline_id)
                pipeline.current_phase = next_phase
                pipeline.run_epoch = _pkg.datetime.now(_pkg.UTC)
                # ``updated_at`` is unconditionally set by ``StateStore.save_pipeline``.
                store.save_pipeline(pipeline)

            # #3521: advance contract.current_phase in lockstep with the
            # pipeline record. Historically this mutation was owned by
            # whichever agent called the gateway phase API after the
            # transition; when none did, the contract silently stayed on
            # the previous phase and the gateway commit gate (which keys
            # off the CONTRACT phase) wedged the next phase's consensus.
            # Best-effort + forward-only; never raises.
            _pkg._sync_contract_phase_to_pipeline(
                pipeline, worktree_repo_path, source="auto_advance"
            )

            # Drop the previous phase's in-memory consensus tracker and
            # message-store entries (#2502).  The other phase-transition
            # paths -- ``advance_phase`` REST handler, HITL-revision
            # re-run, and the ``recover_pipeline`` resume path -- all
            # call this; the auto-advance path used to skip it, leaving
            # a stale plan-phase tracker keyed under the bare
            # ``pipeline_id`` for ``_get_concurrent_status`` to find and
            # report as ``is_complete: True`` long after the implement
            # phase had started.  ``_write_brc_history`` runs at the
            # bottom of each phase iteration with
            # ``write_per_slice=False`` (see #2755), so per-slice
            # implement-phase transcripts are on the slice integration
            # branches, and the work commit picks up only the
            # unattributed sibling plus whatever aggregate the writer
            # still emits — refine/plan/pr aggregates, and the
            # non-slice-implement aggregate that any implement-phase
            # run without slice scope lands on work via the ``not
            # buckets`` branch — before we wipe the message store here.
            from routes.phases import _clear_concurrent_state

            _clear_concurrent_state(pipeline_id)

            _pkg.logger.info(
                "Phase advanced (auto), respawning driver thread",
                pipeline_id=pipeline_id,
                from_phase=current_phase.value,
                to_phase=next_phase.value,
            )

            _pkg._spawn_pipeline_run_thread(pipeline_id, repo_path, pipeline.run_epoch)
            return

    except _pkg.PipelineNotFoundError as pnf_err:
        # `PipelineNotFoundError` can be raised either because the pipeline
        # was actually deleted or because of a transient state-store read
        # (e.g., empty content while a concurrent commit on the state
        # worktree races with the read).  Re-verify before treating it as
        # deletion: if the pipeline is still on disk after retry, the
        # original exception was spurious — bump ``run_epoch`` so the
        # finally cleanup detects this thread as superseded and skips the
        # destructive worktree teardown, then relaunch ``_run_pipeline`` so
        # the next phase keeps making progress.  See #2155.
        pipeline_still_exists = False
        _verify_store = None
        try:
            _verify_store = _pkg.get_state_store(repo_path)
        except Exception as verify_store_err:
            # Couldn't even open the state store — treat as transient
            # (corrupt-but-present > deletion) so we skip the respawn
            # rather than amplifying an infrastructure blip.  Note: with
            # ``_verify_store=None`` the bump path below short-circuits,
            # so worktree preservation depends on whether ``run_epoch``
            # was set before the initial PNFE — this path avoids the
            # cascade but does not unconditionally preserve worktrees.
            _pkg.logger.warning(
                "Failed to obtain state store after PipelineNotFoundError; "
                "treating as transient infrastructure failure and skipping respawn",
                pipeline_id=pipeline_id,
                error=str(verify_store_err),
            )
            pipeline_still_exists = True

        if _verify_store is not None:
            for _attempt in range(_pkg._PNFE_VERIFY_ATTEMPTS):
                _pkg.time.sleep(_pkg._PNFE_VERIFY_INTERVAL)
                try:
                    _verify_store.load_pipeline(pipeline_id)
                    pipeline_still_exists = True
                    break
                except _pkg.PipelineNotFoundError:
                    continue
                except _pkg.StateValidationError:
                    # Corrupt JSON or schema mismatch means the file
                    # exists but is unreadable right now — that's not
                    # deletion.  Treat as transient: better to risk a
                    # wasted respawn than to nuke the worktrees on a
                    # transient corruption.
                    pipeline_still_exists = True
                    break
                except _pkg.StateStoreError as verify_err:
                    # Other state-store failures (transient git read
                    # errors, etc.) are also not evidence of deletion.
                    _pkg.logger.warning(
                        "State-store error verifying pipeline existence; "
                        "treating as transient and preserving worktrees",
                        pipeline_id=pipeline_id,
                        error=str(verify_err),
                    )
                    pipeline_still_exists = True
                    break

        if pipeline_still_exists:
            # Cap the respawn cascade so a persistent transient can't
            # leak threads, overseer containers, and state-branch
            # commits without bound.  The recovery code is what runs
            # exactly when the system is misbehaving — it must not
            # amplify the misbehaviour.
            if _respawn_attempt >= _pkg._PNFE_RESPAWN_MAX_ATTEMPTS:
                _pkg.logger.error(
                    "Spurious-PipelineNotFoundError recovery exhausted "
                    "respawn budget; marking pipeline FAILED so an "
                    "operator can investigate via restart_phase",
                    pipeline_id=pipeline_id,
                    attempts=_respawn_attempt,
                    exc_info=pnf_err,
                )
                if _verify_store is not None:
                    try:
                        with _pkg.get_pipeline_state_lock(pipeline_id):
                            _failed_pipeline = _verify_store.load_pipeline(pipeline_id)
                            _failed_pipeline.status = _pkg.PipelineStatus.FAILED
                            _failed_pipeline.error = (
                                "Transient PipelineNotFoundError recovery "
                                f"exhausted after {_respawn_attempt} respawns"
                            )
                            _verify_store.save_pipeline(_failed_pipeline)
                    except Exception as fail_err:
                        _pkg.logger.warning(
                            "Failed to mark pipeline FAILED after exhausting respawn budget",
                            pipeline_id=pipeline_id,
                            error=str(fail_err),
                        )
            else:
                # Recoverable transient — log at warning so it doesn't
                # trip error-rate dashboards every time it self-heals.
                _pkg.logger.warning(
                    "Spurious PipelineNotFoundError during execution — "
                    "pipeline still exists after retry; relaunching driver "
                    "thread and preserving worktrees",
                    pipeline_id=pipeline_id,
                    attempt=_respawn_attempt,
                    exc_info=pnf_err,
                )
                # Bump run_epoch so the finally cleanup observes this
                # thread as superseded (mirrors the advance_phase
                # pattern) and skips worktree teardown.  Capture the
                # pre-bump epoch into the local ``run_epoch`` so the
                # finally guard works even when the *initial* load
                # raised PNFE (in that case run_epoch was never set
                # at line 11393).
                bump_succeeded = False
                if _verify_store is not None:
                    try:
                        with _pkg.get_pipeline_state_lock(pipeline_id):
                            _bumped = _verify_store.load_pipeline(pipeline_id)
                            run_epoch = _bumped.run_epoch or _bumped.created_at
                            _bumped.run_epoch = _pkg.datetime.now(_pkg.UTC)
                            _verify_store.save_pipeline(_bumped)
                            bump_succeeded = True
                    except Exception as bump_err:
                        _pkg.logger.warning(
                            "Failed to bump run_epoch during spurious-PNFE "
                            "recovery; skipping respawn so the existing "
                            "finally cleanup runs without racing a new thread",
                            pipeline_id=pipeline_id,
                            error=str(bump_err),
                        )

                if bump_succeeded:
                    # Exponential backoff between respawn attempts so a
                    # tight cascade can't fire dozens of respawns per
                    # second.  attempt=0 → 1s, 1 → 2s, 2 → 4s, 3 → 8s,
                    # 4 → 16s, capped at _PNFE_RESPAWN_BACKOFF_CAP.
                    _backoff = min(2**_respawn_attempt, _pkg._PNFE_RESPAWN_BACKOFF_CAP)
                    _pkg.time.sleep(_backoff)
                    _pkg.threading.Thread(
                        target=_pkg._run_pipeline,
                        args=(pipeline_id, repo_path),
                        kwargs={"_respawn_attempt": _respawn_attempt + 1},
                        daemon=True,
                        name=(
                            f"pipeline-{pipeline_id}-respawn-"
                            f"{_respawn_attempt + 1}-{_pkg.time.monotonic_ns()}"
                        ),
                    ).start()
        else:
            _pkg.logger.info(
                "Pipeline was deleted during execution, exiting",
                pipeline_id=pipeline_id,
                exc_info=pnf_err,
            )
    except Exception as e:
        _pkg.logger.error(
            "Pipeline execution failed", pipeline_id=pipeline_id, error=str(e), exc_info=True
        )
        persisted_ok = False
        try:
            store = _pkg.get_state_store(repo_path)
            with _pkg.get_pipeline_state_lock(pipeline_id):
                pipeline = store.load_pipeline(pipeline_id)

                # Don't corrupt a recreated pipeline's state
                _fail_epoch = pipeline.run_epoch or pipeline.created_at
                if run_epoch and _fail_epoch != run_epoch:
                    _pkg.logger.info(
                        "Pipeline was recreated, not marking new run as failed",
                        pipeline_id=pipeline_id,
                    )
                else:
                    pipeline.status = _pkg.PipelineStatus.FAILED
                    pipeline.error = str(e)
                    store.save_pipeline(pipeline)
                    persisted_ok = True

                # Report pipeline failure to collaborator
                _pkg.report_pipeline_status(
                    pipeline,
                    event_type="pipeline.failed",
                    message=f"Pipeline failed: {str(e)[:100]}",
                )
                _pkg._emit_pipeline_event(pipeline, "pipeline.failed")
        except Exception as fail_err:
            # If FAILED-marking itself fails (state-store contention, lock
            # timeout, etc.), the pipeline stays at ``running`` with no
            # error recorded — exactly the silent-wedge symptom in #2219.
            # Log so the next occurrence is visible in the orchestrator
            # log instead of vanishing.
            _pkg.logger.error(
                "Failed to mark pipeline FAILED after exception",
                pipeline_id=pipeline_id,
                original_error=str(e),
                mark_error=str(fail_err),
                exc_info=True,
            )
            # Surface a synthetic ``pipeline.failed`` to the EventBus even
            # though the mark-FAILED block raised.  Without this, hosts
            # blocked on ``/status/wait`` (whose event allowlist requires
            # pipeline.failed/completed/cancelled) wait forever on a dead
            # runner — the zombie symptom in #2234.  ``persisted`` carries
            # whether ``save_pipeline`` actually flushed FAILED to disk
            # before the inner block raised: True means disk state matches
            # the event, False means consumers should treat the event as
            # the only authoritative source.
            if _pkg._emit_event is not None:
                try:
                    _pkg._emit_event(
                        _pkg.EventType.PIPELINE_FAILED,
                        pipeline_id,
                        data={
                            "status": _pkg.PipelineStatus.FAILED.value,
                            "persisted": persisted_ok,
                            "original_error": str(e),
                            "mark_error": str(fail_err),
                        },
                    )
                except Exception as emit_err:
                    _pkg.logger.warning(
                        "Failed to emit synthetic pipeline.failed event",
                        pipeline_id=pipeline_id,
                        error=str(emit_err),
                    )
    finally:
        # Stop health monitor polling and unsubscribe from events
        if health_monitor_timer is not None:
            health_monitor_timer.set()
        if poll_thread is not None:
            poll_thread.join(timeout=5)
        if health_monitor_instance is not None:
            try:
                health_monitor_instance.stop()
                _pkg.logger.info("Health monitor stopped", pipeline_id=pipeline_id)
            except Exception as hm_stop_err:
                _pkg.logger.debug(
                    "Failed to stop health monitor",
                    pipeline_id=pipeline_id,
                    error=str(hm_stop_err),
                )

        # Clean up progress store for this pipeline
        try:
            from progress_store import get_progress_store

            progress_store = get_progress_store()
            if progress_store is not None:
                progress_store.clear(pipeline_id)
        except Exception as ps_err:
            _pkg.logger.debug(
                "Failed to clear progress store",
                pipeline_id=pipeline_id,
                error=str(ps_err),
            )

        # Stop overseer container if it was spawned
        if overseer_container_id:
            try:
                _spawner = _pkg._get_spawner()
                _spawner.stop_agent_job(
                    overseer_container_id,
                    cleanup_session=True,
                    timeout=10,
                )
                _pkg.logger.info(
                    "Overseer container stopped",
                    pipeline_id=pipeline_id,
                    container_id=overseer_container_id[:12],
                )
            except Exception as overseer_err:
                _pkg.logger.debug(
                    "Failed to stop overseer container (may have already exited)",
                    pipeline_id=pipeline_id,
                    error=str(overseer_err),
                )

        # Clean up pipeline-level worktrees unless the pipeline has been
        # recreated (delete + create with the same ID).  In that case the
        # new run owns the worktrees and we must not remove them.
        try:
            _spawner = _pkg._get_spawner()
            _store = _pkg.get_state_store(repo_path)
            skip_cleanup = False
            pipeline_was_restarted = False
            try:
                current = _store.load_pipeline(pipeline_id)
                _cleanup_epoch = current.run_epoch or current.created_at
                if run_epoch and _cleanup_epoch != run_epoch:
                    skip_cleanup = True
                    pipeline_was_restarted = True
                    _pkg.logger.info(
                        "Pipeline was recreated/restarted, skipping worktree cleanup",
                        pipeline_id=pipeline_id,
                        old_epoch=run_epoch.isoformat(),
                        new_epoch=_cleanup_epoch.isoformat(),
                    )
                elif current.status == _pkg.PipelineStatus.FAILED:
                    skip_cleanup = True
                    _pkg.logger.info(
                        "Pipeline failed, preserving worktrees for retry",
                        pipeline_id=pipeline_id,
                    )
            except Exception:
                # Pipeline was deleted and not recreated — safe to clean up
                pass

            if not skip_cleanup:
                try:
                    _spawner.gateway.delete_worktrees(
                        container_id=pipeline_id,
                        force=True,
                    )
                    _pkg.logger.info("Pipeline worktrees cleaned up", pipeline_id=pipeline_id)
                except Exception as pipeline_wt_err:
                    _pkg.logger.warning(
                        "Failed to clean up pipeline worktrees",
                        pipeline_id=pipeline_id,
                        error=str(pipeline_wt_err),
                    )

                # Also clean up per-agent session worktrees.  Each agent
                # registers a gateway session under container_id
                # "egg-{pipeline_id}-{role}" and session_create creates a
                # worktree keyed to that name.  The per-agent cleanup path
                # calls delete_session_by_container with the Docker container
                # hash (not the session container_id), so those worktrees are
                # never removed via the normal per-container cleanup.  Sweep
                # them here as a safety net.  delete_worktrees is a no-op for
                # container IDs that have no worktree directory.
                #
                # NOTE: This uses the "egg-{pipeline_id}-{role}" naming for
                # session-created worktrees.  Per-agent worktrees from #1481
                # use "{pipeline_id}-{role}" (no "egg-" prefix) and are
                # cleaned up by cleanup_pipeline() which scans both container
                # labels and the filesystem.
                for role in _pkg.AgentRole:
                    agent_container_id = f"egg-{pipeline_id}-{role.value}"
                    try:
                        _spawner.gateway.delete_worktrees(
                            container_id=agent_container_id,
                            force=True,
                        )
                    except Exception as agent_wt_err:
                        _pkg.logger.warning(
                            "Failed to clean up agent worktrees",
                            pipeline_id=pipeline_id,
                            agent_container_id=agent_container_id,
                            error=str(agent_wt_err),
                        )

        except Exception as wt_err:
            _pkg.logger.warning(
                "Failed to clean up worktrees",
                pipeline_id=pipeline_id,
                error=str(wt_err),
            )

        # Safety-net: clean up any orphaned containers for this pipeline.
        # If the pipeline failed during startup or cleanup timed out, Docker
        # containers may persist.  This is a no-op when no containers exist.
        # Skip when the pipeline was restarted (run_epoch changed) so the
        # new thread's containers are not killed.  See #1386, #1638.
        if not pipeline_was_restarted:
            try:
                # ``gateway_mode`` is the mode this pipeline ran under;
                # the auto-salvage hook needs it to push recovery refs
                # under the same policy (#2429 review).
                removed = _spawner.cleanup_pipeline(
                    pipeline_id,
                    force=True,
                    preserve_worktrees=skip_cleanup,
                    salvage_mode=gateway_mode,
                    salvage_base_branch=pipeline.base_branch,
                )
                if removed > 0:
                    _pkg.logger.info(
                        "Safety-net cleanup removed orphaned containers",
                        pipeline_id=pipeline_id,
                        containers_removed=removed,
                    )
            except Exception as cleanup_err:
                _pkg.logger.warning(
                    "Safety-net container cleanup failed",
                    pipeline_id=pipeline_id,
                    error=str(cleanup_err),
                )
