"""run_pipeline HITL-gate converge-before-advance loop block helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _gate_wait_cancelled(resolved_decision, *, store, pipeline_id: str) -> bool:
    """True when a gate's ``wait_for_decision`` was unblocked by an operator cancel (#3633).

    ``wait_for_decision`` returns on two very different events: a human
    resolved the decision, or the PATCH cancel route called
    ``cancel_decision`` on it — which it does deliberately, under the comment
    "cancel any pending decisions so ``wait_for_decision()`` unblocks"
    (``_routes_crud.py``). Downstream the two were indistinguishable: a
    cancelled decision carries no ``resolution``, and the empty string is a
    member of ``_APPROVE_KEYWORDS``, so the gate read the operator's cancel as
    an approval, took the "Approved — resume and advance" branch, wrote
    ``RUNNING`` over the persisted ``CANCELLED``, and let the driver advance
    into the next phase and mint a fresh cohort.

    That is the #3633 symptom reached through the one path the persisted-status
    layers cannot see: they all re-read a status this block has already
    overwritten. ``wait_for_decision``'s own docstring asks callers to inspect
    the returned status for exactly this reason; the gate never did.

    The persisted status is checked too, so a cancel that lands *after*
    ``cancel_decision`` has already swept the queue — or one whose decision was
    resolved by a racing operator — still bails. Delegates to
    ``_pipeline_cancelled`` for that half, inheriting its FAILED carve-out
    (#1273) and its best-effort store-hiccup tolerance.
    """
    if getattr(resolved_decision, "status", None) == _pkg.DecisionStatus.CANCELLED:
        return True
    return _pkg._pipeline_cancelled(store, pipeline_id)


def _run_hitl_gate_converge(
    pipeline,
    *,
    current_phase,
    gateway_mode,
    pipeline_id,
    repo_path,
    spawner,
    store,
    worktree_repo_path,
):
    """Refine/plan HITL-gate converge-before-advance loop-body block
    (extracted verbatim from _run_pipeline). Returns (pipeline, action);
    action=="continue" -> caller re-enters the outer while-loop."""
    if current_phase.value in _pkg._HITL_GATE_PHASES and not pipeline.config.hitl_gates:
        _pkg.report_pipeline_status(
            pipeline,
            event_type="phase.gate_skipped",
            message=(
                f"{current_phase.value} phase gate skipped "
                f"(hitl_gates=False) — advancing autonomously"
            ),
        )
        _pkg.logger.warning(
            "HITL gate: refine/plan gate on an autonomous pipeline "
            "(hitl_gates=False); surfacing but not blocking — advancing "
            "without human approval (the converge-before-advance loop "
            "requires a human, so it cannot run unattended)",
            pipeline_id=pipeline_id,
            phase=current_phase.value,
        )
        # Decision-ledger visibility on the autonomous path (#3390):
        # no human is present to resolve a backstop HITL, so mirror
        # the gate-skip posture — surface a missing ledger loudly
        # (event + warning) but never block.
        try:
            _ledger_note, _ledger_missing, _ledger_explicit_none, _ledger_summary = (
                _pkg._collect_decision_ledger_status(
                    worktree_repo_path,
                    pipeline_id,
                    _pkg._pipeline_identifier(pipeline.issue_number, pipeline_id),
                    current_phase,
                )
            )
            _persisted = _pkg._persist_decision_ledger_summary(
                store, pipeline_id, current_phase, _ledger_summary
            )
            if _persisted is not None:
                pipeline = _persisted
            if _ledger_missing:
                _pkg.logger.warning(
                    "Decision ledger missing at autonomous gate skip (#3390)",
                    pipeline_id=pipeline_id,
                    phase=current_phase.value,
                )
                _pkg.report_pipeline_status(
                    pipeline,
                    event_type="phase.decision_ledger_missing",
                    message=(
                        f"{current_phase.value} phase advanced autonomously "
                        f"with no decision ledger — {_ledger_note}"
                    ),
                )
            elif _ledger_explicit_none is not None:
                # No human is present to confirm the attestation
                # (#3462) — mirror the gate-skip posture: surface
                # loudly, never block.
                _pkg.report_pipeline_status(
                    pipeline,
                    event_type="phase.decision_ledger_explicit_none",
                    message=(
                        f"{current_phase.value} phase advanced autonomously "
                        f"on an unconfirmed no-decisions attestation — "
                        f"{_ledger_note}"
                    ),
                )
        except Exception as ledger_err:  # noqa: BLE001
            _pkg.logger.warning(
                "Decision-ledger check raised on autonomous path (continuing)",
                pipeline_id=pipeline_id,
                phase=current_phase.value,
                error=str(ledger_err),
            )
    elif current_phase.value in _pkg._HITL_GATE_PHASES:
        # --- Decision-ledger backstop (#3390) ---
        # Propose-time validation guarantees every refine/plan
        # producer attested its ledger, so reaching this gate with
        # zero registered decisions AND no explicit-none attestation
        # means a path bypassed consensus (force-advance, resume) or
        # the claim was lost. Never silently advance past that:
        # surface a dedicated HITL whose default remedy is a phase
        # re-run (the converge loop's standard corrective), with an
        # explicit operator override to proceed.
        _ledger_note = ""
        _ledger_missing = False
        _ledger_explicit_none: tuple[str, str, list[dict]] | None = None
        try:
            _ledger_note, _ledger_missing, _ledger_explicit_none, _ledger_summary = (
                _pkg._collect_decision_ledger_status(
                    worktree_repo_path,
                    pipeline_id,
                    _pkg._pipeline_identifier(pipeline.issue_number, pipeline_id),
                    current_phase,
                )
            )
            _persisted = _pkg._persist_decision_ledger_summary(
                store, pipeline_id, current_phase, _ledger_summary
            )
            if _persisted is not None:
                pipeline = _persisted
        except Exception as ledger_err:  # noqa: BLE001
            # Never let a helper bug strand the pipeline — the
            # propose-time hard gate remains the primary enforcement.
            _pkg.logger.warning(
                "Decision-ledger status check raised (continuing)",
                pipeline_id=pipeline_id,
                phase=current_phase.value,
                error=str(ledger_err),
            )

        if _ledger_missing:
            dq = _pkg.get_decision_queue(pipeline_id, repo_path)
            _backstop = dq.queue_decision(
                question=(
                    f"The {current_phase.value} phase reached its gate "
                    f"without a decision ledger (#3390). {_ledger_note}\n\n"
                    f"Re-running the phase lets its agents register the "
                    f"decisions the drafts should have surfaced (or attest "
                    f"an explicit empty ledger); proceeding accepts the "
                    f"unverified ledger and presents the normal phase gate."
                ),
                context=_ledger_note,
                options=[
                    _pkg._LEDGER_BACKSTOP_RERUN_OPTION,
                    _pkg._LEDGER_BACKSTOP_PROCEED_OPTION,
                ],
                decision_type="choice",
                phase=current_phase,
            )
            with _pkg.get_pipeline_state_lock(pipeline_id):
                pipeline = store.load_pipeline(pipeline_id)
                pipeline.status = _pkg.PipelineStatus.AWAITING_HUMAN
                phase_execution = pipeline.get_phase_execution(current_phase)
                phase_execution.status = _pkg.PipelineStatus.AWAITING_HUMAN
                store.save_pipeline(pipeline)
            _pkg.report_pipeline_status(
                pipeline,
                event_type="decision.created",
                message=(
                    f"Decision ledger missing for {current_phase.value} "
                    f"phase — awaiting operator direction"
                ),
            )
            _pkg._emit_pipeline_event(pipeline, "decision.created")

            _backstop_resolved = dq.wait_for_decision(_backstop.id)
            if _pkg._gate_wait_cancelled(_backstop_resolved, store=store, pipeline_id=pipeline_id):
                _pkg.logger.info(
                    "Pipeline cancelled while awaiting the decision-ledger "
                    "backstop — exiting the gate without advancing (#3633)",
                    pipeline_id=pipeline_id,
                    phase=current_phase.value,
                )
                return pipeline, "break"
            _backstop_resolution = str(
                getattr(_backstop_resolved, "resolution", None) or ""
            ).strip()
            _proceed = (
                _backstop_resolved.status != _pkg.DecisionStatus.RESOLVED
                or "proceed" in _backstop_resolution.lower()
            )
            if not _proceed:
                # Default remedy: re-run the phase so producers can
                # register (or explicitly attest) the ledger. Any
                # free-text resolution rides along as the directive.
                _rerun_directive = (
                    f"The {current_phase.value} phase reached its gate "
                    f"without a decision ledger: no HITL decisions were "
                    f"registered and no producer attested an explicit "
                    f"empty ledger (#3390). Review your draft for "
                    f"operator-grade choices; register each via "
                    f"`egg-contract add-decision` and cite its cq-N in "
                    f"the draft, or attest `no_decisions_rationale` when "
                    f"proposing if the phase genuinely raises none."
                )
                if _backstop_resolution.lower() != (_pkg._LEDGER_BACKSTOP_RERUN_OPTION.lower()):
                    _rerun_directive += f"\n\nOperator note: {_backstop_resolution}"
                _pkg.logger.info(
                    "Decision-ledger backstop: re-running phase (#3390)",
                    pipeline_id=pipeline_id,
                    phase=current_phase.value,
                )
                with _pkg.get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    pipeline.status = _pkg.PipelineStatus.RUNNING
                    phase_execution = pipeline.get_phase_execution(current_phase)
                    phase_execution.status = _pkg.PipelineStatus.RUNNING
                    phase_execution.completed_at = None
                    phase_execution.hitl_review_cycles += 1
                    _alert_threshold = pipeline.config.max_hitl_review_cycles
                    if phase_execution.hitl_review_cycles >= _alert_threshold:
                        _pkg._broadcast_hitl_nonconvergence_alert(
                            pipeline_id,
                            pipeline,
                            current_phase,
                            phase_execution.hitl_review_cycles,
                            _alert_threshold,
                        )
                    _pkg._perform_hitl_phase_rerun(
                        store=store,
                        spawner=spawner,
                        pipeline=pipeline,
                        phase_execution=phase_execution,
                        pipeline_id=pipeline_id,
                        current_phase=current_phase,
                        feedback_text=_rerun_directive,
                        event_message=(
                            f"Re-running {current_phase.value}: decision ledger missing (#3390)"
                        ),
                    )
                return pipeline, "continue"  # Re-enter outer loop → re-run phase
            _pkg.logger.warning(
                "Decision-ledger backstop: operator chose to proceed without a ledger (#3390)",
                pipeline_id=pipeline_id,
                phase=current_phase.value,
                resolution=_backstop_resolution[:200],
            )
        elif _ledger_explicit_none is not None:
            # --- Explicit-none attestation confirmation (#3462) ---
            # The producer's claim that this phase raises no operator
            # decisions bypasses the entire register → bridge →
            # resolve chain, and is itself a judgment call the HITL
            # contract assigns to the operator. Surface it as its own
            # confirmable decision (see the helper): confirming records
            # the operator's endorsement on the ledger note; rejecting
            # re-runs the phase to register cq-N entries.
            _rerun_requested, _ledger_note, pipeline = _pkg._handle_explicit_none_attestation_gate(
                pipeline=pipeline,
                pipeline_id=pipeline_id,
                repo_path=repo_path,
                current_phase=current_phase,
                ledger_note=_ledger_note,
                explicit_none=_ledger_explicit_none,
                store=store,
                spawner=spawner,
            )
            if _rerun_requested:
                return pipeline, "continue"  # Re-enter outer loop → re-run phase

        # Check for an existing pending phase_gate decision for this
        # phase.  A prior agent-exit event may
        # have already created one — creating a duplicate confuses the
        # human reviewer.  See #1152.
        existing_pending_gate = any(
            d.decision_type == "phase_gate"
            and d.phase == current_phase
            and d.status == _pkg.DecisionStatus.PENDING
            for d in pipeline.decisions
        )

        if existing_pending_gate:
            _pkg.logger.info(
                "HITL gate: reusing existing pending phase_gate decision",
                pipeline_id=pipeline_id,
                phase=current_phase.value,
            )
            # Find the existing decision to wait on
            dq = _pkg.get_decision_queue(pipeline_id, repo_path)
            decision = next(
                d
                for d in reversed(pipeline.decisions)
                if d.decision_type == "phase_gate"
                and d.phase == current_phase
                and d.status == _pkg.DecisionStatus.PENDING
            )
        else:
            draft_content = _pkg._read_phase_draft(
                worktree_repo_path,
                current_phase.value,
                issue_number=pipeline.issue_number,
                pipeline_id=pipeline_id,
                branch=pipeline.branch,
            )
            phase_label = "analysis" if current_phase.value == "refine" else current_phase.value

            # Warn if draft is missing — the agent may not have written
            # it to the expected path.  See #1016.
            if draft_content is None:
                _pkg.logger.warning(
                    "HITL gate: draft not found on work branch",
                    pipeline_id=pipeline_id,
                    phase=current_phase.value,
                    worktree_path=str(worktree_repo_path),
                )
                draft_content = (
                    f"**Warning**: No {phase_label} draft was found on the "
                    f"work branch. The agent may not have written the output "
                    f"to the expected path."
                )

            question = (
                f"The {current_phase.value} phase has completed. "
                f"Please review the {phase_label} and approve to continue, "
                f"or provide feedback to request changes."
            )
            # Auditability (#3390): make "N registered" vs "explicitly
            # none" vs "MISSING (operator overrode)" readable at the
            # gate without a get_contract round-trip.
            if _ledger_note:
                question += f"\n\n{_ledger_note}"

            # Lead the gate comment with the simplifier's human-focused
            # companion (simplified, jargon-free) when present, and link
            # the full agent draft for depth. Falls back to the full
            # draft inline when no companion exists (older pipelines,
            # or the companion failed to land).
            human_content = _pkg._read_human_phase_draft(
                worktree_repo_path,
                current_phase.value,
                issue_number=pipeline.issue_number,
                pipeline_id=pipeline_id,
                branch=pipeline.branch,
            )
            gate_context = draft_content
            if human_content:
                full_draft_link = ""
                draft_rel = _pkg._get_draft_path(
                    current_phase.value,
                    issue_number=pipeline.issue_number,
                    pipeline_id=pipeline_id,
                )
                if pipeline.repo and pipeline.branch and draft_rel:
                    blob = f"https://github.com/{pipeline.repo}/blob/{pipeline.branch}"
                    full_draft_link = (
                        f"\n\n[View the full detailed {phase_label} draft]({blob}/{draft_rel})"
                    )
                gate_context = f"{human_content}{full_draft_link}"

            # Detect whether the gate content changed compared to the
            # previous phase_gate decision for this phase (if any).
            #
            # NB: this compares ``gate_context``, which leads with the
            # simplifier's human-focused summary when a companion
            # exists. That summary is intentionally high-level and
            # lossy, so a re-refinement that materially changes the
            # detailed agent draft *without* altering the summary will
            # report ``content_changed=False``. The flag only feeds the
            # overseer's no-op-rerun health heuristic
            # (``overseer/monitor.py`` ``_check_rerun_anomaly``) — it
            # never gates re-prompting — so a missed change here is at
            # worst a suppressed advisory alert, not a correctness
            # issue. We compare the gate content (not the full draft)
            # deliberately so the heuristic tracks what the operator
            # actually sees at the gate.
            _content_changed: bool | None = None
            _prev_gate = next(
                (
                    d
                    for d in reversed(pipeline.decisions)
                    if d.decision_type == "phase_gate"
                    and d.phase == current_phase
                    and d.status == _pkg.DecisionStatus.RESOLVED
                ),
                None,
            )
            if _prev_gate is not None:
                _content_changed = gate_context != _prev_gate.context

            dq = _pkg.get_decision_queue(pipeline_id, repo_path)
            decision = dq.queue_decision(
                question=question,
                context=gate_context,
                options=["approve", "request changes"],
                decision_type="phase_gate",
                phase=current_phase,
                content_changed=_content_changed,
            )

        # Reload pipeline to pick up the decision persisted by queue_decision(),
        # otherwise the stale local object overwrites it with an empty decisions list.
        with _pkg.get_pipeline_state_lock(pipeline_id):
            pipeline = store.load_pipeline(pipeline_id)
            pipeline.status = _pkg.PipelineStatus.AWAITING_HUMAN
            # Also mark the phase as awaiting human so the DAG visualization
            # shows the HITL gate on the correct phase box.
            phase_execution = pipeline.get_phase_execution(current_phase)
            phase_execution.status = _pkg.PipelineStatus.AWAITING_HUMAN
            store.save_pipeline(pipeline)

        # Report HITL gate to collaborator
        _pkg.report_pipeline_status(
            pipeline,
            event_type="decision.created",
            message=f"Awaiting human approval for {current_phase.value} phase",
        )
        _pkg._emit_pipeline_event(pipeline, "decision.created")

        dq.wait_for_decision(decision.id)

        # Check resolution — did the human approve or request changes?
        resolved_decision = dq.get_decision(decision.id)

        # ...or did neither happen, because the operator cancelled the
        # pipeline and the route cancelled this decision to unblock the wait
        # above? Bail before any of the resolution parsing below: an unset
        # resolution reads as an approval (``"" in _APPROVE_KEYWORDS``), and
        # the approve branch rewrites the operator's CANCELLED to RUNNING and
        # advances the phase — the #3633 spawn, through the one path the
        # persisted-status layers cannot see. "break" leaves the driver loop
        # the same way its own CANCELLED check at the loop head does, so the
        # ``finally`` observes CANCELLED and preserves the worktrees
        # ``restart_phase`` resumes from (#1725).
        if _pkg._gate_wait_cancelled(resolved_decision, store=store, pipeline_id=pipeline_id):
            _pkg.logger.info(
                "Pipeline cancelled while awaiting the phase gate — "
                "exiting the gate without advancing (#3633)",
                pipeline_id=pipeline_id,
                phase=current_phase.value,
            )
            return pipeline, "break"

        resolution = (resolved_decision.resolution or "").strip()

        # JSON-first resolution parsing: try structured payload before
        # falling back to keyword matching for legacy bare-string resolutions.
        _is_approved = False
        _needs_revision = False
        _revision_feedback: str | None = None

        try:
            payload = _pkg.json.loads(resolution)
            if isinstance(payload, dict) and "action" in payload:
                action = payload["action"]
                feedback_text = payload.get("feedback", "")

                if action == "approve":
                    _is_approved = True
                elif action == "select":
                    # Selection from a choice menu — treat as approval
                    _is_approved = True
                elif action == "submit_feedback":
                    # Feedback submission — treat as approval (info collected)
                    _is_approved = True
                elif action in ("request_changes", "change_approach"):
                    if feedback_text:
                        # R-1: Extract readable feedback, not raw JSON
                        _needs_revision = True
                        _revision_feedback = feedback_text
                    else:
                        # JSON request_changes without feedback — same as bare label
                        _needs_revision = True
                        _revision_feedback = None
                else:
                    # Unknown action — fall through to legacy matching
                    raise _pkg.json.JSONDecodeError("unknown action", resolution, 0)
            else:
                # Valid JSON but no action field — fall through to legacy
                raise _pkg.json.JSONDecodeError("no action field", resolution, 0)
        except _pkg.json.JSONDecodeError, TypeError, AttributeError:
            # Legacy bare-string resolution — existing keyword matching
            if resolution.lower() in _pkg._APPROVE_KEYWORDS:
                _is_approved = True
            elif resolution.lower() in _pkg._BARE_OPTION_LABELS:
                # Bare "request changes" without feedback
                _needs_revision = True
                _revision_feedback = None
            elif resolution:
                # Free-text feedback
                _needs_revision = True
                _revision_feedback = resolution

        # Holds the operator's resolution from the "bare request →
        # asked for specifics → approve-with-context" follow-up path,
        # if that path is taken. When set, it (not the original
        # ``resolution``) carries any context attached to the final
        # gate approval, so the convergence re-run below must thread it
        # rather than the stale original resolution (#3392 review).
        followup_resolution: str | None = None

        if _needs_revision and _revision_feedback is None:
            # Bare request without actionable feedback — ask for specifics.
            # This handles both legacy "request changes" and JSON
            # {"action":"request_changes"} without feedback text.
            _pkg.logger.info(
                "HITL gate: bare option label without feedback, requesting specifics",
                pipeline_id=pipeline_id,
                phase=current_phase,
                resolution=resolution,
            )
            # Extract a human-friendly label from the resolution for the
            # follow-up prompt (avoid displaying raw JSON to the user).
            try:
                _parsed = _pkg.json.loads(resolution)
                display_resolution = (
                    _parsed.get("action", resolution).replace("_", " ")
                    if isinstance(_parsed, dict)
                    else resolution
                )
            except _pkg.json.JSONDecodeError, TypeError, AttributeError:
                display_resolution = resolution
            followup = dq.queue_decision(
                question=(
                    f'You selected "{display_resolution}" but didn\'t provide specific feedback. '
                    f"Please describe what changes you'd like to see in the {phase_label}, "
                    f"or approve to continue."
                ),
                context=draft_content,
                options=["approve"],
                decision_type="phase_gate",
                phase=current_phase,
            )
            dq.wait_for_decision(followup.id)
            resolved_followup = dq.get_decision(followup.id)
            if _pkg._gate_wait_cancelled(resolved_followup, store=store, pipeline_id=pipeline_id):
                _pkg.logger.info(
                    "Pipeline cancelled while awaiting gate follow-up "
                    "specifics — exiting the gate without advancing (#3633)",
                    pipeline_id=pipeline_id,
                    phase=current_phase.value,
                )
                return pipeline, "break"
            followup_resolution = (resolved_followup.resolution or "").strip()

            # Parse follow-up resolution (also JSON-first)
            try:
                fp = _pkg.json.loads(followup_resolution)
                if isinstance(fp, dict) and "action" in fp:
                    fa = fp["action"]
                    if fa == "approve":
                        _is_approved = True
                        _needs_revision = False
                    elif fa in ("request_changes", "change_approach"):
                        ft = fp.get("feedback", "")
                        if ft:
                            _revision_feedback = ft
                        else:
                            _is_approved = True
                            _needs_revision = False
                    else:
                        raise _pkg.json.JSONDecodeError("unknown", followup_resolution, 0)
                else:
                    raise _pkg.json.JSONDecodeError("no action", followup_resolution, 0)
            except _pkg.json.JSONDecodeError, TypeError, AttributeError:
                if (
                    followup_resolution.lower() in _pkg._APPROVE_KEYWORDS
                    or followup_resolution.lower() in _pkg._BARE_OPTION_LABELS
                ):
                    _pkg.logger.info(
                        "HITL follow-up: no actionable feedback, treating as approval",
                        pipeline_id=pipeline_id,
                        phase=current_phase,
                    )
                    _is_approved = True
                    _needs_revision = False
                elif followup_resolution:
                    _revision_feedback = followup_resolution

        if _needs_revision and _revision_feedback:
            # Human provided feedback — re-run the phase with corrections
            _pkg.logger.info(
                "HITL gate: changes requested, re-running phase",
                pipeline_id=pipeline_id,
                phase=current_phase,
                feedback_preview=_revision_feedback[:200],
            )
            with _pkg.get_pipeline_state_lock(pipeline_id):
                pipeline = store.load_pipeline(pipeline_id)
                pipeline.status = _pkg.PipelineStatus.RUNNING
                phase_execution = pipeline.get_phase_execution(current_phase)
                phase_execution.status = _pkg.PipelineStatus.RUNNING
                phase_execution.completed_at = None  # Reset — phase is re-running
                phase_execution.hitl_review_cycles += 1

                # No force-advance (#3392). The converge-before-advance
                # loop is human-gated every round, so an unbounded loop
                # cannot burn compute silently and we must never advance
                # with the operator's feedback unaddressed. After the
                # configured number of rounds, emit a non-fatal overseer
                # alert for visibility, then always re-run. The
                # ``max_hitl_review_cycles`` config is now this alert
                # threshold, not a force-advance budget.
                _alert_threshold = pipeline.config.max_hitl_review_cycles
                if phase_execution.hitl_review_cycles >= _alert_threshold:
                    _pkg._broadcast_hitl_nonconvergence_alert(
                        pipeline_id,
                        pipeline,
                        current_phase,
                        phase_execution.hitl_review_cycles,
                        _alert_threshold,
                    )
                # #2795: the directive + frozen iteration summary
                # accumulate across kickbacks so iteration N+1's prompts
                # render them with explicit precedence prose.
                _pkg._perform_hitl_phase_rerun(
                    store=store,
                    spawner=spawner,
                    pipeline=pipeline,
                    phase_execution=phase_execution,
                    pipeline_id=pipeline_id,
                    current_phase=current_phase,
                    feedback_text=_revision_feedback,
                    event_message=f"Human requested changes to {current_phase.value}",
                )
            return pipeline, "continue"  # Re-enter outer loop → re-run phase with feedback

        # Before advancing, surface any contract-scoped decisions /
        # feedback the phase's agents registered via ``egg-contract``.
        # Without this bridge, approving the phase_gate silently
        # discards them (#1889).  Wrapped in try/except so a bug
        # here can never strand the pipeline.
        _decisions_resolved_this_round = 0
        try:
            _decisions_resolved_this_round = _pkg._queue_and_await_contract_decisions(
                dq,
                worktree_repo_path,
                pipeline_id,
                _pkg._pipeline_identifier(pipeline.issue_number, pipeline_id),
                current_phase,
            )
        except Exception as bridge_err:
            _pkg.logger.warning(
                "Contract decision bridge failed (continuing)",
                pipeline_id=pipeline_id,
                phase=current_phase.value,
                error=str(bridge_err),
            )

        # Converge-before-advance (#3392): if the operator just
        # resolved one or more decisions, re-run the phase so the
        # documents reflect those resolutions and any decision the
        # resolutions induce is surfaced in the next round. Re-asks of
        # already-answered questions are suppressed by carry-forward
        # (find_resolved_question), so the open-decision set shrinks
        # toward a fixpoint; we advance only on a round that resolved
        # nothing new. The phase gate is re-presented after the re-run.
        if _decisions_resolved_this_round and current_phase.value in _pkg._HITL_GATE_PHASES:
            # Preserve any operator context attached to the approve so
            # the re-run's agents see it (the bridge already persisted
            # the decision answers themselves; this carries the gate
            # prose that would otherwise be dropped on a re-run round).
            # When the operator went through the "bare request → asked
            # for specifics → approve-with-context" follow-up path, the
            # context lives in ``followup_resolution`` (the final
            # answer), not the stale original ``resolution`` — prefer
            # it so that context is not silently dropped (#3392 review).
            _context_source = followup_resolution if followup_resolution is not None else resolution
            _approve_context = ""
            try:
                _ap = _pkg.json.loads(_context_source)
                if isinstance(_ap, dict):
                    _approve_context = (_ap.get("context") or _ap.get("feedback") or "").strip()
            except _pkg.json.JSONDecodeError, TypeError, AttributeError:
                _approve_context = ""

            _rerun_feedback = (
                f"The operator resolved {_decisions_resolved_this_round} HITL "
                f"decision(s) for the {current_phase.value} phase. Update the "
                f"{current_phase.value} document(s) to reflect the resolved "
                f"decisions (read them from the contract's `decisions`), and "
                f"register any new decisions the resolutions induce."
            )
            if _approve_context:
                _rerun_feedback += f"\n\nOperator note at the gate: {_approve_context}"

            _pkg.logger.info(
                "HITL gate: decisions resolved, re-running phase to fold them in",
                pipeline_id=pipeline_id,
                phase=current_phase.value,
                resolved_count=_decisions_resolved_this_round,
            )
            with _pkg.get_pipeline_state_lock(pipeline_id):
                pipeline = store.load_pipeline(pipeline_id)
                pipeline.status = _pkg.PipelineStatus.RUNNING
                phase_execution = pipeline.get_phase_execution(current_phase)
                phase_execution.status = _pkg.PipelineStatus.RUNNING
                phase_execution.completed_at = None
                phase_execution.hitl_review_cycles += 1
                _alert_threshold = pipeline.config.max_hitl_review_cycles
                if phase_execution.hitl_review_cycles >= _alert_threshold:
                    _pkg._broadcast_hitl_nonconvergence_alert(
                        pipeline_id,
                        pipeline,
                        current_phase,
                        phase_execution.hitl_review_cycles,
                        _alert_threshold,
                    )
                _pkg._perform_hitl_phase_rerun(
                    store=store,
                    spawner=spawner,
                    pipeline=pipeline,
                    phase_execution=phase_execution,
                    pipeline_id=pipeline_id,
                    current_phase=current_phase,
                    feedback_text=_rerun_feedback,
                    event_message=(
                        f"Folding {_decisions_resolved_this_round} resolved "
                        f"decision(s) into {current_phase.value}"
                    ),
                )
            return pipeline, "continue"  # Re-enter outer loop → re-run phase, re-surface gate

        # Approved — resume and advance
        with _pkg.get_pipeline_state_lock(pipeline_id):
            pipeline = store.load_pipeline(pipeline_id)
            pipeline.status = _pkg.PipelineStatus.RUNNING
            # Restore phase status to COMPLETE now that the HITL gate is cleared
            phase_execution = pipeline.get_phase_execution(current_phase)
            phase_execution.status = _pkg.PipelineStatus.COMPLETE
            if phase_execution.completed_at is None:
                phase_execution.completed_at = _pkg.datetime.now(_pkg.UTC)
            store.save_pipeline(pipeline)

        # Persist phase gate resolution to contract and draft so
        # next-phase agents can see the human's decisions.  #1295
        _pkg._persist_phase_gate_resolution(
            worktree_repo_path,
            pipeline_id,
            resolved_decision,
            current_phase.value,
            pipeline.issue_number,
        )

        # Commit and push updated statefiles (contract + draft with resolution)
        try:
            _pkg._commit_statefiles_to_worktree(
                worktree_repo_path,
                f"Persist HITL resolution after {current_phase.value} phase gate",
                pipeline_identifier=_pkg._pipeline_identifier(pipeline.issue_number, pipeline_id),
                pipeline_id=pipeline_id,
            )
        except Exception as git_err:
            # Catch broadly: see #2219.  The helper raises
            # ``TimeoutExpired`` and ``OSError`` paths that a
            # ``CalledProcessError``-only handler did not catch.
            _pkg.logger.warning(
                "Failed to commit statefiles after phase gate resolution (continuing)",
                pipeline_id=pipeline_id,
                error=str(git_err),
            )

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
                    "Failed to push statefiles after phase gate resolution (continuing)",
                    pipeline_id=pipeline_id,
                    error=str(push_err),
                )
    return pipeline, None
