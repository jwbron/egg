"""decision-ledger + gap-gate + apply-handoff helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _sync_pipeline_decisions_to_contract(
    repo_path: _pkg.Path,
    worktree_repo_path: _pkg.Path,
    pipeline_id: str,
) -> None:
    """Sync resolved non-phase-gate pipeline decisions to the contract.

    Converts HITLDecision objects from pipeline state into contract Decision
    objects so that implement-phase agents can see what was decided during
    refine/plan phases.

    Only syncs decisions with decision_type != "phase_gate" (substantive
    choices, not process-control gates).  Skips decisions already present
    in the contract (matched by question text) to avoid duplicates on
    re-runs after HITL revision cycles.

    Args:
        repo_path: Orchestrator's main repo path — root for the state
            store that owns pipeline records.
        worktree_repo_path: Pipeline's per-run worktree path — root for
            the contract under ``<worktree>/.egg-state/contracts/``.
    """
    try:
        from egg_contracts.loader import load_contract, save_contract
        from egg_contracts.models import Decision, DecisionOption, DecisionType
    except ImportError:
        _pkg.logger.warning("egg_contracts not available, skipping decision sync")
        return

    # Load pipeline from the orchestrator's state store, NOT the per-run
    # worktree.  Pipeline records live under ``repo_path``'s persistent
    # state-store worktree; the per-run worktree has none.  Conflating
    # the two silently no-op'd this helper for every issue-mode pipeline
    # since #950 (#2345).
    store = _pkg.get_state_store(repo_path)
    try:
        pipeline = store.load_pipeline(pipeline_id)
    except Exception as exc:
        _pkg.logger.warning(
            "decision_sync_pipeline_load_failed",
            pipeline_id=pipeline_id,
            state_store_repo_path=str(repo_path),
            error=str(exc),
        )
        return

    # Filter to resolved, non-phase-gate decisions
    substantive_decisions = [
        d
        for d in pipeline.decisions
        if d.decision_type != "phase_gate" and d.status == _pkg.DecisionStatus.RESOLVED
    ]

    if not substantive_decisions:
        _pkg.logger.debug("No substantive decisions to sync", pipeline_id=pipeline_id)
        return

    try:
        contract = load_contract(pipeline_id, worktree_repo_path)
    except Exception:
        _pkg.logger.warning(
            "Contract not found, skipping decision sync",
            pipeline_id=pipeline_id,
        )
        return

    # Build set of existing contract decision questions for deduplication
    existing_questions = {d.question for d in contract.decisions}

    # Determine next decision ID (continue numbering after existing ones)
    max_existing_id = 0
    for d in contract.decisions:
        # Extract numeric suffix from "decision-N"
        try:
            num = int(d.id.split("-")[1])
            max_existing_id = max(max_existing_id, num)
        except IndexError, ValueError:
            pass

    synced_count = 0
    for pipeline_decision in substantive_decisions:
        if pipeline_decision.question in existing_questions:
            continue

        max_existing_id += 1
        decision_id = f"decision-{max_existing_id}"

        # Convert pipeline options (list[str]) to contract DecisionOption objects
        contract_options = [
            DecisionOption(id=f"opt-{i + 1}", label=opt)
            for i, opt in enumerate(pipeline_decision.options)
        ]

        contract_decision = Decision(
            id=decision_id,
            question=pipeline_decision.question,
            type=DecisionType.HITL,
            options=contract_options,
            resolved=True,
            resolution=pipeline_decision.resolution,
            resolved_by="human",
            resolved_at=pipeline_decision.resolved_at,
        )
        contract.decisions.append(contract_decision)
        existing_questions.add(pipeline_decision.question)
        synced_count += 1

    if synced_count > 0:
        save_contract(contract, worktree_repo_path)
        _pkg.logger.info(
            "Synced pipeline decisions to contract",
            pipeline_id=pipeline_id,
            synced_count=synced_count,
            total_contract_decisions=len(contract.decisions),
        )


def _ledger_attestation_question(
    role: str,
    rationale: str,
    phase_value: str,
    candidates: list[dict] | None = None,
) -> str:
    """Compose the explicit-none confirmation question (#3462, #3526).

    A producer's claim that a phase raises no operator decisions is itself
    a judgment call about what *is* a judgment call — exactly the class of
    decision the HITL contract assigns to the operator. It therefore
    surfaces as its own confirmable decision, not a sentence embedded in
    the phase_gate question. The considered-candidate enumeration (#3526)
    is included so the operator confirms specific dispositions rather
    than a paragraph.
    """
    candidate_block = ""
    if candidates:
        rendered = _pkg._format_considered_candidates(candidates)
        if rendered:
            candidate_block = f"\nCandidates considered and dispositioned away:\n\n{rendered}\n"
    return (
        f"The {role} attests the {phase_value} phase deliberately raises "
        f"no operator decisions (#3462):\n\n"
        f"> {rationale}\n"
        f"{candidate_block}\n"
        f"Confirm to proceed to the phase gate, or choose "
        f"“{_pkg._LEDGER_BACKSTOP_RERUN_OPTION}” to send the phase back so its "
        f"agents register the decisions as first-class contract entries "
        f"(cq-N). Any free-text reply is treated as a re-run directive and "
        f"forwarded to the agents."
    )


def _unwrap_choice_resolution(resolution: str) -> str:
    """Unwrap the ``{"action":"select","selected":<label>}`` envelope.

    The SDLC HITL CLI resolves a ``choice`` decision with that structured
    envelope (mirrors ``routes.decisions._normalize_choice_resolution``);
    a bare string / non-JSON resolution passes through unchanged.
    """
    try:
        payload = _pkg.json.loads(resolution)
        if isinstance(payload, dict) and payload.get("action") == "select":
            selected = payload.get("selected")
            if isinstance(selected, str):
                return selected
    except ValueError, TypeError:
        pass
    return resolution


def _ledger_attestation_confirmed(resolution: str) -> bool:
    """Return True when ``resolution`` confirms the explicit-none attestation.

    Conservative on purpose (#3462): only the bare keyword ``confirm`` or
    the full confirm-option label counts. Anything else — the re-run
    option, or free text naming decisions the operator expected — kicks
    the phase back, with the text riding along as the directive.
    """
    normalized = _pkg._unwrap_choice_resolution(resolution).strip().lower()
    return normalized in ("confirm", _pkg._LEDGER_ATTESTATION_CONFIRM_OPTION.lower())


def _ledger_attestation_rerun_directive(phase_value: str, rationale: str, resolution: str) -> str:
    """Compose the re-run directive for a rejected explicit-none attestation (#3462).

    The operator declined to confirm that the phase raises no operator
    decisions, so the phase re-runs with an instruction to register each
    decision — including ones the producer believes prior context already
    resolves. Any free-text resolution (i.e. not the bare re-run option) is
    an operator note and rides along verbatim so the agents see the specific
    concern.
    """
    directive = (
        f"The operator declined to confirm the {phase_value} phase's "
        f"no-decisions attestation (#3462). The phase claimed: "
        f"“{rationale}”. Register each operator-grade decision via "
        f"`egg-contract add-decision` — including decisions you believe "
        f"prior context already resolves: register those with your "
        f"recommended answer as the first option and cite the resolving "
        f"context in its description. Belief about resolution is a "
        f"recommended disposition, not a reason to skip registration."
    )
    if resolution.strip().lower() != _pkg._LEDGER_BACKSTOP_RERUN_OPTION.lower():
        directive += f"\n\nOperator note: {resolution.strip()}"
    return directive


def _handle_explicit_none_attestation_gate(
    *,
    pipeline,
    pipeline_id: str,
    repo_path,
    current_phase: _pkg.PipelinePhase,
    ledger_note: str,
    explicit_none: tuple[str, str, list[dict]],
    store,
    spawner,
):
    """Surface an explicit-none attestation as a confirmable HITL decision (#3462).

    A producer's claim that a refine/plan phase raises no operator decisions
    bypasses the entire register → bridge → resolve chain, and the claim is
    itself a judgment call the HITL contract assigns to the operator. Rather
    than folding it into the phase_gate question as prose, surface it as its
    own confirmable ``choice`` decision: confirming records the operator's
    endorsement on the ledger note; rejecting re-runs the phase so producers
    register the decisions as first-class ``cq-N`` entries.

    Returns ``(rerun_requested, ledger_note, pipeline)``:

    - ``rerun_requested`` — True when the operator rejected the attestation
      and the phase has already been re-run here; the caller must ``continue``
      its poll loop. False when the attestation was confirmed (or fail-open on
      a cancelled/non-RESOLVED terminal state, or on an operator cancel
      detected *before* the decision was queued or parked); the caller
      proceeds to the phase gate — where its own ``_gate_wait_cancelled``
      check turns the cancelled case into a driver break.
    - ``ledger_note`` — the note to thread into the phase_gate question,
      annotated with the confirmation outcome.
    - ``pipeline`` — the (possibly reloaded) pipeline the caller must rebind,
      since queuing the confirmation decision reloads and mutates state.
    """
    attest_role, attest_rationale, attest_candidates = explicit_none
    attest_question = _pkg._ledger_attestation_question(
        attest_role, attest_rationale, current_phase.value, attest_candidates
    )
    # A converge-loop round (or a resume) re-enters this gate with the same
    # attestation — do not re-ask a question the operator already answered,
    # and reuse a pending one instead of queueing a duplicate (mirrors the
    # phase_gate's #1152 guard).
    prior_confirm = next(
        (
            d
            for d in reversed(pipeline.decisions)
            if d.decision_type == "choice"
            and d.phase == current_phase
            and d.question == attest_question
            and d.status == _pkg.DecisionStatus.RESOLVED
            and _pkg._ledger_attestation_confirmed(str(d.resolution or ""))
        ),
        None,
    )
    if prior_confirm is not None:
        return False, ledger_note + " Operator confirmed the attestation.", pipeline

    dq = _pkg.get_decision_queue(pipeline_id, repo_path)
    pending_attest = next(
        (
            d
            for d in reversed(pipeline.decisions)
            if d.decision_type == "choice"
            and d.phase == current_phase
            and d.question == attest_question
            and d.status == _pkg.DecisionStatus.PENDING
        ),
        None,
    )
    if pending_attest is not None:
        attest_decision = pending_attest
        newly_created = False
    else:
        # Never mint a decision for a cancelled run: the cancel route's
        # pending-decision sweep is a one-time snapshot, so a decision created
        # after it has nobody to cancel it and ``wait_for_decision`` below —
        # an unbounded poll — would block for the process lifetime (#3633
        # review round 3). Fail open to the caller, which re-checks the cancel
        # immediately on return and breaks the driver loop.
        if _pkg._pipeline_cancelled(store, pipeline_id):
            _pkg.logger.info(
                "Pipeline cancelled before the explicit-none attestation was "
                "queued — skipping the gate (#3633)",
                pipeline_id=pipeline_id,
                phase=current_phase.value,
            )
            return False, ledger_note, pipeline
        attest_decision = dq.queue_decision(
            question=attest_question,
            context=ledger_note,
            options=[
                _pkg._LEDGER_ATTESTATION_CONFIRM_OPTION,
                _pkg._LEDGER_BACKSTOP_RERUN_OPTION,
            ],
            decision_type="choice",
            phase=current_phase,
        )
        newly_created = True
    # Park under the state lock, re-reading the status from inside it — the
    # cancel route persists CANCELLED under the same lock, so an unconditional
    # write here would lose it and the caller's post-return check would read
    # back this gate's own AWAITING_HUMAN (#3633 review round 3).
    pipeline, _park_cancelled = _pkg._park_at_gate_unless_cancelled(
        store, pipeline_id, current_phase
    )
    if _park_cancelled:
        _pkg.logger.info(
            "Pipeline cancelled before parking at the explicit-none "
            "attestation — skipping the gate (#3633)",
            pipeline_id=pipeline_id,
            phase=current_phase.value,
        )
        return False, ledger_note, pipeline
    # Only announce a freshly-created decision. Reusing a pending decision
    # across polls must not re-emit ``decision.created`` — a duplicate event
    # for a decision the operator is already looking at (#3462 review).
    if newly_created:
        _pkg.report_pipeline_status(
            pipeline,
            event_type="decision.created",
            message=(
                f"{current_phase.value} phase attests no operator "
                f"decisions — awaiting operator confirmation (#3462)"
            ),
        )
        _pkg._emit_pipeline_event(pipeline, "decision.created")

    attest_resolved = dq.wait_for_decision(attest_decision.id)
    attest_resolution = _pkg._unwrap_choice_resolution(
        str(getattr(attest_resolved, "resolution", None) or "")
    ).strip()
    resolved_ok = attest_resolved.status == _pkg.DecisionStatus.RESOLVED
    confirmed = resolved_ok and _pkg._ledger_attestation_confirmed(attest_resolution)

    if resolved_ok and not confirmed:
        # Rejected — re-run the phase so producers register the decisions as
        # first-class cq-N entries.
        rerun_directive = _pkg._ledger_attestation_rerun_directive(
            current_phase.value, attest_rationale, attest_resolution
        )
        _pkg.logger.info(
            "Explicit-none attestation rejected: re-running phase (#3462)",
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
                feedback_text=rerun_directive,
                event_message=(
                    f"Re-running {current_phase.value}: no-decisions attestation rejected (#3462)"
                ),
            )
        return True, ledger_note, pipeline

    if confirmed:
        return False, ledger_note + " Operator confirmed the attestation.", pipeline
    # Fail open to the phase gate on a non-RESOLVED terminal state (cancel):
    # the gate itself still blocks for approval, mirroring the missing-ledger
    # backstop's posture. Record the outcome accurately — do not claim a
    # confirmation the operator never gave (#3462 review).
    return (
        False,
        ledger_note + " Attestation confirmation was cancelled; deferring to the phase gate.",
        pipeline,
    )


def _find_explicit_none_attestation(
    pipeline_id: str,
    phase_value: str,
) -> tuple[str, str, list[dict]] | None:
    """Find a producer's explicit-none decision-ledger attestation (#3390).

    Scans the phase's ``CONSENSUS_PROPOSE`` messages (newest first) for a
    proposal whose attestation carries a non-empty
    ``no_decisions_rationale`` — the durable record that a producer
    *deliberately* registered no decisions this phase (propose-time
    validation guarantees the field was well-formed when accepted).
    Returns ``(role, rationale, candidates_considered)`` or ``None``;
    message-store outages degrade to ``None`` (the caller fails closed
    into the backstop HITL, which the operator can resolve either way —
    never a silent pass).
    """
    try:
        from message_store import MessageType, get_message_store

        messages = get_message_store().get_messages(pipeline_id, limit=500)
    except Exception as exc:  # noqa: BLE001
        _pkg.logger.warning(
            "Decision-ledger attestation scan failed (treating as not found)",
            pipeline_id=pipeline_id,
            phase=phase_value,
            error=str(exc),
        )
        return None

    for message in reversed(messages):
        if message.message_type != MessageType.CONSENSUS_PROPOSE:
            continue
        if message.phase is not None and message.phase != phase_value:
            continue
        payload = (message.metadata or {}).get("payload")
        if not isinstance(payload, dict):
            continue
        attestation = payload.get("attestation")
        if not isinstance(attestation, dict):
            continue
        rationale = attestation.get("no_decisions_rationale")
        if isinstance(rationale, str) and rationale.strip():
            raw_candidates = attestation.get("candidates_considered")
            candidates = (
                [c for c in raw_candidates if isinstance(c, dict)]
                if isinstance(raw_candidates, list)
                else []
            )
            return message.from_role, rationale.strip(), candidates
    return None


def _collect_decision_ledger_status(
    worktree_repo_path: _pkg.Path,
    pipeline_id: str,
    pipeline_identifier: int | str,
    phase: _pkg.PipelinePhase,
) -> tuple[str, bool, tuple[str, str, list[dict]] | None, dict]:
    """Summarize the phase's decision ledger for the gate surface (#3390).

    Returns ``(note, missing, explicit_none, summary)``:

    - ``note`` — an operator-visible one-liner appended to the phase_gate
      question so "N registered" vs "explicitly none" vs "MISSING" is
      readable at the gate without a ``get_contract`` round-trip.
    - ``missing`` — True only when the phase registered zero decisions
      AND no producer attested an explicit empty ledger. With propose-time
      enforcement in place this means the gate was reached on a path that
      bypassed consensus (force-advance, resume) or the producer's claim
      was lost — the caller surfaces a dedicated backstop HITL rather
      than silently advancing.
    - ``explicit_none`` — the ``(role, rationale, candidates_considered)``
      of a producer's explicit-none attestation when that is what stands
      in for a ledger (zero registered decisions), else ``None``. The
      caller surfaces it as its own confirmable decision (#3462) rather
      than trusting the self-attestation. Mutually exclusive with
      ``missing``.
    - ``summary``: a JSON-serializable snapshot for
      ``PhaseExecution.decision_ledger`` (#3526): registered ids,
      explicit-none flag, and considered candidates, so
      decisions-surfaced-per-phase is queryable from pipeline state.
      The key set is **uniform across all three branches** —
      ``registered``, ``resolved``, ``explicit_none``, ``attested_by``,
      ``missing``, ``candidates_considered`` are always present (with
      ``attested_by=None`` / ``missing=False`` on the branches that
      don't set them) so a downstream consumer never has to guess
      whether a key is absent because it doesn't apply or because the
      branch simply forgot it.
    """
    phase_value = phase.value
    registered_ids: list[str] = []
    try:
        from egg_contracts.loader import load_contract

        contract = load_contract(pipeline_identifier, worktree_repo_path)
    except Exception as exc:  # noqa: BLE001
        _pkg.logger.warning(
            "Decision-ledger status: contract not loadable",
            pipeline_id=pipeline_id,
            phase=phase_value,
            error=str(exc),
        )
        contract = None

    if contract is not None:
        for d in contract.decisions:
            d_type = getattr(d.type, "value", d.type)
            if d_type != "hitl":
                continue
            d_phase = getattr(d.phase, "value", d.phase) if d.phase is not None else None
            if d_phase is None or d_phase == phase_value:
                registered_ids.append(d.id)

    if registered_ids:
        resolved = 0
        for d in contract.decisions:
            if d.id in registered_ids and d.resolved:
                resolved += 1
        return (
            f"Decision ledger: {len(registered_ids)} decision(s) registered this "
            f"phase ({', '.join(registered_ids)}), {resolved} resolved.",
            False,
            None,
            {
                "registered": registered_ids,
                "resolved": resolved,
                "explicit_none": False,
                "attested_by": None,
                "missing": False,
                "candidates_considered": [],
            },
        )

    explicit_none = _pkg._find_explicit_none_attestation(pipeline_id, phase_value)
    if explicit_none is not None:
        role, rationale, candidates = explicit_none
        candidate_note = f" ({len(candidates)} candidate(s) considered)" if candidates else ""
        return (
            f"Decision ledger: explicitly none — {role} attested: {rationale}{candidate_note}",
            False,
            explicit_none,
            {
                "registered": [],
                "resolved": 0,
                "explicit_none": True,
                "attested_by": role,
                "missing": False,
                "candidates_considered": candidates,
            },
        )

    return (
        "⚠️ Decision ledger MISSING: this phase registered no HITL decisions "
        "and no producer attested an explicit empty ledger (#3390). "
        "“0 decisions” here cannot be distinguished from “failed "
        "to register”.",
        True,
        None,
        {
            "registered": [],
            "resolved": 0,
            "explicit_none": False,
            "attested_by": None,
            "missing": True,
            "candidates_considered": [],
        },
    )


def _await_unresolved_gap_gate(
    store: _pkg.Any,
    pipeline_id: str,
    repo_path: _pkg.Path,
    worktree_repo_path: _pkg.Path,
    pipeline_identifier: int | str,
    phase: _pkg.PipelinePhase,
    hitl_gates: bool = True,
) -> bool:
    """Block phase finalize while the contract carries unresolved TaskGaps.

    A tester→coder :class:`TaskGap` left ``resolved == False`` ships into
    the committed contract snapshot and fails ``test_models_gaps.py`` red
    in CI on the already-open PR (#3298 class 4). The implement phase is
    **not** in ``_HITL_GATE_PHASES``, so the autonomous run loop would
    otherwise mark it complete and finalize with the gap open and no
    human in the loop. This surfaces a blocking ``phase_gate`` HITL
    decision listing the open gaps and waits — mirroring the
    unresolved-HITL guard in ``complete_phase`` (#1788). See #3300.

    The operator resolves the gap (set the gap's ``resolved=true`` via
    the contract-mutate path, e.g. by re-running/kicking the coder) and
    approves, or picks the override option to ship with the gap open. The
    contract is re-read after each approval so a stale ``approve`` cannot
    advance with a gap still open. Returns ``True`` when a gate was
    surfaced and the contract may have changed (resolved or overridden),
    ``False`` when the contract was already clean (the common path) or
    the escalation could only be logged (autonomous run, below).

    **Autonomous runs.** ``wait_for_decision`` polls indefinitely and
    both options require a human, so a fully-autonomous pipeline
    (``hitl_gates is False``) has no path forward — blocking here would
    convert a red-but-progressing PR into an indefinite stall (the
    health monitor would eventually tear it down). When ``hitl_gates is
    False`` we therefore *surface* the escalation (event + warning) but
    do **not** block: the reactive ``test_models_gaps.py`` CI check
    remains the backstop, exactly as it was before this gate existed.

    A best-effort scan: contract load failures fail open (log + return)
    so a transient read error can never strand the pipeline.
    """
    try:
        from egg_contracts.loader import load_contract
    except ImportError:
        _pkg.logger.warning(
            "egg_contracts not available, skipping unresolved-gap gate",
            pipeline_id=pipeline_id,
        )
        return False

    def _load_open_gaps() -> list[tuple[str, _pkg.Any]] | None:
        try:
            contract = load_contract(pipeline_identifier, worktree_repo_path)
        except Exception as e:  # noqa: BLE001
            _pkg.logger.warning(
                "Could not load contract for unresolved-gap gate (skipping)",
                pipeline_id=pipeline_id,
                error=str(e),
            )
            return None
        return contract.unresolved_gaps()

    open_gaps = _load_open_gaps()
    if not open_gaps:
        return False

    if not hitl_gates:
        # No human in the loop — do not block forever. Both options need a
        # human, so blocking would convert a red-but-progressing PR into an
        # indefinite stall. Surface the escalation (so observers still see
        # it) + log loudly, and let the reactive CI backstop catch the open
        # gap on the PR, exactly as before this gate existed.
        _pkg.report_pipeline_status(
            store.load_pipeline(pipeline_id),
            event_type="phase.gap_gate",
            message=f"{phase.value} phase has unresolved coverage gaps",
        )
        _pkg.logger.warning(
            "Unresolved-gap gate: open gaps on an autonomous pipeline "
            "(hitl_gates=False); surfacing but not blocking",
            pipeline_id=pipeline_id,
            phase=phase.value,
            open_gap_ids=[f"{t}/{g.id}" for t, g in open_gaps],
        )
        return False

    def _set_status(status: _pkg.PipelineStatus) -> _pkg.Pipeline:
        # Mirror the phase_gate block: drive both pipeline and the phase
        # box so the DAG visualization renders the gate on the right
        # phase, and the operator's wait-status monitor wakes.
        #
        # Only the RUNNING restore goes through here, and only after the
        # post-wait cancel check has cleared. The AWAITING_HUMAN park uses
        # ``_park_at_gate_unless_cancelled`` instead, which re-reads the
        # status from inside this same lock so it cannot overwrite an
        # operator cancel (#3633 review round 3).
        with _pkg.get_pipeline_state_lock(pipeline_id):
            pipeline = store.load_pipeline(pipeline_id)
            pipeline.status = status
            phase_execution = pipeline.get_phase_execution(phase)
            if phase_execution is not None:
                phase_execution.status = status
            store.save_pipeline(pipeline)
        return pipeline

    dq = _pkg.get_decision_queue(pipeline_id, repo_path)
    gated = False

    while open_gaps:
        gated = True
        gap_lines = "\n".join(
            f"- `{task_id}` / `{gap.id}` ({gap.from_role}→{gap.to_role}): {gap.description}"
            for task_id, gap in open_gaps
        )
        question = (
            f"The {phase.value} phase has {len(open_gaps)} unresolved coverage "
            f"gap{'s' if len(open_gaps) != 1 else ''}. Resolve "
            f"{'them' if len(open_gaps) != 1 else 'it'} (mark the gap resolved "
            "via the contract) and approve, or choose 'override' to finalize "
            "with the gap open."
        )
        context = (
            "These tester→coder coverage gaps are still open on the contract. "
            "Finalizing with an open gap ships it into the committed contract "
            "and fails CI (test_models_gaps.py) red on the PR.\n\n"
            f"{gap_lines}"
        )
        # Never mint a decision for a cancelled run. The cancel route sweeps
        # the pending queue once; a decision created after that sweep has
        # nobody to cancel it, and ``wait_for_decision`` below is an unbounded
        # poll — the driver thread would block for the process lifetime and
        # ``_run_pipeline``'s ``finally`` would never run (#3633 review round
        # 3). ``gated`` is already True here, which is the same value an
        # ordinary gating returns, so ``_run_implement_advance``'s own cancel
        # re-check is what stops the driver.
        if _pkg._pipeline_cancelled(store, pipeline_id):
            _pkg.logger.info(
                "Unresolved-gap gate: pipeline cancelled before the gate was "
                "queued — leaving the persisted CANCELLED intact (#3633)",
                pipeline_id=pipeline_id,
                phase=phase.value,
            )
            return gated
        decision = dq.queue_decision(
            question=question,
            context=context,
            options=["approve", "override"],
            decision_type="phase_gate",
            phase=phase,
        )

        # Mark AWAITING_HUMAN + surface to event watchers, mirroring the
        # phase_gate block so the operator's wait-status monitor wakes — but
        # only if the operator has not cancelled since the check above. The
        # re-read happens inside the same state lock as the write (the cancel
        # route persists CANCELLED under it), so the park cannot clobber the
        # cancel that the post-wait check below is trying to read.
        pipeline, _park_cancelled = _pkg._park_at_gate_unless_cancelled(store, pipeline_id, phase)
        if _park_cancelled:
            _pkg.logger.info(
                "Unresolved-gap gate: pipeline cancelled before parking at "
                "the gate — leaving the persisted CANCELLED intact (#3633)",
                pipeline_id=pipeline_id,
                phase=phase.value,
            )
            return gated
        _pkg.report_pipeline_status(
            pipeline,
            event_type="phase.gap_gate",
            message=f"{phase.value} phase has unresolved coverage gaps",
        )
        if _pkg._emit_event is not None:
            _pkg._emit_event(
                _pkg.EventType.DECISION_CREATED,
                pipeline_id,
                data={"phase": phase.value},
            )

        resolved = dq.wait_for_decision(decision.id)
        # ...unless what unblocked the wait was the operator cancelling the
        # pipeline, which sweeps this decision to CANCELLED. Restoring RUNNING
        # here would overwrite the persisted CANCELLED that every driver work
        # loop keys on, re-admitting the run the operator just stopped
        # (#3633 review). Bail before the status write, not after it.
        #
        # Skipping the write is necessary but not sufficient: ``gated`` is the
        # same value an ordinary gating returns, so the *caller* has to re-read
        # the cancel and stop the driver. ``_run_implement_advance`` does, and
        # must keep doing so — IMPLEMENT is terminal, so a driver that merely
        # falls through here writes COMPLETE over the CANCELLED this bail just
        # preserved (#3633 review round 2).
        if _pkg._pipeline_cancelled(store, pipeline_id):
            _pkg.logger.info(
                "Unresolved-gap gate: pipeline cancelled while awaiting the "
                "operator — leaving the persisted CANCELLED intact (#3633)",
                pipeline_id=pipeline_id,
                phase=phase.value,
            )
            return gated

        # Restore RUNNING now the gate cleared (re-set to AWAITING_HUMAN
        # above on the next loop if gaps remain).
        _set_status(_pkg.PipelineStatus.RUNNING)

        if resolved.status != _pkg.DecisionStatus.RESOLVED:
            # Cancelled / abandoned — don't spin; let the loop proceed so
            # a cancel can tear the pipeline down.
            _pkg.logger.warning(
                "Unresolved-gap gate ended without resolution; proceeding",
                pipeline_id=pipeline_id,
                phase=phase.value,
                decision_status=getattr(resolved.status, "value", resolved.status),
            )
            return gated

        resolution = (resolved.resolution or "").strip().lower()
        if "override" in resolution:
            _pkg.logger.warning(
                "Unresolved-gap gate overridden — finalizing with open gaps",
                pipeline_id=pipeline_id,
                phase=phase.value,
                open_gap_ids=[f"{t}/{g.id}" for t, g in open_gaps],
            )
            # Record the override on the frozen phase artifacts for audit
            # parity with the complete_phase endpoint's ``force`` path
            # (otherwise the load-bearing run-loop override left only a
            # transient log line). Values must be strings —
            # PhaseExecution.artifacts is dict[str, str].
            with _pkg.get_pipeline_state_lock(pipeline_id):
                pipeline = store.load_pipeline(pipeline_id)
                phase_execution = pipeline.get_phase_execution(phase)
                if phase_execution is not None:
                    merged = dict(phase_execution.artifacts)
                    merged["force_completed_gaps"] = _pkg.json.dumps(
                        [f"{t}/{g.id}" for t, g in open_gaps]
                    )
                    phase_execution.artifacts = merged
                    store.save_pipeline(pipeline)
            return gated

        # Approval path: re-read the contract. If the operator actually
        # marked the gaps resolved, the gate clears; otherwise re-surface.
        reloaded = _load_open_gaps()
        if reloaded is None:
            # Load failed — fail open rather than strand the pipeline.
            return gated
        open_gaps = reloaded
        if open_gaps:
            _pkg.logger.info(
                "Unresolved-gap gate approved but gaps still open; re-surfacing",
                pipeline_id=pipeline_id,
                phase=phase.value,
                remaining=len(open_gaps),
            )

    return gated


def _next_phases_for_epic(
    pipeline: _pkg.Pipeline,
    current_phase: _pkg.PipelinePhase,
    default_next_phases: list[_pkg.PipelinePhase],
) -> list[_pkg.PipelinePhase]:
    """Reroute auto-advance through ``APPLY`` for Jira-epic pipelines.

    Issue #1557: when ``pipeline.is_epic`` is true the orchestrator
    inserts the new ``APPLY`` phase between ``PLAN`` and ``IMPLEMENT``
    so the ``APPLIER`` role can drive Jira mutations (epic-Description
    write, child create / link / Won't-Do) on HITL approval. Non-epic
    pipelines see ``default_next_phases`` returned unchanged so the
    pre-#1557 scheduling is preserved bit-for-bit.

    The orchestrator-side scheduler is the authoritative gate per the
    architecture's "VALID_TRANSITIONS lists APPLY but the scheduler
    decides whether to actually pick it" design (see the comment on
    :data:`gateway.phase_transition.VALID_TRANSITIONS`). Returns a
    single-element list so the call site's ``next_phases[0]`` indexing
    works without change.
    """
    if not getattr(pipeline, "is_epic", False):
        return default_next_phases
    if current_phase == _pkg.PipelinePhase.PLAN:
        return [_pkg.PipelinePhase.APPLY]
    if current_phase == _pkg.PipelinePhase.APPLY:
        return [_pkg.PipelinePhase.IMPLEMENT]
    return default_next_phases


def _drain_wontdo_batch_after_apply(
    pipeline: _pkg.Pipeline,
    worktree_repo_path: _pkg.Path,
) -> None:
    """Run the orchestrator-only Won't-Do drain after ``APPLY`` consensus.

    Trigger chain (issue #1557 task-2-7): the HITL operator approves
    the plan-gate → ``_persist_phase_gate_resolution`` flips state →
    the scheduler routes through ``APPLY`` → the applier writes a
    handoff JSON at ``.egg-state/agent-outputs/<pipeline>-wontdo.json``
    listing every obsolete child key it could not transition itself
    (decision-15: agent-facing routes deny Jira transitions) → the
    APPLIER's CONSENSUS_PROPOSE → REVIEWER_CONTRACT ACK confirms →
    this hook fires from the auto-advance block, iterates the handoff,
    and POSTs to ``/api/v1/jira/ticket/transition`` with the launcher-
    secret bearer token.

    Runs **out of band** from ``_persist_phase_gate_resolution`` so a
    slow Jira API does not extend the HITL approve POST's latency SLA
    (task-2-7 acceptance). Fail-open: a missing handoff file means
    "no Won't-Dos to drain" and returns silently; a per-transition
    failure surfaces as a logger warning but does not block the
    pipeline from advancing to ``IMPLEMENT``.

    Naming note (reviewer_code v1 non-blocking): the handoff file
    this function READS is the applier's *output*
    (``<pipeline.id>-wontdo.json``), distinct from the applier's
    *input* handoff (``<pipeline.id>-apply-handoff.json``) written
    by :func:`_write_apply_phase_handoff` just before APPLY spawns.

    Per-Task lifecycle (reviewer_contract v1 finding #3 / task-2-7):
    the drain registers an ``on_entry_result`` callback with
    ``run_wontdo_drain``. After each transition attempt, the callback
    loads the contract via ``egg_contracts.loader.load_contract``,
    locates the corresponding Task (by ``task_id`` when the applier
    included one in the handoff entry, otherwise by ``jira_key``
    match), and writes ``Task.jira_action_status = 'applied'`` /
    ``'failed'`` plus the failure reason into ``Task.notes``. The
    write is best-effort: contract-load / save failures surface as a
    logger warning so a brittle contract state never breaks the
    drain — the operator can re-run later with the same handoff JSON
    (the gateway's idempotency cache absorbs the duplicate transition
    calls within the 5-minute window).
    """
    handoff_path = (
        _pkg.Path(worktree_repo_path)
        / ".egg-state"
        / "agent-outputs"
        / f"{pipeline.id}-wontdo.json"
    )
    if not handoff_path.exists():
        _pkg.logger.debug(
            "Won't-Do drain skipped — no handoff file produced by applier",
            pipeline_id=pipeline.id,
            handoff_path=str(handoff_path),
        )
        return

    # Per-entry contract writeback callback (reviewer_contract v1 #3).
    # Each invocation looks up the task by ``task_id`` (when the
    # applier set it on the handoff entry) or by ``jira_key`` match
    # otherwise, flips ``jira_action_status`` to ``'applied'`` /
    # ``'failed'`` and records the failure reason in ``Task.notes``.
    def _on_entry_result(entry: _pkg.Any, ok: bool, reason: str) -> None:
        try:
            try:
                from egg_contracts.loader import load_contract, save_contract
            except ImportError:  # pragma: no cover - defensive
                _pkg.logger.warning(
                    "Won't-Do drain: egg_contracts loader unavailable; "
                    "skipping per-Task lifecycle writeback",
                    pipeline_id=pipeline.id,
                )
                return
            try:
                contract = load_contract(pipeline.id, worktree_repo_path)
            except Exception as load_err:  # noqa: BLE001
                _pkg.logger.warning(
                    "Won't-Do drain: contract load failed; skipping per-Task lifecycle writeback",
                    pipeline_id=pipeline.id,
                    error=str(load_err),
                )
                return
            target_task = None
            entry_task_id = getattr(entry, "task_id", None)
            entry_key = getattr(entry, "jira_key", None)
            for sl in getattr(contract, "slices", []) or []:
                for tsk in getattr(sl, "tasks", []) or []:
                    if entry_task_id and tsk.id == entry_task_id:
                        target_task = tsk
                        break
                    if (
                        not entry_task_id
                        and entry_key
                        and getattr(tsk, "jira_key", None) == entry_key
                    ):
                        target_task = tsk
                        break
                if target_task is not None:
                    break
            if target_task is None:
                # No matching task — applier-written handoff may have
                # entries for keys outside the contract's task list
                # (e.g. consolidate-into "obsolete-only" rows). Log
                # at DEBUG since this is expected for split / consolidate
                # patterns.
                _pkg.logger.debug(
                    "Won't-Do drain: no contract task matches handoff entry; "
                    "skipping lifecycle writeback for this row",
                    pipeline_id=pipeline.id,
                    entry_task_id=entry_task_id,
                    entry_key=entry_key,
                )
                return
            target_task.jira_action_status = "applied" if ok else "failed"
            if not ok:
                existing_notes = target_task.notes or ""
                failure_note = f"wontdo drain failed: {reason}"
                target_task.notes = existing_notes + ("\n" if existing_notes else "") + failure_note
            try:
                save_contract(contract, worktree_repo_path)
            except Exception as save_err:  # noqa: BLE001
                _pkg.logger.warning(
                    "Won't-Do drain: contract save failed after lifecycle writeback",
                    pipeline_id=pipeline.id,
                    error=str(save_err),
                )
        except Exception as cb_err:  # noqa: BLE001 - defensive
            _pkg.logger.warning(
                "Won't-Do drain: per-Task callback raised (continuing)",
                pipeline_id=pipeline.id,
                error=str(cb_err),
            )

    # Contract-state idempotency gate. The drain consults this predicate
    # before posting each transition so a benign re-run (orchestrator
    # restart, manual re-drain, re-entry of the apply phase) does not
    # double-POST transitions whose outcomes the gateway's 5-minute
    # idempotency cache has long since forgotten — and does not flip an
    # ``'applied'`` Task back to ``'failed'`` when Jira returns 400 for
    # an already-transitioned ticket.
    def _entry_already_applied(entry: _pkg.Any) -> bool:
        try:
            try:
                from egg_contracts.loader import load_contract
            except ImportError:  # pragma: no cover - defensive
                _pkg.logger.warning(
                    "Won't-Do drain idempotency gate disarmed: egg_contracts.loader not importable",
                    pipeline_id=pipeline.id,
                )
                return False
            try:
                contract = load_contract(pipeline.id, worktree_repo_path)
            except Exception as load_err:  # noqa: BLE001 - defensive
                # Contract unreadable / corrupted: idempotency gate is
                # disarmed for this drain run. The drain re-POSTs every
                # entry, Jira returns 400 for already-transitioned ones,
                # and ``_on_entry_result`` flips ``'applied'`` →
                # ``'failed'`` — surface this loudly so the operator can
                # repair the contract before the next re-run.
                _pkg.logger.warning(
                    "Won't-Do drain idempotency gate disarmed: load_contract failed",
                    pipeline_id=pipeline.id,
                    error=str(load_err),
                )
                return False
            entry_task_id = getattr(entry, "task_id", None)
            entry_key = getattr(entry, "jira_key", None)
            for sl in getattr(contract, "slices", []) or []:
                for tsk in getattr(sl, "tasks", []) or []:
                    matches_task = bool(entry_task_id and tsk.id == entry_task_id)
                    matches_key = bool(
                        not entry_task_id
                        and entry_key
                        and getattr(tsk, "jira_key", None) == entry_key
                    )
                    if matches_task or matches_key:
                        return getattr(tsk, "jira_action_status", None) == "applied"
            return False
        except Exception as predicate_err:  # noqa: BLE001 - defensive
            _pkg.logger.warning(
                "Won't-Do drain idempotency gate raised; treating entry as not-yet-applied",
                pipeline_id=pipeline.id,
                error=str(predicate_err),
            )
            return False

    try:
        # Reviewer_code v1 non-blocking note: mirror the dual-import
        # pattern used elsewhere in this module (e.g. ``from
        # jira_epic import resolve_epic_mode``) so the helper still
        # resolves when ``orchestrator/`` is imported as a package
        # rather than treated as ``sys.path`` root.
        try:
            from wontdo_drain import run_wontdo_drain
        except ImportError:  # pragma: no cover — packaged-import fallback
            from orchestrator.wontdo_drain import run_wontdo_drain  # type: ignore[no-redef]

        result = run_wontdo_drain(
            handoff_path=handoff_path,
            on_entry_result=_on_entry_result,
            is_already_applied=_entry_already_applied,
        )
    except Exception as exc:  # noqa: BLE001 — defensive: drain must not crash auto-advance
        _pkg.logger.warning(
            "Won't-Do drain failed after APPLY phase (continuing)",
            pipeline_id=pipeline.id,
            error=str(exc),
        )
        return
    _pkg.logger.info(
        "Won't-Do drain complete after APPLY phase",
        pipeline_id=pipeline.id,
        succeeded=len(result.succeeded),
        failed=len(result.failed),
        skipped=len(result.skipped),
    )


def _write_apply_phase_handoff(
    pipeline: _pkg.Pipeline,
    worktree_repo_path: _pkg.Path,
    approved_phase: str,
) -> None:
    """Write the applier handoff JSON before the ``APPLY`` phase spawns.

    The applier prompt consumes a one-line JSON identifying which
    artifact was just approved so it can branch between refine-apply
    (writing the analysis to the epic Description) and plan-apply
    (walking ``Task.jira_action`` + driving the Jira CLI per task).

    The handoff lands at
    ``.egg-state/agent-outputs/<pipeline-id>-apply-handoff.json``
    inside the per-pipeline worktree so the applier (running in a
    sandbox container with the same worktree mounted) reads from a
    deterministic path. Fail-open: I/O errors surface as a logger
    warning but never abort phase advancement.
    """
    handoff_dir = _pkg.Path(worktree_repo_path) / ".egg-state" / "agent-outputs"
    try:
        handoff_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _pkg.logger.warning(
            "Failed to create agent-outputs dir for applier handoff (continuing)",
            pipeline_id=pipeline.id,
            error=str(exc),
        )
        return
    contract_path = (
        _pkg.Path(worktree_repo_path) / ".egg-state" / "contracts" / f"{pipeline.id}.json"
    )
    draft_path = (
        _pkg.Path(worktree_repo_path)
        / ".egg-state"
        / "brc-history"
        / f"{pipeline.id}-{approved_phase}.md"
    )
    payload = {
        "approved_phase": approved_phase,
        "contract_path": str(contract_path),
        "draft_path": str(draft_path),
    }
    handoff_path = handoff_dir / f"{pipeline.id}-apply-handoff.json"
    try:
        handoff_path.write_text(_pkg.json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        _pkg.logger.warning(
            "Failed to write applier handoff JSON (continuing)",
            pipeline_id=pipeline.id,
            handoff_path=str(handoff_path),
            error=str(exc),
        )
        return
    _pkg.logger.info(
        "Applier handoff JSON written for APPLY phase",
        pipeline_id=pipeline.id,
        approved_phase=approved_phase,
        handoff_path=str(handoff_path),
    )


def _persist_phase_gate_resolution(
    repo_path: _pkg.Path,
    pipeline_id: str,
    decision: _pkg.HITLDecision,
    phase: str,
    issue_number: int | None = None,
) -> None:
    """Persist a phase-gate resolution to the contract and draft.

    After a human approves a phase gate, the resolution context needs to be
    visible to agents in the next phase.  This function:

    1. Adds the resolution as a HITL decision in the contract so next-phase
       agents see it when they load the contract.
    2. Appends a ``## HITL Resolution`` section to the phase draft file so
       agents reading the draft also see the human's decisions.

    See: #1295
    """
    # Extract structured context from JSON resolution, or classify a bare string
    resolution_context: str = ""
    raw = (decision.resolution or "").strip()
    if raw:
        payload: object = None
        try:
            payload = _pkg.json.loads(raw)
        except _pkg.json.JSONDecodeError, TypeError:
            payload = None

        if isinstance(payload, dict):
            # Coerced for the same reason the gate coerces its own read of
            # these fields: nothing type-checks them, and a non-string
            # ``context`` lands in ``Decision(resolution=...)`` below,
            # where pydantic rejects it (``resolution: str | None``) and
            # the broad ``except`` swallows it as a warning — so the
            # operator approves with a note and the note never reaches the
            # contract (#3636 review). Coercing here also keeps the draft
            # section rendering the JSON form rather than a Python repr.
            # ``.strip()`` mirrors the gate's own read at
            # ``_run_hitl_gate.py`` so a whitespace-only ``context`` is
            # falsy for both readers, rather than threading no operator
            # note at the gate while persisting a blank one here.
            resolution_context = _pkg._coerce_gate_resolution_text(
                payload.get("context") or payload.get("feedback")
            ).strip()
            if not resolution_context:
                _pkg.logger.debug(
                    "Phase gate approved without context, nothing to persist",
                    pipeline_id=pipeline_id,
                    phase=phase,
                )
                return
        else:
            # A bare string is never valid JSON, so every non-structured
            # resolution lands here. Route it through the same first-line
            # classifier the gate uses (#3636) instead of storing it raw:
            #
            # * ``approve`` alone yields ``""`` → the early return below, so
            #   a process-control answer is excluded from the contract and
            #   draft, which is the rule ``docs/hitl-decisions.md`` states
            #   and which the structured form already honoured.
            # * ``approve\n\n<note>`` yields ``<note>`` — the same spelling
            #   the gate threads into the re-run as ``Operator note at the
            #   gate``, rather than the note with the option word glued on.
            #
            # The non-approve arm matters: ``_routes_lifecycle`` calls this
            # helper on the stored gate decision outside the gate's
            # approve-only path, so a change request's specifics must still
            # persist — hence the ``_feedback or ""`` fallback rather than
            # dropping every non-approval.
            _approved, _feedback, _context = _pkg._classify_bare_gate_resolution(raw)
            resolution_context = _context if _approved else (_feedback or "")

    if not resolution_context:
        _pkg.logger.debug(
            "Phase gate resolution has no context to persist",
            pipeline_id=pipeline_id,
            phase=phase,
        )
        return

    # --- 1. Sync to contract ---
    try:
        from egg_contracts.loader import load_contract, save_contract
        from egg_contracts.models import Decision, DecisionOption, DecisionType

        contract = load_contract(pipeline_id, repo_path)

        existing_questions = {d.question for d in contract.decisions}
        question_text = f"[Phase gate: {phase}] {decision.question}"

        if question_text not in existing_questions:
            # Determine next decision ID
            max_existing_id = 0
            for d in contract.decisions:
                try:
                    num = int(d.id.split("-")[1])
                    max_existing_id = max(max_existing_id, num)
                except IndexError, ValueError:
                    pass

            contract_options = [
                DecisionOption(id=f"opt-{i + 1}", label=opt)
                for i, opt in enumerate(decision.options)
            ]

            contract_decision = Decision(
                id=f"decision-{max_existing_id + 1}",
                question=question_text,
                type=DecisionType.HITL,
                options=contract_options,
                resolved=True,
                resolution=resolution_context,
                resolved_by="human",
                resolved_at=decision.resolved_at,
            )
            contract.decisions.append(contract_decision)
            save_contract(contract, repo_path)
            _pkg.logger.info(
                "Persisted phase gate resolution to contract",
                pipeline_id=pipeline_id,
                phase=phase,
            )
    except ImportError:
        _pkg.logger.warning("egg_contracts not available, skipping phase gate contract sync")
    except Exception:
        _pkg.logger.warning(
            "Failed to persist phase gate resolution to contract (continuing)",
            pipeline_id=pipeline_id,
            phase=phase,
            exc_info=True,
        )

    # --- 2. Append to draft ---
    try:
        draft_rel = _pkg._get_draft_path(phase, issue_number, pipeline_id)
        if draft_rel:
            draft_path = repo_path / draft_rel
            if draft_path.exists():
                existing = draft_path.read_text(encoding="utf-8")
                if "## HITL Resolution" not in existing:
                    section = (
                        f"\n\n## HITL Resolution\n\n"
                        f"The following was approved by a human reviewer at the "
                        f"{phase} phase gate:\n\n{resolution_context}\n"
                    )
                    draft_path.write_text(existing + section, encoding="utf-8")
                    _pkg.logger.info(
                        "Appended HITL resolution to draft",
                        pipeline_id=pipeline_id,
                        phase=phase,
                        draft=draft_rel,
                    )
    except Exception:
        _pkg.logger.warning(
            "Failed to append phase gate resolution to draft (continuing)",
            pipeline_id=pipeline_id,
            phase=phase,
            exc_info=True,
        )
