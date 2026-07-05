"""HITL phase-rerun + iteration context helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _build_phase_iteration_context(
    operator_directives: list[_pkg.OperatorDirective] | None,
    iteration_history: list[_pkg.IterationSummary] | None,
) -> str:
    """Render operator directives + prior iteration history as a prompt section.

    Issued in iteration N+1 prompts (for **both** producers and reviewers)
    after one or more HITL phase-gate kickbacks. Replaces the unstructured
    ``## Review Feedback`` rendering that previously squatted on the
    agentic-cycle feedback channel — operator directives now have their own
    section with explicit precedence prose so reviewers cannot faithfully
    NACK a directive-driven change against a stale default rubric (#2795).

    Returns an empty string when there are no directives and no history
    so the caller can unconditionally append the result.
    """
    directives = operator_directives or []
    history = iteration_history or []
    if not directives and not history:
        return ""

    lines: list[str] = ["## Phase Iteration Context\n"]
    if directives:
        lines.append(
            "The operator has kicked this phase back through HITL one or "
            "more times. The directives below **override prompt-template "
            "defaults**. If a rubric item in your role's instructions "
            "conflicts with a directive, the directive wins. Later "
            "directives override earlier ones.\n"
        )
        lines.append("### Operator Directives (chronological)\n")
        for idx, directive in enumerate(directives, start=1):
            ts = directive.created_at.isoformat()
            lines.append(f"**Directive {idx}** (iteration {directive.iteration_n}, {ts}):")
            lines.append("")
            lines.append(directive.feedback_text.rstrip())
            lines.append("")

    if history:
        lines.append("### Prior Iteration History\n")
        lines.append(
            "Each entry below is a frozen snapshot of a previously kicked-"
            "back iteration's BRC outcome — what the reviewers concluded "
            "and why. Use it to see which rubric items tripped last round "
            "so you do not repeat the same NACKs.\n"
        )
        for summary in history:
            ts = summary.completed_at.isoformat()
            lines.append(f"**Iteration {summary.iteration_n}** (completed {ts}):")
            if summary.final_proposal_commit:
                # SHAs are pre-filtered by _build_iteration_summary_from_tracker
                # (empty + RECONSTRUCTED_NO_SHA dropped before the dict is
                # populated), so every value here is a real commit.
                commit_parts = [
                    f"{producer}={sha[:12]}"
                    for producer, sha in sorted(summary.final_proposal_commit.items())
                ]
                lines.append(f"- Final proposal commits: {', '.join(commit_parts)}")
            if summary.verdict_matrix:
                verdicts = "; ".join(
                    f"{edge}: {state}" for edge, state in sorted(summary.verdict_matrix.items())
                )
                lines.append(f"- Verdict matrix: {verdicts}")
            if summary.nack_reasons:
                lines.append(f"- NACK reasons ({len(summary.nack_reasons)}):")
                for reason in summary.nack_reasons:
                    lines.append(f"  - {reason}")
            if summary.artifacts_snapshot:
                arts = ", ".join(sorted(summary.artifacts_snapshot.keys()))
                lines.append(f"- Artifacts at iteration close: {arts}")
            lines.append("")

    return "\n".join(lines)


def _build_iteration_summary_from_tracker(
    tracker: _pkg.Any,
    iteration_n: int,
    artifacts: dict[str, str] | None = None,
    completed_at: _pkg.datetime | None = None,
) -> _pkg.IterationSummary:
    """Capture an :class:`IterationSummary` from a live BRC tracker.

    Called by the HITL kickback handler **before** ``_clear_concurrent_state``
    wipes the tracker so the iteration N+1 prompt can render what tripped
    iteration N. Tolerates a ``None`` tracker — returns a summary with only
    the iteration index + completion timestamp populated, which still lets
    downstream prompts mention that a kickback occurred without claiming
    false verdict detail.
    """
    completion = completed_at or _pkg.datetime.now(_pkg.UTC)
    summary = _pkg.IterationSummary(
        iteration_n=iteration_n,
        completed_at=completion,
        artifacts_snapshot=dict(artifacts or {}),
    )
    if tracker is None:
        return summary

    try:
        matrix = getattr(tracker, "matrix", None)
        if matrix is None:
            return summary
        # Snapshot the matrix entries + commit SHAs under the tracker's
        # lock so concurrent mutations from a still-live tracker can't
        # tear the read. RLock means re-entry is safe if callers already
        # hold it. Iteration below runs on the local copies.
        lock = getattr(tracker, "_lock", None)
        commits_snapshot: dict[str, str] = {}
        if lock is not None:
            with lock:
                entries_snapshot = list(getattr(matrix, "_entries", {}).items())
                commits_snapshot = dict(getattr(tracker, "_proposal_commit_shas", {}))
        else:
            entries_snapshot = list(getattr(matrix, "_entries", {}).items())
            commits_snapshot = dict(getattr(tracker, "_proposal_commit_shas", {}))

        verdict_matrix: dict[str, str] = {}
        nack_reasons: list[str] = []
        for (reviewer, producer), entry in entries_snapshot:
            state = getattr(entry, "state", None)
            state_val = state.value if state is not None else "unknown"
            verdict_matrix[f"{reviewer}->{producer}"] = state_val
            if state_val == "nacked" and getattr(entry, "reason", ""):
                nack_reasons.append(f"{reviewer}→{producer}: {entry.reason}")
        summary.verdict_matrix = verdict_matrix
        summary.nack_reasons = nack_reasons

        producers = {producer for _, producer in (k for k, _ in entries_snapshot)}
        commits: dict[str, str] = {}
        for producer in producers:
            sha = commits_snapshot.get(producer, "")
            if sha and sha != "RECONSTRUCTED_NO_SHA":
                commits[producer] = sha
        summary.final_proposal_commit = commits
    except Exception as e:  # noqa: BLE001
        _pkg.logger.debug(
            "Failed to snapshot iteration summary from tracker",
            iteration_n=iteration_n,
            error=str(e),
        )
    return summary


def _apply_inline_hitl_kickback_to_phase(
    phase_execution: _pkg.PhaseExecution,
    revision_feedback: str,
    tracker: _pkg.Any = None,
) -> list[_pkg.ContainerInfo]:
    """Apply the inline HITL kickback's phase-state mutations.

    Extracted from the inline ``request_changes`` handler so tests can
    drive the assertion through production code rather than constructing
    a fixture by hand (#2795 review). The caller is still responsible for
    the wrapping concerns: clearing the message store + consensus tracker
    via ``_clear_concurrent_state``, persisting the pipeline via
    ``store.save_pipeline``, and stopping the stale containers returned
    here (the K8s delete is asynchronous so an explicit stop is required
    to avoid iteration N+1 racing iteration N's still-terminating pods).

    Returns the snapshot of containers that were running at kickback
    time, for the caller to issue the defensive stop on.
    """
    # Monotone across the legacy-hitl_feedback migration boundary: a
    # pre-#2795 phase migrates with iteration_history empty but a
    # synthetic OperatorDirective carrying iteration_n derived from
    # hitl_review_cycles. ``len(iteration_history)`` alone would
    # restart at 0 and label two distinct iterations identically; use
    # one past the maximum existing directive index as the floor so
    # the displayed "iteration X" labels stay monotone.
    iteration_n = max(
        len(phase_execution.iteration_history),
        max(
            (d.iteration_n for d in phase_execution.operator_directives),
            default=-1,
        )
        + 1,
    )
    phase_execution.operator_directives.append(
        _pkg.OperatorDirective(
            iteration_n=iteration_n,
            feedback_text=revision_feedback,
        )
    )
    phase_execution.iteration_history.append(
        _pkg._build_iteration_summary_from_tracker(
            tracker,
            iteration_n=iteration_n,
            artifacts=phase_execution.artifacts,
        )
    )
    stale_containers = list(phase_execution.containers)
    phase_execution.containers = []
    phase_execution.agents = []
    phase_execution.artifacts = {}
    phase_execution.review_cycles = 0
    return stale_containers


def _broadcast_hitl_nonconvergence_alert(
    pipeline_id: str,
    pipeline: _pkg.Pipeline,
    current_phase: _pkg.PipelinePhase,
    cycles: int,
    threshold: int,
) -> None:
    """Non-fatal overseer alert when the HITL converge loop runs long (#3392).

    The converge-before-advance loop is human-gated every round (the
    operator resolves decisions before each re-run), so a long-running loop
    cannot burn compute silently and is never force-advanced. After
    ``threshold`` rounds we surface an ``OVERSEER_ALERT`` so a pathological
    non-convergence — a real carry-forward bug, or a genuinely churning
    design — is visible. Best-effort: a broadcast failure never blocks the
    re-run.
    """
    try:
        from message_store import Message, MessageType

        store_fn = _pkg._get_message_store()
        if store_fn is None:
            return
        msg_store = store_fn()
        phase = current_phase.value if current_phase else None
        msg_store.add_message(
            Message(
                pipeline_id=pipeline_id,
                from_role="orchestrator",
                to_role="all",
                message_type=MessageType.OVERSEER_ALERT,
                subject="hitl_nonconvergence: orchestrator [medium]",
                body=(
                    f"The {phase} phase HITL converge-before-advance loop has run "
                    f"{cycles} rounds (>= {threshold}) without reaching a fixpoint. "
                    f"Each round is human-gated, so this is surfaced for visibility, "
                    f"not force-advanced. Investigate whether a decision keeps "
                    f"re-surfacing (carry-forward bug) or the design is genuinely "
                    f"churning. See #3392."
                ),
                metadata={"reason": "hitl_nonconvergence", "cycles": cycles},
                phase=phase,
            )
        )
    except Exception as alert_err:  # noqa: BLE001
        _pkg.logger.warning(
            "Failed to broadcast HITL non-convergence alert (non-fatal)",
            pipeline_id=pipeline_id,
            error=str(alert_err),
        )


def _perform_hitl_phase_rerun(
    *,
    store: _pkg.Any,
    spawner: _pkg.Any,
    pipeline: _pkg.Pipeline,
    phase_execution: _pkg.PhaseExecution,
    pipeline_id: str,
    current_phase: _pkg.PipelinePhase,
    feedback_text: str,
    event_message: str,
) -> None:
    """Tear down the current phase iteration and arm a re-run (#3392).

    Shared by the two re-run triggers in the converge-before-advance HITL
    loop: the operator-feedback kickback (``request_changes`` /
    ``change_approach``) and the decision-driven re-run that folds resolved
    HITL answers back into the phase documents. Snapshots the BRC tracker
    for the next iteration's prompt, appends the operator directive +
    iteration summary (#2795), clears concurrent state so the re-run does
    not short-circuit on stale ``CONSENSUS_CONFIRMED`` messages (#1296),
    persists, and stops the stale containers (the K8s delete is async, so an
    explicit idempotent stop prevents iteration N+1 racing iteration N's
    still-terminating pods).

    The caller must already hold the pipeline state lock, have set the
    pipeline/phase status back to RUNNING, and incremented
    ``phase_execution.hitl_review_cycles``. The caller issues the
    ``continue`` that re-enters the outer loop.
    """
    # Capture the BRC tracker state BEFORE _clear_concurrent_state drops
    # it — that's our only chance to snapshot this iteration's verdicts for
    # the next iteration's prompt.
    rerun_tracker = None
    try:
        from peer_consensus import get_peer_consensus_tracker as _gpct

        rerun_tracker = _gpct(pipeline_id)
    except Exception as tracker_err:  # noqa: BLE001
        _pkg.logger.debug(
            "Tracker lookup failed during HITL re-run snapshot",
            pipeline_id=pipeline_id,
            error=str(tracker_err),
        )

    stale_containers = _pkg._apply_inline_hitl_kickback_to_phase(
        phase_execution,
        feedback_text,
        tracker=rerun_tracker,
    )

    from routes.phases import _clear_concurrent_state

    _clear_concurrent_state(pipeline_id)

    store.save_pipeline(pipeline)

    for _ctr in stale_containers:
        if _ctr.container_id and _ctr.status == _pkg.ContainerStatus.RUNNING:
            try:
                spawner.backend.stop_container(_ctr.container_id, timeout=10)
            except Exception as stop_err:  # noqa: BLE001
                _pkg.logger.debug(
                    "Best-effort HITL re-run teardown failed",
                    pipeline_id=pipeline_id,
                    container_id=_ctr.container_id,
                    error=str(stop_err),
                )

    _pkg.report_pipeline_status(
        pipeline,
        event_type="phase.revision_requested",
        message=event_message,
    )
    _pkg._emit_pipeline_event(pipeline, "phase.revision_requested")
