"""implement-phase support (lifted closures) helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _commit_and_push_slice_statefiles_impl(
    message: str,
    *,
    pipeline_id,
    worktree_repo_path,
    pipeline,
    store,
    spawner,
    gateway_mode,
    issue_number,
) -> None:
    """Commit + push pipeline-scoped ``.egg-state/`` writes to the work branch.

    Contract mutations — agent task-record updates via
    ``mutate_contract`` and the ``slice.status`` flips below — land
    on the shared pipeline worktree's disk copy only. Without a
    slice-boundary commit, the work branch's contract file stays
    frozen at the init-time "Initialize SDLC contract" commit for
    the entire implement phase, and a mid-phase orchestrator crash
    or worktree prune loses every accumulated task record (#3117).
    The phase-boundary commit at the end of the run loop is too
    coarse for multi-slice phases.

    Scope (per #3117): this closes durability for the post-prune
    audit record, operator/PR-side review of mid-phase contract
    state, and orchestrator-restart resume at slice granularity.
    It is deliberately NOT the read path for live agents — agents
    read the contract via ``mcp__sdlc__show_contract`` against the
    orchestrator's in-memory state, never from their checkout's
    ``.egg-state/contracts/`` file (#3077).

    Best-effort: slice completion must not block on statefile
    durability; failures are logged and the next boundary (later
    slice close or phase completion) carries the writes. The commit
    runs under the per-pipeline state lock to serialise concurrent
    slice-close threads against the shared worktree's git index;
    the push runs outside the lock. The expected case is a linear
    fast-forward (lock-serialised commits stack), and a no-op FF
    of the same SHA from two threads is harmless. The residual
    hazard is ``_reconcile_and_retry_push`` on a non-FF rejection
    (``gateway_client.py:1361``): two threads both fetching+rebasing
    in the shared worktree can interleave ``.git/index.lock``.
    Within the implement phase no other writer pushes to
    ``pipeline.branch`` so non-FF shouldn't fire in normal
    operation; an external push (operator hand-fix, stale
    concurrent orchestrator) is the only known trigger.
    """
    try:
        with _pkg.get_pipeline_state_lock(pipeline_id):
            committed = _pkg._commit_statefiles_to_worktree(
                worktree_repo_path,
                message,
                _pkg._pipeline_identifier(issue_number, pipeline_id),
                pipeline_id=pipeline_id,
            )
    except Exception as commit_err:  # noqa: BLE001
        # The helper raises CalledProcessError / TimeoutExpired
        # from subprocess.run and OSError from glob (#2219 family).
        _pkg.logger.warning(
            "Failed to commit slice statefiles to work branch (continuing) (#3117)",
            pipeline_id=pipeline_id,
            commit_message=message,
            error=str(commit_err),
        )
        return
    if not committed or not pipeline.branch or worktree_repo_path == store.repo_path:
        return
    try:
        spawner.gateway.push_worktree_branch(
            pipeline_id=pipeline_id,
            repo_path=str(worktree_repo_path),
            branch=pipeline.branch,
            mode=gateway_mode,  # type: ignore[arg-type]
            base_branch=pipeline.base_branch,
        )
    except Exception as push_err:  # noqa: BLE001
        # Gateway HTTP push (GatewayError / OSError). The commit is
        # already on the local work branch; the next successful
        # push carries it.
        _pkg.logger.warning(
            "Failed to push slice statefiles to work branch (continuing) (#3117)",
            pipeline_id=pipeline_id,
            commit_message=message,
            error=str(push_err),
        )


def _persist_slice_status_complete_impl(
    slice_id: str,
    *,
    pipeline_id,
    worktree_repo_path,
    commit_and_push,
    pr_number: int | None = None,
    pr_url: str | None = None,
    basis: str | None = None,
    commit_to_branch: bool = True,
) -> None:
    """Mark ``slice_id`` as ``SliceStatus.COMPLETE`` on the contract.

    Durable signal so the bootstrap reconciliation pass below and
    the ``restart_agent`` parent-complete fallback can skip the
    slice without a GitHub round-trip (#2549, #2470). Best-effort:
    on save failure the in-memory scheduler state still reflects
    completion for this pass and the next ``start_pipeline``
    re-detects via the merged-detection helper.

    With *commit_to_branch* (the default), the saved contract —
    along with any other uncommitted pipeline statefiles, e.g.
    agent task-record mutations made during the slice — is
    committed and pushed to the pipeline work branch so the durable
    copy tracks the live one (#3117). The bootstrap reconciliation
    passes set it to ``False`` and batch a single commit after the
    loop instead of one per reconciled slice.

    Called only after a slice successfully closes (BRC consensus
    reached + PR opened, or merged-skip / bootstrap-COMPLETE
    reconciliation). Failed slices — ``exit_code_inner != 0``
    (#16410) or ``pr_created == False`` (#16588) — return early
    without calling this helper, so their accumulated task-record
    mutations remain uncommitted in the worktree until the next
    successful slice's commit (the pipeline-scoped glob picks them
    up) or the phase-boundary commit, whichever fires first.

    ``basis`` lets a caller declare *why* the slice is complete when
    not every task is marked COMPLETE on the contract: ``"merged"``
    (integration branch ancestry-verified merged into its parent) or
    ``"consensus_complete"`` (BRC consensus reached pre-restart, PR
    not yet opened). The PR-open caller passes ``pr_number`` instead.
    Absent any of these — and with tasks still pending — the write is
    a #3214 false-complete and :func:`_validate_slice_completion_basis`
    raises :class:`SliceCompletionInvariantError` rather than persist
    a slice as done that never ran.

    When the caller just opened the slice's PR it passes
    ``pr_number`` / ``pr_url`` so the linkage lands in the same
    contract write (#3122) — the context-PR body refresh and any
    later stack consumer read them from ``Slice.pr_number``.
    ``None`` (the merged-skip and bootstrap callers) leaves any
    previously recorded linkage untouched.

    TODO(#3122): the three ``None`` callers — bootstrap layer-A
    (contract-recorded COMPLETE), bootstrap layer-B (merged on
    origin), and the run-loop merged-skip — do not recover the
    slice PR number from GitHub (`gh pr list --head … --state
    merged`), so on a resume past those points the slice-table
    entries for merged slices stay unlinked. Acceptable for v1
    because the per-slice ``— #N`` link is most useful while the
    stack is live, but worth backfilling if reviewers ask for
    complete cross-linkage on archived stacks.
    """
    from egg_contracts.loader import load_contract, save_contract
    from egg_contracts.models import SliceStatus

    try:
        with _pkg.get_pipeline_state_lock(pipeline_id):
            contract_local = load_contract(pipeline_id, worktree_repo_path)
            for s in contract_local.slices:
                if s.id == slice_id:
                    # #3214 — refuse to persist a contradictory COMPLETE.
                    # An interior forest node marked COMPLETE without a
                    # valid basis (tasks pending, no PR, no verified
                    # merge/consensus) skips a slice that never ran and
                    # wedges the chain a phase later. Fail loud here, at
                    # the source of the bad write, instead.
                    invalid = _pkg._validate_slice_completion_basis(
                        s, pr_number=pr_number, basis=basis
                    )
                    if invalid is not None:
                        _pkg.logger.error(
                            "Refusing to persist slice.status=COMPLETE — "
                            "invalid completion basis (#3214)",
                            pipeline_id=pipeline_id,
                            slice_id=slice_id,
                            reason=invalid,
                        )
                        raise _pkg.SliceCompletionInvariantError(invalid)
                    s.status = SliceStatus.COMPLETE
                    if pr_number is not None:
                        s.pr_number = pr_number
                    if pr_url is not None:
                        s.pr_url = pr_url
                    _pkg.logger.info(
                        "Slice marked COMPLETE",
                        pipeline_id=pipeline_id,
                        slice_id=slice_id,
                        basis=(
                            basis
                            or (
                                "pr"
                                if (pr_number is not None or s.pr_number is not None)
                                else "tasks_complete"
                            )
                        ),
                        pr_number=pr_number if pr_number is not None else s.pr_number,
                    )
                    break
            save_contract(contract_local, worktree_repo_path)
    except _pkg.SliceCompletionInvariantError:
        # Fail loud — never swallow the completion invariant into the
        # best-effort save handler below (#3214).
        raise
    except Exception as save_err:  # noqa: BLE001
        # Contract load/save under per-pipeline state lock.
        # Catches loader validation errors, atomic-rename / fdopen
        # I/O failures, and pydantic re-serialisation errors.
        # Best-effort: the in-memory scheduler still reflects
        # COMPLETE for this pass; next start_pipeline re-detects.
        _pkg.logger.warning(
            "Failed to persist slice.status=COMPLETE",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            error=str(save_err),
        )
        return
    if commit_to_branch:
        commit_and_push(f"Persist contract after slice {slice_id} completion (#3117)")


def _contract_loader_impl(*, pipeline_id, worktree_repo_path) -> _pkg.Any:
    from egg_contracts.loader import load_contract

    try:
        return load_contract(pipeline_id, worktree_repo_path)
    except Exception:  # noqa: BLE001
        # Best-effort loader for callers that just need "current
        # contract or None". Catches loader validation errors,
        # OSError on the contract file read, and any pydantic
        # re-serialisation failure.
        return None


def _slice_close_evidence_gate(
    pipeline_id,
    spawner,
    worktree_repo_path,
    slice_id,
    integration_branch,
    *,
    gateway_mode,
    pipeline,
) -> tuple[_pkg.Any | None, str | None]:
    """Load the contract once and run the slice-close evidence gate (#3125).

    Returns ``(contract, evidence_failure)``. The contract is loaded
    ONCE under the per-pipeline state lock and reused for both the
    evidence-reachability gate and the caller's slice PR data snapshot
    (both readers previously took the lock independently, and
    collapsing them saves one file read + lock acquire per slice close,
    #3125 review). The lock covers only the read; it is released
    before the gateway round-trips inside the gate so other writers
    are not serialised. ``contract`` may be ``None`` when the load
    fails; the gate falls back to its own load in that case (and
    skips gracefully if that fails too).

    The gate itself (``_check_slice_evidence_reachability``): every
    commit SHA cited by this slice's contract task records must be an
    ancestor of the integration branch tip, or the slice PR would ship
    without a deliverable the task record claims is done (the
    post-confirmation ``complete-task --commit`` unblock flow, #3124).
    It runs BEFORE any close side effect. Only repo-backed pipelines
    are gated; ``evidence_failure`` is ``None`` otherwise.

    On a definitive failure this helper lands an unresolved HITL
    Decision on the contract (#3572) before returning: the caller's
    ``record_failure`` only arms the descendant cascade, nothing
    re-drives the close, and a consensus-complete slice otherwise
    parks silently until an operator notices the failed phase.
    """
    contract_post: _pkg.Any | None = None
    try:
        from egg_contracts.loader import load_contract

        with _pkg.get_pipeline_state_lock(pipeline_id):
            contract_post = load_contract(pipeline_id, worktree_repo_path)
    except Exception as load_err:  # noqa: BLE001
        _pkg.logger.warning(
            "Slice close: contract load failed (continuing) (#3125)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            error=str(load_err),
        )

    if not pipeline.repo:
        return contract_post, None

    evidence_failure = _pkg._check_slice_evidence_reachability(
        pipeline_id,
        spawner,
        worktree_repo_path,
        slice_id,
        integration_branch,
        gateway_mode=gateway_mode,
        contract=contract_post,
    )
    if evidence_failure is not None:
        _pkg._escalate_evidence_gate_to_hitl(
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            failure=evidence_failure,
            worktree_repo_path=worktree_repo_path,
            current_phase=getattr(pipeline, "current_phase", None),
        )
    return contract_post, evidence_failure
