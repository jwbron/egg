"""plan-draft synthesis + contract population helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal  # noqa: F401

import routes.pipelines as _pkg  # noqa: E402,F401

if TYPE_CHECKING:
    from egg_contracts.models import Slice as ContractSlice  # noqa: F401


def _synthesize_plan_draft(
    repo_path: _pkg.Path,
    pipeline_id: str,
    pipeline_mode: str = "issue",
    issue_number: int | None = None,
) -> None:
    """Synthesize a plan draft from multi-agent plan outputs.

    In multi-agent plan mode, ARCHITECT and RISK_ANALYST write to
    .egg-state/agent-outputs/.  TASK_PLANNER writes the plan draft
    directly to .egg-state/drafts/{id}-plan.md.  This function combines
    the remaining agent outputs into the plan draft (if the task_planner
    has not already written one) so that _populate_contract_from_plan()
    and the HITL gate can find it.
    """
    draft_rel = _pkg._get_draft_path("plan", issue_number=issue_number, pipeline_id=pipeline_id)
    if not draft_rel:
        _pkg.logger.debug(
            "No draft path for plan phase, skipping synthesis",
            pipeline_id=pipeline_id,
        )
        return

    draft_path = repo_path / draft_rel
    if draft_path.exists():
        # Draft already written (e.g. by a single-agent run) — don't overwrite.
        return

    outputs_dir = repo_path / ".egg-state" / "agent-outputs"
    if not outputs_dir.is_dir():
        _pkg.logger.warning(
            "No agent-outputs directory, cannot synthesize plan draft",
            pipeline_id=pipeline_id,
        )
        return

    # Derive the pipeline identifier for namespaced output filenames.
    _synth_id = _pkg._pipeline_identifier(issue_number, pipeline_id)

    from egg_contracts.artifact_spec import resolve_artifact_path

    sections: list[str] = []
    # Spec *names* — not bare filenames — so the agent-output path knowledge
    # lives only in egg_contracts.artifact_spec (the slice-2 single-source-of-
    # truth ratchet covers this reader, not just the prompt builder).
    # ``resolve_artifact_path("<name>", id)`` yields the namespaced
    # ``.egg-state/agent-outputs/{id}-<file>`` path; the old un-namespaced
    # global filename (basename minus the ``{id}-`` prefix) stays the fallback.
    agent_specs = [
        ("architect-output", "Architecture Analysis"),
        ("architect-slices", "Slice Scaffold"),
        ("risk-analyst-output", "Risk Assessment"),
    ]

    for spec_name, heading in agent_specs:
        prefixed_rel = resolve_artifact_path(spec_name, _synth_id)
        global_filename = _pkg.Path(prefixed_rel).name.removeprefix(f"{_synth_id}-")
        # Try prefixed filename first, fall back to old global filename
        prefixed_file = repo_path / prefixed_rel
        if prefixed_file.exists():
            output_file = prefixed_file
        else:
            output_file = outputs_dir / global_filename
        if not output_file.exists():
            continue
        try:
            raw = output_file.read_text()
            data = _pkg.json.loads(raw)
            # Agent outputs may contain a "content" or "output" key with
            # the main text, or may be the full JSON blob.
            content = data.get("content") or data.get("output") or _pkg.json.dumps(data, indent=2)
        except _pkg.json.JSONDecodeError:
            # Fall back to raw text if not valid JSON
            content = raw
        except Exception as e:
            _pkg.logger.warning(
                "Failed to read agent output for plan draft",
                pipeline_id=pipeline_id,
                file=global_filename,
                error=str(e),
            )
            continue

        # Skip empty or whitespace-only outputs
        if not content or not content.strip():
            _pkg.logger.warning(
                "Agent output is empty, skipping from plan draft",
                pipeline_id=pipeline_id,
                file=global_filename,
            )
            continue

        sections.append(f"## {heading}\n\n{content}")

    if not sections:
        _pkg.logger.warning(
            "No agent outputs found to synthesize plan draft",
            pipeline_id=pipeline_id,
        )
        return

    draft_content = "\n\n".join(sections) + "\n"

    # Guard against a draft that has section headings but no real content.
    stripped = draft_content
    for _, heading in agent_specs:
        stripped = stripped.replace(f"## {heading}", "")
    if len(stripped.strip()) < _pkg._MIN_PLAN_DRAFT_CONTENT_LENGTH:
        _pkg.logger.warning(
            "Synthesized plan draft has insufficient content, not writing",
            pipeline_id=pipeline_id,
            content_length=len(stripped.strip()),
        )
        return

    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(draft_content, encoding="utf-8")
    _pkg.logger.info(
        "Synthesized plan draft from agent outputs",
        pipeline_id=pipeline_id,
        path=str(draft_path),
        sections=len(sections),
    )


def _slice_gate_block_monolithic_demotion(
    worktree_repo_path: _pkg.Path,
    pipeline_id: str,
    issue_number: int | None,
) -> "SliceGateMonolithicBlock | None":  # noqa: UP037 — forward ref; see docstring
    """#2337 defensive recheck for the slice-loop gate.

    Called only when ``contract.slices`` is empty at implement-phase entry.
    Returns a :class:`SliceGateMonolithicBlock` when the on-disk plan
    draft parses to N>1 slices — the exact contract+plan mismatch that
    demoted issue-2261's 15-slice plan to a monolithic slice-1 PR
    (#2337).  When this fires the implement phase should be marked
    FAILED rather than silently routed through ``_run_concurrent_phase``.

    The returned tuple carries the human-readable ``message`` plus the
    parsed ``draft_slice_count`` so the caller can emit a dedicated HITL
    naming the divergence inline without having to re-parse the message
    (#2627 follow-up).  The annotation is quoted because
    ``SliceGateMonolithicBlock`` is declared further down the module to
    keep it grouped with the other #2627 follow-up types.

    Returns ``None`` when:
    * The plan draft is missing on local — there's nothing to parse, and
      the populator's own ``plan_draft_missing`` warning already covers
      that case (with ``source="plan_complete"`` it raises so we wouldn't
      reach this gate at all).
    * The plan parses to 0 or 1 slice — single-slice/no-slice contracts
      legitimately use the monolithic path.
    * Plan parsing fails — defensive: don't block on a parser regression,
      just log and let the gate fall through to monolithic.
    """
    draft_rel = _pkg._get_draft_path("plan", issue_number=issue_number, pipeline_id=pipeline_id)
    if not draft_rel:
        return None
    draft_path = worktree_repo_path / draft_rel
    if not draft_path.exists():
        return None
    try:
        from egg_contracts.plan_parser import parse_plan as _parse_plan_for_gate

        plan_text = draft_path.read_text()
        parsed = _parse_plan_for_gate(plan_text)
        if not parsed.success:
            return None
        draft_slice_count = len(parsed.to_contract_slices())
    except Exception as parse_err:  # noqa: BLE001
        _pkg.logger.debug(
            "Slice-loop gate: draft re-parse failed",
            pipeline_id=pipeline_id,
            error=str(parse_err),
        )
        return None
    if draft_slice_count <= 1:
        return None
    return _pkg.SliceGateMonolithicBlock(
        message=(
            f"plan draft parses to {draft_slice_count} slices but contract.slices "
            f"is empty — populator silently failed earlier (#2337); refusing to "
            f"demote to monolithic implement"
        ),
        draft_slice_count=draft_slice_count,
    )


class PlanDraftMissingOnLocalError(RuntimeError):
    """Raised by the natural plan-completion populator path when the plan
    draft is missing from the local worktree but present on origin.

    This is the silent-failure mode behind #2337: a multi-slice plan-phase
    pipeline whose populator returned without slices because
    ``_sync_worktree_with_remote`` left agents' plan-phase commits on
    origin.  Surfacing as an exception lets the natural call site mark
    the pipeline FAILED instead of silently demoting to monolithic
    implement.  The force-advance call site (#1941) keeps swallowing.
    """


class PlanDraftMissingOnLocalAndOriginError(RuntimeError):
    """Raised by the natural plan-completion populator path when the plan
    draft is missing from BOTH the local worktree and origin.

    Symmetric to :class:`PlanDraftMissingOnLocalError` (#2337) for the
    case where the draft was deleted-and-not-replaced rather than left
    on origin only.  Observed in the wild on issue-1557-v2 (#2627): the
    orchestrator's pre-sync state-write commit deleted the draft and
    the consolidated-write step never replaced it, leaving the pipeline
    to advance to implement with an empty contract and 8 agents
    spinning ``WAITING_FOR_EVENT`` for ~45 min.  Surfacing as an
    exception lets the natural call site mark the pipeline FAILED so
    the operator can intervene.  The force-advance call site (#1941)
    keeps swallowing.
    """


class PopulateOutcome(_pkg.StrEnum):
    """Structured discriminator for :func:`_populate_contract_from_plan` outcomes.

    Added in #2627 follow-up: previously the populator returned ``None``
    on every branch (success, draft-missing, parse-failed, etc.), so
    callers couldn't tell "populated N>0 tasks" from "silently produced
    an empty contract" without re-loading the contract and counting.
    The slice-gate guard at implement-phase entry catches the empty
    contract case after the orchestrator has already transitioned to
    implement, leaving a generic Retry/Accept/Abort HITL that respawns
    into the same broken state.  A structured outcome lets the
    plan-complete and start_phase=implement call sites fail-fast at
    the boundary with an actionable HITL inline.
    """

    POPULATED = "populated"
    DRAFT_MISSING = "draft_missing"
    NO_DRAFT_PATH = "no_draft_path"
    PARSE_FAILED = "parse_failed"
    EMPTY_RESULT = "empty_result"
    CONTRACT_LOAD_FAILED = "contract_load_failed"
    EGG_CONTRACTS_UNAVAILABLE = "egg_contracts_unavailable"
    FOREST_VIOLATION = "forest_violation"
    # #3046 — two slices touch overlapping files with no dependency edge
    # between them; rejected at ingestion like a forest violation.
    SLICE_OVERLAP_VIOLATION = "slice_overlap_violation"
    UNEXPECTED_EXCEPTION = "unexpected_exception"


class PopulateProducedEmptyContractError(RuntimeError):
    """Raised at the natural plan-completion call site when
    :func:`_populate_contract_from_plan_safe` returns a ``PopulateResult``
    whose outcome indicates the populate step did not produce a contract
    with tasks the implement-phase agents can act on.

    Two shapes:

    * ``outcome != POPULATED`` — the populator returned a non-success
      outcome (``EMPTY_RESULT``, ``PARSE_FAILED``, ``CONTRACT_LOAD_FAILED``,
      ``EGG_CONTRACTS_UNAVAILABLE``, ``FOREST_VIOLATION``,
      ``UNEXPECTED_EXCEPTION``, ``NO_DRAFT_PATH``).  ``DRAFT_MISSING`` at
      ``source="plan_complete"`` is pre-empted by
      :class:`PlanDraftMissingOnLocalError` /
      :class:`PlanDraftMissingOnLocalAndOriginError` so it never reaches
      this exception via that path.
    * ``outcome == POPULATED`` with ``slice_count == 0`` — the populator
      considered itself "changed" (PR metadata populated, or
      ``current_phase`` advanced) but produced no slices/tasks, so
      implement-phase agents would have nothing to do.  This is the
      orthogonal silent-corruption shape flagged in #2627's "Additionally
      — and orthogonally" paragraph (#2627 review).

    Orthogonal to :class:`PlanDraftMissingOnLocalError` /
    :class:`PlanDraftMissingOnLocalAndOriginError` (which fire when the
    draft is missing from one or both refs).  The slice-gate at
    implement-phase entry would catch most of these later, but failing at
    the boundary lets the same dedicated HITL fire for both paths.
    Force-advance call sites (#1941) keep swallowing — they inspect the
    return value but never raise.
    """

    def __init__(self, outcome: _pkg.PopulateOutcome, slice_count: int = 0) -> None:
        if outcome == _pkg.PopulateOutcome.POPULATED:
            # Populator returned "changed=True" but produced no slices/tasks
            # (only PR metadata or current_phase advance changed).  #2627
            # review's "POPULATED with slice_count == 0" case.
            message = (
                "plan populate completed but produced 0 slices/tasks — "
                "refusing to advance plan phase with empty contract"
            )
        else:
            message = (
                f"plan populate produced {outcome.value} outcome — refusing to "
                f"advance plan phase with empty contract"
            )
        super().__init__(message)
        self.outcome = outcome
        self.slice_count = slice_count


class PopulateResult(_pkg.NamedTuple):
    """Return type of :func:`_populate_contract_from_plan` and its safe wrapper.

    ``slice_count`` and ``task_count`` are populated only on
    ``POPULATED`` (zero on every failure outcome).  ``FOREST_VIOLATION``
    is observed at the wrapper after catching the inner raise — the
    inner function continues to ``raise ForestValidationError`` so
    HTTP callers keep their 422 contract.
    """

    outcome: _pkg.PopulateOutcome
    slice_count: int = 0
    task_count: int = 0


class SliceGateMonolithicBlock(_pkg.NamedTuple):
    """Return type of :func:`_slice_gate_block_monolithic_demotion`.

    Carries the human-readable failure message plus the parsed slice
    count so callers can emit a structured HITL naming the divergence
    inline (#2627 follow-up).  Previously the helper returned a bare
    ``str`` and the slice count had to be re-parsed from the message,
    making the dedicated HITL payload awkward to build.
    """

    message: str
    draft_slice_count: int


def _populate_result_is_empty_contract(result: _pkg.PopulateResult) -> bool:
    """Return True if a ``PopulateResult`` means the contract is empty/broken.

    Centralizes the fail-fast condition used by the natural plan-complete
    handler and the ``start_phase=implement`` safety net.  The two
    branches it discriminates:

    * ``outcome != POPULATED`` — the populator reported any non-success
      outcome (``EMPTY_RESULT``, ``PARSE_FAILED``, ``CONTRACT_LOAD_FAILED``,
      ``EGG_CONTRACTS_UNAVAILABLE``, ``FOREST_VIOLATION``,
      ``UNEXPECTED_EXCEPTION``, ``DRAFT_MISSING``, ``NO_DRAFT_PATH``).
      ``DRAFT_MISSING`` at ``source="plan_complete"`` is pre-empted by
      the ``PlanDraftMissing*`` raises in the safe wrapper so it does
      not reach this check via that path; the safety net (which calls
      the inner directly with no source) does see it here.
    * ``outcome == POPULATED`` with ``slice_count == 0`` — the populator
      considered itself "changed" (PR metadata populated, or
      ``current_phase`` advanced) but produced no slices/tasks, so the
      implement-phase agents would have nothing to do.  Flagged in the
      "Additionally — and orthogonally" paragraph on #2627 and the
      review's "POPULATED with slice_count == 0 still silently advances"
      observation.

    Extracted so the call-site check is unit-testable without standing
    up the full ``_run_pipeline`` integration setup, and so the two
    call sites can't drift out of agreement.  Re #2627 review.
    """
    return result.outcome != _pkg.PopulateOutcome.POPULATED or result.slice_count == 0


def _empty_contract_hitl_question(
    *,
    pipeline_id: str,
    reason: str,
    draft_slice_count: int | None,
    gate: str,
) -> str:
    """Build the HITL question text naming the empty-contract root cause inline.

    ``pipeline_id`` is interpolated into the recovery URL so operators can
    copy it verbatim instead of substituting a literal ``{id}`` placeholder
    by hand (#2627 review).  ``reason`` is the operator-visible identifier
    (typically a :class:`PopulateOutcome` value or the slice-gate's own
    discriminator).  ``draft_slice_count`` is None when the plan draft
    itself could not be parsed (so we can't quote a count).  ``gate`` names
    the call site that detected the divergence — ``slice_gate`` /
    ``start_phase_implement_safety_net`` / ``plan_complete`` — so the
    operator sees which guard fired.

    The opening phrase is "Pipeline blocked at {gate}" rather than
    "Implement-phase blocked at {gate}": ``gate=plan_complete`` fires while
    the *plan* phase is being marked FAILED, before the implement phase is
    spawned, so the implement-specific phrasing would read oddly against
    ``pipeline.error`` and the phase-execution status (#2627 review).
    """
    if draft_slice_count is not None:
        divergence_line = (
            f"contract.slices is empty but the on-disk plan draft parses "
            f"to {draft_slice_count} slices"
        )
    elif reason in _pkg._DIVERGENCE_LINE_BY_REASON:
        # Reason-aware wording for outcomes whose root cause isn't
        # "draft missing/unparseable/empty" — the widened
        # :func:`_populate_result_is_empty_contract` check now routes
        # ``FOREST_VIOLATION`` / ``CONTRACT_LOAD_FAILED`` /
        # ``EGG_CONTRACTS_UNAVAILABLE`` / ``UNEXPECTED_EXCEPTION`` /
        # ``POPULATED``-with-zero-slices through this same HITL, where
        # the generic "draft missing, unparseable, or yielded no tasks"
        # prose would contradict the ``reason=`` field (#2627 review).
        divergence_line = _pkg._DIVERGENCE_LINE_BY_REASON[reason]
    else:
        divergence_line = (
            "contract.slices is empty and the plan draft is missing, "
            "unparseable, or yielded no tasks"
        )
    return (
        f"Pipeline blocked at {gate}: {divergence_line} "
        f"(reason={reason}). The sync helper's auto-reconcile path "
        f"(#2792) tried to bring the worktree forward before the "
        f"populator ran; if you're seeing this, that reconcile either "
        f"didn't fire or didn't restore the draft, so pipeline state "
        f"and the contract have diverged. Plain restart_phase implement "
        f"will respawn into the same broken state. How to proceed?\n"
        f"- 'Repopulate contract from plan draft and retry' — run "
        f"POST /pipelines/{pipeline_id}/phase/populate-contract, then "
        f"restart_phase implement.\n"
        f"- 'Restart plan phase' — restart_phase plan to regenerate the "
        f"draft from scratch.\n"
        f"- 'Abort pipeline' — cancel_task."
    )


def _populate_outcome_to_hitl_reason(outcome: _pkg.PopulateOutcome) -> str:
    """Return the empty-contract HITL ``reason`` for a populate outcome.

    Maps a :class:`PopulateOutcome` to the operator-visible ``reason``
    string used by the dedicated empty-contract HITL:

    * ``POPULATED`` → ``"populated_but_empty_slices"`` — the populator
      ran but yielded 0 slices/tasks (the orthogonal "draft existed,
      populator ran, but produced nothing" case so the HITL doesn't
      claim a bare ``"populated"`` reason that contradicts the empty
      contract — #2627 review).
    * every other outcome → ``outcome.value`` (e.g. ``forest_violation``,
      ``contract_load_failed``, ``empty_result``).

    Extracted so both empty-contract call sites — the plan-complete
    handler (via :func:`_empty_contract_hitl_reason`) and the
    ``start_phase=implement`` safety net — share a single dispatch and
    can't drift if a new outcome needs special-cased reason handling
    (#2627 review follow-up).
    """
    if outcome == _pkg.PopulateOutcome.POPULATED:
        return "populated_but_empty_slices"
    return outcome.value


def _forest_error_to_outcome(err: _pkg.ForestValidationError) -> _pkg.PopulateOutcome:
    """Map a :class:`ForestValidationError` to the matching populate outcome."""
    return _pkg._FOREST_REASON_TO_OUTCOME.get(err.reason, _pkg.PopulateOutcome.FOREST_VIOLATION)


def _plan_preflight_hitl_question(
    *,
    missing_fields: list[str],
    plan_draft_rel: str,
) -> str:
    """Build the HITL question for an implement-start pre-flight rejection (#3100).

    Names the missing plan-draft fields inline and maps each recovery
    option to its concrete operator action, mirroring
    :func:`_empty_contract_hitl_question`'s shape so operators see the
    same actionable-decision pattern at both implement-start gates.
    """
    fields = ", ".join(missing_fields)
    return (
        f"Pipeline blocked at start_phase_implement_plan_preflight: the "
        f"plan draft ({plan_draft_rel}) is missing required field(s) "
        f"{fields}. The context-PR opener reads contract.pr metadata "
        f"from the plan's top-level ``pr:`` block; without it the "
        f"work-branch context PR can never open — both runner-side "
        f"openers soft-fail with missing_pr_metadata on every slice, "
        f"and no advance_phase call runs on the implement-start path "
        f"to enforce the #2777 hard-require (#3100). How to proceed?\n"
        f"- 'Fix the plan draft's pr: block and restart implement' — add "
        f"a top-level ``pr:`` block (title, description, test_plan, "
        f"manual_steps) to the draft's ``# yaml-tasks`` fence on the "
        f"work branch, then restart_phase implement.\n"
        f"- 'Restart plan phase' — restart_phase plan to regenerate the "
        f"draft from scratch.\n"
        f"- 'Abort pipeline' — cancel_task."
    )


def _enforce_implement_start_plan_preflight(
    pipeline_id: str,
    pipeline: _pkg.Pipeline,
    store: _pkg.StateStore,
    worktree_repo_path: _pkg.Path,
    plan_draft_rel: str,
) -> bool:
    """Enforce the #2777 plan pre-flight at the implement-start boundary (#3100).

    The natural plan→implement path runs
    :func:`egg_contracts.plan_parser.validate_plan_preflight` at the
    ``advance_phase`` REST/MCP site (``routes/phases.py``) and rejects
    with a typed 422 when the plan draft lacks the ``pr:`` metadata the
    context-PR opener needs.  ``start_phase=implement`` submits never
    traverse ``advance_phase``, so before #3100 a draft without a
    ``pr:`` block sailed straight into the implement phase: every
    runner-side opener backstop soft-failed with
    ``missing_pr_metadata`` at WARNING level, the slice stack ran with
    no context PR, and the operator discovered the gap only by noticing
    the PR was absent (observed on pipeline-da68d70c and
    pipeline-2d9cc50d, Khan/webapp).

    Runs AFTER the empty-contract gate at the call site, so the
    established empty-contract HITL routing (#2627) is unchanged — this
    gate fires only when the populate succeeded but the draft lacks the
    PR metadata.

    Scope:

    * Remote pipelines only (``pipeline.repo`` or ``pipeline.base_branch``
      set).  Local-mode pipelines never open a context PR (the opener's
      own local-mode skip in
      :func:`_open_context_pr_at_implement_start`), so requiring ``pr:``
      metadata there would fail test pipelines over a PR that would
      never exist.
    * Infra failures log a WARNING and return False — the gate must
      not add a new hard-fail mode for transient errors, and the
      populate path's own outcomes already cover an unreadable draft.
      Only the two named infra-class exceptions are caught: an
      :class:`ImportError` from the ``plan_parser`` import (validator
      unavailable on this host) and an :class:`OSError` from the draft
      ``read_text`` (file vanished, permission flake).  Any other
      exception out of :func:`validate_plan_preflight` propagates to
      the outer ``_run_pipeline`` Exception handler, mirroring the
      ``advance_phase`` site's behaviour — the goal is to never
      swallow a real parser bug under a generic "infra" umbrella.

    Returns True when the pipeline was marked FAILED (the caller must
    return without spawning implement-phase agents), False when the
    pre-flight passed or was legitimately skipped.
    """
    if not (pipeline.repo or pipeline.base_branch):
        return False

    try:
        from egg_contracts.plan_parser import (
            PlanPreflightError,
            validate_plan_preflight,
        )
    except ImportError as imp_err:
        _pkg.logger.warning(
            "Implement-start plan pre-flight: plan_parser import failed "
            "(continuing without the gate) (#3100)",
            pipeline_id=pipeline_id,
            error=str(imp_err),
        )
        return False

    try:
        plan_text = (worktree_repo_path / plan_draft_rel).read_text()
    except OSError as read_err:
        _pkg.logger.warning(
            "Implement-start plan pre-flight: failed to read plan draft "
            "(continuing without the gate) (#3100)",
            pipeline_id=pipeline_id,
            error=str(read_err),
        )
        return False

    try:
        validate_plan_preflight(plan_text)
        return False
    except PlanPreflightError as preflight_err:
        error_msg = (
            "start_phase=implement plan pre-flight failed — plan draft is "
            f"missing required field(s) "
            f"{', '.join(preflight_err.missing_fields)}: refusing to run "
            "the implement phase with no openable context PR (#2777 "
            "pre-flight, #3100 implement-start enforcement)"
        )
        _pkg.logger.error(
            "OVERSEER_ALERT start_phase_implement_plan_preflight_failed",
            pipeline_id=pipeline_id,
            missing_fields=preflight_err.missing_fields,
        )
        with _pkg.get_pipeline_state_lock(pipeline_id):
            disk_pipeline = store.load_pipeline(pipeline_id)
            disk_pipeline.status = _pkg.PipelineStatus.FAILED
            disk_pipeline.error = error_msg
            store.save_pipeline(disk_pipeline)
        _pkg._persist_hitl_decision(
            pipeline_id,
            disk_pipeline,
            store,
            question=_pkg._plan_preflight_hitl_question(
                missing_fields=preflight_err.missing_fields,
                plan_draft_rel=plan_draft_rel,
            ),
            options=list(_pkg._PLAN_PREFLIGHT_HITL_OPTIONS),
            phase=disk_pipeline.current_phase,
        )
        _pkg.report_pipeline_status(
            disk_pipeline,
            event_type="pipeline.failed",
            message=f"Pipeline failed: {error_msg[:100]}",
        )
        _pkg._emit_pipeline_event(disk_pipeline, "pipeline.failed")
        return True


def _empty_contract_hitl_reason(
    err: _pkg.PlanDraftMissingOnLocalError
    | _pkg.PlanDraftMissingOnLocalAndOriginError
    | _pkg.PopulateProducedEmptyContractError,
) -> str:
    """Return the ``reason`` field for the empty-contract HITL.

    Dispatches the operator-visible HITL ``reason`` from one of the
    three plan-complete fail-loud exceptions:

    * :class:`PlanDraftMissingOnLocalError` → ``plan_draft_missing_on_local``
    * :class:`PlanDraftMissingOnLocalAndOriginError` →
      ``plan_draft_missing_on_local_and_origin``
    * :class:`PopulateProducedEmptyContractError` — delegates to
      :func:`_populate_outcome_to_hitl_reason` so the outcome → reason
      mapping is shared with the ``start_phase=implement`` safety net
      (#2627 review).

    Extracted so the plan-complete call site's HITL-reason dispatch
    is unit-testable without standing up the full ``_run_pipeline``
    integration setup (#2627 review).
    """
    if isinstance(err, _pkg.PlanDraftMissingOnLocalError):
        return "plan_draft_missing_on_local"
    if isinstance(err, _pkg.PlanDraftMissingOnLocalAndOriginError):
        return "plan_draft_missing_on_local_and_origin"
    return _pkg._populate_outcome_to_hitl_reason(err.outcome)


def _empty_contract_failure_metadata(
    err: _pkg.PlanDraftMissingOnLocalError
    | _pkg.PlanDraftMissingOnLocalAndOriginError
    | _pkg.PopulateProducedEmptyContractError,
) -> tuple[str, str]:
    """Return ``(teardown_reason, log_event)`` for the plan-complete
    fail-loud handler in :func:`_run_pipeline`.

    Dispatches on the three #2627 fail-loud exception classes:

    * :class:`PlanDraftMissingOnLocalError` — draft missing from the local
      worktree but present on origin (the #2337 silent-failure).
    * :class:`PlanDraftMissingOnLocalAndOriginError` — draft missing from
      both refs (the #2627 silent-failure).
    * :class:`PopulateProducedEmptyContractError` — draft existed but the
      populator yielded an empty/broken contract (the orthogonal "draft
      existed but populate yielded nothing" failure mode #2627 also
      called out).

    Extracted so the dispatch is unit-testable without standing up the
    full ``_run_pipeline`` integration setup — a typo that swapped the
    branches would otherwise pass the existing populator-helper
    tests.  Re #2627 review.

    ``log_event`` uses the ``"OVERSEER_ALERT <discriminator>"`` event-name
    convention so the plan-complete fail-loud path is visible to the same
    log filters operators use for the slice-gate and start_phase
    safety-net (#2627 review).  The matching pre-raise OVERSEER_ALERTs
    (emitted by :func:`_populate_contract_from_plan_safe` for the two
    ``PlanDraftMissing*`` cases, and by ``_run_pipeline``'s plan-complete
    synthesis for :class:`PopulateProducedEmptyContractError`) use the
    same event names so the pre-raise log and the FAILED-cleanup log
    share a single discriminator on every branch.
    """
    if isinstance(err, _pkg.PlanDraftMissingOnLocalError):
        return (
            "plan draft missing on local",
            "OVERSEER_ALERT plan_draft_missing_on_local_but_present_on_origin",
        )
    if isinstance(err, _pkg.PlanDraftMissingOnLocalAndOriginError):
        return (
            "plan draft missing on local and origin",
            "OVERSEER_ALERT plan_draft_missing_on_local_and_origin",
        )
    return (
        f"populate produced {err.outcome.value} outcome",
        "OVERSEER_ALERT plan_populate_produced_empty_contract",
    )


def _origin_has_plan_draft(repo_path: _pkg.Path, branch: str, draft_rel: str) -> bool:
    """Return True if ``origin/{branch}:{draft_rel}`` resolves locally.

    Uses ``git cat-file -e`` against the local refs to origin (the
    immediately preceding ``_sync_worktree_with_remote`` call has already
    fetched), so this is a cheap on-disk check, not a network round-trip.

    A False return collapses two cases: origin really doesn't have the
    draft, or the ``cat-file`` probe itself failed (transient git error,
    timeout, etc.).  The natural plan-completion call site treats False
    as "definitively missing on origin" and, when local is also missing,
    raises :class:`PlanDraftMissingOnLocalAndOriginError` so the pipeline
    is marked FAILED rather than advancing to implement with an empty
    contract (#2627).  This is a deliberate fail-loud choice: a transient
    probe failure combined with a missing local draft will fail the
    pipeline rather than silently advance.  Operators can re-run the
    pipeline; silently shipping an empty contract has no recovery path.
    """
    try:
        result = _pkg.subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                f"safe.directory={repo_path}",
                "-C",
                str(repo_path),
                "cat-file",
                "-e",
                f"origin/{branch}:{draft_rel}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def _auto_populate_contract_at_implement_start(
    worktree_repo_path: _pkg.Path,
    pipeline_id: str,
    pipeline_mode: str,
    issue_number: int | None,
    current_phase: _pkg.PipelinePhase,
    pipeline_branch: str,
    *,
    gateway: _pkg.Any,
    gateway_mode: str,
    base_branch: str | None,
) -> int:
    """Attempt to auto-populate an empty contract at implement start (#2915).

    When a pipeline enters the implement phase with zero slices in the
    contract, this helper tries to populate it from the plan draft. On
    success, commits and pushes the populated contract; on failure, logs
    and returns 0 (still empty).

    Returns the number of slices in the contract after the attempt.

    NOTE: restored in slice-4 v4 of #2908 — the slice-4 base merge
    (commit 06c5a6cb0) accidentally dropped this function when bringing
    slice-1/2/3 work into the coder branch. The orphan import in
    ``orchestrator/tests/test_auto_populate_contract.py`` broke
    ``pytest --collect-only`` and blocked ``make test`` from running
    any tests at all (per tester v3 NACK blocker #1). The function
    body matches ``origin/main`` verbatim; the call site at
    ``_run_pipeline`` is unchanged.
    """
    _pkg.logger.info(
        "Attempting to auto-populate empty contract at implement start (#2915)",
        pipeline_id=pipeline_id,
        issue_number=issue_number,
    )
    try:
        _populate_result = _pkg._populate_contract_from_plan(
            worktree_repo_path,
            pipeline_id,
            pipeline_mode,
            issue_number,
            current_phase=current_phase,
        )
    except _pkg.ForestValidationError as _forest_err:
        _pkg.logger.warning(
            "Auto-populate contract failed: slice-DAG validation error",
            pipeline_id=pipeline_id,
            reason=_forest_err.reason,
            errors=_forest_err.errors,
        )
        return 0
    except Exception as _populate_err:  # noqa: BLE001
        _pkg.logger.warning(
            "Auto-populate contract failed at implement start",
            pipeline_id=pipeline_id,
            error=str(_populate_err),
            exc_info=True,
        )
        return 0

    if (
        _populate_result.outcome != _pkg.PopulateOutcome.POPULATED
        or _populate_result.slice_count == 0
    ):
        _pkg.logger.warning(
            "Auto-populate contract returned empty or failed",
            pipeline_id=pipeline_id,
            outcome=_populate_result.outcome.value,
            slice_count=_populate_result.slice_count,
        )
        return 0

    # Commit the populated contract
    try:
        _committed = _pkg._commit_statefiles_to_worktree(
            worktree_repo_path,
            "Auto-populate contract at implement start (#2915)",
            _pkg._pipeline_identifier(issue_number, pipeline_id),
            pipeline_id=pipeline_id,
        )
        if not _committed:
            _pkg.logger.warning(
                "Auto-populate: commit returned False (nothing to commit)",
                pipeline_id=pipeline_id,
            )
            return 0
    except Exception as _commit_err:  # noqa: BLE001
        _pkg.logger.warning(
            "Auto-populate: commit failed",
            pipeline_id=pipeline_id,
            error=str(_commit_err),
        )
        return 0

    # Push the populated contract. Failure is non-fatal — the contract is
    # already committed locally — but mirror the canonical pattern from
    # agent_salvage._push_recovery (try/except for transport, then check
    # push_result.ok for gateway-reported rejections like non_fast_forward
    # / auth_failed / gateway_unreachable). Thread gateway_mode and
    # base_branch so private-mode pipelines route correctly and non-FF
    # reconcile uses --onto and doesn't replay base-branch commits.
    push_succeeded = False
    try:
        push_result = gateway.push_worktree_branch(
            pipeline_id=pipeline_id,
            repo_path=str(worktree_repo_path),
            branch=pipeline_branch,
            mode=gateway_mode,
            base_branch=base_branch,
        )
    except Exception as _push_err:  # noqa: BLE001
        _pkg.logger.warning(
            "Auto-populate: push transport failure (non-fatal, contract committed locally)",
            pipeline_id=pipeline_id,
            error=str(_push_err),
        )
    else:
        if not push_result.ok:
            _pkg.logger.warning(
                "Auto-populate: push rejected by gateway (non-fatal, contract committed locally)",
                pipeline_id=pipeline_id,
                category=push_result.category,
                detail=push_result.detail,
            )
        else:
            push_succeeded = True

    _pkg.logger.info(
        "Auto-populate contract succeeded"
        if push_succeeded
        else "Auto-populate contract succeeded locally only (push did not land)",
        pipeline_id=pipeline_id,
        slice_count=_populate_result.slice_count,
        push_succeeded=push_succeeded,
    )
    return _populate_result.slice_count


def _populate_contract_from_plan_safe(
    repo_path: _pkg.Path,
    pipeline_id: str,
    pipeline_mode: str = "issue",
    issue_number: int | None = None,
    *,
    source: Literal[
        "plan_complete",
        "advance_phase_force",
        "hitl_plan_gate_approval",
    ] = "advance_phase_force",
    branch: str | None = None,
    current_phase: _pkg.PipelinePhase | None = None,
) -> _pkg.PopulateResult:
    """Run :func:`_populate_contract_from_plan` without propagating failures.

    Shared call path for the three code sites that run the populate step
    when a pipeline leaves the ``plan`` phase: ``_run_pipeline``'s
    post-complete block (``source="plan_complete"``), ``advance_phase``
    (used by the MCP ``advance_phase`` tool, especially with
    ``force=true`` — ``source="advance_phase_force"``), and the HITL
    plan-gate approval path in :func:`start_pipeline`
    (``source="hitl_plan_gate_approval"`` — operator approved the
    plan_gate while the pipeline was AWAITING_HUMAN, recovery
    re-spawns ``_run_pipeline``).  Blocking the phase transition on a
    populate failure would defeat the purpose of the advance hammer
    or recovery path — see #1941 — so all non-natural call sites
    keep the swallow-everything behaviour.

    The natural plan-completion call site (``source="plan_complete"``)
    additionally raises:

    * :class:`PlanDraftMissingOnLocalError` when the draft is missing
      from local but present on origin — the silent-failure mode
      behind #2337.
    * :class:`PlanDraftMissingOnLocalAndOriginError` when the draft is missing from
      BOTH local and origin — the silent-failure mode behind #2627
      (orchestrator-side delete with no consolidated re-write).

    Caller is expected to mark the pipeline FAILED so the operator can
    intervene rather than advancing to implement with an empty
    contract.

    Returns a :class:`PopulateResult` so non-raising failure modes are
    still inspectable: callers that need to fail-fast on
    ``EMPTY_RESULT`` / ``PARSE_FAILED`` (#2627 follow-up) can branch on
    the outcome.  ``ForestValidationError`` raised by the inner is
    caught and translated to ``PopulateResult(FOREST_VIOLATION, 0, 0)``;
    any other unexpected exception translates to
    ``PopulateResult(UNEXPECTED_EXCEPTION, 0, 0)``.
    """
    if source == "plan_complete" and branch is not None:
        draft_rel = _pkg._get_draft_path("plan", issue_number=issue_number, pipeline_id=pipeline_id)
        if draft_rel is not None:
            local_path = repo_path / draft_rel
            on_local = local_path.exists()
            on_origin = _pkg._origin_has_plan_draft(repo_path, branch, draft_rel)
            if not on_local and on_origin:
                _pkg.logger.error(
                    "OVERSEER_ALERT plan_draft_missing_on_local_but_present_on_origin",
                    pipeline_id=pipeline_id,
                    branch=branch,
                    draft_rel=draft_rel,
                    note=(
                        "_sync_worktree_with_remote returned without bringing "
                        "agents' plan-phase commits into the local worktree; "
                        "blocking phase advance to avoid silent demotion to "
                        "monolithic implement (#2337)"
                    ),
                )
                raise _pkg.PlanDraftMissingOnLocalError(
                    f"plan draft {draft_rel} missing on local but present on "
                    f"origin/{branch} — refusing to advance plan phase"
                )
            if not on_local and not on_origin:
                _pkg.logger.error(
                    "OVERSEER_ALERT plan_draft_missing_on_local_and_origin",
                    pipeline_id=pipeline_id,
                    branch=branch,
                    draft_rel=draft_rel,
                    note=(
                        f"plan draft is missing from both the local worktree "
                        f"and origin/{branch}; advancing would produce an "
                        f"empty contract and strand implement-phase agents "
                        f"with nothing to do (#2627)"
                    ),
                )
                raise _pkg.PlanDraftMissingOnLocalAndOriginError(
                    f"plan draft {draft_rel} missing on local and "
                    f"origin/{branch} — refusing to advance plan phase"
                )

    try:
        return _pkg._populate_contract_from_plan(
            repo_path,
            pipeline_id,
            pipeline_mode,
            issue_number,
            current_phase=current_phase,
        )
    except _pkg.ForestValidationError as forest_err:
        # Slice-DAG structural rejection is the expected #2137 / #3046
        # NACK path — log structurally so the discriminator shows up in
        # operator audit, but don't propagate to the wrapper's
        # caller (the populator already stashed the structured
        # errors on contract.plan_review_feedback so the plan
        # reviewer prompt can NACK the architect). The exception's
        # ``reason`` selects the matching outcome so operators see an
        # accurate discriminator (forest shape vs file-overlap order).
        _pkg.logger.warning(
            "contract_phases_ingest_failed",
            pipeline_id=pipeline_id,
            reason=forest_err.reason,
            source="safe_wrapper",
            errors=forest_err.errors,
        )
        return _pkg.PopulateResult(_pkg._forest_error_to_outcome(forest_err))
    except Exception as pop_err:
        _pkg.logger.warning(
            "contract_phases_ingest_failed",
            pipeline_id=pipeline_id,
            reason="unexpected_exception",
            source="safe_wrapper",
            error=str(pop_err),
            exc_info=True,
        )
        return _pkg.PopulateResult(_pkg.PopulateOutcome.UNEXPECTED_EXCEPTION)


def _merge_preserved_slice_runtime(
    new_slices: "list[ContractSlice]",  # noqa: UP037
    old_slices: "list[ContractSlice]",  # noqa: UP037
) -> None:
    """Carry runtime slice/task state from ``old_slices`` onto ``new_slices`` in place.

    ``_populate_contract_from_plan`` re-parses the plan markdown into a
    fresh set of slices on every call — and its safety-net caller fires
    on *every* ``start_phase=implement`` restart (deliberately outside
    the ``contract_synced`` guard). The plan is the source of truth for
    slice/task STRUCTURE (names, descriptions, dependencies, acceptance
    criteria); it always parses back as ``PENDING`` with the runtime
    bookkeeping fields unset. Blindly assigning ``contract.slices =
    <freshly parsed>`` therefore wipes every slice the slice loop had
    already advanced — resetting COMPLETE slices to PENDING and dropping
    the ``parent_branch_at_creation`` / ``integration_base_sha`` a real
    run stamped — so a restarted pipeline re-runs slice-1 forever and can
    never reach slice-2 (#2908).

    Mirroring the PR-metadata preservation a few lines down in the
    caller, this merges by slice id (and by task id within a slice): the
    plan supplies STRUCTURE while RUNTIME state survives a re-populate.
    Unmatched ids (a re-plan that adds or removes slices/tasks) simply
    keep the plan's fresh ``PENDING`` defaults.

    Task-level runtime fields covered (each is durably written by a
    runtime path that the plan parser cannot reconstruct):

    - ``status``, ``commit``, ``checkpoint_id``, ``review_cycles``,
      ``escalated``, ``gaps`` — slice-loop / reviewer / tester
      bookkeeping.
    - ``role`` + ``delegation_attempts`` — paired SYSTEM-owned
      impasse-delegation state. ``impasse_routing.py`` flips
      ``task.role`` to the suggested alternative and bumps
      ``delegation_attempts`` in the same ``apply_mutation`` cycle
      under ``Role.SYSTEM`` (only SYSTEM owns these two fields); the
      slice-loop dispatcher then routes the task to the new role.
      Preserving the counter without the role would re-spawn the
      original producer on restart and trip ``DELEGATION_LIMIT`` on
      the next impasse, escalating to HITL even though no delegation
      visibly happened — so both fields must survive together.
    - ``notes`` — APPLIER writes Won't-Do drain failure reasons here
      (``pipelines.py`` Won't-Do path) and agents write implementation
      narrative via ``mcp__task__update_notes`` / ``egg-contract
      update-notes``; the plan parser always emits ``""``.
    - ``jira_action_status`` — APPLIER advances ``pending`` →
      ``in_flight`` → ``applied``/``failed`` (#1557 risk_analyst R7);
      idempotency depends on ``applied`` surviving re-populate so the
      next apply skips it instead of re-creating the Jira issue.
    - ``jira_key`` — APPLIER writes the freshly-allocated key back after
      a ``create`` action so re-runs skip the create; plan parser emits
      ``None`` on ``create`` actions, so re-populate would otherwise
      strand the applier into creating duplicate tickets.
    """
    old_by_id = {s.id: s for s in (old_slices or [])}
    for new_slice in new_slices:
        old_slice = old_by_id.get(new_slice.id)
        if old_slice is None:
            continue
        # Slice-level runtime state stamped by ``_run_one_slice_inner``
        # and the bootstrap reconciler — never re-derivable from the plan.
        new_slice.status = old_slice.status
        new_slice.parent_branch_at_creation = old_slice.parent_branch_at_creation
        new_slice.integration_base_sha = old_slice.integration_base_sha
        new_slice.commit = old_slice.commit
        new_slice.review_cycles = old_slice.review_cycles
        # Defensive copy so post-merge mutations of the discarded ``old``
        # contract don't alias-leak into the live ``new`` contract.
        new_slice.review_feedback = list(old_slice.review_feedback)
        new_slice.escalated = old_slice.escalated
        new_slice.escalation_reason = old_slice.escalation_reason
        # Task-level runtime state: match by task id so a re-plan that
        # adds/removes tasks still preserves completion of the survivors.
        old_tasks_by_id = {t.id: t for t in old_slice.tasks}
        for new_task in new_slice.tasks:
            old_task = old_tasks_by_id.get(new_task.id)
            if old_task is None:
                continue
            new_task.status = old_task.status
            new_task.commit = old_task.commit
            new_task.checkpoint_id = old_task.checkpoint_id
            new_task.review_cycles = old_task.review_cycles
            new_task.escalated = old_task.escalated
            # Paired SYSTEM-owned impasse-delegation state — preserving
            # the counter without the role would silently undo the
            # delegation on restart (see docstring).
            new_task.role = old_task.role
            new_task.delegation_attempts = old_task.delegation_attempts
            new_task.gaps = list(old_task.gaps)
            # Runtime narrative + applier idempotency anchors. The
            # plan parser cannot reconstruct any of these — see the
            # docstring for the per-field invariants.
            new_task.notes = old_task.notes
            new_task.jira_action_status = old_task.jira_action_status
            new_task.jira_key = old_task.jira_key


def _populate_contract_from_plan(
    repo_path: _pkg.Path,
    pipeline_id: str,
    pipeline_mode: str = "issue",
    issue_number: int | None = None,
    *,
    current_phase: _pkg.PipelinePhase | None = None,
) -> _pkg.PopulateResult:
    """Read the plan draft and populate the contract with tasks.

    Extracts task structure from markdown headers in the plan draft
    and writes tasks + acceptance criteria to the contract.

    Returns a :class:`PopulateResult` whose ``outcome`` discriminates
    success from each silent-failure mode (#2627 follow-up).  Callers
    that need to fail-fast on an empty contract — natural plan-complete
    and the ``start_phase=implement`` safety net — branch on ``outcome``
    to surface a dedicated HITL instead of advancing into an implement
    phase with nothing to do.  ``ForestValidationError`` continues to
    raise so HTTP callers keep their structured-422 contract; the
    wrapper translates that raise into
    ``PopulateResult(FOREST_VIOLATION, 0, 0)``.

    When ``current_phase`` is provided, the contract's
    ``current_phase`` is advanced to that value **only if it would move
    the phase forward** (REFINE → PLAN → IMPLEMENT → PR).  Backward
    transitions are silently ignored so a respawn of the safety-net
    populator (e.g. when a ``start_phase=implement`` pipeline progresses
    to PR and re-enters ``_run_pipeline``) cannot demote the contract.
    The advance also appends a ``create_transition_entry`` audit log
    entry so operators inspecting the audit trail see the transition.

    This parameter is needed because the natural plan-completion path
    advances ``pipeline.current_phase`` (orchestrator-side) but leaves
    ``contract.current_phase`` for the reviewer agent / gateway phase
    API to advance via ``apply_mutation``.  When ``start_phase=implement``
    no plan reviewer runs, so the populator nudges the contract itself
    (#2427 sub-bug).
    """
    try:
        from egg_contracts.loader import load_contract, save_contract
    except ImportError:
        _pkg.logger.warning(
            "contract_phases_ingest_failed",
            pipeline_id=pipeline_id,
            reason="egg_contracts_unavailable",
        )
        return _pkg.PopulateResult(_pkg.PopulateOutcome.EGG_CONTRACTS_UNAVAILABLE)

    # Resolve draft path
    draft_rel = _pkg._get_draft_path("plan", issue_number=issue_number, pipeline_id=pipeline_id)
    if not draft_rel:
        _pkg.logger.warning(
            "contract_phases_ingest_failed",
            pipeline_id=pipeline_id,
            reason="no_draft_path",
        )
        return _pkg.PopulateResult(_pkg.PopulateOutcome.NO_DRAFT_PATH)

    plan_path = repo_path / draft_rel
    if not plan_path.exists():
        _pkg.logger.warning(
            "contract_phases_ingest_failed",
            pipeline_id=pipeline_id,
            reason="plan_draft_missing",
            path=str(plan_path),
        )
        return _pkg.PopulateResult(_pkg.PopulateOutcome.DRAFT_MISSING)

    try:
        contract = load_contract(pipeline_id, repo_path)
    except Exception as load_err:
        _pkg.logger.warning(
            "contract_phases_ingest_failed",
            pipeline_id=pipeline_id,
            reason="contract_load_failed",
            error=str(load_err),
        )
        return _pkg.PopulateResult(_pkg.PopulateOutcome.CONTRACT_LOAD_FAILED)

    try:
        from egg_contracts.plan_parser import parse_plan

        plan_text = plan_path.read_text()
        result = parse_plan(plan_text)

        if not result.success:
            _pkg.logger.warning(
                "contract_phases_ingest_failed",
                pipeline_id=pipeline_id,
                reason="parse_failed",
                error=result.error,
            )
            return _pkg.PopulateResult(_pkg.PopulateOutcome.PARSE_FAILED)

        for warning in result.warnings:
            _pkg.logger.warning(
                "Plan parse warning",
                pipeline_id=pipeline_id,
                warning_message=warning.message,
                warning_context=warning.context,
            )

        contract_slices = result.to_contract_slices()
        changed = False

        if contract_slices:
            # Forest validation (#2137 TASK-2-2): the slice DAG must be
            # a forest (every slice has ≤1 DAG parent). Multi-parent
            # slices break the stacked-PR invariant and are rejected
            # at ingestion so the plan reviewer NACKs the planner.
            #
            # ``parse_plan`` was already imported unconditionally above,
            # so we don't guard ``validate_forest`` import — if the
            # parser module is unavailable the populator has already
            # failed; silently defaulting ``forest_errors = []`` would
            # let a broken-import multi-parent contract slip past the
            # gate (reviewer_code_holistic v2 finding #5).
            from egg_contracts.plan_parser import (
                validate_forest,
                validate_slice_file_overlap,
            )

            forest_errors = validate_forest(contract_slices)

            if forest_errors:
                # Stash the structured errors onto the contract's
                # ``plan_review_feedback`` so the plan reviewer's
                # prompt picks them up and NACKs the planner with the
                # error verbatim. The slices are NOT written to the
                # contract — leaving ``contract.slices`` empty makes
                # downstream phases visibly broken so the violation
                # cannot silently leak through.
                _pkg.logger.warning(
                    "contract_phases_ingest_failed",
                    pipeline_id=pipeline_id,
                    reason="forest_violation",
                    errors=forest_errors,
                )
                feedback_lines = [
                    "Plan ingestion REJECTED: the slice DAG is not a forest.",
                    "",
                    "Each slice must have at most one DAG parent. The "
                    "implement phase ships every slice as a stacked PR with "
                    "exactly one base branch — multi-parent slices break "
                    "this invariant. Re-emit the plan with "
                    "``serialized_chain_order`` populated on the downstream "
                    "slice (see issue #2137 plan TASK-2-3 for the rule).",
                    "",
                    "Structured errors:",
                ]
                feedback_lines.extend(f"- {e}" for e in forest_errors)
                contract.plan_review_feedback = "\n".join(feedback_lines)
                save_contract(contract, repo_path)
                # Raise a structured ForestValidationError so any
                # caller running this in an HTTP context (e.g. a
                # plan-ingestion API endpoint) can surface a 422 with
                # the inlined errors. Internal callers
                # (``_populate_contract_from_plan_safe`` and the
                # pipeline run-loop) catch and log instead — the
                # ``plan_review_feedback`` stash above is the durable
                # signal the reviewer prompt picks up either way.
                raise _pkg.ForestValidationError("slice DAG is not a forest", errors=forest_errors)

            # File-overlap ordering validation (#3046). The forest is
            # valid (≤1 parent per slice), but the implement phase cuts
            # each slice's integration branch off its dependency parent
            # (roots off ``work``) — so two slices that touch the same
            # file MUST be ordered along a dependency chain, or their
            # branches fork independently off the shared base and their
            # edits collide at integration (the guaranteed modify/delete
            # conflict observed on #3023). Reject overlapping-but-unordered
            # slices here, with the SAME NACK-the-architect handling as a
            # forest violation: stash the structured errors on
            # ``plan_review_feedback`` and leave ``contract.slices`` empty
            # so the defect cannot silently leak into the implement phase.
            overlap_errors = validate_slice_file_overlap(contract_slices)
            if overlap_errors:
                _pkg.logger.warning(
                    "contract_phases_ingest_failed",
                    pipeline_id=pipeline_id,
                    reason="slice_overlap_violation",
                    errors=overlap_errors,
                )
                feedback_lines = [
                    "Plan ingestion REJECTED: slices touch overlapping files "
                    "without a dependency ordering.",
                    "",
                    "The implement phase cuts each slice's integration branch "
                    "off its dependency parent (root slices off the ``work`` "
                    "branch) and ships it as a stacked PR. Two slices that "
                    "touch the same file must be ordered along a single "
                    "dependency chain so the later slice's branch is forked "
                    "from a base that already contains the earlier slice's "
                    "commits — otherwise both branches fork independently off "
                    "the shared base and their edits collide at integration "
                    "(a guaranteed modify/delete conflict). The forest "
                    "constraint means the fix is always to serialise the "
                    "overlapping cluster into ONE linear ``dependencies`` "
                    "chain (you cannot depend on two parents) — or merge the "
                    "slices into one.",
                    "",
                    "Structured errors:",
                ]
                feedback_lines.extend(f"- {e}" for e in overlap_errors)
                contract.plan_review_feedback = "\n".join(feedback_lines)
                save_contract(contract, repo_path)
                raise _pkg.ForestValidationError(
                    "slices share files without a dependency ordering",
                    errors=overlap_errors,
                    reason="slice_overlap_violation",
                )
            # Preserve runtime slice/task progress across re-populates so
            # the safety-net populator (which fires on every
            # ``start_phase=implement`` restart) cannot reset COMPLETE
            # slices to PENDING and strand the pipeline on slice-1 (#2908).
            _pkg._merge_preserved_slice_runtime(contract_slices, contract.slices)
            contract.slices = contract_slices
            changed = True

        # Populate PR metadata from plan if available
        if result.pr_title:
            from egg_contracts.models import PRMetadata

            # Preserve orchestrator-populated runtime fields on
            # ``PRMetadata`` across re-populates. The planner-emitted
            # title/description/test_plan/manual_steps flow in fresh
            # from the parsed plan; the fields below are populated by
            # orchestrator code paths (the up-front context-PR opener
            # in ``_open_context_pr_at_implement_start``, the
            # conditional-ACK gate at ``complete_phase``) and would
            # otherwise be silently dropped when this safety-net
            # populator re-runs (e.g. on a ``start_phase=implement``
            # re-entry where ``deferred_actions`` was already populated
            # during implement-phase close).
            #
            # ``deferred_actions`` is the merge-blocking *Pre-merge
            # Obligations* handoff written by ``decisions.py`` after a
            # conditional-ACK gate resolves; losing it here erases the
            # reviewer's only durable handoff for git-mv / migration /
            # cross-repo flips. See test
            # ``test_populate_contract_from_plan_preserves_deferred_actions``.
            preserved_pr_number = contract.pr.context_pr_number if contract.pr is not None else None
            preserved_deferred_actions = (
                list(contract.pr.deferred_actions) if contract.pr is not None else []
            )
            contract.pr = PRMetadata(
                title=result.pr_title,
                description=result.pr_description or "",
                test_plan=result.pr_test_plan or "",
                manual_steps=result.pr_manual_steps or "",
                context_pr_number=preserved_pr_number,
                deferred_actions=preserved_deferred_actions,
            )
            changed = True

        if current_phase is not None and contract.current_phase != current_phase:
            # Forward-only: never demote.  Without this guard a respawn
            # of _run_pipeline (e.g. when a start_phase=implement pipeline
            # progresses past the implement boundary and re-enters the
            # safety-net call site) would silently roll
            # contract.current_phase back from IMPLEMENT to whatever the
            # call site hardcoded. The PR phase was removed in #2777
            # (cq-4); IMPLEMENT is now terminal.
            _phase_order = (
                _pkg.PipelinePhase.REFINE,
                _pkg.PipelinePhase.PLAN,
                _pkg.PipelinePhase.IMPLEMENT,
            )
            if (
                contract.current_phase in _phase_order
                and current_phase in _phase_order
                and _phase_order.index(current_phase) > _phase_order.index(contract.current_phase)
            ):
                from egg_contracts.audit import create_transition_entry
                from egg_contracts.models import AuditRole

                old_phase = contract.current_phase
                contract.audit_log.append(
                    create_transition_entry(
                        actor="orchestrator",
                        role=AuditRole.SYSTEM,
                        from_phase=old_phase.value,
                        to_phase=current_phase.value,
                        reason=(
                            "populator advanced contract.current_phase "
                            "(no apply_mutation caller for this pipeline; #2427)"
                        ),
                    )
                )
                contract.current_phase = current_phase
                changed = True

        if changed:
            save_contract(contract, repo_path)
            slice_count = len(contract.slices)
            task_count = sum(len(s.tasks) for s in contract.slices)
            _pkg.logger.info(
                "contract_phases_populated",
                pipeline_id=pipeline_id,
                phase_count=slice_count,
                task_count=task_count,
                has_pr_metadata=contract.pr is not None,
            )
            return _pkg.PopulateResult(
                _pkg.PopulateOutcome.POPULATED,
                slice_count=slice_count,
                task_count=task_count,
            )
        else:
            # Parse succeeded but yielded neither phases nor PR metadata —
            # this is the #1931 failure mode (empty contract with no error).
            # Emit a discriminator so the gap is visible in audit logs.
            _pkg.logger.warning(
                "contract_phases_ingest_failed",
                pipeline_id=pipeline_id,
                reason="empty_result",
                warning_count=len(result.warnings),
            )
            return _pkg.PopulateResult(_pkg.PopulateOutcome.EMPTY_RESULT)

    except _pkg.ForestValidationError:
        # Re-raise so callers with HTTP context (or the safe wrapper)
        # can surface the structured errors. The populator already
        # stashed feedback on contract.plan_review_feedback before
        # raising.
        raise
    except Exception as e:
        _pkg.logger.warning(
            "contract_phases_ingest_failed",
            pipeline_id=pipeline_id,
            reason="unexpected_exception",
            source="parse_save",
            error=str(e),
            exc_info=True,
        )
        return _pkg.PopulateResult(_pkg.PopulateOutcome.UNEXPECTED_EXCEPTION)


# Single source of truth for ForestValidationError.reason → PopulateOutcome
# mapping. Both ``_populate_contract_from_plan_safe`` and the
# ``start_phase=implement`` safety net translate a structural NACK into an
# outcome the empty-contract HITL prose dispatcher (#3046) can key off, so
# centralising the table here keeps the two catch sites from drifting if a
# third reason is added to :class:`ForestValidationError` (forest-shape vs.
# file-overlap-ordering today). Unknown reasons fall back to
# ``FOREST_VIOLATION`` — that's the conservative choice because the operator
# prose for forest violations names the slice DAG generally rather than the
# specific defect, so a new reason without a dedicated outcome still routes to
# actionable (if generic) HITL prose. Lives here (not the barrel) because it
# references the PopulateOutcome enum at definition time; it re-exports through
# the barrel so ``_pkg._FOREST_REASON_TO_OUTCOME`` resolves (#3312 slice-4).
_FOREST_REASON_TO_OUTCOME: dict[str, PopulateOutcome] = {
    "slice_overlap_violation": PopulateOutcome.SLICE_OVERLAP_VIOLATION,
    "forest_violation": PopulateOutcome.FOREST_VIOLATION,
}
