"""BRC content + route-version + plan/artifact validators (#3312)."""

from pathlib import Path
from typing import Any

import routes.signals as _pkg
from flask import Response
from state_store import (
    StateStoreError,
)

from ._responses import make_error_response

# --- BRC content validation (#1716) ---
_BRC_MIN_CONTENT_LEN = 50
# Pre-merge conditions are imperative instructions (e.g. "git mv X Y"),
# not full rationale, so they have a lower minimum length (#2005).
_BRC_CONDITION_MIN_LEN = 10
_BRC_BOILERPLATE = frozenset({"lgtm", "looks good", "no issues", "approved", "ok"})

# Kinds whose content is an imperative instruction rather than rationale.
_BRC_CONDITION_KINDS = frozenset({"pre-merge condition"})


def _validate_brc_content(body: str, kind: str) -> str | None:
    """Validate that BRC message content is substantive.

    Returns an error message string if validation fails, or None if content
    is acceptable.  ``kind`` is a human-readable label for the message type
    (e.g. "proposal summary", "ACK reason") used in error messages.

    Content kinds whose lowercase form appears in ``_BRC_CONDITION_KINDS``
    use a shorter minimum length because they are imperative instructions
    (e.g. "git mv X Y") rather than full rationale.
    """
    stripped = (body or "").strip()
    if not stripped:
        return f"{kind} must not be empty"
    if stripped.lower() in _BRC_BOILERPLATE:
        return (
            f"{kind} is boilerplate ('{stripped}'). Provide substantive rationale: "
            f"what was read/built, what was checked/tested, why the verdict follows"
        )
    min_length = (
        _BRC_CONDITION_MIN_LEN if kind.lower() in _BRC_CONDITION_KINDS else _BRC_MIN_CONTENT_LEN
    )
    if len(stripped) < min_length:
        return (
            f"{kind} is too short ({len(stripped)} chars, minimum {min_length}). "
            f"Provide substantive rationale: what was read/built, what was checked/tested, "
            f"why the verdict follows"
        )
    return None


def _require_route_version(payload: dict[str, Any], key: str) -> tuple[Response, int] | None:
    """Enforce ``payload[key]`` is an integer >= 1 at the HTTP signals boundary.

    Mirrors ``_require_version_int`` in ``sandbox/egg_agent_tools/handlers/brc.py``
    so the route surface shares the MCP handler's contract — a client POSTing
    directly to ``/signals/...`` cannot bypass the version-match guard in
    ``check_ack_guard`` / ``check_nack_guard`` by omitting the version field
    (#2674).  Returns an error response tuple on failure, or ``None`` on
    success.

    Treats absent and explicit ``null`` the same (both → "required"), matching
    the MCP helper.  On success, coerces ``payload[key]`` to ``int`` in place
    so downstream ``check_ack_guard`` / ``check_nack_guard`` can compare
    integers directly (the original raw value may have been a numeric string).
    """
    raw = payload.get(key)
    if raw is None:
        return make_error_response(
            f"'{key}' is required (the producer's current proposal version "
            "you reviewed; read it from the CONSENSUS_PROPOSE message)",
            400,
        )
    try:
        version = int(raw)
    except (TypeError, ValueError) as _exc:
        # `as _exc` is unused but forces the parentheses to stay: PEP 758
        # (Python 3.14) makes `except T, V:` a legal multi-class form, and
        # ruff format normalises to that shorter shape — which is
        # byte-identical to Python 2's `except Exception, var:` instance-
        # binding form and reads as a different operation at a glance.  The
        # binding pins the parens so the syntax cannot regress.
        return make_error_response(
            f"'{key}' must be an integer; got {raw!r}",
            400,
        )
    if version < 1:
        return make_error_response(
            f"'{key}' must be >= 1; got {version} (v0 means no proposal exists yet)",
            400,
        )
    payload[key] = version
    return None


def _validate_tester_check_coverage(
    pipeline_id: str, payload: dict[str, Any], repo_path: Path
) -> None:
    """Validate that tester proposals report all configured repo checks as passed.

    Compares the ``checks_passed`` list in the tester's attestation against the
    checks configured in ``repositories.yaml``.  Raises ``ValueError`` if any
    configured check is missing (i.e. did not pass), which prevents the proposal
    from being recorded (issues #1459, #1467, #1966).
    """
    attestation = payload.get("attestation", {})

    # Skip validation when tests were blocked — mirrors attestation-level
    # behaviour in attestation_schemas.py (issue #1459).
    if attestation.get("tests_execution_blocked"):
        return

    checks_passed = {name.lower() for name in attestation.get("checks_passed", [])}
    if not checks_passed:
        # Empty checks_passed is already caught by strict attestation validation,
        # but guard here for completeness.
        return

    try:
        pipeline = _pkg.get_state_store(repo_path).load_pipeline(pipeline_id)
    except StateStoreError:
        return

    repo = pipeline.repo
    if not repo:
        return

    try:
        from config.repo_config import get_repo_checks
    except ImportError:
        try:
            from repo_config import get_repo_checks  # type: ignore[no-redef]
        except ImportError:
            return

    try:
        configured_checks = get_repo_checks(repo)
    except Exception:
        _pkg.logger.warning(
            "Failed to load repo checks config, skipping coverage validation",
            pipeline_id=pipeline_id,
            repo=repo,
        )
        return
    if not configured_checks:
        return

    configured_names = {check["name"].lower() for check in configured_checks}
    missing = configured_names - checks_passed
    if missing:
        missing_list = ", ".join(sorted(missing))
        raise ValueError(
            f"Tester proposal is missing passing checks: {missing_list}. "
            f"All checks from repositories.yaml must pass before proposing "
            f"consensus. Fix failing checks and re-propose."
        )


