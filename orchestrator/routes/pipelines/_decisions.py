"""HITL + divergence-reconcile decision helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _format_nack_summary(nack_details: list[dict]) -> str:
    """Format unresolved NACK details into a human-readable summary string."""
    return "; ".join(
        f"{n['reviewer']} NACKed {n['producer']}: {n.get('reason') or 'no reason given'}"
        for n in nack_details
    )


def _incomplete_consensus_decision_text(
    final_consensus: dict,
    container_failure_count: int,
    orchestrator_mode: bool = False,
) -> tuple[str, str]:
    """Build (question, log_suffix) for incomplete-consensus HITL escalation.

    Distinguishes the two failure modes — unresolved NACKs vs. agents that
    never confirmed — so the operator sees actionable detail in `/sdlc`.

    ``orchestrator_mode`` selects a mode-aware prefix: when the orchestrator
    owns the event loop, no up-front containers ever ran, so the terminal
    here is the consensus timeout, not container exit — the "All containers
    exited" prefix would mislead an operator reading `/sdlc`.
    """
    nacks = final_consensus.get("unresolved_nacks", []) or []
    blocking = final_consensus.get("blocking_agents", []) or []
    if container_failure_count:
        prefix = f"{container_failure_count} container(s) exited with non-zero code; "
    elif orchestrator_mode:
        prefix = "Consensus timed out; "
    else:
        prefix = "All containers exited; "
    # Retry semantics must match what "Retry phase" actually executes on
    # resolve — the restart_phase route (#3421 dispatch, #3080 preservation
    # semantics): fresh worktrees re-fork from the shared work branch tip,
    # and unpushed per-role commits survive only via best-effort salvage.
    retry_copy = (
        "'Retry phase' re-runs the phase from the shared work branch tip "
        "(work pushed to the shared branch is preserved; unpushed per-role "
        "commits are salvaged best-effort to egg/recovered/*)."
    )
    if nacks:
        summary = _pkg._format_nack_summary(nacks)
        question = (
            f"{prefix}consensus incomplete with {len(nacks)} unresolved NACK(s): "
            f"{summary}. {retry_copy} How to proceed?"
        )
        log_suffix = f"\n--- INCOMPLETE CONSENSUS / UNRESOLVED NACKs ({len(nacks)}) ---\n{summary}"
    else:
        agent_list = ", ".join(blocking) if blocking else "unknown"
        question = (
            f"{prefix}consensus incomplete; agents never confirmed: {agent_list}. "
            f"{retry_copy} How to proceed?"
        )
        log_suffix = (
            f"\n--- INCOMPLETE CONSENSUS / NO CONFIRMATION ---\nblocking_agents={agent_list}"
        )
    return question, log_suffix


def _persist_hitl_decision(
    pipeline_id: str,
    pipeline: _pkg.Pipeline,
    store: _pkg.StateStore,
    *,
    question: str,
    options: list[str],
    phase: _pkg.PipelinePhase | None = None,
    context: str | None = None,
):
    """Create and persist an HITL decision under the pipeline state lock.

    `pipeline.add_decision()` only mutates an in-memory object.  The caller
    of `_run_concurrent_phase` reloads the pipeline fresh from disk before
    writing FAILED, so any in-memory decision is silently dropped — the
    on-disk state (which `/sdlc` reads via `pipeline.get_pending_decisions()`)
    never sees it.  This helper mirrors the *persistence half* of
    `DecisionQueue.queue_decision()` and the HITL-gate write at
    pipelines.py:13080-13089: load → mutate → save under the reentrant
    pipeline state lock.  Note: it intentionally does **not** invoke
    `_notify_handlers` — no production code currently registers a
    `DecisionHandler` and `/sdlc` reads from disk on each request, so
    notifications are not needed for the issue-2203 path.  The in-memory
    `pipeline` argument is also synced so callers observe consistent state.

    ``context`` is set on the persisted decision before save so dispatch
    handlers in :mod:`routes.decisions` can route on a stable string
    discriminator rather than the prose-y ``question`` text (see the
    ``failed_role:`` pattern).

    Returns the created decision, or None if persistence failed (logged;
    callers should not raise — losing an HITL decision is bad but losing
    the rest of the cleanup path is worse).
    """
    try:
        with _pkg.get_pipeline_state_lock(pipeline_id):
            disk_pipeline = store.load_pipeline(pipeline_id)
            decision = disk_pipeline.add_decision(
                question=question,
                options=options,
                phase=phase or disk_pipeline.current_phase,
            )
            if context is not None:
                decision.context = context
            store.save_pipeline(disk_pipeline)
        # Defensive copy: avoid sharing the list reference with the
        # disk-loaded copy, which is local and goes out of scope.
        pipeline.decisions = list(disk_pipeline.decisions)
        return decision
    except Exception:
        _pkg.logger.warning(
            "Failed to persist HITL decision",
            pipeline_id=pipeline_id,
            question=question[:100],
            exc_info=True,
        )
        return None


def _cancel_consensus_timeout_decisions(pipeline: _pkg.Pipeline) -> int:
    """Cancel any pending consensus-timeout HITL on ``pipeline`` (#3315 facet c).

    Pure mutator (no lock / load / save): marks every pending
    ``consensus_timeout_incomplete`` decision ``CANCELLED`` with an
    auto-withdrawal note and returns how many it cancelled.  Called from the
    consensus-success path (under the pipeline state lock, on the freshly
    loaded pipeline that is about to be saved) so a stale forced-choice a
    *superseded* thread opened before the phase converged is withdrawn in the
    same write that marks the agents COMPLETE — the operator is never left
    disposing of a decision the system already obsoleted by converging.
    """
    withdrawn = 0
    for decision in pipeline.get_pending_decisions():
        if decision.context != _pkg._CONSENSUS_TIMEOUT_HITL_CONTEXT:
            continue
        decision.status = _pkg.DecisionStatus.CANCELLED
        decision.resolution = "auto-withdrawn: consensus subsequently converged"
        decision.resolved_at = _pkg.datetime.now(_pkg.UTC)
        withdrawn += 1
    return withdrawn


def _find_pending_divergence_reconcile_decision(pipeline: _pkg.Pipeline):
    """Return the oldest pending reconcile HITL on ``pipeline`` (or None).

    Used by the non-blocking ``populate_contract`` route to dedupe re-POSTs
    against a pipeline already paused on a reconcile HITL — without this,
    every retry would append a fresh decision and bloat ``pipeline.decisions``
    (the abort path still works on the most recent decision; this is a UX /
    cleanliness fix, not a correctness fix).
    """
    for decision in pipeline.get_pending_decisions():
        if decision.context == _pkg._DIVERGENCE_RECONCILE_HITL_CONTEXT:
            return decision
    return None


def _divergence_reconcile_is_abort(resolution: str) -> bool:
    """True when a reconcile-HITL resolution selects abort (#2979).

    Accepts the canonical ``Abort pipeline`` label, a couple of forgiving
    synonyms, and the JSON ``{"action": ...}`` envelope the collaborator
    UI sends.  Any *other* resolution — the resume label, free text, an
    empty string — is treated as "Reconciled — resume", so an ambiguous
    resolution errs toward re-attempting the (now non-destructive) sync
    rather than failing the pipeline.
    """
    r = resolution.strip()
    if not r:
        return False
    try:
        payload = _pkg.json.loads(r)
        if isinstance(payload, dict) and "action" in payload:
            r = str(payload["action"])
    except _pkg.json.JSONDecodeError, TypeError:
        pass
    return r.strip().lower() in {
        _pkg._DIVERGENCE_RECONCILE_ABORT.lower(),
        "abort",
        "cancel",
    }


def _divergence_reconcile_hitl_question(
    *,
    pipeline_id: str,
    phase: _pkg.PipelinePhase | None,
    backup_ref: str | None,
    local_only_commit_shas: tuple[str, ...] | list[str],
    rebase_category: str | None = None,
    rebase_detail: str | None = None,
) -> str:
    """Build the HITL question for the non-destructive divergence pause (#2979).

    The worktree diverged from origin and the rebase autoresolve could
    not reconcile it.  Nothing has been discarded — the autoresolve
    aborted back to the clean local HEAD, so the orchestrator's committed
    work is intact — and the pipeline is paused (AWAITING_HUMAN, not
    FAILED).  The operator reconciles the orchestrator-side worktree
    manually, then either resumes (the sync re-runs and the phase's
    post-processing continues from where it paused) or aborts.

    ``rebase_category`` / ``rebase_detail`` name the actual autoresolve
    failure (conflicting paths, rebase argv, git output excerpt) so the
    operator can judge the pause from the decision alone (#3416).
    """
    phase_label = phase.value if phase is not None else "current phase"
    if rebase_category or rebase_detail:
        failure_label = rebase_category or "unknown failure"
        failure_line = (
            f"({failure_label}: {rebase_detail})" if rebase_detail else f"({failure_label})"
        )
    else:
        failure_line = (
            "(failure detail unavailable — see the divergence_rebase_failed "
            "log line for the rebase output)"
        )
    backup_line = (
        f"A backup ref pins the current tip: {backup_ref} (inspect with `git log {backup_ref}`)."
        if backup_ref
        else "Backup ref write failed — see the WARN log for the inlined commit SHAs."
    )
    if local_only_commit_shas:
        commits_block = "Local-only commits preserved on the worktree HEAD:\n  - " + "\n  - ".join(
            local_only_commit_shas
        )
    else:
        commits_block = (
            "The local-only commit list could not be enumerated; check the "
            "WARN log and the backup ref for the exact set."
        )
    return (
        f"Pipeline {pipeline_id}: the worktree diverged from origin at the "
        f"{phase_label} boundary and the rebase autoresolve could not "
        f"reconcile it {failure_line}. "
        f"Nothing was discarded — the worktree is left at the local HEAD "
        f"with the orchestrator's committed work intact, and the pipeline "
        f"is paused (not failed) for a manual reconcile (#2979). "
        f"{backup_line}\n{commits_block}\n\n"
        f"Reconcile the orchestrator-side worktree manually (e.g. rebase the "
        f"local commits onto origin/<branch> and resolve the conflict), then "
        f"choose:\n"
        f"- '{_pkg._DIVERGENCE_RECONCILE_RESUME}' — re-run the worktree sync and "
        f"resume the {phase_label} phase's post-processing from where it "
        f"paused (no full phase re-run).\n"
        f"- '{_pkg._DIVERGENCE_RECONCILE_ABORT}' — fail the pipeline; the backup "
        f"ref preserves the commits for offline inspection."
    )
