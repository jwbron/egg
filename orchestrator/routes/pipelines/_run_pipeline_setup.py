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
