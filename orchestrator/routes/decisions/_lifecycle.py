"""cancel / answer-feedback / queue-status endpoints (#3312 decomposition)."""

from datetime import UTC, datetime

import routes.decisions as _pkg
from decision_queue import DecisionNotFoundError
from flask import Response, request
from state_store import InvalidPipelineIdError, PipelineNotFoundError

from . import logger
from ._responses import make_error_response, make_success_response


def cancel_decision(pipeline_id: str, decision_id: str) -> tuple[Response, int]:
    """
    Cancel a pending decision.

    URL params:
        pipeline_id: Pipeline ID
        decision_id: Decision ID

    Response:
        {
            "success": true,
            "message": "Decision cancelled"
        }
    """
    try:
        store, _pipeline = _pkg.get_state_store_for_pipeline(pipeline_id)
    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )

    try:
        queue = _pkg.get_decision_queue(pipeline_id, store.repo_path)
        decision = queue.cancel_decision(decision_id)

        logger.info(
            "Decision cancelled",
            pipeline_id=pipeline_id,
            decision_id=decision_id,
            source=getattr(request, "egg_source", "unknown"),
        )

        return make_success_response(
            "Decision cancelled",
            data={
                "decision": {
                    "id": decision.id,
                    "status": decision.status.value,
                }
            },
        )

    except DecisionNotFoundError:
        return make_error_response(
            f"Decision {decision_id} not found",
            status_code=404,
        )


def answer_feedback(pipeline_id: str) -> tuple[Response, int]:
    """Answer a contract-scoped feedback request from the host operator.

    Agents register open-ended feedback via ``register_feedback_request``
    (``mcp__sdlc__request_feedback``).  Pre-proposal feedback — e.g. a
    refiner asking for a goal on an empty contract — is written only to
    the gateway-backed contract as ``contract.feedback`` (id
    ``feedback-N``).  It is **never** queued as an orchestrator decision
    until *after* the phase_gate is approved by the post-gate bridge
    (:func:`routes.pipelines._queue_and_await_contract_decisions`), so a
    refiner that blocks on the answer before producing any proposal
    deadlocks the pipeline: ``provide_input`` 404s (no such decision in
    the queue) and the only documented answer path
    (``egg-contract``) runs inside agent containers, unreachable from
    the host (#3007).

    This endpoint gives the host operator a first-class answer path. It
    writes the answers straight into the contract and marks the feedback
    submitted — mirroring the write-back the post-gate bridge performs —
    so the waiting agent unblocks on its next contract poll. It is
    lifecycle-secret guarded: only the operator/MCP carry the secret, so
    an agent cannot answer its own feedback (parity with decision
    resolve; see #1769).

    URL params:
        pipeline_id: Pipeline ID

    Request body:
        {
            "answers": {"Q1": "...", "Q2": "..."},   # question id -> answer
            "feedback_id": "feedback-1"                # optional guard
        }
    """
    raw = request.get_json(silent=True)
    if raw is not None and not isinstance(raw, dict):
        return make_error_response("Request body must be a JSON object")
    data = raw if raw is not None else {}

    raw_answers = data.get("answers")
    if not isinstance(raw_answers, dict) or not raw_answers:
        return make_error_response("Missing 'answers' (object mapping question id to answer text)")
    non_string = sorted(str(k) for k, v in raw_answers.items() if not isinstance(v, str))
    if non_string:
        return make_error_response(
            "Answer values must be strings; non-string values for question id(s) "
            f"{non_string}. To leave a question unanswered, omit its id from 'answers'.",
        )
    answers = {str(k): v for k, v in raw_answers.items()}
    requested_feedback_id = data.get("feedback_id")

    try:
        _store, pipeline = _pkg.get_state_store_for_pipeline(pipeline_id)
    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )

    # Lazy imports: ``contract_store`` and ``routes.pipelines`` pull in
    # heavy state-store / docker dependencies; importing them at module
    # top would couple decisions.py to initialisation order. Same pattern
    # as contracts.py's ``_branch_read_contract``. The ``egg_contracts``
    # try/except mirrors the post-gate bridge in
    # ``_queue_and_await_contract_decisions`` so grepping for
    # ``egg_contracts`` ImportError handling finds both sites.
    import contract_store

    try:
        from egg_contracts import (
            ContractNotFoundError,
            ContractValidationError,
            load_contract,
            save_contract,
        )
    except ImportError as exc:
        logger.error(
            "egg_contracts not available; cannot answer contract feedback",
            pipeline_id=pipeline_id,
            error=str(exc),
        )
        return make_error_response(
            "Contract subsystem (egg_contracts) is not available on this host",
            status_code=500,
        )
    from routes.pipelines import _pipeline_identifier

    worktree = contract_store.resolve_pipeline_worktree(pipeline_id)
    if worktree is None:
        return make_error_response(
            f"Pipeline worktree not found for {pipeline_id}",
            status_code=404,
        )

    identifier = _pipeline_identifier(getattr(pipeline, "issue_number", None), pipeline_id)

    with contract_store.lock_for(identifier):
        try:
            contract = load_contract(identifier, worktree)
        except ContractNotFoundError:
            return make_error_response(
                f"Contract for {pipeline_id} not found",
                status_code=404,
            )
        except ContractValidationError as exc:
            return make_error_response(
                f"Contract validation failed: {exc}",
                status_code=500,
            )

        feedback = contract.feedback
        if feedback is None:
            return make_error_response(
                "No feedback request is pending on this contract",
                status_code=404,
            )
        if requested_feedback_id and feedback.id != requested_feedback_id:
            return make_error_response(
                f"Feedback {requested_feedback_id} not found "
                f"(pending feedback on this contract is {feedback.id})",
                status_code=404,
            )
        if feedback.submitted:
            return make_error_response(
                f"Feedback {feedback.id} has already been submitted",
                status_code=409,
            )

        valid_ids = {q.id for q in feedback.questions}
        unknown_ids = sorted(qid for qid in answers if qid not in valid_ids)
        if unknown_ids:
            return make_error_response(
                f"Unknown question id(s) {unknown_ids}; "
                f"valid ids for {feedback.id}: {sorted(valid_ids)}",
                status_code=400,
            )

        for question in feedback.questions:
            if question.id in answers:
                question.answer = answers[question.id]
        # Mark submitted after applying answers — even a partial answer
        # set counts as the human responding, matching the bridge's
        # write-back so the agent isn't re-prompted.
        feedback.submitted = True
        feedback.submitted_by = "human"
        feedback.submitted_at = datetime.now(UTC)

        try:
            save_contract(contract, worktree)
        except Exception as exc:
            logger.error(
                "Failed to save contract after answering feedback",
                pipeline_id=pipeline_id,
                feedback_id=feedback.id,
                error=str(exc),
            )
            return make_error_response(
                f"Failed to save contract: {exc}",
                status_code=500,
            )

        answered_feedback = {
            "id": feedback.id,
            "submitted": feedback.submitted,
            "questions": [
                {"id": q.id, "question": q.question, "answer": q.answer} for q in feedback.questions
            ],
        }

    logger.info(
        "Contract feedback answered by operator",
        pipeline_id=pipeline_id,
        feedback_id=answered_feedback["id"],
        answered_questions=sorted(answers.keys()),
        source=getattr(request, "egg_source", "unknown"),
    )

    return make_success_response(
        "Feedback answered",
        data={"feedback": answered_feedback},
    )


def get_queue_status(pipeline_id: str) -> tuple[Response, int]:
    """
    Get decision queue status.

    URL params:
        pipeline_id: Pipeline ID

    Response:
        {
            "success": true,
            "data": {
                "total_decisions": 5,
                "pending": 2,
                "resolved": 3,
                "pending_decisions": [...]
            }
        }
    """
    try:
        store, _pipeline = _pkg.get_state_store_for_pipeline(pipeline_id)
    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )

    try:
        queue = _pkg.get_decision_queue(pipeline_id, store.repo_path)
        status = queue.get_queue_status()

        return make_success_response("Status retrieved", data=status)

    except Exception as e:
        logger.error("Failed to get queue status", pipeline_id=pipeline_id, error=str(e))
        return make_error_response(f"Failed to get status: {e}", status_code=500)
