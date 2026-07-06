"""run_pipeline setup-block helpers helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _sync_contract_setup(
    pipeline,
    *,
    gateway_mode,
    pipeline_id,
    pipeline_mode,
    repo_path,
    source_branch_for_contract_pull,
    spawner,
    store,
    worktree_repo_path,
):
    """Create/sync the worktree companion contract (extracted verbatim from
    _run_pipeline). Returns (pipeline, done); done=True means the caller must
    return immediately (a contract-setup failure already marked the pipeline)."""
    if not pipeline.contract_synced:
        try:
            from egg_contracts.loader import compose_task_description, create_contract

            # Every entry path (GitHub issue, JIRA, free-text) anchors
            # the task the same way (#3163): identity first, then the
            # operator's submit description. Before #3163 issue
            # pipelines deliberately got ``None`` here (#3042 "agents
            # fetch the live body"), which left the #3123 binding
            # prompt section empty for the most common pipeline type.
            issue_url = (
                f"https://github.com/{pipeline.repo}/issues/{pipeline.issue_number}"
                if pipeline.issue_number is not None
                else None
            )
            task_description = compose_task_description(
                description=pipeline.prompt,
                issue_number=pipeline.issue_number,
                issue_url=issue_url,
                jira_ticket=pipeline.jira_ticket,
            )

            # When source_branch is set, try to carry over the contract
            # (with any resolved HITL decisions) from there instead of
            # overwriting with a fresh zero-state contract (#2035).
            pulled_contract = False
            if source_branch_for_contract_pull:
                try:
                    pulled_contract = _pkg._pull_contract_from_source_branch(
                        repo_path=worktree_repo_path,
                        source_branch=source_branch_for_contract_pull,
                        issue_number=pipeline.issue_number,
                        pipeline_id=pipeline.id,
                        spawner=spawner,
                        gateway_mode=gateway_mode,
                        task_description=task_description,
                    )
                except Exception:
                    _pkg.logger.warning(
                        "Unexpected error pulling contract from source branch — falling back to fresh contract",
                        pipeline_id=pipeline_id,
                        source_branch=source_branch_for_contract_pull,
                        exc_info=True,
                    )
                    pulled_contract = False

            if not pulled_contract:
                if pipeline.issue_number is not None:
                    create_contract(
                        issue_number=pipeline.issue_number,
                        title=f"Issue #{pipeline.issue_number}",
                        url=issue_url or "",
                        pipeline_id=pipeline.id,
                        repo_root=worktree_repo_path,
                        task_description=task_description,
                    )
                else:
                    # ``pipeline.issue_number is None`` covers both
                    # free-text submits and JIRA-driven pipelines
                    # (``pipeline.jira_ticket`` set). The event-pump
                    # never delivers the orchestrator-built spawn
                    # prompt to the agent, so the contract (read via
                    # ``egg-contract show`` + the #3123 prompt
                    # section) is the reliable channel for the
                    # complete task; the ``title`` arg is only used
                    # for the ``IssueInfo`` label and is dropped
                    # without an ``issue_number``, so it is not a
                    # substitute (#3033).
                    create_contract(
                        pipeline_id=pipeline.id,
                        title=(pipeline.prompt or "")[:100],
                        task_description=task_description,
                        repo_root=worktree_repo_path,
                    )

            # Write pre-generated drafts for short-flow pipelines so the
            # existing plan parser can populate the contract with tasks.
            if pipeline.analysis or pipeline.plan:
                drafts_dir = worktree_repo_path / ".egg-state" / "drafts"
                drafts_dir.mkdir(parents=True, exist_ok=True)

                if pipeline.analysis:
                    analysis_rel = _pkg._get_draft_path(
                        "refine",
                        issue_number=pipeline.issue_number,
                        pipeline_id=pipeline_id,
                    )
                    if analysis_rel:
                        (worktree_repo_path / analysis_rel).write_text(
                            pipeline.analysis, encoding="utf-8"
                        )
                        _pkg.logger.info(
                            "Wrote pre-generated analysis draft",
                            pipeline_id=pipeline_id,
                            path=analysis_rel,
                        )

                if pipeline.plan:
                    plan_rel = _pkg._get_draft_path(
                        "plan",
                        issue_number=pipeline.issue_number,
                        pipeline_id=pipeline_id,
                    )
                    if plan_rel:
                        (worktree_repo_path / plan_rel).write_text(pipeline.plan, encoding="utf-8")
                        _pkg.logger.info(
                            "Wrote pre-generated plan draft",
                            pipeline_id=pipeline_id,
                            path=plan_rel,
                        )

                        # Populate the contract from the plan's yaml-tasks appendix
                        _inline_plan_populate_result = _pkg._populate_contract_from_plan(
                            worktree_repo_path,
                            pipeline_id,
                            pipeline_mode,
                            pipeline.issue_number,
                        )
                        # #2627 follow-up: warn-and-continue on non-POPULATED.
                        # This is the initial-contract creation path (a
                        # pre-generated plan handed to ``start_pipeline``);
                        # failing here would block legitimate pipelines that
                        # recover via the natural plan-phase populator a few
                        # blocks later.  We only attach the structured
                        # outcome as audit signal.
                        if _inline_plan_populate_result.outcome != _pkg.PopulateOutcome.POPULATED:
                            _pkg.logger.warning(
                                "Pre-generated plan populate produced non-POPULATED outcome",
                                pipeline_id=pipeline_id,
                                outcome=_inline_plan_populate_result.outcome.value,
                            )

            # Commit all .egg-state/ files so they're on the feature branch
            issue_ref = (
                f"issue #{pipeline.issue_number}"
                if pipeline.issue_number is not None
                else f"pipeline {pipeline_id}"
            )
            try:
                _pkg._commit_statefiles_to_worktree(
                    worktree_repo_path,
                    f"Initialize SDLC contract for {issue_ref}",
                    pipeline_identifier=_pkg._pipeline_identifier(
                        pipeline.issue_number, pipeline_id
                    ),
                    pipeline_id=pipeline_id,
                )
            except Exception as git_err:
                # Catch broadly so TimeoutExpired/OSError also produce
                # an explicit FAILED state rather than silently
                # propagating to the outer handler (#2219).
                _pkg.logger.error(
                    "Failed to commit initial statefiles — aborting pipeline",
                    pipeline_id=pipeline_id,
                    error=str(git_err),
                )
                with _pkg.get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    pipeline.status = _pkg.PipelineStatus.FAILED
                    pipeline.contract_synced = False
                    pipeline.error = f"Failed to commit initial statefiles: {git_err}"
                    store.save_pipeline(pipeline)
                return pipeline, True

            # Push contract statefiles to remote so agents see them.
            # This MUST succeed before agents start — otherwise agents'
            # diffs will include .egg-state/ files they can't push (#1431).
            push_succeeded = False
            # For prompt-driven pipelines, pipeline.branch is None at this
            # point — the branch name is only persisted later when the
            # agent container is spawned (line ~6279).  Derive it here so
            # the push actually happens.  The worktree was already created
            # on this branch by the gateway.
            push_branch = pipeline.branch or f"egg/{pipeline_id}/work"
            if not pipeline.branch:
                pipeline.branch = push_branch
                with _pkg.get_pipeline_state_lock(pipeline_id):
                    p = store.load_pipeline(pipeline_id)
                    if not p.branch:
                        p.branch = push_branch
                        store.save_pipeline(p)
                        _pkg.logger.info(
                            "Recorded generated branch on pipeline (pre-push)",
                            pipeline_id=pipeline_id,
                            branch=push_branch,
                        )
            if worktree_repo_path != repo_path:
                push_err_msg = ""
                # push_worktree_branch reconciles non-fast-forward
                # rejections internally (fetch+rebase+retry), so a
                # single call is sufficient — no outer retry needed.
                try:
                    push_result = spawner.gateway.push_worktree_branch(
                        pipeline_id=pipeline_id,
                        repo_path=str(worktree_repo_path),
                        branch=push_branch,
                        mode=gateway_mode,
                        base_branch=pipeline.base_branch,
                    )
                    push_succeeded = bool(push_result)
                    if not push_succeeded:
                        push_err_msg = push_result.describe()
                except Exception as push_err:
                    push_succeeded = False
                    push_err_msg = str(push_err)

                if not push_succeeded:
                    _pkg.logger.error(
                        "Contract init push failed after retry — aborting pipeline",
                        pipeline_id=pipeline_id,
                        error=push_err_msg,
                    )
                    with _pkg.get_pipeline_state_lock(pipeline_id):
                        pipeline = store.load_pipeline(pipeline_id)
                        pipeline.status = _pkg.PipelineStatus.FAILED
                        pipeline.contract_synced = False
                        pipeline.error = f"Failed to push contract init to remote: {push_err_msg}"
                        store.save_pipeline(pipeline)
                    return pipeline, True
            else:
                _pkg.logger.warning(
                    "Skipped contract init push — worktree path equals repo path",
                    pipeline_id=pipeline_id,
                    worktree_repo_path=str(worktree_repo_path),
                    repo_path=str(repo_path),
                )

            with _pkg.get_pipeline_state_lock(pipeline_id):
                pipeline = store.load_pipeline(pipeline_id)
                pipeline.contract_synced = push_succeeded
                store.save_pipeline(pipeline, commit=False)
            _pkg.logger.info(
                "Pipeline contract created in worktree",
                pipeline_id=pipeline_id,
                mode=pipeline_mode,
            )
        except Exception as contract_err:
            _pkg.logger.error(
                "Failed to create contract in worktree",
                pipeline_id=pipeline_id,
                error=str(contract_err),
            )
            with _pkg.get_pipeline_state_lock(pipeline_id):
                pipeline = store.load_pipeline(pipeline_id)
                pipeline.status = _pkg.PipelineStatus.FAILED
                pipeline.error = f"Failed to create contract: {contract_err}"
                store.save_pipeline(pipeline)
            return pipeline, True
    return pipeline, False


def _map_host_repos(
    pipeline,
    *,
    host_gid,
    host_repo_map,
    host_uid,
    pipeline_id,
    pipeline_repos,
    spawner,
    worktree_id,
    repo_volumes,
    worktree_repo_path,
):
    """Map host repos -> container volumes + resolve the worktree repo path
    (extracted verbatim from _run_pipeline). repo_volumes/worktree_repo_path
    come in with their defaults and are returned possibly-updated."""
    if host_repo_map:
        try:
            # Request repos in owner/repo format if available, else bare names
            wt_repos = pipeline_repos if pipeline_repos else list(host_repo_map.keys())
            # When the pipeline specifies a base_branch, pass it through
            # so the worktree is branched from that ref instead of the
            # repo's default branch.  Otherwise let the gateway resolve
            # the remote default branch per-repo (see #860).
            # Retry worktree creation on transient gateway errors
            # (e.g., 500s from concurrent pipeline starts contending
            # on per-repo locks).  See #1386.
            wt_max_attempts = 3
            wt_backoff = 2.0
            wt_result = None
            for wt_attempt in range(1, wt_max_attempts + 1):
                try:
                    wt_result = spawner.gateway.create_worktrees(
                        container_id=worktree_id,
                        repos=wt_repos,
                        uid=host_uid,
                        gid=host_gid,
                        base_branch=pipeline.base_branch,
                    )
                    break  # Success — exit retry loop
                except _pkg.GatewayError as gw_err:
                    is_transient = gw_err.status_code is None or gw_err.status_code >= 500
                    if not is_transient or wt_attempt == wt_max_attempts:
                        # Surface gw_err.details so per-repo failures
                        # captured by the gateway aren't dropped.  See
                        # #2186.
                        _pkg.logger.error(
                            "Worktree creation failed permanently",
                            pipeline_id=pipeline_id,
                            attempts=wt_attempt,
                            status_code=gw_err.status_code,
                            error_message=gw_err.message,
                            details=gw_err.details,
                        )
                        detail_suffix = f" (details: {gw_err.details})" if gw_err.details else ""
                        raise RuntimeError(
                            f"Failed to create worktrees for pipeline {pipeline_id} "
                            f"after {wt_max_attempts} attempts: "
                            f"{gw_err.message}{detail_suffix}"
                        ) from gw_err
                    _pkg.logger.warning(
                        "Worktree creation failed, retrying",
                        pipeline_id=pipeline_id,
                        attempt=wt_attempt,
                        max_attempts=wt_max_attempts,
                        error=str(gw_err),
                        details=gw_err.details,
                    )
                    _pkg.time.sleep(wt_backoff)
                    wt_backoff *= 2

            if wt_result and wt_result.success and wt_result.worktrees:
                # Gateway returns worktrees keyed by the full ``owner/repo``
                # slug (#3393 slice-3, operator ruling #6). The on-disk
                # worktree directory (and the container mount target) is
                # still the bare repo name at /home/egg/repos/<name>, so
                # the path reconstruction below strips the owner prefix
                # from each key.
                repo_volumes = wt_result.worktrees

                # Derive the orchestrator-accessible worktree path.
                # Reviewer containers write verdict/draft/check files into
                # the worktree, so the orchestrator must read from there.
                # Match against pipeline.repo (full owner/repo slug, which
                # is now the map key) explicitly to avoid picking the wrong
                # repo in multi-repo pipelines.
                matched = False
                if pipeline.repo and pipeline.repo in wt_result.worktrees:
                    repo_short = pipeline.repo.split("/")[-1]
                    candidate = _pkg.WORKTREE_BASE_DIR / worktree_id / repo_short
                    if candidate.exists():
                        worktree_repo_path = candidate
                        matched = True
                if not matched:
                    # Fallback: take the first existing worktree path.
                    # Keys are ``owner/repo``; the on-disk dir is the bare
                    # leaf, so strip the owner prefix before joining.
                    for owner_repo in wt_result.worktrees:
                        candidate = _pkg.WORKTREE_BASE_DIR / worktree_id / owner_repo.split("/")[-1]
                        if candidate.exists():
                            worktree_repo_path = candidate
                            break

                _pkg.logger.info(
                    "Worktrees created for pipeline",
                    pipeline_id=pipeline_id,
                    worktrees=list(repo_volumes.keys()),
                )
            else:
                raise RuntimeError(
                    f"Worktree creation returned no worktrees for pipeline {pipeline_id}: "
                    f"errors={wt_result.errors}"
                )

            if wt_result.errors:
                for err in wt_result.errors:
                    _pkg.logger.warning("Worktree error", pipeline_id=pipeline_id, error=err)

        except RuntimeError:
            raise  # Re-raise our own RuntimeError
        except Exception as wt_err:
            raise RuntimeError(
                f"Failed to create worktrees for pipeline {pipeline_id}: {wt_err}"
            ) from wt_err
    return repo_volumes, worktree_repo_path


def _start_phase_setup(pipeline, *, pipeline_id, pipeline_mode, store, worktree_repo_path):
    """start_phase==implement safety-net: populate the contract / apply the
    plan draft before the implement phase spawns (extracted verbatim from
    _run_pipeline). Returns (pipeline, done); done=True -> caller returns."""
    if pipeline.config.start_phase == "implement":
        plan_draft_rel = _pkg._get_draft_path(
            "plan",
            issue_number=pipeline.issue_number,
            pipeline_id=pipeline.id,
        )
        if plan_draft_rel and (worktree_repo_path / plan_draft_rel).exists():
            # Advance contract.current_phase alongside slice/PR
            # ingestion.  In the natural flow contract.current_phase
            # is mutated by the plan reviewer agent (or the gateway
            # phase API) via apply_mutation; with start_phase=implement
            # no such reviewer ever runs, so the contract would stay
            # at REFINE forever (#2427 sub-bug).  We pass
            # pipeline.current_phase rather than a hardcoded literal
            # so the right value follows automatically if start_phase
            # ever supports values other than 'implement'.  The
            # populator enforces forward-only advancement, so a
            # respawn during the PR phase cannot demote the contract.
            # Note: the *outer* guard above remains hardcoded to
            # ``"implement"``; widening it to other start_phase values
            # is a two-line change (this guard plus the matching
            # ``initial_phase`` mapping in start_pipeline).
            # Catch ``ForestValidationError`` here so a malformed
            # plan landing at the safety-net path lands on the
            # dedicated empty-contract HITL — the same recovery
            # surface the natural plan-complete path uses via
            # :func:`_populate_contract_from_plan_safe`'s
            # forest-violation translation.  Without this catch the
            # safety net (which calls the inner directly so the
            # ``PlanDraftMissing*`` raises don't fire here) would
            # propagate the exception to the outer pipeline
            # ``except`` and the operator would see a generic
            # ``status: failed`` instead of the actionable
            # repopulate/restart-plan/abort decision (#2627 review).
            try:
                _safety_net_populate_result = _pkg._populate_contract_from_plan(
                    worktree_repo_path,
                    pipeline_id,
                    pipeline_mode,
                    pipeline.issue_number,
                    current_phase=pipeline.current_phase,
                )
            except _pkg.ForestValidationError as forest_err:
                # #3046 — overlap violations map to their own outcome so
                # the empty-contract HITL prose matches the discriminator.
                _pkg.logger.warning(
                    "contract_phases_ingest_failed",
                    pipeline_id=pipeline_id,
                    reason=forest_err.reason,
                    source="safety_net",
                    errors=forest_err.errors,
                )
                _safety_net_populate_result = _pkg.PopulateResult(
                    _pkg._forest_error_to_outcome(forest_err)
                )
            # #2627 follow-up: fail-fast whenever the safety-net populate
            # did not produce a contract with tasks.  Without this guard
            # the implement phase spawns into the same empty-contract
            # state that #2627 surfaced — the slice-gate at
            # implement-phase entry would eventually catch it, but at
            # that point the pipeline has already advanced and the
            # operator sees the empty-contract divergence after the
            # loop is running.  Catching it here is earlier and cheaper.
            #
            # Routes through :func:`_populate_result_is_empty_contract`
            # so the two empty-contract call sites (this safety net
            # and the natural plan-complete handler below) can't drift
            # out of agreement.  See that helper's docstring for the
            # full discriminator rules.
            if _pkg._populate_result_is_empty_contract(_safety_net_populate_result):
                # Reason dispatch shared with the plan-complete handler
                # via :func:`_populate_outcome_to_hitl_reason` so the
                # POPULATED → "populated_but_empty_slices" translation
                # (and any future special-cased outcome) can't drift
                # between the two call sites (#2627 review follow-up).
                _safety_net_reason = _pkg._populate_outcome_to_hitl_reason(
                    _safety_net_populate_result.outcome
                )
                if _safety_net_populate_result.outcome == _pkg.PopulateOutcome.POPULATED:
                    _safety_net_error = (
                        "start_phase=implement safety-net populate "
                        "completed but produced 0 slices/tasks — refusing "
                        "to spawn implement-phase agents on an empty "
                        "contract (#2627)"
                    )
                else:
                    _safety_net_error = (
                        f"start_phase=implement safety-net populate produced "
                        f"{_safety_net_populate_result.outcome.value} outcome — "
                        f"refusing to spawn implement-phase agents on an "
                        f"empty contract (#2627)"
                    )
                with _pkg.get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    pipeline.status = _pkg.PipelineStatus.FAILED
                    pipeline.error = _safety_net_error
                    store.save_pipeline(pipeline)
                # Emit the dedicated empty-contract HITL inline so the
                # operator sees an actionable decision instead of a
                # generic ``status: failed`` with no recovery path
                # other than ``restart_phase implement`` (which would
                # respawn into the same empty-contract state).
                _pkg._emit_empty_contract_hitl(
                    pipeline_id,
                    pipeline,
                    store,
                    reason=_safety_net_reason,
                    draft_slice_count=None,
                    gate="start_phase_implement_safety_net",
                    phase=pipeline.current_phase,
                )
                _pkg.logger.error(
                    "OVERSEER_ALERT start_phase_implement_safety_net_empty_contract",
                    pipeline_id=pipeline_id,
                    outcome=_safety_net_populate_result.outcome.value,
                    slice_count=_safety_net_populate_result.slice_count,
                    reason=_safety_net_reason,
                )
                _pkg.report_pipeline_status(
                    pipeline,
                    event_type="pipeline.failed",
                    message=f"Pipeline failed: {_safety_net_error[:100]}",
                )
                _pkg._emit_pipeline_event(pipeline, "pipeline.failed")
                return pipeline, True

            # #3100: the natural plan→implement path enforces the
            # #2777 plan pre-flight (``validate_plan_preflight``) at
            # the advance_phase site; implement-start submits skip
            # that site entirely, so a plan draft without a ``pr:``
            # block previously entered the implement phase and every
            # context-PR opener backstop soft-failed with
            # ``missing_pr_metadata`` forever.  Enforce the same
            # validator here — after the empty-contract gate so the
            # #2627 HITL routing above is unchanged.
            if _pkg._enforce_implement_start_plan_preflight(
                pipeline_id,
                pipeline,
                store,
                worktree_repo_path,
                plan_draft_rel,
            ):
                return pipeline, True
    return pipeline, False


def _sync_source_branch_drafts(
    *, gateway_mode, pipeline, pipeline_id, spawner, store, worktree_repo_path
):
    """Carry over analysis/plan drafts from the source branch when set (extracted verbatim from _run_pipeline; pure side-effect, no return)."""
    if pipeline.source_branch and not (pipeline.plan is not None and pipeline.analysis is not None):
        # source_branch is cleared inside _read_source_branch_artifacts
        # when artifacts are actually found.
        try:
            _pkg._read_source_branch_artifacts(
                repo_path=worktree_repo_path,
                source_branch=pipeline.source_branch,
                issue_number=pipeline.issue_number,
                pipeline_id=pipeline_id,
                store=store,
                pipeline=pipeline,
                source_artifact_prefix=pipeline.source_artifact_prefix,
                spawner=spawner,
                gateway_mode=gateway_mode,
            )
        except Exception:
            _pkg.logger.warning(
                "Failed to read artifacts from source branch",
                source_branch=pipeline.source_branch,
                pipeline_id=pipeline_id,
                exc_info=True,
            )

        # Write source-branch artifacts to disk so the safety-net
        # _populate_contract_from_plan() call below can find them.
        # The inline-plan path writes drafts inside the contract_synced
        # block, but that block is skipped on pipeline restarts
        # (contract already synced).  Writing here ensures the draft
        # files exist regardless of contract_synced state.
        if pipeline.plan is not None or pipeline.analysis is not None:
            drafts_dir = worktree_repo_path / ".egg-state" / "drafts"
            drafts_dir.mkdir(parents=True, exist_ok=True)

            if pipeline.plan is not None:
                plan_rel = _pkg._get_draft_path(
                    "plan",
                    issue_number=pipeline.issue_number,
                    pipeline_id=pipeline_id,
                )
                if plan_rel:
                    plan_path = worktree_repo_path / plan_rel
                    plan_path.write_text(pipeline.plan, encoding="utf-8")
                    _pkg.logger.info(
                        "Wrote source-branch plan draft to worktree",
                        pipeline_id=pipeline_id,
                        path=plan_rel,
                    )

            if pipeline.analysis is not None:
                analysis_rel = _pkg._get_draft_path(
                    "refine",
                    issue_number=pipeline.issue_number,
                    pipeline_id=pipeline_id,
                )
                if analysis_rel:
                    analysis_path = worktree_repo_path / analysis_rel
                    analysis_path.write_text(pipeline.analysis, encoding="utf-8")
                    _pkg.logger.info(
                        "Wrote source-branch analysis draft to worktree",
                        pipeline_id=pipeline_id,
                        path=analysis_rel,
                    )


def _resolve_worktree_repo(
    pipeline, *, gateway_mode, pipeline_id, repo_path, spawner, store, worktree_repo_path
):
    """Resolve the per-pipeline worktree repo path + reconcile a stale
    worktree (extracted verbatim from _run_pipeline). Returns (pipeline,
    done); done=True -> caller returns immediately."""
    if worktree_repo_path != repo_path:
        # Determine whether the most recent prior phase completed
        # successfully — this controls whether local-ahead commits are
        # pushed (success) or discarded (failure).
        prior_phase_succeeded = True
        current_phase = pipeline.current_phase
        phase_order = [
            _pkg.PipelinePhase.REFINE,
            _pkg.PipelinePhase.PLAN,
            _pkg.PipelinePhase.IMPLEMENT,
        ]
        current_idx = phase_order.index(current_phase) if current_phase in phase_order else 0
        if current_idx > 0:
            prior_phase = phase_order[current_idx - 1]
            prior_exec = pipeline.phases.get(prior_phase.value)
            if prior_exec and prior_exec.status in (
                _pkg.PipelineStatus.FAILED,
                _pkg.PipelineStatus.CANCELLED,
            ):
                prior_phase_succeeded = False

        # #2979: sync the worktree, pausing for a manual reconcile if
        # it diverges and the rebase autoresolve can't reconcile it.
        # The helper blocks (AWAITING_HUMAN) on a reconcile HITL and
        # resumes the phase start once the operator acks — nothing is
        # discarded and the pipeline is never failed for a recoverable
        # divergence.
        phase_start_sync_outcome, phase_start_sync_aborted = (
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
                prior_phase_succeeded=prior_phase_succeeded,
            )
        )
        if phase_start_sync_aborted:
            # Operator aborted the manual reconcile (or the pause
            # budget was exhausted).  Fail the pipeline; the local
            # commits remain pinned under the backup ref for offline
            # recovery — nothing was discarded.
            _pkg._fail_pipeline_after_divergence_abort(
                pipeline_id,
                store,
                phase=current_phase,
                backup_ref=phase_start_sync_outcome.backup_ref,
                local_only_commit_shas=phase_start_sync_outcome.local_only_commit_shas,
            )
            return pipeline, True

        # When resuming a stale pipeline branch (cancelled run from
        # days/weeks ago), rebase origin/<branch> onto origin/<base>
        # before any orchestrator/agent commits land — otherwise the
        # final PR carries 70+ stale-from-main commits as ancestors
        # (#2098).  No-op for fresh pipelines and for branches already
        # caught up with base.
        if pipeline.branch and pipeline.base_branch:
            try:
                _pkg._rebase_pipeline_branch_onto_base(
                    spawner,
                    pipeline_id,
                    worktree_repo_path,
                    pipeline_branch=pipeline.branch,
                    base_branch=pipeline.base_branch,
                    gateway_mode=gateway_mode,
                )
            except _pkg.StalePipelineBranchError as stale_err:
                with _pkg.get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    pipeline.status = _pkg.PipelineStatus.FAILED
                    pipeline.error = str(stale_err)
                    store.save_pipeline(pipeline)
                return pipeline, True

        # Remove legacy unprefixed draft files (analysis.md, plan.md)
        # that may have been left by earlier pipelines on this branch.
        # Uses git rm so deletions are committed directly.  See #1559.
        cleanup_committed = _pkg._cleanup_stale_generic_drafts(worktree_repo_path)
        if cleanup_committed and pipeline.branch:
            try:
                spawner.gateway.push_worktree_branch(
                    pipeline_id=pipeline_id,
                    repo_path=str(worktree_repo_path),
                    branch=pipeline.branch,
                    mode=gateway_mode,
                    base_branch=pipeline.base_branch,
                )
            except Exception:
                _pkg.logger.warning(
                    "Failed to push stale draft cleanup (continuing)",
                    pipeline_id=pipeline_id,
                )
    return pipeline, False
