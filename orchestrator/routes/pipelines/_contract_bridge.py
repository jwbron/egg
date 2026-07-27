"""Contract-decision bridge for routes/pipelines (#1889).

Split out of ``_ledger.py`` to keep it under the 1,500-line file-size cap
(#3312). Barrel-resident and test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _queue_and_await_contract_decisions(
    dq: _pkg.Any,
    worktree_repo_path: _pkg.Path,
    pipeline_id: str,
    pipeline_identifier: int | str,
    phase: _pkg.PipelinePhase,
    cancelled: _pkg.Callable[[], bool] | None = None,
) -> int:
    """Promote unresolved contract decisions/feedback into the orchestrator queue.

    ``cancelled`` is an optional predicate the caller supplies to answer "has
    the operator cancelled this pipeline?". It is consulted at two points, and
    the two close different interleavings:

    - **Before pass 1**, because the cancel route's pending-decision sweep is
      a one-time snapshot. A cancel that lands entirely *before* this batch is
      queued has nothing to sweep, so every decision below is minted with
      nobody to cancel it and the very first ``wait_for_decision`` — an
      unbounded poll with no timeout — blocks for the process lifetime,
      leaking the driver thread and skipping ``_run_pipeline``'s ``finally``
      (#3633 review round 3). The post-wait check cannot help here: it is
      never reached.
    - **After each blocking wait**, which covers the cancel that lands once
      some of the batch is already pending. Those entries *are* swept, so
      their waits return immediately with no resolution; the check stops the
      remaining waits rather than letting the loop walk a cancelled run's
      questions.

    A cancel landing between the pre-pass-1 check and a later
    ``queue_decision`` in pass 1 still mints unsweepable entries — but the
    *first* wait is on an entry the sweep did reach (or, if the cancel beat
    the whole batch, the pre-check fired), so the post-wait check returns
    before the unsweepable ones are ever waited on.

    Callers must still re-check the cancel themselves once this returns: the
    early return is deliberately indistinguishable from a zero-resolution
    round.

    Returns the number of contract decisions/feedback this call surfaced and
    the operator *resolved* this round — the converge-before-advance signal
    (#3392). Decisions that were surfaced but came back non-RESOLVED (e.g. the
    operator cancelled them) are **not** counted: the contract ``cq-N`` stays
    open, and counting it would re-run the phase, re-surface the still-open
    question (carry-forward only adopts *resolved* questions), and loop with no
    termination now that the force-advance backstop is gone. A non-zero count
    means the operator just answered something, so the caller re-runs the
    phase to fold the resolutions into the documents; a zero count means the
    round resolved nothing new and the caller may advance.


    Agents register architectural questions via ``egg-contract add-decision``
    and ``add-feedback``.  Those writes only touch ``.egg-state/contracts/
    {identifier}.json`` — the orchestrator's decision queue never sees them,
    so approving the phase_gate silently drops the questions and the next
    phase's agents have to guess (issue #1889).

    This helper bridges contract-scoped questions for the current phase into
    the orchestrator queue after phase_gate approval, so HTTP/MCP callers
    (e.g. the ``/sdlc`` skill's Phase 4 handler) surface them as individual
    ``choice`` / ``feedback`` decisions.  Resolutions are written back to
    the contract so implement-phase agents see the human's answers.

    All pending decisions (plus the feedback entry, if any) are queued up
    front before any ``wait_for_decision`` call, so ``get_status`` surfaces
    them as a single batch.  Callers can then prompt for up to 4 at a time
    and submit answers in parallel, collapsing what was previously N prompts
    and N polling cycles into ~⌈N/4⌉ prompts and one cycle (issue #1956).

    Once the batch is queued, a single ``decision.created`` event is
    published to the EventBus so event-driven watchers (the ``wait-status``
    monitor long-polling ``/status/wait``) wake immediately.
    ``DecisionQueue.queue_decision`` itself emits no event, so without this
    the bridged decisions are created silently and the operator only
    discovers them via a manual ``get_status`` (issue #2770).
    """
    try:
        from egg_contracts.loader import load_contract, save_contract
    except ImportError:
        _pkg.logger.warning(
            "egg_contracts not available, skipping contract decision bridge",
            pipeline_id=pipeline_id,
        )
        return 0

    try:
        contract = load_contract(pipeline_identifier, worktree_repo_path)
    except Exception as e:
        _pkg.logger.debug(
            "Contract not loadable, skipping contract decision bridge",
            pipeline_id=pipeline_id,
            error=str(e),
        )
        return 0

    phase_value = phase.value
    pending_decisions = [
        d
        for d in contract.decisions
        if not d.resolved
        and getattr(d.type, "value", d.type) == "hitl"
        and (d.phase is None or getattr(d.phase, "value", d.phase) == phase_value)
    ]
    fb = contract.feedback
    pending_feedback = None
    if fb is not None and not fb.submitted:
        fb_phase_val = getattr(fb.phase, "value", fb.phase) if fb.phase is not None else None
        if fb_phase_val is None or fb_phase_val == phase_value:
            pending_feedback = fb

    if not pending_decisions and pending_feedback is None:
        return 0

    _pkg.logger.info(
        "Bridging contract decisions/feedback into orchestrator queue",
        pipeline_id=pipeline_id,
        phase=phase_value,
        decision_count=len(pending_decisions),
        has_feedback=pending_feedback is not None,
    )

    def _save_contract_update(mutator: _pkg.Callable[[_pkg.Any], bool]) -> None:
        try:
            latest = load_contract(pipeline_identifier, worktree_repo_path)
        except Exception as e:
            _pkg.logger.warning(
                "Could not reload contract to persist bridged resolution",
                pipeline_id=pipeline_id,
                error=str(e),
            )
            return
        if not mutator(latest):
            return
        try:
            save_contract(latest, worktree_repo_path)
        except Exception as e:
            _pkg.logger.warning(
                "Failed to save contract after bridged resolution",
                pipeline_id=pipeline_id,
                error=str(e),
            )

    # A cancel that landed before pass 1 left nothing in the queue to sweep,
    # so every decision minted below would be unsweepable and the first wait
    # in pass 2 would never return. Check once here, before anything is
    # queued — the post-wait checks in pass 2 cannot reach that interleaving
    # (#3633 review round 3).
    if cancelled is not None and cancelled():
        _pkg.logger.info(
            "Contract decision bridge skipped: pipeline cancelled before queueing (#3633)",
            pipeline_id=pipeline_id,
            phase=phase_value,
        )
        return 0

    # Pass 1: queue every pending decision + feedback up front.
    queued_decisions: list[tuple[str, _pkg.Any]] = []
    for contract_decision in pending_decisions:
        options_labels = [opt.label for opt in contract_decision.options]
        queued = dq.queue_decision(
            question=contract_decision.question,
            context=(
                f"Open contract question {contract_decision.id}, "
                f"registered by an agent during the {phase_value} phase."
            ),
            options=options_labels,
            decision_type="choice",
            phase=phase,
        )
        queued_decisions.append((contract_decision.id, queued))

    queued_feedback: _pkg.HITLDecision | None = None
    if pending_feedback is not None:
        questions_payload = [
            {"id": q.id, "question": q.question, "answer": ""} for q in pending_feedback.questions
        ]
        queued_feedback = dq.queue_decision(
            question=f"Open feedback request {pending_feedback.id}",
            context=(
                f"Open contract feedback {pending_feedback.id}, "
                f"registered by an agent during the {phase_value} phase."
            ),
            options=[],
            decision_type="feedback",
            questions=questions_payload,
            phase=phase,
        )

    # Surface the freshly-queued batch to event-driven watchers before
    # blocking on resolution. ``DecisionQueue.queue_decision`` emits no
    # EventBus event, so without this the bridged decisions are created
    # silently — the operator's ``wait-status`` monitor never wakes and
    # only finds them via a manual ``get_status`` (#2770). The phase_gate
    # decision emits ``decision.created`` the same way.
    if _pkg._emit_event is not None:
        _pkg._emit_event(
            _pkg.EventType.DECISION_CREATED,
            pipeline_id,
            data={"phase": phase_value},
        )

    # Pass 2: wait for each to resolve and persist back to the contract.
    # Count only decisions whose queue resolution was RESOLVED — a
    # CANCELLED / non-resolved outcome leaves the contract ``cq-N`` open and
    # must NOT count toward the convergence signal, or the caller would re-run
    # the phase, re-surface the still-open question (carry-forward only adopts
    # *resolved* questions), and loop without the operator ever being able to
    # break out (#3392 review).
    resolved_count = 0
    for contract_id, queued in queued_decisions:
        resolved = dq.wait_for_decision(queued.id)
        if cancelled is not None and cancelled():
            _pkg.logger.info(
                "Contract decision bridge abandoned: pipeline cancelled (#3633)",
                pipeline_id=pipeline_id,
                phase=phase_value,
                resolved_count=resolved_count,
            )
            return resolved_count
        if resolved.status != _pkg.DecisionStatus.RESOLVED:
            continue
        resolved_count += 1
        resolution_str = (resolved.resolution or "").strip()

        def _apply(latest: _pkg.Any, _cd_id: str = contract_id, _res: str = resolution_str) -> bool:
            for d in latest.decisions:
                if d.id == _cd_id:
                    d.resolved = True
                    d.resolution = _res
                    d.resolved_by = "human"
                    d.resolved_at = _pkg.datetime.now(_pkg.UTC)
                    return True
            return False

        _save_contract_update(_apply)

    feedback_resolved = False
    if queued_feedback is not None and pending_feedback is not None:
        resolved = dq.wait_for_decision(queued_feedback.id)
        if cancelled is not None and cancelled():
            _pkg.logger.info(
                "Contract feedback bridge abandoned: pipeline cancelled (#3633)",
                pipeline_id=pipeline_id,
                phase=phase_value,
                resolved_count=resolved_count,
            )
            return resolved_count
        if resolved.status == _pkg.DecisionStatus.RESOLVED:
            feedback_resolved = True
            answers: dict[str, str] = {}
            try:
                payload = _pkg.json.loads(resolved.resolution or "")
                if isinstance(payload, dict):
                    raw_answers = payload.get("answers")
                    if isinstance(raw_answers, dict):
                        answers = {str(k): str(v) for k, v in raw_answers.items()}
            except _pkg.json.JSONDecodeError, TypeError:
                pass

            fb_id = pending_feedback.id

            def _apply_fb(
                latest: _pkg.Any, _fb_id: str = fb_id, _answers: dict[str, str] = answers
            ) -> bool:
                if latest.feedback is None or latest.feedback.id != _fb_id:
                    return False
                for q in latest.feedback.questions:
                    if q.id in _answers:
                        q.answer = _answers[q.id]
                # Always mark submitted after resolution — even if
                # individual answers didn't parse, the human responded
                # and shouldn't be asked again.
                latest.feedback.submitted = True
                latest.feedback.submitted_by = "human"
                latest.feedback.submitted_at = _pkg.datetime.now(_pkg.UTC)
                return True

            _save_contract_update(_apply_fb)

    # Convergence signal (#3392): the number of decisions + feedback this
    # round the operator actually *resolved* (not merely surfaced). Non-zero ⇒
    # the operator answered something ⇒ caller re-runs the phase to fold the
    # resolutions in. A surfaced-but-cancelled decision is deliberately
    # excluded: counting it would re-run the phase, re-surface the still-open
    # question, and loop indefinitely now that the force-advance backstop is
    # gone.
    return resolved_count + (1 if feedback_resolved else 0)
