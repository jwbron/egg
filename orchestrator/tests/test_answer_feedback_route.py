"""Tests for POST /api/v1/pipelines/<id>/feedback/answer (#3007).

The route lets the host operator answer a contract-scoped feedback
request (``contract.feedback`` / ``feedback-N``) that an agent registered
via ``register_feedback_request``.  Such pre-proposal feedback never
becomes an orchestrator decision, so ``provide_input`` 404s; this route
writes the answers straight into the contract and marks it submitted so
the waiting agent unblocks.

These tests exercise the real contract load/save against a temp worktree
so the on-disk write-back is covered end to end.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Mirror the docker/k8s mocking the other orchestrator tests rely on so the
# lazy ``from routes.pipelines import _pipeline_identifier`` import inside the
# handler does not require a real docker SDK.
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))


PIPELINE_ID = "test-pipeline"


@pytest.fixture
def client():
    from flask import Flask
    from routes.decisions import decisions_bp

    app = Flask(__name__)
    app.register_blueprint(decisions_bp)
    app.config["TESTING"] = True
    return app.test_client()


def _write_contract(worktree: Path, *, feedback=True, submitted=False):
    """Create + persist a contract with (optionally) a pending feedback."""
    from egg_contracts import Contract, Feedback, FeedbackQuestion, save_contract
    from egg_contracts.models import PipelinePhase

    contract = Contract(pipeline_id=PIPELINE_ID, current_phase=PipelinePhase.REFINE)
    if feedback:
        contract.feedback = Feedback(
            id="feedback-1",
            phase=PipelinePhase.REFINE,
            questions=[
                FeedbackQuestion(id="Q1", question="What problem should be refined?"),
                FeedbackQuestion(id="Q2", question="Any success criteria?"),
            ],
            submitted=submitted,
        )
    save_contract(contract, worktree)
    return contract


def _patch_resolution(tmp_path, *, issue_number=None):
    """Patch store + worktree resolution for the route to the temp worktree."""
    store_mock = MagicMock(repo_path=tmp_path)
    pipeline_mock = MagicMock(issue_number=issue_number)
    return (
        patch(
            "routes.decisions.get_state_store_for_pipeline",
            return_value=(store_mock, pipeline_mock),
        ),
        patch("contract_store.resolve_pipeline_worktree", return_value=tmp_path),
    )


def _post(client, body):
    return client.post(f"/api/v1/pipelines/{PIPELINE_ID}/feedback/answer", json=body)


def test_answers_feedback_writes_contract_and_marks_submitted(client, tmp_path):
    from egg_contracts import load_contract

    _write_contract(tmp_path)
    store_patch, worktree_patch = _patch_resolution(tmp_path)

    with store_patch, worktree_patch:
        resp = _post(client, {"answers": {"Q1": "Add retry logic", "Q2": "p99 < 200ms"}})

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["success"] is True
    assert payload["data"]["feedback"]["submitted"] is True

    # The write-back must hit disk so the agent's next contract poll sees it.
    contract = load_contract(PIPELINE_ID, tmp_path)
    assert contract.feedback.submitted is True
    assert contract.feedback.submitted_by == "human"
    assert contract.feedback.submitted_at is not None
    assert contract.feedback.get_question("Q1").answer == "Add retry logic"
    assert contract.feedback.get_question("Q2").answer == "p99 < 200ms"


def test_partial_answer_still_marks_submitted(client, tmp_path):
    from egg_contracts import load_contract

    _write_contract(tmp_path)
    store_patch, worktree_patch = _patch_resolution(tmp_path)

    with store_patch, worktree_patch:
        resp = _post(client, {"answers": {"Q1": "just the goal"}})

    assert resp.status_code == 200
    contract = load_contract(PIPELINE_ID, tmp_path)
    assert contract.feedback.submitted is True
    assert contract.feedback.get_question("Q1").answer == "just the goal"
    # Unanswered question stays None — operator chose to leave it blank.
    assert contract.feedback.get_question("Q2").answer is None


def test_no_pending_feedback_returns_404(client, tmp_path):
    _write_contract(tmp_path, feedback=False)
    store_patch, worktree_patch = _patch_resolution(tmp_path)

    with store_patch, worktree_patch:
        resp = _post(client, {"answers": {"Q1": "x"}})

    assert resp.status_code == 404
    assert "no feedback" in resp.get_json()["message"].lower()


def test_already_submitted_returns_409(client, tmp_path):
    _write_contract(tmp_path, submitted=True)
    store_patch, worktree_patch = _patch_resolution(tmp_path)

    with store_patch, worktree_patch:
        resp = _post(client, {"answers": {"Q1": "x"}})

    assert resp.status_code == 409
    assert "already" in resp.get_json()["message"].lower()


def test_unknown_question_id_returns_400(client, tmp_path):
    _write_contract(tmp_path)
    store_patch, worktree_patch = _patch_resolution(tmp_path)

    with store_patch, worktree_patch:
        resp = _post(client, {"answers": {"Q9": "nonexistent"}})

    assert resp.status_code == 400
    assert "Q9" in resp.get_json()["message"]


def test_feedback_id_mismatch_returns_404(client, tmp_path):
    _write_contract(tmp_path)
    store_patch, worktree_patch = _patch_resolution(tmp_path)

    with store_patch, worktree_patch:
        resp = _post(client, {"answers": {"Q1": "x"}, "feedback_id": "feedback-2"})

    assert resp.status_code == 404
    msg = resp.get_json()["message"]
    assert "feedback-2" in msg and "feedback-1" in msg


def test_missing_answers_returns_400(client, tmp_path):
    _write_contract(tmp_path)
    store_patch, worktree_patch = _patch_resolution(tmp_path)

    with store_patch, worktree_patch:
        resp = _post(client, {})

    assert resp.status_code == 400
    assert "answers" in resp.get_json()["message"].lower()


def test_worktree_not_found_returns_404(client, tmp_path):
    store_mock = MagicMock(repo_path=tmp_path)
    pipeline_mock = MagicMock(issue_number=None)
    with (
        patch(
            "routes.decisions.get_state_store_for_pipeline",
            return_value=(store_mock, pipeline_mock),
        ),
        patch("contract_store.resolve_pipeline_worktree", return_value=None),
    ):
        resp = _post(client, {"answers": {"Q1": "x"}})

    assert resp.status_code == 404
    assert "worktree" in resp.get_json()["message"].lower()


def test_requires_lifecycle_secret(client, tmp_path):
    """Agents (no bearer token) must not be able to answer feedback."""
    _write_contract(tmp_path)
    store_patch, worktree_patch = _patch_resolution(tmp_path)

    with store_patch, worktree_patch:
        resp = client.post(
            f"/api/v1/pipelines/{PIPELINE_ID}/feedback/answer",
            json={"answers": {"Q1": "x"}},
            _lifecycle_auth=False,
        )

    assert resp.status_code == 401


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
