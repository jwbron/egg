"""Considered-candidate helpers for the decision ledger (#3526).

The structured explicit-none ledger: rendering ``candidates_considered``
entries for operator surfaces, recovering refine's ``deferred_to_plan``
candidates for the plan-phase handoff, building the plan-prompt section
that pre-seeds them, and persisting the gate-time ledger summary.
Barrel-resident and test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _format_considered_candidates(candidates: list[dict]) -> str:
    """Render ``candidates_considered`` entries as a markdown bullet list (#3526).

    One bullet per candidate (question, disposition, why) so the
    operator confirms an enumerated claim rather than a free-form
    paragraph. Malformed entries render best-effort (propose-time
    validation makes them unlikely, but this surface must never crash
    the gate).
    """
    lines = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        question = str(c.get("question") or "?").strip()
        disposition = str(c.get("disposition") or "?").strip()
        why = str(c.get("why") or "").strip()
        label = "deferred to plan" if disposition == "deferred_to_plan" else disposition
        lines.append(f"- **{question}** ({label}): {why}")
    return "\n".join(lines)


def _find_deferred_plan_candidates(pipeline_id: str) -> list[dict]:
    """Collect refine's ``deferred_to_plan`` candidates for the plan phase (#3526).

    Scans the refine phase's ``CONSENSUS_PROPOSE`` messages (newest
    first) for the latest proposal whose attestation carries
    ``candidates_considered``, and returns the entries dispositioned
    ``deferred_to_plan``. This is what turns a refine deferral into a
    handoff: the plan prompt pre-seeds these as candidates the planner
    must register (or disposition ``not_operator_grade`` with a concrete
    why). Candidates ride on both ledger forms; a refiner that
    registered decisions AND deferred others is picked up the same way.
    Message-store outages degrade to an empty list (the plan phase
    simply gets no pre-seeded section, matching pre-#3526 behavior).
    """
    try:
        from message_store import MessageType, get_message_store

        messages = get_message_store().get_messages(pipeline_id, limit=500)
    except Exception as exc:  # noqa: BLE001
        _pkg.logger.warning(
            "Deferred-candidate scan failed (treating as none)",
            pipeline_id=pipeline_id,
            error=str(exc),
        )
        return []

    for message in reversed(messages):
        if message.message_type != MessageType.CONSENSUS_PROPOSE:
            continue
        if message.phase is not None and message.phase != "refine":
            continue
        payload = (message.metadata or {}).get("payload")
        if not isinstance(payload, dict):
            continue
        attestation = payload.get("attestation")
        if not isinstance(attestation, dict):
            continue
        candidates = attestation.get("candidates_considered")
        if not isinstance(candidates, list) or not candidates:
            continue
        return [
            c
            for c in candidates
            if isinstance(c, dict) and c.get("disposition") == "deferred_to_plan"
        ]
    return []


def _format_deferred_candidates_with_ids(candidates: list[dict]) -> str:
    """Render deferred candidates as bullets carrying their ``dq-`` ids (#3564).

    Like :func:`_format_considered_candidates` but prefixes each bullet
    with the stable ``dq-<hash>`` identity the propose-time coverage gate
    recomputes (``egg_contracts.decisions.deferred_question_id``). The id
    is what the plan producer echoes back in its attestation's
    ``deferred_resolutions``, so it must render verbatim and
    copy-pastable. Malformed entries render best-effort — this surface
    must never crash prompt assembly.
    """
    from egg_contracts.decisions import deferred_question_id

    lines = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        # Skip a blank-question candidate rather than render a
        # ``deferred_question_id("?")`` bullet the gate never expects
        # (#3564): ``_validate_deferred_candidate_coverage`` computes the
        # expected ids off the stripped question and skips blanks, so a "?"
        # id rendered here could not be satisfied — the architect would echo
        # it and the gate would false-NACK it with no way to cover it, a
        # plan-phase deadlock. Keeping both surfaces on the same skip-blank
        # rule closes that latent trap if the refine-side non-empty-question
        # invariant is ever relaxed.
        question = str(c.get("question") or "").strip()
        if not question:
            continue
        why = str(c.get("why") or "").strip()
        lines.append(f"- `{deferred_question_id(question)}` — **{question}**: {why}")
    return "\n".join(lines)


def _build_deferred_candidates_section(pipeline_id: str | None) -> list[str]:
    """Render refine's ``deferred_to_plan`` candidates for the plan prompt (#3526).

    Backfill data on #3526 showed deferral-to-plan was a black hole: refine
    registrations fell after the slice/packaging carve-out while plan
    registrations never rose. This section makes deferral a handoff: the
    planner receives the refiner's named deferred candidates as pre-seeded
    items it must register or explicitly disposition. Each candidate
    carries a stable ``dq-<hash>`` id (#3564) that the producer must echo
    in its attestation's ``deferred_resolutions`` — the propose-time gate
    recomputes the ids and NACKs a plan proposal that leaves one
    unaccounted. Returns an empty list (no section) when the pipeline has
    no deferred candidates or the message store is unavailable, matching
    pre-#3526 behavior.
    """
    if not pipeline_id:
        return []
    try:
        deferred = _pkg._find_deferred_plan_candidates(pipeline_id)
    except Exception as exc:  # noqa: BLE001
        _pkg.logger.warning(
            "Deferred-candidate prompt section skipped",
            pipeline_id=pipeline_id,
            error=str(exc),
        )
        return []
    if not deferred:
        return []
    rendered = _pkg._format_deferred_candidates_with_ids(deferred)
    if not rendered:
        return []
    return [
        "**Deferred from refine (#3526): you MUST close these out.** The "
        "refine phase deferred the following decision candidates to plan; "
        "they were surfaced to the operator only as dispositions, so they "
        "have NOT been decided:\n",
        rendered,
        "",
        "For EACH candidate above, either register it via "
        "`egg-contract add-decision` once your design makes the options "
        "concrete (put your recommended option first; reframing the "
        "question as the design firms up is fine), or disposition it "
        "`not_operator_grade` with a concrete why (e.g. the design "
        "dissolved the choice).\n",
        "Then, when you propose, echo EVERY `dq-` id above in your "
        "attestation via repeated `--deferred` flags (#3564). These ride "
        "on top of — they do not replace — your ledger attestation, so keep "
        "the `--decisions-registered` (or `--no-decisions-rationale "
        "--considered ...`) flag your propose already needs; `--deferred` "
        "alone yields an attestation missing the required exactly-one-of "
        "ledger field and is shape-NACKed:\n",
        "```bash",
        "egg-orch consensus propose ... \\",
        '  --decisions-registered "cq-<N>,..." \\',
        '  --deferred "dq-<hash> :: registered :: cq-<N>" \\',
        '  --deferred "dq-<hash> :: not_operator_grade :: <why the design dissolved it>"',
        "```",
        "The orchestrator recomputes these ids from the refine attestation "
        "and REJECTS your proposal if any deferred question is neither "
        "registered nor dissolved — a deferred candidate that silently "
        "disappears is exactly the leak this section exists to close.\n",
    ]


def _persist_decision_ledger_summary(
    store,
    pipeline_id: str,
    current_phase: _pkg.PipelinePhase,
    summary: dict,
):
    """Persist the gate-time ledger summary onto ``PhaseExecution`` (#3526).

    The per-phase decision ledger was previously reconstructible only by
    parsing HITL question text out of pipeline state, which is how the
    #3526 backfill dated the surfacing regression, weeks after the fact.
    Persisting the structured summary makes decisions-surfaced-per-phase
    a first-class, queryable signal. Best-effort: a persistence failure
    logs and returns ``None`` (the caller keeps its current pipeline
    binding); it must never block the gate.
    """
    try:
        with _pkg.get_pipeline_state_lock(pipeline_id):
            pipeline = store.load_pipeline(pipeline_id)
            pipeline.get_phase_execution(current_phase).decision_ledger = summary
            store.save_pipeline(pipeline)
            return pipeline
    except Exception as exc:  # noqa: BLE001
        _pkg.logger.warning(
            "Decision-ledger summary persistence failed (continuing)",
            pipeline_id=pipeline_id,
            phase=current_phase.value,
            error=str(exc),
        )
        return None
