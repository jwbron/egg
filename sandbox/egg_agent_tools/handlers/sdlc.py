"""SDLC/HITL handlers (decisions, feedback, HITL-answer checks)."""

from __future__ import annotations

from typing import Any

from egg_agent_tools.handlers._gateway import (
    container_id_field,
    gateway_request,
    get_contract_identifier,
    get_repo_path,
)
from egg_agent_tools.handlers.errors import GatewayError, HandlerError

_VALID_PHASES = {"refine", "plan", "implement", "pr"}


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
    return result.get("data", {})  # type: ignore[no-any-return]


def register_open_question(req: dict[str, Any]) -> dict[str, Any]:
    """Create a HITL decision point on the contract.

    Request:
        question (str): required.
        options (list[str]): optional choices; an "Other" option is always
            appended automatically for parity with the CLI.
        phase (str): optional override; defaults to the contract's
            ``current_phase``.
        repo_path (str): optional override for repo path.
        pipeline_id / issue: optional contract identifier.

    Response:
        { ok: True, decision: {...}, id: "decision-N" }
    """
    question = req.get("question")
    if not question or not isinstance(question, str):
        raise HandlerError("'question' is required")
    phase = req.get("phase")
    if phase is not None and phase not in _VALID_PHASES:
        raise HandlerError(f"'phase' must be one of {sorted(_VALID_PHASES)}; got {phase!r}")
    options = list(req.get("options") or [])
    repo_path = req.get("repo_path") or get_repo_path()

    identifier = _resolve_identifier(req)
    contract = _fetch_contract(identifier, repo_path)
    decisions = contract.get("decisions", [])
    next_idx = len(decisions)
    decision_phase = phase or contract.get("current_phase")

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

    new_decision = {
        "id": f"decision-{next_idx + 1}",
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
    if not result.get("success"):
        raise GatewayError(result.get("message", "decision mutate failed"))

    return {
        "ok": True,
        "id": new_decision["id"],
        "decision": new_decision,
    }


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