def _validate_plan_extensions(
    *,
    pipeline_id: str,
    commit_sha: str,
    plan_text: str,
    plan_rel: str,
    pipeline_state: Any,
) -> None:
    """Plan-draft-specific extensions on top of the spec-driven presence check.

    The generalized ``_validate_producer_artifacts`` performs the #3016
    presence check for every registered artifact; for ``plan-draft`` we
    additionally need:

    1. **Parseability** (#3026): the draft parses via the *same* ``parse_plan``
       the contract populator runs. A draft that is complete in prose but omits
       the machine-readable ``# yaml-tasks`` appendix parses to ``success=False``
       — it passes BRC consensus and phase completion on content grounds, then
       fails the *whole pipeline* at ``populate_contract`` (``parse_failed`` /
       ``empty_result``) ~40 min later, after the plan HITL gate. Using the
       same parser means the propose-time check and the populate check cannot
       diverge on parse failures. (``parse_plan`` itself guarantees
       ``success=True`` ⇒ ≥1 phase ⇒ ≥1 slice via ``to_contract_slices`` —
       the per-phase placeholder-task injection sees to it — so a separate
       ``slice_count == 0`` check would be dead code here.)
    2. **Role↔files alignment** (#2527 / #2528): no task is assigned to a role
       whose blocklist forbids its files (would 403 at push time per
       ``gateway/phase_filter.py::FileRestriction.is_file_blocked`` /
       ``shared/egg_restrictions/patterns.py``).

    Called by ``_validate_producer_artifacts`` only for the ``plan-draft``
    artifact, after the presence check has confirmed the draft is committed
    and non-empty.  Graceful degradation: returns silently when the parser
    or ``to_contract_slices`` raises (e.g. a future Pydantic field
    tightening) or when the role-alignment validator raises; only raises
    when it can positively confirm an unparseable plan or a misassigned task.
    """
    try:
        from egg_contracts.plan_parser import (
            parse_plan,
            validate_forest,
            validate_slice_file_overlap,
            validate_task_role_alignment,
        )
    except ImportError:
        return

    try:
        parsed = parse_plan(plan_text)
    except Exception as exc:
        _pkg.logger.warning(
            "plan proposal validation: parser raised (non-blocking)",
            pipeline_id=pipeline_id,
            commit_sha=commit_sha,
            error=str(exc),
        )
        return

    # (1) Parseability — the #3026 fix. Reuses the contract populator's parser
    # so a draft that would later populate an empty contract on the
    # ``not parsed.success`` branch of ``_populate_result_is_empty_contract`` is
    # NACKed now instead. (``success=True`` already guarantees ``phases`` is
    # non-empty and every phase has ≥1 task via the parser's
    # placeholder-task injection, so ``to_contract_slices()`` cannot return an
    # empty list — no separate slice-count check is needed.)
    if not parsed.success:
        detail = f" {parsed.error}" if parsed.error else ""
        raise ValueError(
            f"Plan proposal rejected: the plan draft at `{plan_rel}` "
            f"({commit_sha[:8]}) does not parse into any tasks.{detail} The "
            f"contract populator runs this exact parser at plan-completion; a "
            f"draft that is complete in prose but omits the machine-readable "
            f"``# yaml-tasks`` appendix passes consensus and then fails the whole "
            f"pipeline at populate. Add a ``# yaml-tasks`` code fence enumerating "
            f"your slices and tasks, commit and push it, then re-propose."
        )

    # Symmetric with the ``parse_plan`` / ``validate_task_role_alignment``
    # wraps above and below: a future Pydantic field tightening on ``Task`` /
    # ``Slice`` could cause this to raise, and the old single-try posture
    # silently skipped any such failure rather than surfacing a 500.
    try:
        slices = parsed.to_contract_slices()
    except Exception as exc:
        _pkg.logger.warning(
            "plan proposal validation: to_contract_slices raised (non-blocking)",
            pipeline_id=pipeline_id,
            commit_sha=commit_sha,
            error=str(exc),
        )
        return

    # (2) Slice-DAG shape — forest (#2137) and file-overlap ordering
    # (#3046). Both validators run at *populate* time today (the contract
    # populator in ``routes/pipelines.py::_populate_contract_from_plan``),
    # where a violation stashes ``plan_review_feedback`` and fails the whole
    # pipeline ~24 min after consensus — the #3211 anti-pattern. Running the
    # SAME validators here NACKs the producer while it is still alive in BRC
    # (the #3016 pattern), so it re-emits a forest *before* consensus rather
    # than a phase later. Reusing the populator's exact validators means the
    # propose-time check and the populate check cannot diverge. Same
    # non-blocking posture as the wraps above: a validator that *raises*
    # (e.g. a future ``Slice`` field tightening) degrades to skip; only
    # returned errors NACK.
    try:
        forest_errors = validate_forest(slices)
        overlap_errors = validate_slice_file_overlap(slices)
    except Exception as exc:
        _pkg.logger.warning(
            "plan DAG-shape validation: validator raised (non-blocking)",
            pipeline_id=pipeline_id,
            commit_sha=commit_sha,
            error=str(exc),
        )
        forest_errors = []
        overlap_errors = []

    if forest_errors:
        bullets = "\n".join(f"  - {e}" for e in forest_errors)
        raise ValueError(
            "Plan proposal rejected: the slice DAG is not a forest.\n"
            "Each slice must have at most one DAG parent — the implement "
            "phase ships every slice as a stacked PR with exactly one base "
            "branch, so multi-parent slices break the stacking invariant "
            "and are hard-rejected at plan ingestion. Serialise the "
            "upstream cluster into a linear ``dependencies`` chain and "
            "record the chosen order on the downstream slice's "
            "``serialized_chain_order`` field (see issue #2137 plan "
            "TASK-2-3), then re-propose:\n" + bullets
        )

    if overlap_errors:
        bullets = "\n".join(f"  - {e}" for e in overlap_errors)
        raise ValueError(
            "Plan proposal rejected: slices touch overlapping files "
            "without a dependency ordering.\n"
            "The implement phase cuts each slice's branch off its "
            "dependency parent (roots off ``work``); two slices that touch "
            "the same file must be ordered along ONE dependency chain or "
            "their branches fork independently off the shared base and "
            "collide at integration (a guaranteed modify/delete conflict). "
            "Serialise the overlapping cluster into one linear "
            "``dependencies`` chain — or merge the slices — then "
            "re-propose:\n" + bullets
        )

    # (3) Role↔files alignment (#2527). #2528: pass the pipeline's repo so
    # per-repo ``role_patterns`` from repositories.yaml are honoured — plan-time
    # validation must mirror push-time enforcement, which also reads the per-repo
    # overrides (gateway/agent_restrictions.py).
    try:
        errors = validate_task_role_alignment(
            slices, repo=getattr(pipeline_state, "repo", None) or None
        )
    except Exception as exc:
        _pkg.logger.warning(
            "plan role-alignment validation: validator raised (non-blocking)",
            pipeline_id=pipeline_id,
            commit_sha=commit_sha,
            error=str(exc),
        )
        return

    if not errors:
        return

    bullet_list = "\n".join(f"  - {e}" for e in errors)
    raise ValueError(
        "Plan proposal rejected: task role↔files alignment violations.\n"
        "The following tasks are assigned to roles whose blocklist "
        "forbids their files (would 403 at push time per "
        "shared/egg_restrictions/patterns.py). Update the affected "
        "tasks' 'role' field and re-propose:\n" + bullet_list
    )


_ARTIFACT_HUMAN_LABEL: dict[str, str] = {
    "analysis-draft": "analysis draft",
    "analysis-draft-human": "human-focused analysis summary",
    "plan-draft": "plan draft",
    "plan-draft-human": "human-focused plan summary",
    "architect-output": "architect-output artifact",
    "architect-slices": "architect-slices scaffold",
    "risk-analyst-output": "risk-analyst-output artifact",
}


def _artifact_human_label(spec_name: str) -> str:
    """Return a human-readable label for ``spec_name`` for error messages.

    Falls back to the bare ``spec_name`` if a new spec row is added without
    a corresponding label entry — the error stays clean (no KeyError) while
    surfacing the unknown name verbatim so it can be added to the table.
    """
    return _ARTIFACT_HUMAN_LABEL.get(spec_name, spec_name)


# Producers that own a decision-ledger attestation (#3390): every refine/plan
# producer except the simplifier, whose human-focused companion summarizes the
# upstream draft and owns no decision surface. Kept in lockstep with the
# refine/plan producer rows in ``egg_contracts.artifact_spec`` — a future
# decision-bearing producer must be added here AND to
# ``PRODUCER_ATTESTATION_MODELS`` in ``orchestrator/attestation_schemas.py``.
_DECISION_ATTESTING_PHASES = frozenset({"refine", "plan"})
_DECISION_ATTESTING_ROLES = frozenset({"refiner", "task_planner", "architect", "risk_analyst"})

# Draft-owning producers whose attested ``cq-N`` ids must be cited in the
# draft itself (the ``--format markdown`` output of ``egg-contract
# add-decision`` embeds the id, so the registration flow passes this for
# free). The architect / risk_analyst structured outputs (JSON/YAML) have no
# prose decision surface, so they get the ledger cross-check only.
_DECISION_CITATION_SPECS = frozenset({"analysis-draft", "plan-draft"})


def _extract_attested_decision_fields(payload: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
    """Pull the raw decision-ledger fields off a proposal payload.

    Returns ``(decisions_registered, no_decisions_rationale,
    candidates_considered, deferred_resolutions)`` exactly as supplied
    (unvalidated; shape validation is
    :func:`egg_contracts.decisions.decision_attestation_errors`'s job).
    A missing or non-dict ``attestation`` yields ``(None, None, None,
    None)``.
    """
    attestation = payload.get("attestation")
    if not isinstance(attestation, dict):
        return None, None, None, None
    return (
        attestation.get("decisions_registered"),
        attestation.get("no_decisions_rationale"),
        attestation.get("candidates_considered"),
        attestation.get("deferred_resolutions"),
    )


def _validate_decision_attestation_shape(
    payload: dict[str, Any],
    *,
    agent_role: str,
    phase: str,
) -> None:
    """Enforce the decision-ledger attestation *shape* at propose time (#3390).

    The proposal ``attestation`` must carry exactly one of
    ``decisions_registered`` (non-empty list of ``cq-N`` ids) or
    ``no_decisions_rationale`` (non-empty string; the explicit empty
    ledger). Shared with the Pydantic model via
    ``decision_attestation_errors`` so the layers cannot drift.

    This half needs neither contract nor commit access — only the
    payload — so ``_validate_producer_artifacts`` runs it *ahead* of its
    branch-verification early return. That guarantees the ledger claim is
    hard-failed for every refine/plan producer even under a degraded
    fetch (non-shared object store) where the contract cross-check in
    :func:`_validate_decision_attestation` would be skipped.
    """
    if phase not in _DECISION_ATTESTING_PHASES or agent_role not in _DECISION_ATTESTING_ROLES:
        return

    registered, rationale, candidates, deferred_resolutions = _extract_attested_decision_fields(
        payload
    )

    try:
        from egg_contracts.decisions import decision_attestation_errors
    except ImportError:
        return

    errors = decision_attestation_errors(registered, rationale, candidates, deferred_resolutions)
    if not errors and phase != "plan" and deferred_resolutions:
        # ``deferred_resolutions`` echoes dq-ids the plan prompt surfaced
        # (#3564); only plan producers ever receive those, so its presence
        # on any other phase is a protocol misunderstanding worth failing
        # loudly rather than silently ignoring.
        errors = [
            "deferred_resolutions is a plan-phase field (#3564): it echoes "
            "the dq-<hash> ids of refine-deferred questions surfaced in the "
            f"plan prompt, and a {phase} attestation has none to echo. To "
            "defer a candidate to plan, disposition it 'deferred_to_plan' "
            "in candidates_considered instead."
        ]
    if not errors and phase == "plan" and isinstance(candidates, list):
        # Plan is the pipeline's last decision surface (#3526): a plan
        # producer cannot defer a candidate to itself. Anything still
        # potentially operator-grade at plan time must be registered.
        deferred = [
            c.get("question")
            for c in candidates
            if isinstance(c, dict) and c.get("disposition") == "deferred_to_plan"
        ]
        if deferred:
            errors = [
                "a plan-phase attestation cannot disposition candidates as "
                "'deferred_to_plan': plan is the last decision surface; "
                "register each as a cq-N via `egg-contract add-decision` or "
                "disposition it 'not_operator_grade' with a concrete why. "
                f"Offending candidate(s): {', '.join(repr(q) for q in deferred)}."
            ]
    if errors:
        raise ValueError(
            f"{agent_role} proposal rejected: decision-ledger attestation "
            f"missing or malformed ({' '.join(errors)}) — every {phase} "
            f"producer must attest its HITL decision ledger when proposing "
            f"(#3390). Include "
            f'`attestation={{"decisions_registered": ["cq-1", ...]}}` listing '
            f"every decision you registered this phase, or "
            f'`attestation={{"no_decisions_rationale": "<why none>", '
            f'"candidates_considered": [{{"question": ..., "disposition": '
            f'"not_operator_grade"|"deferred_to_plan", "why": ...}}, ...]}}` '
            f"when the phase deliberately raises none (#3526: the empty "
            f"ledger must enumerate the candidates it considered). Register "
            f"decisions first via `egg-contract add-decision` / "
            f"`mcp__sdlc__register_open_question`, then re-propose."
        )


# Plan producers whose proposal must account for every refine-deferred
# question (#3564). Mirrors the prompt injection exactly: the deferred
# section (with its ``dq-`` ids) is rendered into the concurrent-mode
# architect prompt and the single-agent plan prompt — task_planner and
# risk_analyst never receive it, so requiring coverage from them would
# NACK a producer for ids it was never shown.
_DEFERRAL_COVERAGE_ROLES = frozenset({"architect"})


def _validate_deferred_candidate_coverage(
    pipeline_id: str,
    payload: dict[str, Any],
    *,
    agent_role: str,
    phase: str,
) -> None:
    """NACK a plan proposal that drops a refine-deferred question (#3564).

    #3563 made refine's ``deferred_to_plan`` candidates a prompt-level
    handoff; this is the deterministic propose-time gate that hard-closes
    the leak. The plan prompt renders each deferred candidate with a
    stable content-derived ``dq-<hash>`` id
    (:func:`egg_contracts.decisions.deferred_question_id`); the producer
    echoes each id in its attestation's ``deferred_resolutions`` (as
    ``registered`` with the cq-N it became, or ``not_operator_grade``
    with why the design dissolved it). This validator recomputes the ids
    from the same message-store scan the prompt used
    (``_find_deferred_plan_candidates``) and rejects the proposal when
    any expected id is unaccounted, when an echoed id matches no deferred
    question (a typo or hallucination), or when a ``registered`` echo
    cites a cq-N absent from the attestation's own
    ``decisions_registered`` (whose contract existence the ledger
    cross-check enforces separately).

    Exact-id matching is safe *because* identity rides on the id, not the
    question text — the planner legitimately reframes deferred questions
    as the design firms up, which is why #3564 rejected text matching.

    Needs only the payload and the message store (no contract or commit
    access), so ``_validate_producer_artifacts`` hoists it — with the
    shape check — ahead of its branch-verification early return.
    Graceful degradation: a message-store failure skips the check with a
    warning (an orchestrator-side glitch, not a producer fault), matching
    ``_find_deferred_plan_candidates``'s prompt-time behavior. The
    asymmetric-outage case (store down at prompt-build time, up now)
    still validates: the rejection message lists each missing question
    verbatim, so a producer that never saw the prompt section can act on
    the NACK alone.
    """
    if phase != "plan" or agent_role not in _DEFERRAL_COVERAGE_ROLES:
        return

    try:
        from egg_contracts.decisions import deferred_question_id
    except ImportError:
        return

    # Lazy import mirroring ``_pipeline_identifier`` below: keep the heavy
    # ``routes.pipelines`` package out of signals import time.
    try:
        from routes.pipelines import _find_deferred_plan_candidates
    except ImportError:
        try:
            from .pipelines import (  # type: ignore[no-redef]
                _find_deferred_plan_candidates,
            )
        except ImportError:
            return

    try:
        deferred = _find_deferred_plan_candidates(pipeline_id)
    except Exception as exc:  # noqa: BLE001
        _pkg.logger.warning(
            "deferred-candidate coverage check skipped: scan failed (non-blocking)",
            pipeline_id=pipeline_id,
            role=agent_role,
            error=str(exc),
        )
        return

    expected: dict[str, str] = {}
    for candidate in deferred:
        if not isinstance(candidate, dict):
            continue
        question = str(candidate.get("question") or "").strip()
        if question:
            expected[deferred_question_id(question)] = question
    if not expected:
        return

    registered, _rationale, _candidates, resolutions = _extract_attested_decision_fields(payload)
    attested_cqs = {i for i in (registered or []) if isinstance(i, str)}

    covered: set[str] = set()
    problems: list[str] = []
    for entry in resolutions if isinstance(resolutions, list) else []:
        # Shape validity (ids well-formed, resolution kinds, cq/why
        # payloads) is the shape validator's job and runs first; this
        # loop re-guards defensively so a direct call cannot crash.
        if not isinstance(entry, dict):
            continue
        deferred_id = entry.get("deferred_id")
        if deferred_id not in expected:
            problems.append(
                f"`{deferred_id}` matches no refine-deferred question of this "
                "pipeline — copy the dq- ids verbatim from the 'Deferred "
                "from refine' section of your prompt"
            )
            continue
        if entry.get("resolution") == "registered":
            cq = entry.get("cq")
            if cq not in attested_cqs:
                problems.append(
                    f"`{deferred_id}` claims to be registered as `{cq}`, but "
                    f"`{cq}` is not in this attestation's decisions_registered "
                    "— register it via `egg-contract add-decision` and attest "
                    "the returned id"
                )
                continue
        covered.add(deferred_id)

    missing = [i for i in expected if i not in covered]
    if problems or missing:
        missing_lines = "; ".join(f"`{i}` ({expected[i]!r})" for i in missing)
        detail = " ".join(problems)
        if missing:
            detail = (
                f"unaccounted deferred question(s): {missing_lines}. {detail}"
                if detail
                else f"unaccounted deferred question(s): {missing_lines}."
            )
        raise ValueError(
            f"{agent_role} proposal rejected: the refine phase deferred "
            f"decision candidates to plan and your attestation does not "
            f"account for all of them (#3564) — {detail} For EACH dq- id, "
            f"either register the question (possibly reframed) via "
            f"`egg-contract add-decision` and echo "
            f'`--deferred "dq-<hash> :: registered :: cq-<N>"`, or dissolve '
            f"it with "
            f'`--deferred "dq-<hash> :: not_operator_grade :: <why>"`. '
            f"A deferred question that silently disappears is the leak "
            f"this gate exists to close; then re-propose."
        )


def _validate_decision_attestation(
    pipeline_id: str,
    payload: dict[str, Any],
    *,
    agent_role: str,
    phase: str,
    worktree_path: Path,
    identifier: str | int,
) -> None:
    """Require a well-formed decision-ledger attestation at propose time (#3390).

    The two-wave bridge and the converge-before-advance loop (#3392) can
    only surface decisions that were *registered*; nothing verifies that a
    phase which should raise decisions actually did, so a producer that
    silently skips ``egg-contract add-decision`` advances the pipeline
    with an empty ledger the operator cannot distinguish from
    "deliberately none". This validator makes the ledger claim mandatory
    and hard-fails the propose when it is missing, malformed, or refers
    to decisions that were never registered:

    1. **Shape** — delegated to :func:`_validate_decision_attestation_shape`.
       ``_validate_producer_artifacts`` also runs that helper *ahead* of
       its branch-verification early return, so the shape claim is
       enforced even on the degraded-fetch path that reaches consensus
       without the cross-check below. It is re-run here so a direct call
       (or the plan back-compat wrapper) still enforces it.
    2. **Cross-check** — every attested id must exist on the contract as
       a HITL decision registered for the *current phase*. A hallucinated
       or phase-misfiled id is rejected with the actionable fix. Only
       ``hitl``-type decisions are recognised, matching the gate-side
       counter in ``_collect_decision_ledger_status`` so the two views
       cannot diverge into a spurious ``MISSING`` backstop.

    Graceful degradation mirrors ``_validate_producer_artifacts``: when
    the contract cannot be loaded (orchestrator-side glitch, not a
    producer fault) the cross-check is skipped with a warning — but the
    shape requirement is enforced by the hoisted shape check, so the
    ledger claim itself is never skipped.
    """
    _validate_decision_attestation_shape(payload, agent_role=agent_role, phase=phase)
    _validate_deferred_candidate_coverage(pipeline_id, payload, agent_role=agent_role, phase=phase)

    if phase not in _DECISION_ATTESTING_PHASES or agent_role not in _DECISION_ATTESTING_ROLES:
        return

    registered, _rationale, _candidates, _deferred = _extract_attested_decision_fields(payload)

    attested_ids = list(registered or [])
    if not attested_ids:
        return

    try:
        contract = _pkg.load_contract(identifier, worktree_path)
    except Exception as exc:
        _pkg.logger.warning(
            "decision-ledger cross-check skipped: contract not loadable (non-blocking)",
            pipeline_id=pipeline_id,
            role=agent_role,
            phase=phase,
            error=str(exc),
        )
        return

    # Recognise only ``hitl``-type decisions, matching the gate-side
    # counter in ``_collect_decision_ledger_status`` (#3390). If the two
    # views disagreed — cross-check accepting an ``AUTO``-type id the gate
    # never counts — an attested non-HITL id would pass propose yet still
    # trip a spurious ``MISSING`` backstop at the gate.
    by_id: dict[str, Any] = {}
    for d in contract.decisions:
        d_id = getattr(d, "id", None)
        if not isinstance(d_id, str):
            continue
        d_type = getattr(d, "type", None)
        d_type = getattr(d_type, "value", d_type)
        if d_type != "hitl":
            continue
        by_id[d_id] = d

    for attested in attested_ids:
        decision = by_id.get(attested)
        if decision is None:
            raise ValueError(
                f"{agent_role} proposal rejected: attested decision "
                f"`{attested}` is not registered on the contract. The "
                f"ledger attestation must list ids returned by "
                f"`egg-contract add-decision` — register the decision "
                f"first, then re-propose with the real id."
            )
        d_phase = getattr(decision, "phase", None)
        d_phase = getattr(d_phase, "value", d_phase)
        if d_phase is not None and d_phase != phase:
            raise ValueError(
                f"{agent_role} proposal rejected: attested decision "
                f"`{attested}` is registered for the '{d_phase}' phase, "
                f"not the current '{phase}' phase. Attest only decisions "
                f"you registered for this phase (re-register with "
                f"`--phase {phase}` if the question genuinely belongs "
                f"here)."
            )


def _validate_decision_citations(
    pipeline_id: str,
    payload: dict[str, Any],
    *,
    agent_role: str,
    phase: str,
    draft_text: str,
    draft_rel: str,
) -> None:
    """Require the draft to cite every attested ``cq-N`` id (#3390).

    A decision that is registered but invisible in the draft is easy for
    the operator (and the BRC reviewers) to miss — the ``--format
    markdown`` output of ``egg-contract add-decision`` embeds the id, so
    a producer that copies it into the draft's Open Questions section
    passes this for free. Only well-formed ``cq-N`` entries are checked;
    a malformed attestation is ``_validate_decision_attestation``'s job
    (which raises the canonical shape error).
    """
    if phase not in _DECISION_ATTESTING_PHASES or agent_role not in _DECISION_ATTESTING_ROLES:
        return

    registered, _rationale, _candidates, _deferred = _extract_attested_decision_fields(payload)
    if not isinstance(registered, list) or not registered:
        return

    try:
        from egg_contracts.decisions import CQ_ID_PATTERN, extract_cq_citations
    except ImportError:
        return

    attested = [i for i in registered if isinstance(i, str) and CQ_ID_PATTERN.match(i)]
    if not attested:
        return

    cited = extract_cq_citations(draft_text)
    missing = [i for i in attested if i not in cited]
    if missing:
        raise ValueError(
            f"{agent_role} proposal rejected: attested decision(s) "
            f"{', '.join(f'`{i}`' for i in missing)} are not cited anywhere "
            f"in `{draft_rel}` at the proposed commit. Copy the "
            f"`egg-contract add-decision --format markdown` output for each "
            f"decision into the draft's Open Questions section (it embeds "
            f"the id), commit and push, then re-propose — a registered "
            f"decision the draft never mentions is invisible to the "
            f"operator and the reviewers (#3390)."
        )


def _validate_producer_artifacts(
    pipeline_id: str,
    payload: dict[str, Any],
    repo_path: Path,
    *,
    agent_role: str,
    phase: str | None = None,
    pipeline_state: Any | None = None,
    worktree_path: Path | None = None,
    branch_verified: bool | None = True,
) -> None:
    """Spec-driven propose-time validation for every registered producer (#3077 slice-3).

    Generalises the per-role ``_validate_producer_draft_present`` (refine) /
    ``_validate_plan_proposal`` (plan) dispatch that #3077 slices 1–2 inherited
    into a single helper: for every CONSENSUS_PROPOSE carrying a ``commit_sha``,
    resolve ``specs_for(phase, producer_role)`` against the
    :mod:`egg_contracts.artifact_spec` registry (slice-2) and run the existing
    server-side ``git show`` presence check per registered artifact. Plan-draft
    extensions (parseability #3026, role↔files alignment #2527/#2528) layer on
    top via :func:`_validate_plan_extensions` for the ``plan-draft`` artifact
    only — other producers have no parseable appendix or role-alignment data,
    so existence-only is the right check there.

    Roles with no registered artifact (e.g. ``coder``, ``tester``, every
    reviewer role) validate nothing — :func:`specs_for` returns an empty tuple
    and the function falls through cleanly. ``no_changes_needed`` proposes
    skip validation upstream in ``handle_consensus_propose_signal`` (the
    producer asserts they have no work in this slice, so there is no artifact
    to present).

    Why presence per artifact, not per phase: refine has one producer with one
    artifact (analysis-draft); plan has three producers (task_planner →
    plan-draft; architect → architect-output + architect-slices; risk_analyst
    → risk-analyst-output). Iterating ``specs_for(phase, agent_role)`` covers
    every (phase, role)-bound artifact uniformly. Adding a future row in
    :mod:`egg_contracts.artifact_spec` automatically grows the validator
    surface — that's the slice-2 ratchet's whole point.

    ``pipeline_state`` / ``worktree_path`` are threaded in by
    :func:`handle_consensus_propose_signal` to reuse the lookups it already
    performed for ``_verify_commit_on_branch``; the function loads them
    itself when called directly (e.g. from unit tests). ``phase`` may be
    passed explicitly (the wrapper :func:`_validate_plan_proposal` and unit
    tests do this) or omitted to resolve it from ``pipeline_state.current_phase``.

    Graceful degradation mirrors the original validators': returns silently
    when the proposal carries no commit SHA, the pipeline has no branch,
    ``specs_for`` resolves to an empty tuple, ``git show`` errors for an
    infrastructure reason (timeout / git failure), the plan parser raises,
    or ``to_contract_slices`` raises (a future Pydantic field tightening).
    Inconclusive branch verification (``branch_verified is None`` — an
    orchestrator-side fetch glitch, not a producer fault) skips the checks
    only when the commit object is *also* absent locally
    (:func:`_commit_object_resolvable`); with the object present, a non-zero
    ``git show`` reliably means "path absent at commit", so validation
    proceeds — an unconditional skip-on-``None`` let a persistent fetch
    failure disable the predecessor validator entirely (#3081). The checks
    raise only when they can positively confirm — at a locally-resolved
    commit — that an artifact is absent/empty, unparseable, or
    role-misassigned. ``branch_verified`` defaults to ``True`` so direct
    callers (unit tests) that don't run ``_verify_commit_on_branch`` still
    get the check.
    """
    commit_sha = (payload.get("commit_sha") or "").strip()
    if not commit_sha:
        return

    # Early bail-out for roles with no registered artifacts in *any* phase
    # (coder, tester, documenter, every reviewer). This both saves the
    # pipeline-state lookup and — more importantly — keeps us out of the
    # state-store failure path the original per-role dispatch never
    # reached for these roles. A future spec row that adds a producer
    # outside the current set automatically opts that role into
    # validation, so the ratchet still expands as the registry grows.
    # Lazy import so the spec module's pure-Python invariant (no
    # orchestrator / gateway deps — see
    # ``shared/egg_contracts/artifact_spec.py``) stays symmetric: signals.py
    # imports the spec rather than the spec importing us.
    try:
        from egg_contracts.artifact_spec import all_specs, specs_for
    except ImportError:
        return

    if agent_role not in {spec.producer_role for spec in all_specs()}:
        return

    if pipeline_state is None:
        try:
            pipeline_state = _pkg.get_state_store(repo_path).load_pipeline(pipeline_id)
        except StateStoreError:
            return

    if not pipeline_state.branch:
        return

    # Resolve phase. Callers in production
    # (``handle_consensus_propose_signal``) and the back-compat plan
    # wrapper pass ``phase`` explicitly; the direct-from-tests path leaves
    # it ``None`` and we fall back first to ``pipeline_state.current_phase``,
    # then — when that isn't a usable string (test ``MagicMock`` that
    # doesn't set ``current_phase.value``, a pipeline whose state is
    # partially loaded) — to the spec registry itself. The registry
    # locks each producer role to exactly one phase, so iterating
    # ``all_specs()`` is unambiguous: pick the phase of the matching
    # producer_role row, if any. Roles with no registered artifact still
    # fall through cleanly via the ``specs_for`` empty-tuple below.
    if phase is None:
        phase_attr = getattr(pipeline_state, "current_phase", None)
        if phase_attr is not None:
            candidate = phase_attr.value if hasattr(phase_attr, "value") else phase_attr
            if isinstance(candidate, str):
                phase = candidate
    if phase is None:
        for spec in all_specs():
            if spec.producer_role == agent_role:
                phase = spec.phase
                break

    if phase is None:
        return

    specs = specs_for(phase, agent_role)
    if not specs:
        return

    if worktree_path is None:
        worktree_path = _pkg.resolve_worktree_path(pipeline_id, repo_path)

    # Decision-ledger shape enforcement (#3390) runs BEFORE the
    # branch-verification early return below: the shape check needs no
    # contract/commit access, so hoisting it guarantees a refine/plan
    # producer cannot reach consensus with a missing or malformed ledger
    # attestation even on the degraded-fetch path (non-shared object
    # store) that skips the presence loop and the contract cross-check.
    # The deferred-coverage gate (#3564) is hoisted for the same reason:
    # it needs only the payload and the message store.
    _pkg._validate_decision_attestation_shape(payload, agent_role=agent_role, phase=phase)
    _pkg._validate_deferred_candidate_coverage(
        pipeline_id, payload, agent_role=agent_role, phase=phase
    )

    # Orchestrator-side commit verification was inconclusive — a non-zero
    # ``git show`` below could be "commit not in local object cache" rather
    # than "path absent at commit". But that ambiguity only exists when the
    # commit object is actually absent: when it resolves locally (always, in
    # the shared-object-store deployment), the checks below stay sound, so
    # keep validating. Skipping unconditionally on ``None`` is how #3081
    # shipped a full consensus with no canonical draft — the fetch failure
    # was persistent, so the "transient glitch" skip became "never validate".
    if branch_verified is None and not _pkg._commit_object_resolvable(worktree_path, commit_sha):
        _pkg.logger.warning(
            "producer artifact presence check skipped: branch verification "
            "inconclusive and commit not in local object store",
            pipeline_id=pipeline_id,
            role=agent_role,
            phase=phase,
            commit_sha=commit_sha,
        )
        return

    # Resolve the identifier the spec path-template renders against.  The
    # registry expects whatever ``_pipeline_identifier`` returns — an integer
    # issue number for the bare ``issue-<N>`` form, the qualified pipeline id
    # for re-runs (``issue-<N>-<suffix>`` — #3068). Imported lazily so we
    # don't pull the ~24k-line ``routes.pipelines`` module into ``signals``
    # import time.
    try:
        from routes.pipelines import _pipeline_identifier
    except ImportError:
        try:
            from .pipelines import _pipeline_identifier  # type: ignore[no-redef]
        except ImportError:
            return

    identifier = _pipeline_identifier(pipeline_state.issue_number, pipeline_id)

    # Decision-ledger attestation (#3390): every refine/plan producer must
    # attest the HITL decisions it registered (or explicitly attest none) and
    # the attested ids must exist on the contract for this phase. Runs before
    # the presence loop so the shape error — the most actionable fix — leads.
    _pkg._validate_decision_attestation(
        pipeline_id,
        payload,
        agent_role=agent_role,
        phase=phase,
        worktree_path=worktree_path,
        identifier=identifier,
    )

    for spec in specs:
        artifact_rel = spec.resolve_path(identifier)
        try:
            result = _pkg.subprocess.run(
                ["git", "-C", str(worktree_path), "show", f"{commit_sha}:{artifact_rel}"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except Exception as exc:
            _pkg.logger.warning(
                "producer artifact presence check: git show failed (non-blocking)",
                pipeline_id=pipeline_id,
                role=agent_role,
                phase=phase,
                artifact=spec.name,
                commit_sha=commit_sha,
                error=str(exc),
            )
            # Infrastructure failure on one artifact must not poison the
            # remaining rows (a transient git timeout shouldn't mask a
            # second-artifact absence). Continue to the next spec; existing
            # graceful-degradation semantics for the per-spec ``git show``
            # are preserved.
            continue

        if result.returncode == 0 and result.stdout.strip():
            # Plan-draft additionally checks parseability (#3026) and
            # role↔files alignment (#2527/#2528). All other artifacts are
            # existence-only by design — see ``task-3-1`` description and
            # the slice-2 spec note that the registry rows are deliberately
            # parse-free.
            if spec.name == "plan-draft":
                _pkg._validate_plan_extensions(
                    pipeline_id=pipeline_id,
                    commit_sha=commit_sha,
                    plan_text=result.stdout,
                    plan_rel=artifact_rel,
                    pipeline_state=pipeline_state,
                )
            # Decision-citation check (#3390): the draft must cite every
            # attested ``cq-N`` so a registered decision is visible where
            # the operator and reviewers read it.
            if spec.name in _DECISION_CITATION_SPECS:
                _pkg._validate_decision_citations(
                    pipeline_id,
                    payload,
                    agent_role=agent_role,
                    phase=phase,
                    draft_text=result.stdout,
                    draft_rel=artifact_rel,
                )
            continue

        label = _pkg._artifact_human_label(spec.name)
        raise ValueError(
            f"{agent_role} proposal rejected: no {label} found at "
            f"`{artifact_rel}` in the proposed commit ({commit_sha[:8]}). "
            f"The phase gate, contract population, and resume all read the "
            f"{label} from this exact path — an artifact committed to a "
            f"different path (or an empty one) is invisible to them. Write "
            f"your {label} to `{artifact_rel}`, commit and push it, then "
            f"re-propose."
        )

    # The simplifier persists exactly ONE artifact per phase — the registered
    # human-focused companion. Its review reasoning and simplification
    # constraints belong in the BRC channel (its ACK/NACK and review verdict),
    # not a second persisted document. The presence loop above only confirms
    # the human companion *exists*; it never catches an *extra* draft the agent
    # freelanced alongside it (e.g. a "*-simplifier-*.md" guardrails/constraints
    # companion). The producer prompt and reviewer rubric forbid that in prose,
    # but those are soft gates — this makes "one simplifier draft, no more" a
    # structural invariant at propose time. Deliberately scoped to the
    # simplifier: other producers' extra-draft policy is out of scope here.
    if agent_role == "simplifier":
        _pkg._reject_extra_simplifier_drafts(
            pipeline_id=pipeline_id,
            worktree_path=worktree_path,
            commit_sha=commit_sha,
            identifier=identifier,
            allowed_rel_paths={spec.resolve_path(identifier) for spec in specs},
            phase=phase,
        )


def _reject_extra_simplifier_drafts(
    *,
    pipeline_id: str,
    worktree_path: Path,
    commit_sha: str,
    identifier: str | int,
    allowed_rel_paths: set[str],
    phase: str,
) -> None:
    """Reject a simplifier proposal that persists any draft beyond its one
    registered human companion.

    The simplifier is a producer-only role whose sole output is the
    human-focused ``*-human.md`` summary. A second persisted document (the
    historically-observed ``*-simplifier-*.md`` constraints/guardrails
    companion) is redundant with the architect/planner's authoritative draft
    and the BRC review, and is an extra surface to drift out of sync with
    operator decisions. The review reasoning the companion tried to carry
    belongs in the BRC channel instead.

    This inspects only the files the *proposed commit* introduced under
    ``.egg-state/drafts/{identifier}-`` (so upstream producers' drafts, which
    that commit does not touch, are never implicated) and raises if any of them
    is not in ``allowed_rel_paths`` (the registered human-companion path).

    Graceful degradation mirrors the surrounding validator: a git failure or a
    non-zero ``git show`` (we cannot list the commit's files) returns silently
    rather than blaming the producer. The raise fires only when we can
    positively name an extra draft the commit added.
    """
    drafts_prefix = f".egg-state/drafts/{identifier}-"
    try:
        result = _pkg.subprocess.run(
            [
                "git",
                "-C",
                str(worktree_path),
                "show",
                "--name-only",
                "--pretty=format:",
                commit_sha,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception as exc:
        _pkg.logger.warning(
            "simplifier extra-draft check: git show failed (non-blocking)",
            pipeline_id=pipeline_id,
            phase=phase,
            commit_sha=commit_sha,
            error=str(exc),
        )
        return

    if result.returncode != 0:
        # Could not enumerate the commit's files (e.g. commit not resolvable
        # locally). Treat as inconclusive and degrade gracefully — the presence
        # check already gates the human companion itself.
        return

    extras = sorted(
        path
        for line in result.stdout.splitlines()
        for path in (line.strip(),)
        if path.startswith(drafts_prefix) and path not in allowed_rel_paths
    )
    if not extras:
        return

    allowed_display = ", ".join(f"`{p}`" for p in sorted(allowed_rel_paths))
    extras_display = ", ".join(f"`{p}`" for p in extras)
    raise ValueError(
        f"simplifier proposal rejected: the proposed commit ({commit_sha[:8]}) "
        f"introduces draft file(s) {extras_display} beyond your one registered "
        f"human-focused companion ({allowed_display}). The simplifier persists "
        f"exactly one artifact per phase — the plain-language summary. Your "
        f"review reasoning and any simplification constraints belong in the BRC "
        f"channel (your ACK/NACK and review verdict), not a second persisted "
        f"document. Remove the extra draft file(s), commit, and re-propose."
    )


def _validate_plan_proposal(
    pipeline_id: str,
    payload: dict[str, Any],
    repo_path: Path,
    *,
    pipeline_state: Any | None = None,
    worktree_path: Path | None = None,
    branch_verified: bool | None = True,
) -> None:
    """Back-compat wrapper around :func:`_validate_producer_artifacts` (#3077 slice-3).

    Pre-slice-3 this function carried the bespoke plan-only presence +
    parseability + role-alignment logic. Slice-3 generalised the presence
    check via the artifact spec; the plan extensions (parseability #3026,
    role↔files alignment #2527/#2528) now live in
    :func:`_validate_plan_extensions` and are layered on the ``plan-draft``
    artifact by :func:`_validate_producer_artifacts`. This wrapper preserves
    the historical signature (and the unit-test surface that pins it) by
    delegating with ``agent_role="task_planner"`` and ``phase="plan"`` —
    pre-existing plan tests stay green without modification.
    """
    _pkg._validate_producer_artifacts(
        pipeline_id,
        payload,
        repo_path,
        agent_role="task_planner",
        phase="plan",
        pipeline_state=pipeline_state,
        worktree_path=worktree_path,
        branch_verified=branch_verified,
    )
