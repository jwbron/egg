"""restart-route bodies helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _restart_agent_body(pipeline_id: str, agent_role: str) -> tuple[_pkg.Response, int]:
    """Restart a single agent in a pipeline (orchestrator-native).

    After #3164 the orchestrator unconditionally owns the BRC event
    loop: agent work runs as one-shot Jobs spawned per actionable
    event by the event loop, and the in-pod wait arm is gone. A
    resident pod spawned here without ``EGG_EVENT_ACTION`` would
    immediately log FATAL and ``exit 64``, so ``restart_agent`` no
    longer spawns anything itself. Instead it:

      0. Enforces the per-(pipeline, role, slice) restart budget
         (``check_and_increment_restart_count``); a request over budget is
         rejected with HTTP 429 before any state is mutated (#3244).
      1. Best-effort deletes the role's live one-shot Job(s) (to kill a
         stuck pod). One-shot Jobs carry an event-discriminator suffix
         in their name, so they are found by label
         (``LABEL_PIPELINE_ID`` + ``LABEL_AGENT_ROLE`` [+ ``LABEL_SLICE_ID``
         when slice-scoped]), not by name.
      2. Resets the role's consensus state and health-monitor anchor.
      3. Marks the agent record RUNNING with ``container_id = None``.

    For a pipeline that is already RUNNING, the live event loop (polling
    ~every 5s during the concurrent phase) spawns a fresh one-shot pod once
    the role's consensus state is reset — that is the respawn. For a pipeline
    that was FAILED/CANCELLED the event loop and its ``_run_pipeline`` driver
    thread are already dead, so the route also relaunches a fresh driver
    thread (mirroring ``restart_phase``) to restart the event loop; otherwise
    the reset would leave the pipeline RUNNING-but-idle with nothing to
    respawn it (#3244). The agent's per-agent worktree is preserved so
    committed work is retained.

    URL params:
        pipeline_id: Pipeline ID
        agent_role: Agent role to restart (e.g. "coder", "tester")

    Query string (optional):
        slice_id: Slice scope (``slice-<N>``). When supplied, the
            slice-scoped Job and worktree are restarted, ``EGG_SLICE_ID``
            is propagated to the new Job, and consensus reset targets
            the per-slice tracker. ``slice_id`` may also be supplied via
            the JSON body. When omitted for a role that runs as a
            per-slice agent, it is derived from the phase's agent records
            (#2759): if exactly one slice has a non-complete record for
            the role, that slice is used; otherwise the request is
            rejected with the candidate list rather than spawning an
            unscoped agent. The scan is scoped to ``pipeline.current_phase``
            only — if the pipeline has advanced past the slice's phase
            (e.g. to ``pr`` or a later iteration) no current-phase records
            will name the role, derivation falls through, and the operator
            should supply ``slice_id`` explicitly. This is operator guidance,
            not a code-enforced precondition: the fall-through branch
            proceeds to a pipeline-level spawn rather than rejecting.
            Genuinely pipeline-level agents (no per-slice records for the
            role) omit ``slice_id``.

    Request body (optional):
        {
            "reason": "Human-readable reason for the restart",
            "slice_id": "slice-2"
        }

    Response:
        {
            "success": true,
            "data": {
                "agent_role": "coder",
                "slice_id": "slice-2",
                "respawn": "delegated to orchestrator event loop",
                "restart_count": 1
            }
        }
    """
    repo_path = _pkg.get_repo_path()

    try:
        store, pipeline = _pkg._resolve_pipeline(pipeline_id, repo_path)
    except _pkg.InvalidPipelineIdError:
        return _pkg.make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}", status_code=400
        )
    except _pkg.PipelineNotFoundError:
        return _pkg.make_error_response(f"Pipeline {pipeline_id} not found", status_code=404)

    # Validate agent role
    try:
        role = _pkg.AgentRole(agent_role)
    except ValueError:
        return _pkg.make_error_response(f"Invalid agent role: {agent_role}", status_code=400)

    # Validate pipeline is in a restartable state.  CANCELLED is included so
    # that a cancel_task(cleanup=false) pipeline can be resumed without a
    # full resubmission (see #1725).
    if pipeline.status not in (
        _pkg.PipelineStatus.RUNNING,
        _pkg.PipelineStatus.AWAITING_HUMAN,
        _pkg.PipelineStatus.FAILED,
        _pkg.PipelineStatus.CANCELLED,
    ):
        return _pkg.make_error_response(
            f"Pipeline {pipeline_id} is not in a restartable state (status: {pipeline.status.value})",
            status_code=409,
        )

    body = _pkg.request.get_json(silent=True) or {}
    reason = body.get("reason", "Manual restart via API")

    # Slice scope (#2410): query param wins over body so the URL
    # form is unambiguous; both forms validate against the canonical
    # ``slice-<N>`` shape via ``extract_slice_id``.
    raw_slice_id = _pkg.request.args.get("slice_id")
    slice_payload = {"slice_id": raw_slice_id} if raw_slice_id is not None else body
    try:
        slice_id = _pkg.extract_slice_id(slice_payload)
    except ValueError as e:
        return _pkg.make_error_response(str(e), status_code=400)

    # Slice auto-derivation (#2759). A slice-mode restart that omits
    # ``slice_id`` would otherwise spawn the agent pipeline-level:
    # ``EGG_SLICE_ID`` is set by the spawner only when ``slice_id`` is
    # non-None, so the respawned agent's BRC signals route to the bare
    # pipeline tracker instead of the slice's tracker. The slice's own
    # tracker keeps the dead agent registered while the live one ACKs
    # into the wrong tracker — the slice's consensus then wedges with no
    # message-bus recovery path. Since ``restart_agent`` is the
    # operator's normal tool for recovering a failed container, the
    # omission must not silently produce an unscoped agent.
    #
    # When the role runs as a per-slice agent (it has slice-scoped
    # records in the current phase), derive the slice: the k8s monitor
    # marks a cleanly-exited agent COMPLETE and a crashed one FAILED, so
    # a single non-COMPLETE record isolates the slice that needs the
    # restart. If the choice is ambiguous — multiple non-COMPLETE
    # records, or none at all — reject with the candidate list so the
    # operator re-issues with an explicit ``slice_id``.
    if slice_id is None:
        derive_phase_exec = pipeline.phases.get(pipeline.current_phase.value)
        if derive_phase_exec is not None:
            role_records = [
                a
                for a in derive_phase_exec.agents
                if hasattr(a, "role")
                and (a.role == role or (hasattr(a.role, "value") and a.role.value == role.value))
            ]
            sliced_records = [a for a in role_records if getattr(a, "slice_id", None)]
            if sliced_records:
                known_slices = sorted({a.slice_id for a in sliced_records})
                restart_candidates = sorted(
                    {
                        a.slice_id
                        for a in sliced_records
                        if a.status != _pkg.AgentExecutionStatus.COMPLETE
                    }
                )
                if len(restart_candidates) == 1:
                    slice_id = restart_candidates[0]
                    _pkg.logger.info(
                        "restart_agent: derived slice_id from phase agent records",
                        pipeline_id=pipeline_id,
                        agent_role=agent_role,
                        slice_id=slice_id,
                    )
                else:
                    detail = (
                        "no slice has a non-complete agent record for this role"
                        if not restart_candidates
                        else f"{len(restart_candidates)} slices have a non-complete record"
                    )
                    return _pkg.make_error_response(
                        f"Agent role {agent_role!r} runs as a per-slice agent in "
                        f"pipeline {pipeline_id}; restart_agent could not derive "
                        f"slice_id ({detail}). Re-issue with an explicit slice_id.",
                        status_code=400,
                        details={
                            "agent_role": agent_role,
                            "known_slices": known_slices,
                            "restart_candidates": restart_candidates,
                        },
                        reason="slice_id_required",
                    )

    # Slice-existence check (#2421): a well-formed but unknown
    # ``slice_id`` would otherwise spawn an orphan Job + worktree
    # the rest of the system has no record of. The shape regex in
    # ``extract_slice_id`` only catches malformed values; only the
    # contract knows which slices the pipeline actually has.
    #
    # Pipelines without a contract are not
    # slice-aware, so any non-``None`` ``slice_id`` targeting them is
    # by definition unknown — reject outright. For contracted
    # pipelines, load the contract and check membership; fall through
    # silently if the contract can't be loaded (worktree pruned,
    # contract not yet populated, filesystem error) so we don't
    # regress legitimate restarts on the existing pipeline-level path.
    #
    # After #3164 ``restart_agent`` no longer spawns a worktree itself,
    # so the slice's parent-edge / base-branch resolution that used to
    # feed the spawn is gone. Only the existence check below remains.
    if slice_id is not None:
        if not pipeline.has_contract:
            return _pkg.make_error_response(
                f"slice_id {slice_id!r} is invalid for pipeline "
                f"{pipeline_id} (pipeline has no contract; not slice-aware)",
                status_code=404,
                details={
                    "slice_id": slice_id,
                    "known_slices": [],
                },
            )
        try:
            from egg_contracts.loader import (
                ContractNotFoundError,
                ContractValidationError,
                load_contract,
            )
            from routes import resolve_worktree_path
        except ImportError:
            _pkg.logger.warning(
                "Required modules unavailable; skipping slice_id existence check",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
            )
        else:
            contract = None
            try:
                worktree_path = resolve_worktree_path(pipeline_id, _pkg.Path(repo_path))
                contract_id = _pkg._pipeline_identifier(pipeline.issue_number, pipeline_id)
                try:
                    contract = load_contract(contract_id, worktree_path)
                except ContractNotFoundError:
                    # Contract not yet populated — fall through silently
                    # (``contract`` already initialised to ``None`` above).
                    pass
            except (OSError, ValueError, ContractValidationError) as exc:
                # Worktree pruned, filesystem failure, or corrupt/invalid
                # contract JSON: log and fall through. The reviewer's #2421
                # ask was to catch the easy "wrong slice_id" case, not to
                # gate restarts on contract reachability. Programmer errors
                # (AttributeError, TypeError, NameError) are left to
                # propagate so they surface during development.
                _pkg.logger.warning(
                    "Could not load contract for slice_id existence check; allowing restart",
                    pipeline_id=pipeline_id,
                    slice_id=slice_id,
                    error=str(exc),
                )
            if contract is not None:
                slice_obj = next((s for s in contract.slices if s.id == slice_id), None)
                if slice_obj is None:
                    return _pkg.make_error_response(
                        f"slice_id {slice_id!r} does not match any slice in "
                        f"pipeline {pipeline_id}'s contract",
                        status_code=404,
                        details={
                            "slice_id": slice_id,
                            "known_slices": sorted(s.id for s in contract.slices),
                        },
                    )

    spawner = _pkg._get_spawner()

    current_phase = pipeline.current_phase.value
    phase_exec = pipeline.phases.get(current_phase)

    # Enforce the per-(pipeline, role, slice) restart budget BEFORE any
    # destructive action (#3244 review). Pre-#3164 this cap lived inside
    # ``restart_agent_job``, which the route no longer calls — without
    # re-enforcing it here an operator/overseer could call ``restart_agent``
    # without bound, each call resetting consensus and actively preventing a
    # live phase from converging. ``check_and_increment_restart_count`` raises
    # when the budget is exhausted; reject loudly (429) instead of flipping
    # status / resetting consensus and returning a misleading success. The
    # returned count is the source of truth for the ``restart_count``
    # telemetry below (the old read-only ``get_restart_count`` read always
    # reported 0 on this path since nothing incremented it).
    try:
        new_restart_count = spawner.check_and_increment_restart_count(
            pipeline_id, role, slice_id=slice_id
        )
    except _pkg.KubernetesSpawnError as budget_err:
        _pkg.logger.warning(
            "restart_agent rejected: restart budget exhausted",
            pipeline_id=pipeline_id,
            agent_role=agent_role,
            slice_id=slice_id,
            error=str(budget_err),
        )
        return _pkg.make_error_response(str(budget_err), status_code=429)

    # Early status update: transition FAILED/CANCELLED -> RUNNING so that
    # get_status returns "running" immediately. Unlike a RUNNING pipeline —
    # whose live event loop picks up the consensus reset below and respawns
    # within one poll — a FAILED/CANCELLED pipeline has NO live event loop:
    # ``_run_concurrent_phase`` already returned and ``stop_event_loop()``
    # tore the loop down on its way out, and the ``_run_pipeline`` driver
    # thread has exited. Resetting consensus alone would leave the pipeline
    # RUNNING-but-idle with nothing to respawn it (#3244 review). So when we
    # make this transition we record it and relaunch a fresh ``_run_pipeline``
    # driver thread at the end of the route (mirroring ``restart_phase`` step
    # 7) — that restarts the event loop, which then performs the respawn.
    pipeline_was_inactive = pipeline.status in (
        _pkg.PipelineStatus.FAILED,
        _pkg.PipelineStatus.CANCELLED,
    )
    if pipeline_was_inactive:
        early_lock = _pkg.get_pipeline_state_lock(pipeline_id)
        with early_lock:
            pipeline = store.load_pipeline(pipeline_id)
            if pipeline.status in (_pkg.PipelineStatus.FAILED, _pkg.PipelineStatus.CANCELLED):
                pipeline.status = _pkg.PipelineStatus.RUNNING
                _phase_exec = pipeline.phases.get(current_phase)
                if _phase_exec is not None:
                    _phase_exec.status = _pkg.PipelineStatus.RUNNING
                # Bump run_epoch so the relaunched driver thread (below) owns a
                # fresh epoch namespace and any stale thread that observes the
                # transition detects itself as superseded (mirrors
                # ``restart_phase`` / ``advance_phase``).
                pipeline.run_epoch = _pkg.datetime.now(_pkg.UTC)
                pipeline.updated_at = _pkg.datetime.now(_pkg.UTC)
                store.update_pipeline(pipeline_id, pipeline.model_dump(mode="json"))
            else:
                # Lost the race — another writer already moved it off
                # FAILED/CANCELLED, so its driver thread / event loop is
                # live and will own the respawn. Don't relaunch a duplicate.
                pipeline_was_inactive = False

    # #3164: ``restart_agent`` no longer spawns a resident pod. The
    # orchestrator event loop owns the BRC respawn — once the role's
    # consensus state is reset (below), it spawns a fresh one-shot pod
    # within one ~5s poll. Here we only (1) kill any live one-shot Job
    # for the role so a stuck pod is torn down, then (2) reset consensus
    # + health so the event loop reschedules.

    # Delete the role's live one-shot Job(s), best-effort. One-shot
    # event Jobs carry an event-discriminator SUFFIX in their name (one
    # Job per actionable BRC event), so they can't be addressed by a
    # deterministic name — find them by LABEL. Match on pipeline +
    # role (and slice when scoped). Zero matches is fine (the role may
    # have already exited cleanly); the event loop will respawn either
    # way once consensus is reset. Wrap broadly so a k8s/list failure
    # never fails the restart.
    job_labels = {
        _pkg.LABEL_PIPELINE_ID: pipeline_id,
        # The role label value is the underscore form (e.g.
        # ``reviewer_code``), which is exactly ``agent_role`` / ``role.value``.
        _pkg.LABEL_AGENT_ROLE: role.value,
    }
    if slice_id is not None:
        job_labels[_pkg.LABEL_SLICE_ID] = slice_id
    try:
        live_jobs = spawner.k8s.list_containers(labels=job_labels)
        removed_jobs = 0
        for job in live_jobs:
            try:
                # Mirror the cleanup call sites: prefer the explicit
                # ``job_name`` (already Job-prefixed), fall back to the
                # container id which ``remove_agent_job`` -> ``remove_container``
                # resolves to a Job name.
                spawner.remove_agent_job(job.job_name or job.container_id, force=True)
                removed_jobs += 1
            except Exception as job_err:  # noqa: BLE001 - best-effort teardown
                _pkg.logger.warning(
                    "Failed to delete live one-shot Job during restart (best-effort)",
                    pipeline_id=pipeline_id,
                    agent_role=agent_role,
                    slice_id=slice_id,
                    job_name=getattr(job, "job_name", None),
                    error=str(job_err),
                )
        _pkg.logger.info(
            "restart_agent: deleted live one-shot Job(s) for role",
            pipeline_id=pipeline_id,
            agent_role=agent_role,
            slice_id=slice_id,
            removed=removed_jobs,
        )
    except Exception as list_err:  # noqa: BLE001 - best-effort teardown
        _pkg.logger.warning(
            "Failed to list live one-shot Jobs during restart (best-effort)",
            pipeline_id=pipeline_id,
            agent_role=agent_role,
            slice_id=slice_id,
            error=str(list_err),
        )

    # Reset consensus state for this agent so the event loop reschedules
    # a fresh one-shot pod for it. If consensus reset fails, log a
    # warning but don't fail the restart: the agent will re-enter
    # consensus on its own. Slice-scoped restarts (#2410) target the
    # per-slice tracker; the pipeline-level tracker has no record of the
    # slice agent.
    # Slice-scoped restarts (#2410) target the per-slice tracker; the
    # pipeline-level tracker has no record of the slice agent.
    #
    # INVARIANT (#3200 task-7-1, mid-phase BRC record survival): this reset
    # clears the *peer consensus tracker* (the ephemeral ACK/NACK/proposal
    # bookkeeping the restarted agent rebuilds by re-proposing) but MUST NOT
    # clear the *Redis message store* (``pipeline:{id}:messages``). That store
    # is the durable BRC message record — CONSENSUS_PROPOSE/ACK/NACK and the
    # conditional-ACK obligations — and a mid-phase restart deliberately
    # preserves it so the reseeded/resumed session can re-pull it via
    # ``GET /<pipeline_id>/brc-transcript`` + ``read_peer_artifact`` and
    # re-derive the #3189 deterministic anchors. The store is cleared only at
    # phase transitions (``_clear_concurrent_state``) and pipeline
    # create/delete (``_clear_pipeline_runtime_state``), never here. Do NOT
    # add ``get_message_store().clear()`` / ``_clear_concurrent_state`` to the
    # restart path — that would lose the record across the restart boundary.
    try:
        try:
            from peer_consensus import get_peer_consensus_tracker
        except ImportError:
            from ..peer_consensus import (
                get_peer_consensus_tracker,  # type: ignore[import-not-found]
            )

        tracker = get_peer_consensus_tracker(pipeline_id, slice_id)
        if tracker:
            tracker.remove_agent(agent_role)
            _pkg.logger.info(
                "Reset consensus state for agent",
                pipeline_id=pipeline_id,
                agent_role=agent_role,
                slice_id=slice_id,
            )
    except ImportError:
        pass
    except Exception as e:
        _pkg.logger.warning(
            "Failed to reset consensus state (agent will re-enter consensus)",
            pipeline_id=pipeline_id,
            agent_role=agent_role,
            slice_id=slice_id,
            error=str(e),
        )

    # Reset health-monitor anchor so the pre-respawn _last_heartbeat does not
    # generate a stale-elapsed heartbeat_timeout alert against the fresh
    # container (issue #2084).
    #
    # #2270 slice-5 (restart hygiene): ``reset_agent`` also drops the agent's
    # accumulated per-agent escalation state (escalation flags, error counts,
    # active alerts). Clearing it on restart is what stops a freshly-restarted
    # agent from inheriting a stale redirect/escalation history that would push
    # it straight to HITL on its first post-restart stall. The Tier-2 overseer's
    # own escalation-history clear + generation reset live on
    # ``OverseerMonitor`` (overseer/monitor.py:reset_escalation_history /
    # reset_generation), which the on-demand adjudicator constructs fresh.
    try:
        try:
            from health_monitor import get_health_monitor
        except ImportError:
            from ..health_monitor import (
                get_health_monitor,  # type: ignore[import-not-found]
            )
        _hm = get_health_monitor()
        if _hm is not None:
            _hm.reset_agent(agent_role)
    except Exception as e:
        _pkg.logger.warning(
            "Failed to reset health-monitor state for restarted agent",
            pipeline_id=pipeline_id,
            agent_role=agent_role,
            error=str(e),
        )

    # Update pipeline state. No resident container is spawned (#3164) —
    # the event loop will respawn a one-shot pod within one poll once the
    # consensus reset above takes effect. We mark the agent RUNNING with
    # ``container_id = None`` (the live pod is set by the event loop) and
    # refresh ``started_at`` so the overseer's
    # phase_minimum_working_window suppression on the
    # ``agent-heartbeat-stall`` trigger anchors on the restart (#2084).
    lock = _pkg.get_pipeline_state_lock(pipeline_id)
    with lock:
        pipeline = store.load_pipeline(pipeline_id)
        if phase_exec is not None:
            # Re-fetch from the freshly loaded pipeline (the outer check gates
            # on "did the phase exist before the restart?").
            fresh_phase_exec = pipeline.phases.get(current_phase)
            if fresh_phase_exec is not None:
                from models import AgentExecution  # type: ignore

                respawn_started_at = _pkg.datetime.now(_pkg.UTC)
                # Match on ``(role, slice_id)`` — without the slice tiebreaker
                # the first matching role wins, which on a multi-slice phase
                # mutates the wrong slice's record (#2422). ``slice_id`` is
                # the route-level scope already plumbed into the consensus
                # tracker above.
                found = False
                for agent in fresh_phase_exec.agents:
                    if not hasattr(agent, "role"):
                        continue
                    role_match = agent.role == role or (
                        hasattr(agent.role, "value") and agent.role.value == role.value
                    )
                    if not role_match:
                        continue
                    if getattr(agent, "slice_id", None) != slice_id:
                        continue
                    agent.container_id = None
                    agent.status = _pkg.AgentExecutionStatus.RUNNING
                    agent.started_at = respawn_started_at
                    found = True
                    break
                if not found:
                    fresh_phase_exec.agents.append(
                        AgentExecution(
                            role=role,
                            container_id=None,
                            status=_pkg.AgentExecutionStatus.RUNNING,
                            started_at=respawn_started_at,
                            slice_id=slice_id,
                        )
                    )

        pipeline.updated_at = _pkg.datetime.now(_pkg.UTC)
        store.update_pipeline(pipeline_id, pipeline.model_dump(mode="json"))

    # ``restart_count`` is the value just incremented by
    # ``check_and_increment_restart_count`` above (#3244). It is scoped to the
    # same ``(pipeline_id, agent_role, slice_id)`` bucket the cap is enforced
    # on, so it correctly reports the operator's "you've burned N of M
    # restarts" telemetry — the pre-fix read-only ``get_restart_count`` read
    # always reported 0 here because nothing on this path incremented it.
    response_data: dict[str, object] = {
        "agent_role": agent_role,
        "slice_id": slice_id,
        "respawn": "delegated to orchestrator event loop",
        "restart_count": new_restart_count,
    }

    # When the pipeline was FAILED/CANCELLED its event loop and driver thread
    # are dead (see the early-status comment above), so the consensus reset
    # alone has nothing to act on it. Relaunch a fresh ``_run_pipeline`` driver
    # thread — exactly as ``restart_phase`` step 7 does — to restart the event
    # loop, which then respawns the role's one-shot Job within one poll. For a
    # pipeline that was already RUNNING we skip this: its live event loop owns
    # the respawn and a second driver thread would race it (#3244 review).
    if pipeline_was_inactive:
        _pkg._spawn_pipeline_run_thread(pipeline_id, store.repo_path, pipeline.run_epoch)
        _pkg.logger.info(
            "restart_agent: relaunched driver thread for inactive pipeline",
            pipeline_id=pipeline_id,
            agent_role=agent_role,
            slice_id=slice_id,
            run_epoch=pipeline.run_epoch.isoformat() if pipeline.run_epoch else None,
        )

    _pkg.logger.info(
        "Agent restart requested (respawn delegated to event loop)",
        pipeline_id=pipeline_id,
        agent_role=agent_role,
        slice_id=slice_id,
        restart_count=response_data.get("restart_count"),
        reason=reason,
    )

    return _pkg.make_success_response(
        f"Agent {agent_role} restarted",
        data=response_data,
    )


def _restart_phase_body(pipeline_id: str, phase: str) -> tuple[_pkg.Response, int]:
    """Restart all agents in a pipeline phase.

    Stops and removes all containers for the phase, resets consensus and
    review cycle state, and respawns all agents.  Prior phase artifacts
    (from earlier phases) are preserved.

    Preservation semantics (#3080): per-agent worktrees AND their local
    branches are deleted, so per-role branch tips do not survive a phase
    restart.  Unpushed commits are salvaged to ``egg/recovered/*`` refs
    on a best-effort basis (#2429) — ``auto_salvage_pipeline``
    re-enumerates worktrees with ``validate_git=True``, so worktrees
    with a corrupted ``.git`` marker (the #1723 failure class) may be
    skipped without salvage.  The respawned agents' fresh worktrees
    re-fork from the shared work branch tip (``origin/<assigned_branch>``,
    base-branch fallback when unpushed — see #3068).  Anything that
    lived only on a per-role branch (e.g. a reviewer's merge history)
    is therefore discarded from agent trees; only state pushed to the
    shared work branch is re-materialised on respawn.  Operators needing
    per-worktree retention should use ``restart_agent`` instead.

    URL params:
        pipeline_id: Pipeline ID
        phase: Phase name to restart (e.g. "implement")

    Request body (optional):
        {
            "reason": "Human-readable reason for the restart"
        }

    Response:
        {
            "success": true,
            "data": {
                "phase": "implement",
                "agents_to_restart": ["coder", "tester", "documenter", ...]
            }
        }
    """
    repo_path = _pkg.get_repo_path()

    try:
        store, pipeline = _pkg._resolve_pipeline(pipeline_id, repo_path)
    except _pkg.InvalidPipelineIdError:
        return _pkg.make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}", status_code=400
        )
    except _pkg.PipelineNotFoundError:
        return _pkg.make_error_response(f"Pipeline {pipeline_id} not found", status_code=404)

    # Validate phase
    try:
        _pkg.PipelinePhase(phase)
    except ValueError:
        return _pkg.make_error_response(f"Invalid phase: {phase}", status_code=400)

    # Validate pipeline is in a restartable state.  CANCELLED is included so
    # that a cancel_task(cleanup=false) pipeline can be resumed without a
    # full resubmission (see #1725).
    if pipeline.status not in (
        _pkg.PipelineStatus.RUNNING,
        _pkg.PipelineStatus.AWAITING_HUMAN,
        _pkg.PipelineStatus.FAILED,
        _pkg.PipelineStatus.CANCELLED,
    ):
        return _pkg.make_error_response(
            f"Pipeline {pipeline_id} is not in a restartable state (status: {pipeline.status.value})",
            status_code=409,
        )

    # Only the current phase can be restarted — restarting a completed or
    # future phase would corrupt pipeline state.
    if phase != pipeline.current_phase.value:
        return _pkg.make_error_response(
            f"Phase {phase} is not the current phase (current: {pipeline.current_phase.value})",
            status_code=409,
        )

    phase_exec = pipeline.phases.get(phase)
    if phase_exec is None:
        return _pkg.make_error_response(
            f"Phase {phase} not found in pipeline {pipeline_id}", status_code=404
        )

    body = _pkg.request.get_json(silent=True) or {}
    reason = body.get("reason", "Manual phase restart via API")

    # Compute gateway mode from pipeline config (not hardcoded "public")
    gateway_mode, _ = _pkg._compute_gateway_mode(pipeline)

    spawner = _pkg._get_spawner()

    # Acquire the pipeline state lock to collect agent roles, snapshot
    # container IDs, and update pipeline status to RUNNING *before* the
    # slow container teardown.  This ensures that ``get_status`` returns
    # ``running`` immediately, even if the MCP call times out during
    # container stop/remove (see #1594).
    lock = _pkg.get_pipeline_state_lock(pipeline_id)
    with lock:
        # Re-load pipeline under the lock so agent_roles reflects the
        # latest state (guards against concurrent modifications).
        pipeline = store.load_pipeline(pipeline_id)

        # Re-check current phase under the lock to prevent TOCTOU race:
        # the pipeline could have advanced between the earlier check and
        # lock acquisition.
        if phase != pipeline.current_phase.value:
            return _pkg.make_error_response(
                f"Phase {phase} is not the current phase (current: {pipeline.current_phase.value})",
                status_code=409,
            )

        phase_exec = pipeline.phases.get(phase)
        if phase_exec is None:
            return _pkg.make_error_response(
                f"Phase {phase} not found in pipeline {pipeline_id}", status_code=404
            )

        # 1. Collect agent roles for respawning. Prefer the runtime cache
        #    on ``phase_exec.agents`` since it reflects the roster from
        #    the most recent spawn, but fall back to the deterministic
        #    source the executor itself consults — ``get_roles_for_phase``.
        #    Without this fallback a restart whose clear step ran
        #    (``phase_exec.agents = []`` below) but whose spawn step
        #    failed leaves the pipeline unrecoverable: every subsequent
        #    ``restart_phase`` 400s on the now-empty cache, and
        #    ``start_pipeline`` 409s on the CANCELLED state (#2515).
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
            # Mirror ``_run_concurrent_phase`` exactly so the route's
            # response (and the downstream worktree-delete / health-
            # monitor reset) matches the roster the spawn will actually
            # produce.
            try:
                from egg_contracts.agent_roles import (
                    get_roles_for_phase as _get_roles_for_phase,
                )

                for r in _get_roles_for_phase(
                    phase,
                    include_reviewers=True,
                    repo=pipeline.repo,
                    has_contract=getattr(pipeline, "has_contract", True),
                ):
                    try:
                        agent_roles.append(_pkg.AgentRole(r.value))
                    except ValueError:
                        continue
            except Exception as exc:  # noqa: BLE001
                # Catch derivation failures so the route returns 400
                # rather than 500 — deliberate divergence from
                # ``_run_concurrent_phase``, which lets the same failure
                # propagate up the worker thread. In a synchronous HTTP
                # context an honest 400 ("No agents found") is more
                # useful to the operator than a 500.
                _pkg.logger.warning(
                    "restart_phase: failed to derive default roster fallback",
                    pipeline_id=pipeline_id,
                    phase=phase,
                    error=str(exc),
                )

            if not agent_roles:
                return _pkg.make_error_response(
                    f"No agents found in phase {phase} to restart", status_code=400
                )

            _pkg.logger.info(
                "restart_phase: phase_exec.agents empty, derived roster from pipeline config",
                pipeline_id=pipeline_id,
                phase=phase,
                agent_roles=[r.value for r in agent_roles],
            )

        # 2. Snapshot container IDs for teardown outside the lock
        old_container_ids = [c.container_id for c in phase_exec.containers]

        # 3. Fully reset phase execution state so the new _run_pipeline
        #    thread treats this as a fresh phase.  Set pipeline status to
        #    RUNNING and bump run_epoch so any lingering old _run_pipeline
        #    thread detects the restart and exits (see #1638).
        #    NOTE: artifacts are intentionally preserved — they may contain
        #    outputs from partial work useful as context for the retry.
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
        # ``updated_at`` is unconditionally set by ``StateStore.save_pipeline``
        # (which ``update_pipeline`` routes through).
        store.update_pipeline(pipeline_id, pipeline.model_dump(mode="json"))

    # --- Outside the lock: slow, idempotent, best-effort operations ---

    # 3b. Persist the in-flight phase's BRC message record to disk BEFORE the
    #     destructive container/worktree teardown (#3200 task-7-1, mid-phase
    #     BRC record survival). Today ``_write_brc_history`` runs only at phase
    #     transitions (``_persist_phase_brc_history`` in complete/advance_phase,
    #     #1827); a mid-phase restart never wrote the durable on-disk
    #     transcript.
    #
    #     PRIMARY mechanism is option (a), the live Redis stream: it survives a
    #     bare restart (the store is cleared only at phase transitions /
    #     pipeline create+delete, never here — see step 5), so a reseeded
    #     session re-pulls the in-flight record from Redis via
    #     ``/brc-transcript`` + ``read_peer_artifact``. The slice-scoped
    #     CONSENSUS_PROPOSE/ACK/NACK records of an in-flight implement slice
    #     rely on (a) for survival.
    #
    #     This disk persist (option (b)) is a belt-and-suspenders ADD-ON with a
    #     deliberately NARROW durability scope — do not overstate it. It calls
    #     ``_persist_phase_brc_history`` -> ``_write_brc_history(
    #     write_per_slice=False)``. For a slice-aware implement phase that path
    #     writes ONLY the ``{id}-implement-unattributed.{md,json}`` sibling
    #     (non-CONSENSUS BRC types: HEARTBEAT/STATUS/HANDOFF/AGENT_FAILED/
    #     NUDGE/OVERSEER_ALERT) and SKIPS the per-slice bucket loop; the
    #     slice's CONSENSUS_* proposals/verdicts/open-NACKs are NOT written to
    #     disk here (write_per_slice=False avoids the #2755 add/add conflict on
    #     ``work``; per-slice files are owned by the slice integration branch).
    #     So across a FULL Redis loss (orchestrator pod death, the cold-start
    #     case task-6-1 covers) the in-flight slice record does NOT survive on
    #     disk — only (a) preserves it. What (b) does buy: for non-slice phases
    #     (plan/refine/pr) and non-slice implement runs the aggregate
    #     ``{id}-{phase}.{md,json}`` transcript IS written, and for slice runs
    #     the unattributed audit sibling is captured — extending the #1827
    #     persist-before-clear invariant to the restart path for everything
    #     except the per-slice CONSENSUS buckets. Best-effort and front-running
    #     teardown: a transcript-write hiccup must never block recovery of a
    #     wedged phase (mirrors the salvage step below).
    try:
        _pkg._persist_phase_brc_history(pipeline, store, phase)
    except Exception as brc_persist_err:  # noqa: BLE001
        _pkg.logger.warning(
            "Failed to persist in-flight BRC history during phase restart (continuing)",
            pipeline_id=pipeline_id,
            phase=phase,
            error=str(brc_persist_err),
        )

    # 4. Stop and remove old containers
    for container_id in old_container_ids:
        try:
            spawner.stop_agent_container(container_id, cleanup_session=True)
        except Exception as e:
            _pkg.logger.warning(
                "Failed to stop container during phase restart",
                container_id=container_id[:12] if container_id else "?",
                error=str(e),
            )
        try:
            spawner.remove_agent_container(container_id, force=True, cleanup_session=False)
        except Exception as e:
            _pkg.logger.warning(
                "Failed to remove container during phase restart",
                container_id=container_id[:12] if container_id else "?",
                error=str(e),
            )

    # 4b. Delete per-agent worktrees so respawned containers get fresh mounts.
    #     Without this, stale worktree directories (e.g. broken btrfs mounts)
    #     survive container removal and cause create_worktree to skip creation
    #     or fail.  Mirrors cleanup_pipeline's worktree deletion.  (#1723)
    #
    #     Enumerate from disk rather than guess names: slice-scoped worktrees
    #     are ``{pipeline_id}-slice-{N}-{role}``, not ``{pipeline_id}-{role}``,
    #     so a name-guess loop misses every per-slice worktree on a slice
    #     pipeline and leaves them behind.  (#2522)
    #
    #     ``validate_git=False`` so that broken/corrupted worktrees (missing
    #     or unreadable ``.git`` marker — exactly the #1723 btrfs failure
    #     class) still reach ``delete_worktrees``. The default
    #     ``validate_git=True`` is salvage-correct (you can't salvage a
    #     broken worktree) but cleanup-incorrect (you must still delete it).
    restart_role_values = {role.value for role in agent_roles}
    try:
        all_worktrees = _pkg.agent_salvage.enumerate_agent_worktrees(
            pipeline_id, validate_git=False
        )
    except (OSError, ImportError, RuntimeError) as e:
        _pkg.logger.warning(
            "Failed to enumerate per-agent worktrees during phase restart",
            pipeline_id=pipeline_id,
            error=str(e),
        )
        all_worktrees = []
    worktrees_to_delete = [wt for wt in all_worktrees if wt.agent_role in restart_role_values]

    # Salvage unpushed agent commits before deleting worktrees (#2429).
    # Restart is *the* scenario where unpushed commits accumulate: an
    # operator hits this endpoint precisely because agents are wedged or
    # timed out — the same conditions that prevent pushes from landing on
    # ``origin/<assigned_branch>``. Without this hook, restart would be
    # the one orchestrator-side worktree-delete code path that bypasses
    # salvage and silently destroys recoverable work. Best-effort: any
    # failure logs and continues so cleanup cannot be blocked by salvage.
    if worktrees_to_delete:
        try:
            _pkg.agent_salvage.auto_salvage_pipeline(
                spawner.gateway,
                pipeline_id,
                worktree_filter={wt.worktree_id for wt in worktrees_to_delete},
                mode=gateway_mode,
                base_branch=pipeline.base_branch,
            )
        except Exception as e:
            _pkg.logger.warning(
                "Auto-salvage failed during phase restart; proceeding with worktree deletion",
                pipeline_id=pipeline_id,
                error=str(e),
            )

    for wt in worktrees_to_delete:
        log_extras: dict[str, str] = {}
        if wt.slice_id is not None:
            log_extras["slice_id"] = wt.slice_id
        try:
            spawner.gateway.delete_worktrees(container_id=wt.worktree_id, force=True)
            _pkg.logger.info(
                "Deleted per-agent worktree during phase restart",
                agent_worktree_id=wt.worktree_id,
                pipeline_id=pipeline_id,
                **log_extras,
            )
        except Exception as e:
            _pkg.logger.warning(
                "Failed to delete per-agent worktree during phase restart",
                agent_worktree_id=wt.worktree_id,
                pipeline_id=pipeline_id,
                error=str(e),
                **log_extras,
            )

    # 5. Reset consensus state.
    #    Slice-4 TASK-4-1: mirror the slice-aware semantics of
    #    ``restart_agent`` (line ~2859) — clear BOTH the pipeline-level
    #    tracker AND every per-slice tracker keyed
    #    ``f"{pipeline_id}/{slice_id}"`` (see
    #    ``peer_consensus._tracker_key``). Phase-level restart wipes
    #    the entire phase, so any per-slice consensus state that
    #    survived the restart is stale and would deadlock the new run
    #    if left in place.
    #
    #    INVARIANT (#3200 task-7-1, mid-phase BRC record survival): like
    #    ``restart_agent`` above, this clears the *peer consensus tracker*
    #    (ephemeral ACK/NACK state) but MUST NOT clear the *Redis message
    #    store* (``pipeline:{id}:messages``). That store is the durable BRC
    #    message record; a mid-phase phase restart preserves it so the
    #    reseeded session can re-pull it (``/brc-transcript`` +
    #    ``read_peer_artifact``) and re-derive the #3189 anchors. The store is
    #    cleared only at phase transitions / pipeline create+delete, never on
    #    restart. Do NOT add ``get_message_store().clear()`` here.
    try:
        try:
            from peer_consensus import get_peer_consensus_tracker
        except ImportError:
            from ..peer_consensus import (
                get_peer_consensus_tracker,  # type: ignore[import-not-found]
            )

        tracker = get_peer_consensus_tracker(pipeline_id)
        if tracker:
            tracker.clear()
            _pkg.logger.info("Cleared peer consensus tracker", pipeline_id=pipeline_id)

        # Per-slice trackers. Best-effort contract load: if the
        # contract cannot be read (corrupt on disk, etc.), the
        # pipeline-level clear above still ran, and the slice
        # trackers will be reconstructed lazily on next consensus
        # activity — preserving the historical pipeline-level-only
        # behaviour as a fallback rather than blocking the restart.
        # **Worktree-path resolution (reviewer_code v1 blocker 2)**:
        # active pipelines' contracts live in the per-pipeline
        # worktree at ``/home/egg/.egg-worktrees/<pipeline_id>/<repo>/``
        # — NOT under ``store.repo_path`` (the main orchestrator repo).
        # Without ``resolve_worktree_path`` the ``load_contract`` call
        # below silently fails with ``ContractNotFoundError`` for every
        # active pipeline, the per-slice loop never iterates, and the
        # whole per-slice clear becomes a no-op. Pattern mirrors
        # ``routes/signals.py:709`` and ``routes/pipelines.py:10017``.
        try:
            from egg_contracts.loader import load_contract
        except ImportError:
            load_contract = None  # type: ignore[assignment]
        if load_contract is not None:
            try:
                from routes import resolve_worktree_path
            except ImportError:
                try:
                    from .. import (
                        resolve_worktree_path,  # type: ignore[no-redef]
                    )
                except ImportError:
                    resolve_worktree_path = None  # type: ignore[assignment]
            try:
                if resolve_worktree_path is not None:
                    _contract_repo_path = resolve_worktree_path(
                        pipeline_id, _pkg.Path(store.repo_path)
                    )
                else:
                    _contract_repo_path = _pkg.Path(store.repo_path)
                _contract = load_contract(pipeline_id, _contract_repo_path)
            except Exception as load_err:  # noqa: BLE001 — best-effort
                _pkg.logger.warning(
                    "Could not load contract to enumerate slice trackers "
                    "during phase restart; per-slice consensus state may "
                    "be left stale until lazy reconstruction",
                    pipeline_id=pipeline_id,
                    error=str(load_err),
                )
                _contract = None
            if _contract is not None and getattr(_contract, "slices", None):
                for _s in _contract.slices:
                    _slice_tracker = get_peer_consensus_tracker(pipeline_id, slice_id=_s.id)
                    if _slice_tracker:
                        _slice_tracker.clear()
                        _pkg.logger.info(
                            "Cleared per-slice peer consensus tracker",
                            pipeline_id=pipeline_id,
                            slice_id=_s.id,
                        )
    except ImportError:
        pass
    except Exception as e:
        _pkg.logger.warning(
            "Failed to clear peer consensus",
            pipeline_id=pipeline_id,
            error=str(e),
        )

    # 6. Reset restart counts for this pipeline
    spawner.reset_restart_counts(pipeline_id)

    # 6b. Drop health-monitor anchors for every respawned role so the Tier-1
    #     heartbeat clock does not survive the restart and fire stale-elapsed
    #     alerts that the overseer would faithfully escalate (issue #2084).
    try:
        try:
            from health_monitor import get_health_monitor
        except ImportError:
            from ..health_monitor import (
                get_health_monitor,  # type: ignore[import-not-found]
            )
        _hm = get_health_monitor()
        if _hm is not None:
            for role in agent_roles:
                _hm.reset_agent(role.value)
    except Exception as e:
        _pkg.logger.warning(
            "Failed to reset health-monitor state during phase restart",
            pipeline_id=pipeline_id,
            phase=phase,
            error=str(e),
        )

    # 7. Launch a new _run_pipeline thread to monitor the restarted phase.
    #    Container spawning is handled by _run_concurrent_phase within the
    #    thread, matching the recovery pattern used by start_pipeline.
    #    See #1638: the original polling thread died when the pipeline
    #    failed; without this, consensus completion is never detected.
    agents_to_restart = [role.value for role in agent_roles]
    repo_path_for_thread = store.repo_path

    _pkg._spawn_pipeline_run_thread(pipeline_id, repo_path_for_thread, pipeline.run_epoch)

    _pkg.logger.info(
        "Phase restarted",
        pipeline_id=pipeline_id,
        phase=phase,
        agents_to_restart=agents_to_restart,
        reason=reason,
    )

    return _pkg.make_success_response(
        f"Phase {phase} restarted with {len(agents_to_restart)} agent(s)",
        data={
            "phase": phase,
            "agents_to_restart": agents_to_restart,
        },
    )
