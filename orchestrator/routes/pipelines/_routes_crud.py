"""CRUD-route bodies helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _create_pipeline_body() -> tuple[_pkg.Response, int]:
    """
    Create a new pipeline.

    Request body:
        {
            "issue_number": 123,
            "repo": "owner/name",
            "branch": "egg/issue-123",
            "config": {...}  // optional
        }

    Response:
        {
            "success": true,
            "message": "Pipeline created",
            "data": {
                "pipeline": {...}
            }
        }
    """
    data = _pkg.request.get_json()
    if data is None:
        return _pkg.make_error_response("Missing request body")
    if not isinstance(data, dict):
        return _pkg.make_error_response("Request body must be a JSON object")

    network_mode = data.get("network_mode")
    if network_mode is not None and network_mode not in ("public", "private"):
        return _pkg.make_error_response(
            f"Invalid network_mode: {network_mode!r} (must be 'public' or 'private')"
        )

    issue_number = data.get("issue_number")
    repo = data.get("repo")
    branch = data.get("branch")
    base_branch = data.get("base_branch")
    prompt = data.get("prompt")

    # #3393 (multi-repo): a submission may carry a ``repos`` list instead of
    # (or in addition to) the single ``repo``. Normalize it up front and derive
    # the primary onto the legacy ``repo``/``base_branch`` scalars so the
    # single-repo plumbing below (naming, base-branch detection, branch checks)
    # keeps working and a direct HTTP submission — one that bypasses the
    # submit_task MCP tool that would otherwise mirror the primary — is
    # supported. ``repos_entries`` is None for a single-repo submission.
    repos_entries: list[dict[str, str | None]] | None = None
    repos_arg = data.get("repos")
    if repos_arg is not None:
        _repos_err, repos_entries, _primary_repo, _primary_base = _pkg._normalize_submission_repos(
            repos_arg
        )
        if _repos_err:
            return _pkg.make_error_response(
                _repos_err, status_code=400, details={"reason": "invalid_repos"}
            )
        if repo and _primary_repo and repo != _primary_repo:
            return _pkg.make_error_response(
                f"Conflicting repo {repo!r} and repos primary {_primary_repo!r}; "
                "pass one or the other.",
                status_code=400,
                details={"reason": "repo_repos_conflict"},
            )
        if not repo:
            repo = _primary_repo
        if not base_branch:
            base_branch = _primary_base
    mode = data.get("mode", "issue")
    analysis = data.get("analysis")
    plan = data.get("plan")
    source_branch = data.get("source_branch")
    if source_branch is not None:
        if not _pkg.re.match(r"^[a-zA-Z0-9_./-]+$", source_branch) or ".." in source_branch:
            return _pkg.make_error_response(
                f"Invalid source_branch: {source_branch!r}",
                status_code=400,
            )
    source_artifact_prefix = data.get("source_artifact_prefix")
    if source_artifact_prefix is not None:
        if not _pkg.re.match(r"^[a-zA-Z0-9_.-]+$", source_artifact_prefix):
            return _pkg.make_error_response(
                f"Invalid source_artifact_prefix: {source_artifact_prefix!r}",
                status_code=400,
            )

    # Issue #1557: Jira-epic SDLC parameters. ``jira_ticket`` is the
    # Atlassian key; ``epic_mode`` is the operator's override
    # (``'auto' | 'fresh' | 'reassess'``). The MCP submit_task tool
    # normalises ``jira_ticket`` to upper-case before forwarding.
    jira_ticket_arg = data.get("jira_ticket")
    epic_mode_arg = data.get("epic_mode")
    if jira_ticket_arg is not None:
        if not isinstance(jira_ticket_arg, str) or not _pkg.re.fullmatch(
            r"[A-Z][A-Z0-9_]*-\d+", jira_ticket_arg
        ):
            return _pkg.make_error_response(
                f"Invalid jira_ticket: {jira_ticket_arg!r} (expected <PROJECT>-<number>)",
                status_code=400,
                details={"reason": "invalid_jira_ticket"},
            )
    if epic_mode_arg is not None:
        if epic_mode_arg not in ("auto", "fresh", "reassess"):
            return _pkg.make_error_response(
                f"Invalid epic_mode: {epic_mode_arg!r} (must be 'auto' / 'fresh' / 'reassess')",
                status_code=400,
                details={"reason": "invalid_epic_mode"},
            )
        if not jira_ticket_arg:
            return _pkg.make_error_response(
                "epic_mode requires jira_ticket",
                status_code=400,
                details={"reason": "epic_mode_without_ticket"},
            )

    # Validate mode
    valid_modes = {m.value for m in _pkg.PipelineMode}
    if mode not in valid_modes:
        return _pkg.make_error_response(
            f"Invalid mode: {mode!r} (must be one of {sorted(valid_modes)})"
        )

    if not repo:
        return _pkg.make_error_response("Missing repo")

    # Repo format sanity check — a lightweight shell-metacharacter guard.
    # The repo_config allowlist (repositories.yaml) is enforced gateway-side.
    if not _pkg.re.match(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$", repo):
        return _pkg.make_error_response(
            f"Invalid repo format: {repo!r} (expected owner/name)",
            status_code=400,
            details={"reason": "repo_not_allowed"},
        )

    # Validate branch and base_branch — reject values that could be
    # interpreted as git flags (e.g. "--upload-pack=...") or contain
    # path-traversal sequences.  Same regex used for source_branch above.
    for _ref_name, _ref_val in [("branch", branch), ("base_branch", base_branch)]:
        if _ref_val is not None:
            if not _pkg.re.match(r"^[a-zA-Z0-9_./-]+$", _ref_val) or ".." in _ref_val:
                return _pkg.make_error_response(
                    f"Invalid {_ref_name}: {_ref_val!r}",
                    status_code=400,
                )

    # Issue-driven or explicitly-named pipelines require a branch;
    # prompt-driven ones do not.
    pipeline_id = data.get("pipeline_id")

    if (issue_number or pipeline_id) and not branch:
        return _pkg.make_error_response("Missing branch")

    # #2399 — push the pipeline tip to ``<branch>/work`` so slice
    # integration branches at ``<branch>/slice-N`` can coexist as
    # siblings under the same namespace (git rejects a leaf ref and
    # children of that ref's path with ``directory file conflict``).
    branch = _pkg._ensure_pipeline_work_ref(branch)

    # Wait for the gateway to be ready before any gateway-dependent work.
    # On fresh deploys / pod restarts the orchestrator can accept requests
    # while the gateway HTTP listener is still coming up; without this gate
    # the first submission proceeds, hits the gateway during pipeline-level
    # worktree creation or per-agent fan-out, and surfaces as a cascade of
    # generic per-agent ConnectionRefused / "Remote end closed connection"
    # errors that operators have to reverse-engineer.  See #1851.
    try:
        _ready_timeout = int(_pkg.os.environ.get("EGG_GATEWAY_READY_TIMEOUT_SECONDS", "60"))
    except ValueError:
        _ready_timeout = 60
    _ready_timeout = max(0, _ready_timeout)
    if _ready_timeout > 0:
        _gw_ready = _pkg.get_gateway_client()
        if not _gw_ready.wait_for_healthy(timeout_seconds=_ready_timeout):
            _last = _gw_ready.check_health()
            _resp, _status = _pkg.make_error_response(
                f"Gateway not ready after {_ready_timeout}s "
                f"(status={_last.status}): {_last.error or 'unhealthy'}. "
                "Retry once the gateway has finished starting up.",
                status_code=503,
                details={
                    "reason": "gateway_not_ready",
                    "gateway_status": _last.status,
                    "gateway_error": _last.error,
                    "timeout_seconds": _ready_timeout,
                },
            )
            _resp.headers["Retry-After"] = str(_ready_timeout)
            return _resp, _status

    repo_path = _pkg.get_repo_path()

    # #3038: resolve the repo's default branch ONCE at submit time and
    # persist it on the pipeline record, so every downstream consumer
    # (the context-PR opener, the restart/spawn paths, the gateway
    # ``register_session`` base, the spawner ``EGG_BASE_BRANCH`` export)
    # reads a concrete base off the record instead of re-deriving it on
    # every invocation. Re-deriving each time opened a narrow race the
    # #3035 reviewer flagged: a single flaky ``git symbolic-ref
    # origin/HEAD`` read drops the opener into the ``origin/main →
    # origin/master → "main"`` fallback chain, which can pick the wrong
    # default on a ``master`` repo and 422 a second ``create_pr``.
    # Persisting closes the race because the consumers' ``base_branch or
    # _detect_default_branch(...)`` short-circuits on the stored value and
    # never reaches the subprocess. ``_detect_default_branch`` is the
    # local/fast helper (``git symbolic-ref``) and is the same resolution
    # the stale-branch reuse check below already performs.
    #
    # An explicit ``base_branch`` (validated above) is passed through
    # untouched; ``repo`` is already guaranteed non-empty by the early
    # ``Missing repo`` guard, so only the ``base_branch`` side needs a
    # check here.
    if not base_branch:
        base_branch = _pkg._detect_default_branch(repo_path)

    # Check that the target branch does not already exist on the remote.
    # This catches conflicts early (before spawning agents).  However,
    # allow branch reuse when the pipeline is in a terminal state
    # (CANCELLED/FAILED/COMPLETE) or doesn't exist at all — this lets
    # callers resubmit against the same branch after a prior run ended.
    if branch:
        try:
            gw = _pkg.get_gateway_client()
            if gw.ls_remote_branch(
                pipeline_id=pipeline_id or f"branch-check-{_pkg.uuid4().hex[:8]}",
                repo_path=str(repo_path),
                ref=f"refs/heads/{branch}",
            ):
                # Branch exists — only block if there is an active pipeline
                _branch_store = _pkg.get_state_store(repo_path)
                _has_active_pipeline = False
                # When pipeline_id is None (auto-generated later), we skip
                # the existence check — we can't look up a pipeline that
                # hasn't been assigned an ID yet.  This is acceptable because
                # auto-generated IDs are unique and won't collide.
                if pipeline_id and _branch_store.pipeline_exists(pipeline_id):
                    try:
                        _existing = _branch_store.load_pipeline(pipeline_id)
                        _terminal = {
                            _pkg.PipelineStatus.CANCELLED,
                            _pkg.PipelineStatus.FAILED,
                            _pkg.PipelineStatus.COMPLETE,
                        }
                        _has_active_pipeline = _existing.status not in _terminal
                    except Exception:
                        # If we can't load the pipeline, treat as no active pipeline
                        pass

                if _has_active_pipeline:
                    hint = ""
                    if pipeline_id:
                        hint = (
                            f" Use a qualifier to create a separate pipeline"
                            f" (e.g. '{pipeline_id}-<qualifier>')."
                        )
                    return _pkg.make_error_response(
                        f"Branch '{branch}' already exists on remote.{hint}",
                        status_code=409,
                        details={"reason": "branch_exists", "branch": branch},
                    )
                else:
                    # No active pipeline, but the branch may carry commits
                    # from a prior failed/cancelled run.  Inheriting that
                    # state was the precondition for #2222 (stale
                    # pipeline-branch tip + advanced main → contaminated
                    # PR via the push-reconcile fallback).  Compare the
                    # branch tip to the configured base; only a fresh
                    # branch (tip == base) is safe to silently reuse.
                    #
                    # Resolve the default branch via ``_detect_default_branch``
                    # rather than hardcoding ``"main"`` so repos whose default
                    # is ``master`` / ``develop`` still get the stale-branch
                    # check (otherwise the ``origin/main`` lookup returns
                    # ``None``, the guard falls through, and the precondition
                    # check is silently disabled).
                    _resolved_base = base_branch or _pkg._detect_default_branch(repo_path)
                    _branch_sha = gw.get_remote_branch_sha(
                        pipeline_id=pipeline_id or f"branch-check-{_pkg.uuid4().hex[:8]}",
                        repo_path=str(repo_path),
                        ref=f"refs/heads/{branch}",
                    )
                    _base_sha = gw.get_remote_branch_sha(
                        pipeline_id=pipeline_id or f"branch-check-{_pkg.uuid4().hex[:8]}",
                        repo_path=str(repo_path),
                        ref=f"refs/heads/{_resolved_base}",
                    )
                    # When either lookup returns ``None`` the stale-branch
                    # check is bypassed.  ``get_remote_branch_sha`` swallows
                    # transient gateway errors and returns ``None`` (same
                    # value it returns when the ref legitimately doesn't
                    # exist), so we surface a warning here to make the
                    # silent skip visible to operators investigating a
                    # post-merge contamination — rather than letting the
                    # precondition fix vanish behind a transient hiccup.
                    if _branch_sha is None or _base_sha is None:
                        _pkg.logger.warning(
                            "Stale-branch check skipped: SHA lookup returned None "
                            "(transient gateway error or ref missing — see #2222)",
                            branch=branch,
                            base_branch=_resolved_base,
                            branch_sha=_branch_sha,
                            base_sha=_base_sha,
                        )
                    if _branch_sha and _base_sha and _branch_sha != _base_sha:
                        _pkg.logger.warning(
                            "Branch exists with prior-pipeline commits — refusing reuse (#2222)",
                            branch=branch,
                            base_branch=_resolved_base,
                            branch_sha=_branch_sha,
                            base_sha=_base_sha,
                        )
                        cleanup_hint = (
                            f" Run cancel_task(task_id='{pipeline_id}', cleanup=true) "
                            "to delete the stale branch and pipeline state, then "
                            "resubmit."
                            if pipeline_id
                            else (
                                " Delete the stale branch and any associated "
                                "pipeline state, then resubmit."
                            )
                        )
                        return _pkg.make_error_response(
                            f"Branch '{branch}' exists with commits from a prior "
                            f"pipeline run (tip {_branch_sha[:8]} != "
                            f"origin/{_resolved_base} {_base_sha[:8]}). Starting a "
                            "new pipeline on top of it would inherit that history.",
                            status_code=409,
                            details={
                                "reason": "stale_branch",
                                "branch": branch,
                                "branch_sha": _branch_sha,
                                "base_sha": _base_sha,
                                "hint": cleanup_hint.strip(),
                            },
                        )
                    _pkg.logger.info(
                        "Branch exists but no active pipeline — allowing reuse",
                        branch=branch,
                        pipeline_id=pipeline_id,
                        branch_sha=_branch_sha,
                        base_sha=_base_sha,
                    )
        except Exception as e:
            # Non-fatal — if we can't reach the gateway, let creation proceed
            # and fail later on push.
            _pkg.logger.warning(
                "Branch existence check failed, proceeding anyway",
                branch=branch,
                error=str(e),
            )

    # Validate config before creating the pipeline so invalid config
    # returns a 400 instead of bubbling up as a 500.
    config = data.get("config")
    if config is not None:
        if isinstance(config, str):
            try:
                config = _pkg.json.loads(config)
            except _pkg.json.JSONDecodeError as e:
                return _pkg.make_error_response(f"Invalid config JSON: {e}")
        try:
            from models import PipelineConfig
            from pydantic import ValidationError

            PipelineConfig.model_validate(config)
        except ValidationError as e:
            errors = [
                {"field": ".".join(str(loc) for loc in err["loc"]), "message": err["msg"]}
                for err in e.errors()
            ]
            return _pkg.make_error_response(
                f"Invalid pipeline config: {errors}",
                details={"validation_errors": errors},
            )

    # Validate analysis/plan size before creating the pipeline.
    _MAX_DRAFT_LEN = 200_000
    for field_name in ("analysis", "plan"):
        value = data.get(field_name)
        if isinstance(value, str) and len(value) > _MAX_DRAFT_LEN:
            return _pkg.make_error_response(
                f"{field_name} exceeds maximum length ({len(value)} > {_MAX_DRAFT_LEN})"
            )

    # Issue #1557: epic detection. Before persisting, resolve
    # is_epic + pipeline_mode against the gateway when a jira_ticket
    # was supplied. Failures are non-fatal (the helper fails open) —
    # we surface them as warnings in the API response but always
    # proceed with the pipeline creation.
    epic_warnings: list[str] = []
    is_epic_resolved = False
    pipeline_mode_resolved: str | None = None
    if jira_ticket_arg:
        try:
            from jira_epic import resolve_epic_mode
        except ImportError:  # pragma: no cover - defensive
            try:
                from orchestrator.jira_epic import resolve_epic_mode  # type: ignore[no-redef]
            except ImportError:
                resolve_epic_mode = None  # type: ignore[assignment]
        if resolve_epic_mode is not None:
            try:
                is_epic_resolved, pipeline_mode_resolved, epic_warnings = resolve_epic_mode(
                    ticket=jira_ticket_arg,
                    epic_mode_arg=epic_mode_arg,
                )
            except Exception as exc:  # pragma: no cover - defensive
                _pkg.logger.warning(
                    "Epic detection raised; treating as non-epic",
                    pipeline_id=pipeline_id,
                    ticket=jira_ticket_arg,
                    error=str(exc),
                )
            # Both explicit overrides (``reassess`` and ``fresh``) against
            # a non-epic ticket are operator errors: the operator
            # specifically asked for epic-mode treatment but the ticket
            # doesn't qualify. Surface as HTTP 400 rather than the
            # silent demotion ``resolve_epic_mode`` returns
            # (is_epic=False with a warning). ``mode='auto'`` continues
            # to demote silently to standard ticket mode — that's the
            # whole point of auto.
            if epic_mode_arg in {"reassess", "fresh"} and not is_epic_resolved:
                return _pkg.make_error_response(
                    f"epic_mode={epic_mode_arg!r} but Jira ticket {jira_ticket_arg!r} is not an Epic",
                    status_code=400,
                    details={
                        "reason": f"{epic_mode_arg}_not_epic",
                        "warnings": epic_warnings,
                    },
                )

    # #3393 (multi-repo): enforce uniform visibility + auth across the run's
    # repos before creating the pipeline. Single-repo submissions are trivially
    # uniform and short-circuit without a gateway round-trip. Runs after the
    # gateway-ready gate above so the visibility lookup can reach the gateway.
    _uniform_repos = (
        [e["repo"] for e in repos_entries] if repos_entries else ([repo] if repo else [])
    )
    _uniformity_err = _pkg._assert_repo_set_uniform([r for r in _uniform_repos if r])
    if _uniformity_err:
        return _pkg.make_error_response(
            _uniformity_err,
            status_code=400,
            details={"reason": "non_uniform_repo_set"},
        )

    # Assemble the full list-shaped repo set persisted onto the Pipeline. The
    # primary (entries[0]) carries the resolved ``base_branch`` (detected above
    # when absent); secondary repos keep their submitted base_branch (None ⇒
    # auto-detected downstream). For a single-repo submission we leave
    # ``repos_specs`` as None and let the Pipeline validator synthesize a
    # one-element list from the legacy singleton (N=1 back-compat).
    repos_specs: list[_pkg.RepoSpec] | None = None
    if repos_entries is not None:
        repos_specs = [
            _pkg.RepoSpec(
                repo=entry["repo"],
                base_branch=(base_branch if idx == 0 else entry["base_branch"]),
            )
            for idx, entry in enumerate(repos_entries)
        ]

    try:
        store = _pkg.get_state_store(repo_path)
        pipeline = store.create_pipeline(
            issue_number=issue_number,
            repo=repo,
            branch=branch,
            base_branch=base_branch,
            repos=repos_specs,
            config=config,
            prompt=prompt,
            network_mode=network_mode,
            pipeline_id=pipeline_id,
            analysis=analysis,
            plan=plan,
            source_branch=source_branch,
            source_artifact_prefix=source_artifact_prefix,
            has_contract=True,
            jira_ticket=jira_ticket_arg,
            is_epic=is_epic_resolved,
            pipeline_mode=pipeline_mode_resolved,
        )

        # Contract creation is deferred to _run_pipeline so it writes
        # into the per-pipeline worktree instead of the main repo.

        # When state_store replaces a terminal pipeline with the same id
        # (state_store.create_pipeline:850), the in-memory consensus
        # tracker / message-store entries for the prior run survive. Same
        # for Redis-backed message-store entries across orchestrator
        # restarts. Clear here so the new run starts with empty consensus
        # state regardless of how the prior run ended (#2053).
        #
        # This is the *primary* eviction site for auto-FAILED prior runs,
        # not just a defensive backstop: paths like restart_agent spawn
        # failure call store.update_pipeline / store.save_pipeline directly
        # (bypassing PATCH), so the PATCH-site clear never fires for them.
        # Without this POST-site clear, those auto-FAILED pipelines would
        # leak consensus + message-store state into the next run that
        # reuses the id.
        _pkg._clear_pipeline_runtime_state(pipeline.id, reason="pipeline_create")

        _pkg.logger.info(
            "Pipeline created",
            pipeline_id=pipeline.id,
            issue_number=issue_number,
        )

        return _pkg.make_success_response(
            "Pipeline created",
            data={"pipeline": pipeline.model_dump(mode="json")},
        )

    except _pkg.StateStoreError as e:
        if "already exists" in str(e):
            # Include existing pipeline details so callers can decide
            # whether to cancel+resubmit or resume monitoring.
            details: dict[str, _pkg.Any] = {}
            try:
                # Derive pipeline ID using the same logic as state_store
                pid = pipeline_id or (f"issue-{issue_number}" if issue_number else None)
                if pid:
                    existing = store.load_pipeline(pid)
                    details = {
                        "existing_pipeline_id": existing.id,
                        "existing_status": existing.status.value,
                        "existing_phase": existing.current_phase.value,
                    }
            except Exception:
                pass  # Best-effort enrichment
            return _pkg.make_error_response(str(e), status_code=409, details=details)
        _pkg.logger.error("Failed to create pipeline", error=str(e))
        return _pkg.make_error_response(f"Failed to create pipeline: {e}", status_code=500)
    except Exception as e:
        # Catch non-StateStoreError exceptions (e.g., ValidationError,
        # OSError) that would otherwise produce a generic 500 from the
        # Flask error handler with no detail (#1396).
        _pkg.logger.error(
            "Unexpected error creating pipeline",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        msg = f"{type(e).__name__}: {e}"
        return _pkg.make_error_response(
            f"Failed to create pipeline: {msg[:500]}",
            status_code=500,
        )


def _update_pipeline_body(pipeline_id: str) -> tuple[_pkg.Response, int]:
    """
    Update a pipeline.

    URL params:
        pipeline_id: Pipeline ID

    Request body:
        {
            "status": "running",
            "current_phase": "plan",
            ...
        }

    Response:
        {
            "success": true,
            "data": {
                "pipeline": {...}
            }
        }
    """
    data = _pkg.request.get_json()
    if data is None:
        return _pkg.make_error_response("Missing request body")
    if not isinstance(data, dict):
        return _pkg.make_error_response("Request body must be a JSON object")

    repo_path = _pkg.get_repo_path()

    try:
        store, _pipeline = _pkg._resolve_pipeline(pipeline_id, repo_path)
        prev_status = _pipeline.status
        pipeline = store.update_pipeline(pipeline_id, data)

        # Emit the terminal event before kicking off cleanup so /status/wait
        # long-pollers wake immediately on cancellation rather than waiting
        # for the late-subscriber synth path on their next poll (#2663). The
        # run loop emits pipeline.completed / pipeline.failed from its own
        # terminal transitions; the PATCH path is the only place the
        # CANCELLED transition originates, so we emit it here. Gate on the
        # status *transition* (not equality) so idempotent retries against an
        # already-cancelled pipeline don't re-wake long-pollers.
        if (
            pipeline.status == _pkg.PipelineStatus.CANCELLED
            and prev_status != _pkg.PipelineStatus.CANCELLED
        ):
            _pkg._emit_pipeline_event(pipeline, "pipeline.cancelled")

        # If pipeline is being cancelled or failed, clean up containers
        # and cancel any pending decisions so wait_for_decision() unblocks.
        if pipeline.status in (_pkg.PipelineStatus.CANCELLED, _pkg.PipelineStatus.FAILED):
            try:
                dq = _pkg.get_decision_queue(pipeline_id, repo_path)
                pending = dq.get_pending_decisions()
                for decision in pending:
                    dq.cancel_decision(decision.id)
                if pending:
                    _pkg.logger.info(
                        "Cancelled pending decisions after pipeline status change",
                        pipeline_id=pipeline_id,
                        decisions_cancelled=len(pending),
                    )
            except Exception as e:
                _pkg.logger.warning(
                    "Failed to cancel pending decisions",
                    pipeline_id=pipeline_id,
                    error=str(e),
                )

            # Sync pipeline state: reload latest state (agents may have
            # written updates between status change and container cleanup),
            # mark all running records as stopped, and re-save.
            try:
                pipeline = _pkg._mark_pipeline_records_terminated(store, pipeline_id)
            except Exception as e:
                _pkg.logger.warning(
                    "Failed to sync pipeline state after termination",
                    pipeline_id=pipeline_id,
                    error=str(e),
                )
                # Reload pipeline so the response reflects current state
                # rather than the stale pre-cleanup object.
                try:
                    pipeline = store.load_pipeline(pipeline_id)
                except Exception:
                    pass  # Use stale pipeline if reload also fails

            # Move container/worktree cleanup to a background daemon thread
            # so the PATCH response returns immediately.  The DELETE handler
            # already re-runs cleanup_pipeline() as a safety net, so it will
            # catch anything the background thread hasn't finished.
            #
            # Compute the salvage mode + base branch up front (in the
            # request thread, where ``pipeline`` is still in scope) so the
            # background thread can pass them to ``cleanup_pipeline``
            # without re-loading state. Using the wrong mode here would
            # mismatch the policy the rest of the pipeline ran under and
            # the launcher-auth push could be rejected — see #2429
            # review.
            _bg_salvage_mode, _ = _pkg._compute_gateway_mode(pipeline)
            _bg_salvage_base_branch = pipeline.base_branch

            def _background_cleanup(pid: str, status_value: str) -> None:
                try:
                    spawner = _pkg._get_spawner()
                    # Preserve worktrees for CANCELLED pipelines so that
                    # restart_phase/restart_agent can resume with local
                    # committed work intact (see #1725).
                    removed = spawner.cleanup_pipeline(
                        pid,
                        force=True,
                        preserve_worktrees=(status_value == "cancelled"),
                        salvage_mode=_bg_salvage_mode,
                        salvage_base_branch=_bg_salvage_base_branch,
                    )
                    if removed > 0:
                        _pkg.logger.info(
                            "Cleaned up pipeline containers after status change",
                            pipeline_id=pid,
                            status=status_value,
                            containers_removed=removed,
                        )
                except (
                    _pkg.DockerClientError,
                    _pkg.DockerException,
                    _pkg.KubernetesClientError,
                ) as e:
                    _pkg.logger.warning(
                        "Failed to clean up pipeline containers",
                        pipeline_id=pid,
                        error=str(e),
                    )
                except Exception as e:
                    _pkg.logger.error(
                        "Unexpected error during pipeline container cleanup",
                        pipeline_id=pid,
                        error=str(e),
                        exc_info=True,
                    )

            cleanup_thread = _pkg.threading.Thread(
                target=_background_cleanup,
                args=(pipeline_id, pipeline.status.value),
                daemon=True,
                name=f"cleanup-{pipeline_id}",
            )
            cleanup_thread.start()

            # Evict per-pipeline runtime state (consensus tracker, legacy
            # consensus evaluator, message store) so a future pipeline
            # that reuses this id (same branch) does not inherit this
            # run's CONFIRMED consensus or message history (#2053).
            _pkg._clear_pipeline_runtime_state(
                pipeline_id, reason=f"pipeline_{pipeline.status.value}"
            )

        _pkg.logger.info("Pipeline updated", pipeline_id=pipeline_id)

        response_data = {"pipeline": pipeline.model_dump(mode="json")}
        if pipeline.status in (_pkg.PipelineStatus.CANCELLED, _pkg.PipelineStatus.FAILED):
            response_data["cleanup_pending"] = True

        return _pkg.make_success_response(
            "Pipeline updated",
            data=response_data,
        )

    except _pkg.InvalidPipelineIdError:
        return _pkg.make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except _pkg.PipelineNotFoundError:
        return _pkg.make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )
    except _pkg.StateValidationError as e:
        return _pkg.make_error_response(
            f"Invalid update: {e}",
            status_code=400,
        )


def _update_pipeline_config_body(pipeline_id: str) -> tuple[_pkg.Response, int]:
    """Update the safely-mutable subset of a live pipeline's config (#3174).

    Currently that subset is ``agent_models`` only. Semantics are a
    per-role merge with the pipeline's existing override map: roles
    absent from the request keep their current value, a string value
    sets that role's override, and an explicit ``null`` clears it (the
    role falls back to the repository default / built-in tiers).

    The updated config takes effect at the next agent spawn — currently
    running agents keep the model they were started with. Pair with
    ``restart_phase`` / ``restart_agent`` to apply the change to a
    running phase. Model *values* are not validated against a registry
    here (any non-Claude string routes to LiteLLM, mirroring submit-time
    behavior); a typo surfaces as a model-not-found error at spawn.

    URL params:
        pipeline_id: Pipeline ID

    Request body:
        {
            "agent_models": {
                "coder": "deepseek-v4-pro",
                "tester": null
            }
        }

    Response:
        {
            "success": true,
            "data": {
                "pipeline_id": "issue-123",
                "agent_models": {...},      # effective map after the merge
                "updated_roles": {...},     # roles set by this request
                "cleared_roles": [...]      # roles cleared by this request
            }
        }
    """
    data = _pkg.request.get_json()
    if data is None:
        return _pkg.make_error_response("Missing request body")
    if not isinstance(data, dict):
        return _pkg.make_error_response("Request body must be a JSON object")

    unsupported = sorted(set(data) - _pkg._MUTABLE_CONFIG_KEYS)
    if unsupported:
        return _pkg.make_error_response(
            f"Unsupported config keys: {unsupported}. This endpoint updates "
            f"only the safely-mutable config subset: {sorted(_pkg._MUTABLE_CONFIG_KEYS)}",
            status_code=400,
        )

    agent_models = data.get("agent_models")
    if not isinstance(agent_models, dict) or not agent_models:
        return _pkg.make_error_response(
            "agent_models must be a non-empty object mapping role -> model "
            "(use null as the model to clear a role's override)",
            status_code=400,
        )

    # Pre-validate role keys against MODEL_OVERRIDE_ROLES so the operator
    # gets the same actionable message as PipelineConfig's field validator
    # instead of a wrapped pydantic StateValidationError. Lazy import
    # mirrors models._validate_agent_models_roles.
    from egg_contracts.agent_roles import MODEL_OVERRIDE_ROLES

    valid_roles = {role.value for role in MODEL_OVERRIDE_ROLES}
    invalid_roles = sorted(role for role in agent_models if role not in valid_roles)
    if invalid_roles:
        return _pkg.make_error_response(
            f"Invalid agent_models role keys: {invalid_roles}. agent_models "
            f"is honored only for SDLC phase producer and reviewer roles: "
            f"{sorted(valid_roles)}",
            status_code=400,
        )
    invalid_values = sorted(
        role
        for role, model in agent_models.items()
        if model is not None and (not isinstance(model, str) or not model.strip())
    )
    if invalid_values:
        return _pkg.make_error_response(
            f"Invalid agent_models values for roles {invalid_values}: each "
            f"value must be a non-empty model string, or null to clear the "
            f"role's override",
            status_code=400,
        )

    repo_path = _pkg.get_repo_path()

    try:
        store, _pipeline = _pkg._resolve_pipeline(pipeline_id, repo_path)

        # Merge under the pipeline state lock so a concurrent writer
        # (another config update, the run loop persisting state) can't
        # interleave between our load and the store's load-modify-save.
        # The per-pipeline lock is an RLock, so update_pipeline's own
        # acquisition nests cleanly.
        with _pkg.get_pipeline_state_lock(pipeline_id):
            current = store.load_pipeline(pipeline_id)

            # Reject mutations on terminal pipelines (#3174 review). No future
            # spawn consumes ``agent_models`` once a pipeline is COMPLETE /
            # FAILED / CANCELLED, so the merge would be a silent no-op; a 409
            # gives the operator a clear signal and matches restart_phase's
            # terminal-state precondition style. Checked under the lock against
            # freshly-loaded state so a concurrent terminal transition can't
            # slip a mutation through.
            if current.status in _pkg.PipelineStatus.terminal():
                return _pkg.make_error_response(
                    f"Pipeline {pipeline_id} is in terminal state "
                    f"{current.status.value}; agent_models cannot be updated "
                    "(no future spawn would consume the change).",
                    status_code=409,
                )

            merged = dict(current.config.agent_models)
            updated_roles: dict[str, str] = {}
            cleared_roles: list[str] = []
            for role_key, model in agent_models.items():
                if model is None:
                    if merged.pop(role_key, None) is not None:
                        cleared_roles.append(role_key)
                else:
                    merged[role_key] = model.strip()
                    updated_roles[role_key] = model.strip()
            pipeline = store.update_pipeline(pipeline_id, {"config.agent_models": merged})

        _pkg.logger.info(
            "Pipeline agent_models updated",
            pipeline_id=pipeline_id,
            updated_roles=updated_roles,
            cleared_roles=cleared_roles,
        )

        return _pkg.make_success_response(
            "Pipeline config updated",
            data={
                "pipeline_id": pipeline.id,
                "agent_models": pipeline.config.agent_models,
                "updated_roles": updated_roles,
                "cleared_roles": cleared_roles,
            },
        )

    except _pkg.InvalidPipelineIdError:
        return _pkg.make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except _pkg.PipelineNotFoundError:
        return _pkg.make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )
    except _pkg.StateValidationError as e:
        return _pkg.make_error_response(
            f"Invalid update: {e}",
            status_code=400,
        )


def _delete_pipeline_body(pipeline_id: str) -> tuple[_pkg.Response, int]:
    """
    Delete a pipeline.

    URL params:
        pipeline_id: Pipeline ID

    Response:
        {
            "success": true,
            "message": "Pipeline deleted"
        }
    """
    repo_path = _pkg.get_repo_path()

    try:
        store, _pipeline = _pkg._resolve_pipeline(pipeline_id, repo_path)

        # Clean up any running containers for this pipeline
        try:
            spawner = _pkg._get_spawner()
            # Pass the running pipeline's gateway mode + base branch so the
            # auto-salvage hook in cleanup_pipeline pushes recovery refs
            # under the same policy the pipeline ran under (#2429 review).
            _delete_salvage_mode, _ = _pkg._compute_gateway_mode(_pipeline)
            removed = spawner.cleanup_pipeline(
                pipeline_id,
                force=True,
                salvage_mode=_delete_salvage_mode,
                salvage_base_branch=_pipeline.base_branch,
            )
            if removed > 0:
                _pkg.logger.info(
                    "Cleaned up pipeline containers",
                    pipeline_id=pipeline_id,
                    containers_removed=removed,
                )
        except (_pkg.DockerClientError, _pkg.DockerException, _pkg.KubernetesClientError) as e:
            _pkg.logger.warning(
                "Failed to clean up pipeline containers",
                pipeline_id=pipeline_id,
                error=str(e),
            )
        except Exception as e:
            _pkg.logger.error(
                "Unexpected error during pipeline container cleanup",
                pipeline_id=pipeline_id,
                error=str(e),
                exc_info=True,
            )

        # Clean up remote branches (best-effort)
        try:
            _pkg._cleanup_remote_branches(pipeline_id, _pipeline, repo_path)
        except Exception as e:
            _pkg.logger.warning(
                "Failed to clean up remote branches",
                pipeline_id=pipeline_id,
                error=str(e),
            )

        # Clean up the message store stream/counters AND the in-memory
        # consensus tracker / legacy evaluator so a fresh pipeline that
        # later reuses this id starts with empty consensus state (#2053).
        _pkg._clear_pipeline_runtime_state(pipeline_id, reason="pipeline_delete")

        store.delete_pipeline(pipeline_id)

        _pkg.logger.info("Pipeline deleted", pipeline_id=pipeline_id)

        return _pkg.make_success_response("Pipeline deleted")

    except _pkg.InvalidPipelineIdError:
        return _pkg.make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except _pkg.PipelineNotFoundError:
        return _pkg.make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )
