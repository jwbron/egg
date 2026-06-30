"""SDLC/HITL handlers (decisions, feedback, HITL-answer checks, contract read, criterion verification)."""

from __future__ import annotations

import logging
from typing import Any

from egg_contracts.decisions import (
    find_duplicate_open_question,
    find_resolved_question,
    next_cq_id,
)

from egg_agent_tools.handlers._gateway import (
    container_id_field,
    gateway_request,
    get_contract_identifier,
    get_repo_path,
)
from egg_agent_tools.handlers.errors import GatewayError, HandlerError

_logger = logging.getLogger(__name__)

_VALID_PHASES = {"refine", "plan", "implement", "pr"}

# Bounded retry on decision TOCTOU collisions.  Two concurrent agents
# creating decisions may both observe ``len(decisions) == N`` and race
# on ``decisions.N``.  Same pattern as ``_GAP_RETRY_ATTEMPTS`` in
# ``task.py``.
_DECISION_RETRY_ATTEMPTS = 3


def _resolve_identifier(req: dict[str, Any]) -> int | str:
    """Resolve the contract identifier from the request or environment."""
    explicit = req.get("issue") or req.get("pipeline_id")
    if explicit:
        return explicit  # type: ignore[no-any-return]
    identifier = get_contract_identifier()
    if identifier is None:
        raise HandlerError(
            "Contract identifier required. "
            "Set EGG_ISSUE_NUMBER or EGG_PIPELINE_ID, or pass 'issue'/'pipeline_id'."
        )
    return identifier


def _fetch_contract(identifier: int | str, repo_path: str | None) -> dict[str, Any]:
    params: dict[str, str] = {}
    if repo_path:
        params["repo_path"] = repo_path
    from egg_agent_tools.handlers._gateway import get_container_id

    cid = get_container_id()
    if cid:
        params["container_id"] = cid

    result = gateway_request(f"/api/v1/contract/{identifier}", params=params or None)
    if not result.get("success"):
        raise GatewayError(result.get("message", "contract fetch failed"))
    return result.get("data") or {}


def register_open_question(req: dict[str, Any]) -> dict[str, Any]:
    """Create a HITL decision point on the contract.

    Request:
        question (str): required.
        options (list[str]): optional choices; an "Other" option is always
            appended automatically for parity with the CLI.
        phase (str): optional override; defaults to the contract's
            ``current_phase``.
        redirect_seed (str): optional. Machine-consumed payload for the
            ``first_principles_reviewer`` seed-redirect accept-path — the FULL
            proposed ``task_description`` the operator adopts when resolving
            this decision. Stored on the decision itself (it rides the same
            contract-mutate RPC that creates the decision), because a BRC
            reviewer has no commit/push path to carry a free-standing
            agent-worktree file into the shared pipeline worktree.
        repo_path (str): optional override for repo path.
        pipeline_id / issue: optional contract identifier.

    Response:
        { ok: True, decision: {...}, id: "cq-N" }

        On a dedup hit (the same normalized question already registered and
        unanswered in the same phase), the existing decision is returned
        verbatim with an extra ``deduped: True`` and no contract write.
    """
    question = req.get("question")
    if not question or not isinstance(question, str):
        raise HandlerError("'question' is required")
    phase = req.get("phase")
    if phase is not None and phase not in _VALID_PHASES:
        raise HandlerError(f"'phase' must be one of {sorted(_VALID_PHASES)}; got {phase!r}")
    options = list(req.get("options") or [])
    redirect_seed = req.get("redirect_seed")
    if redirect_seed is not None and not isinstance(redirect_seed, str):
        raise HandlerError("'redirect_seed' must be a string when provided")
    repo_path = req.get("repo_path") or get_repo_path()

    identifier = _resolve_identifier(req)

    opt_objs: list[dict[str, Any]] = []
    if options:
        for i, opt in enumerate(options, start=1):
            opt_objs.append({"id": f"opt-{i}", "label": opt, "description": None})
        opt_objs.append(
            {
                "id": f"opt-{len(options) + 1}",
                "label": "Other (explain in reply)",
                "description": None,
            }
        )

    # TOCTOU hardening: two concurrent agents creating decisions may
    # both observe ``len(decisions) == N`` and race on ``decisions.N``.
    # Retry up to ``_DECISION_RETRY_ATTEMPTS`` times, re-reading the
    # contract on each attempt (same pattern as ``task_mark_gap``).
    last_error: GatewayError | None = None
    for attempt in range(1, _DECISION_RETRY_ATTEMPTS + 1):
        contract = _fetch_contract(identifier, repo_path)
        decisions = contract.get("decisions", []) or []
        next_idx = len(decisions)
        decision_phase = phase or contract.get("current_phase")

        # Dedupe: a later phase (or a re-run agent) that re-asks a
        # question already registered and unanswered should adopt the
        # existing ``cq-N`` rather than mint a duplicate — otherwise the
        # operator who answers ``cq-1`` still faces an identical ``cq-4``.
        # Idempotent: no contract write, return the prior decision.
        duplicate = find_duplicate_open_question(decisions, question, decision_phase)
        if duplicate is not None:
            # The dedup key is (normalized question, phase) — neither the option
            # set nor the redirect seed is part of it. If the re-registration
            # carries a different option set or a different ``redirect_seed``,
            # the operator only ever sees the stored values; the new ones are
            # silently discarded. Log it so that loss is not invisible
            # (#3374 review).
            new_labels = [o["label"] for o in opt_objs]
            existing_labels = [
                o.get("label") for o in (duplicate.get("options") or []) if isinstance(o, dict)
            ]
            if new_labels != existing_labels:
                _logger.warning(
                    "register_open_question deduped onto %s but the re-registration's "
                    "options differ from the stored ones; keeping the stored set "
                    "(new=%r, stored=%r)",
                    duplicate.get("id"),
                    new_labels,
                    existing_labels,
                )
            existing_seed = duplicate.get("redirect_seed")
            if redirect_seed is not None and redirect_seed != existing_seed:
                _logger.warning(
                    "register_open_question deduped onto %s but the re-registration's "
                    "redirect_seed differs from the stored one; keeping the stored "
                    "seed (the operator adopts the originally-registered redirect)",
                    duplicate.get("id"),
                )
            return {
                "ok": True,
                "id": duplicate.get("id"),
                "decision": duplicate,
                "deduped": True,
            }

        # Carry-forward: a phase that re-runs to fold in operator
        # resolutions (the converge-before-advance loop, #3392) may
        # re-register a question already *answered* in a prior round.
        # Minting a fresh ``cq-N`` here would re-surface an answered
        # decision and the loop would never reach a fixpoint. Adopt the
        # resolved decision instead — idempotent, no contract write, and
        # the response carries the prior ``resolution`` so the agent reads
        # the answer rather than re-asking the operator.
        resolved_match = find_resolved_question(decisions, question, decision_phase)
        if resolved_match is not None:
            _logger.info(
                "register_open_question adopted resolved decision %s "
                "(carry-forward across phase re-run); not re-surfacing",
                resolved_match.get("id"),
            )
            return {
                "ok": True,
                "id": resolved_match.get("id"),
                "decision": resolved_match,
                "deduped": True,
                "carried_forward": True,
            }

        # Agent-registered contract questions allocate ``cq-N`` from a
        # counter that ignores ``decision-N`` entries (written by the
        # orchestrator's pipeline-side bridge). See
        # ``shared/egg_contracts/decisions.py`` for the namespace split
        # rationale (#2616).
        new_id = next_cq_id(decisions)

        new_decision = {
            "id": new_id,
            "question": question,
            "type": "hitl",
            "phase": decision_phase,
            "options": opt_objs,
            "resolved": False,
            "resolution": None,
            "resolved_by": None,
            "resolved_at": None,
            "debounce_until": None,
        }
        if redirect_seed is not None:
            new_decision["redirect_seed"] = redirect_seed

        reason = f"Created HITL decision: {question[:50]}" + ("..." if len(question) > 50 else "")
        result = gateway_request(
            "/api/v1/contract/mutate",
            method="POST",
            data={
                "identifier": identifier,
                "repo_path": repo_path,
                "field_path": f"decisions.{next_idx}",
                "new_value": new_decision,
                "actor": "egg",
                "reason": reason,
                **container_id_field(),
            },
        )
        if result.get("success"):
            return {
                "ok": True,
                "id": new_decision["id"],
                "decision": new_decision,
            }

        message = result.get("message", "decision mutate failed")
        last_error = GatewayError(message)
        retryable = (
            "index" in message.lower()
            or "out of range" in message.lower()
            or "already exists" in message.lower()
            or "conflict" in message.lower()
        )
        if not retryable or attempt == _DECISION_RETRY_ATTEMPTS:
            break

    if last_error is None:
        raise HandlerError("register_open_question failed: no attempts were made")
    raise last_error


def request_feedback(req: dict[str, Any]) -> dict[str, Any]:
    """Create/replace the open-ended feedback request on the contract.

    Request:
        questions (list[str]): at least one question. Also accepts
            ``question`` for CLI parity.
        repo_path, pipeline_id, issue: optional overrides.

    Response:
        { ok: True, id: "feedback-N", questions: [{id, question}, ...] }
    """
    raw_questions = req.get("questions") or req.get("question")
    if isinstance(raw_questions, str):
        questions_list = [raw_questions]
    else:
        questions_list = list(raw_questions or [])
    if not questions_list:
        raise HandlerError("At least one 'question' is required")

    repo_path = req.get("repo_path") or get_repo_path()
    identifier = _resolve_identifier(req)
    contract = _fetch_contract(identifier, repo_path)

    # Generate feedback ID using same helper as the CLI for parity.
    try:
        from egg_contracts.feedback import (
            FeedbackQuestionInput,
            generate_feedback_comment,
            generate_feedback_id,
        )
    except ImportError as exc:  # pragma: no cover - missing dep
        raise HandlerError(f"egg_contracts.feedback unavailable: {exc}") from exc

    existing_feedback = contract.get("feedback")
    warning: str | None = None
    if existing_feedback and not existing_feedback.get("submitted"):
        warning = (
            f"There is already pending feedback ({existing_feedback.get('id')}). "
            "Creating new feedback will replace it."
        )
    existing_ids = [existing_feedback.get("id")] if existing_feedback else []
    feedback_id = generate_feedback_id(existing_ids)

    questions: list[dict[str, Any]] = []
    for i, q in enumerate(questions_list, start=1):
        questions.append({"id": f"Q{i}", "question": q, "answer": None})

    new_feedback = {
        "id": feedback_id,
        "phase": contract.get("current_phase"),
        "questions": questions,
        "submitted": False,
        "submitted_by": None,
        "submitted_at": None,
        "comment_id": None,
        "debounce_until": None,
    }

    result = gateway_request(
        "/api/v1/contract/mutate",
        method="POST",
        data={
            "identifier": identifier,
            "repo_path": repo_path,
            "field_path": "feedback",
            "new_value": new_feedback,
            "actor": "egg",
            "reason": f"Created feedback request with {len(questions)} question(s)",
            **container_id_field(),
        },
    )
    if not result.get("success"):
        raise GatewayError(result.get("message", "feedback mutate failed"))

    # Pre-render the markdown comment so the MCP caller can post it
    # without having to import egg_contracts itself.
    markdown = generate_feedback_comment(
        feedback_id,
        [FeedbackQuestionInput(id=q["id"], question=q["question"]) for q in questions],
    )
    response: dict[str, Any] = {
        "ok": True,
        "id": feedback_id,
        "questions": questions,
        "markdown": markdown,
    }
    if warning:
        response["warning"] = warning
    return response


def check_hitl_answers(req: dict[str, Any]) -> dict[str, Any]:
    """Fetch resolved decisions and feedback (submitted or pending) from the contract.

    Request:
        phase (str): optional filter — only return decisions/feedback
            attached to the given phase. When omitted, returns HITL for
            *all* phases of the pipeline so a later-phase caller can see
            what earlier phases already resolved.
        include_unresolved (bool): if True, also include unresolved
            decisions. Defaults to False.
        repo_path, pipeline_id, issue: optional overrides.

    Response:
        { ok: True, decisions: [...], feedback: {...}|None }

    No CLI counterpart — this is a pure read-through over the contract
    gateway surfaced as a first-class MCP capability so agents never
    have to shell out to `egg-contract show --json | python3 -c ...`
    to extract resolved HITL answers. See decision-13.
    """
    phase = req.get("phase")
    if phase is not None and phase not in _VALID_PHASES:
        raise HandlerError(f"'phase' must be one of {sorted(_VALID_PHASES)}; got {phase!r}")
    include_unresolved = bool(req.get("include_unresolved", False))
    repo_path = req.get("repo_path") or get_repo_path()
    identifier = _resolve_identifier(req)
    contract = _fetch_contract(identifier, repo_path)

    decisions = contract.get("decisions", []) or []
    filtered: list[dict[str, Any]] = []
    for d in decisions:
        if phase and d.get("phase") != phase:
            continue
        if not include_unresolved and not d.get("resolved"):
            continue
        filtered.append(d)

    feedback = contract.get("feedback")
    if feedback and phase and feedback.get("phase") != phase:
        feedback = None

    return {
        "ok": True,
        "decisions": filtered,
        "feedback": feedback,
    }


def show_contract(req: dict[str, Any]) -> dict[str, Any]:
    """Return the contract state, optionally projected to a subset of fields.

    Request:
        fields (list[str]): optional — if supplied, only return the
            named top-level fields. Unknown fields raise HandlerError
            (so agents learn the contract shape rather than silently
            losing data to a typo).
        audit (bool): if True, include the audit log in the response
            (mirrors ``egg-contract show --audit``).
        repo_path, pipeline_id, issue: optional overrides.

    Response:
        { ok: True, contract: {...} }

    Reads contract; no mutations. State-machine effect: none — this is
    a pure read over the contract gateway.
    """
    repo_path = req.get("repo_path") or get_repo_path()
    include_audit = bool(req.get("audit", False))
    identifier = _resolve_identifier(req)

    # Build params, including optional audit-log flag so the payload
    # matches `egg-contract show --audit`.
    params: dict[str, str] = {}
    if repo_path:
        params["repo_path"] = repo_path
    if include_audit:
        params["include_audit_log"] = "true"
    from egg_agent_tools.handlers._gateway import get_container_id

    cid = get_container_id()
    if cid:
        params["container_id"] = cid

    result = gateway_request(f"/api/v1/contract/{identifier}", params=params or None)
    if not result.get("success"):
        raise GatewayError(result.get("message", "contract fetch failed"))
    contract = result.get("data", {}) or {}

    fields = req.get("fields")
    if fields is not None:
        if not isinstance(fields, list):
            raise HandlerError("'fields' must be a list of strings if provided")
        projected: dict[str, Any] = {}
        for name in fields:
            if not isinstance(name, str):
                raise HandlerError(f"'fields' entries must be strings; got {type(name).__name__}")
            if name not in contract:
                raise HandlerError(f"Unknown field: {name}")
            projected[name] = contract[name]
        contract = projected

    return {"ok": True, "contract": contract}


def verify_criterion(req: dict[str, Any]) -> dict[str, Any]:
    """Mark an acceptance criterion as verified.

    REVIEWER role required: the gateway rejects non-REVIEWER writers
    (see shared/egg_contracts/roles.py — 'acceptance_criteria.*.verified'
    is owned by Role.REVIEWER). This handler does NOT re-check the role
    in-process per decision-7; the gateway is the single enforcer.

    Request:
        criterion (str): required — e.g. ``ac-1``.
        repo_path, pipeline_id, issue: optional overrides.

    Response:
        { ok: True, criterion: "ac-1" }

    State-machine effect: flips ``acceptance_criteria.<N>.verified`` to
    True. No-op if already verified.
    """
    criterion_id = req.get("criterion")
    if not criterion_id or not isinstance(criterion_id, str):
        raise HandlerError("'criterion' is required")

    lower = criterion_id.lower()
    stripped = lower.removeprefix("ac-")
    if stripped == lower:
        raise HandlerError(f"Invalid criterion ID '{criterion_id}': expected format 'ac-N'")
    try:
        criterion_num = int(stripped)
    except ValueError as exc:
        raise HandlerError(
            f"Invalid criterion ID '{criterion_id}': expected format 'ac-N'"
        ) from exc
    if criterion_num < 1:
        raise HandlerError(f"Criterion number must be >= 1: {criterion_id}")
    criterion_idx = criterion_num - 1

    repo_path = req.get("repo_path") or get_repo_path()
    identifier = _resolve_identifier(req)

    # Pre-flight bounds check: read the contract to verify the criterion
    # index exists.  Without this, the gateway receives a field_path
    # pointing at a non-existent index, which could error opaquely or
    # (worse) create a sparse array.  Matches the pattern in
    # ``task_mark_gap`` which pre-flights array bounds before writing.
    contract = _fetch_contract(identifier, repo_path)
    criteria = contract.get("acceptance_criteria") or []
    if criterion_idx >= len(criteria):
        raise HandlerError(
            f"Criterion index {criterion_num} out of range for contract "
            f"(has {len(criteria)} acceptance criteria)"
        )

    field_path = f"acceptance_criteria.{criterion_idx}.verified"
    result = gateway_request(
        "/api/v1/contract/mutate",
        method="POST",
        data={
            "identifier": identifier,
            "repo_path": repo_path,
            "field_path": field_path,
            "new_value": True,
            "actor": "egg",
            "reason": f"Verified criterion {criterion_id}",
            **container_id_field(),
        },
    )
    if not result.get("success"):
        raise GatewayError(result.get("message", "criterion verify failed"))

    return {"ok": True, "criterion": criterion_id}
