"""
Unit tests for decision API endpoints with type-aware fields.

Tests POST create-decision with decision_type and questions parameters,
POST without new fields (defaults applied), and GET list/get endpoints
include decision_type and questions in response serialization.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add orchestrator and shared to path
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))


@pytest.fixture
def app(tmp_path):
    """Create a test Flask app with the decisions blueprint."""
    from flask import Flask
    from routes.decisions import decisions_bp

    app = Flask(__name__)
    app.register_blueprint(decisions_bp)
    app.config["TESTING"] = True

    yield app


@pytest.fixture
def client(app):
    """Create a test client."""
    return app.test_client()


def _make_pipeline(pipeline_id="test-pipeline", decisions=None):
    """Create a mock Pipeline object."""
    from models import Pipeline

    pipeline = Pipeline(
        id=pipeline_id,
        issue_number=42,
        repo="owner/repo",
        branch="egg/test",
    )
    if decisions:
        pipeline.decisions = decisions
    return pipeline


def _make_decision(
    decision_id="decision-1",
    question="Test question?",
    decision_type="choice",
    questions=None,
    status="pending",
    resolution=None,
):
    """Create a mock HITLDecision."""
    from models import DecisionStatus, HITLDecision

    return HITLDecision(
        id=decision_id,
        question=question,
        decision_type=decision_type,
        questions=questions or [],
        status=DecisionStatus(status),
        resolution=resolution,
    )


class TestCreateDecisionWithType:
    """Tests for POST create-decision with decision_type and questions."""

    @patch("routes.decisions.get_repo_path")
    @patch("routes.decisions.get_decision_queue")
    def test_create_with_decision_type_feedback(self, mock_get_queue, mock_repo, client, tmp_path):
        """POST with decision_type='feedback' and questions creates correct decision."""
        mock_repo.return_value = tmp_path
        mock_queue = MagicMock()
        questions = [
            {"id": "q-1", "question": "Expected volume?", "answer": ""},
            {"id": "q-2", "question": "Performance reqs?", "answer": ""},
        ]
        mock_decision = _make_decision(
            decision_type="feedback",
            questions=questions,
        )
        mock_queue.queue_decision.return_value = mock_decision
        mock_get_queue.return_value = mock_queue

        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions",
            json={
                "question": "Please provide feedback",
                "decision_type": "feedback",
                "questions": questions,
            },
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        decision = data["data"]["decision"]
        assert decision["decision_type"] == "feedback"
        assert decision["questions"] == questions

        # Verify queue_decision was called with new params
        mock_queue.queue_decision.assert_called_once_with(
            question="Please provide feedback",
            context="",
            options=None,
            decision_type="feedback",
            questions=questions,
            phase=None,
        )

    @patch("routes.decisions.get_repo_path")
    @patch("routes.decisions.get_decision_queue")
    def test_create_with_decision_type_phase_gate(
        self, mock_get_queue, mock_repo, client, tmp_path
    ):
        """POST with decision_type='phase_gate' creates correct decision."""
        mock_repo.return_value = tmp_path
        mock_queue = MagicMock()
        mock_decision = _make_decision(decision_type="phase_gate")
        mock_queue.queue_decision.return_value = mock_decision
        mock_get_queue.return_value = mock_queue

        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions",
            json={
                "question": "Approve the plan?",
                "decision_type": "phase_gate",
                "options": ["approve", "request changes"],
            },
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["data"]["decision"]["decision_type"] == "phase_gate"

    @patch("routes.decisions.get_repo_path")
    @patch("routes.decisions.get_decision_queue")
    def test_create_without_new_fields_defaults(self, mock_get_queue, mock_repo, client, tmp_path):
        """POST without decision_type and questions applies defaults."""
        mock_repo.return_value = tmp_path
        mock_queue = MagicMock()
        mock_decision = _make_decision()  # defaults: decision_type='choice', questions=[]
        mock_queue.queue_decision.return_value = mock_decision
        mock_get_queue.return_value = mock_queue

        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions",
            json={"question": "Pick one?", "options": ["A", "B"]},
        )

        assert response.status_code == 200
        data = response.get_json()
        decision = data["data"]["decision"]
        assert decision["decision_type"] == "choice"
        assert decision["questions"] == []

        # Verify defaults passed through
        mock_queue.queue_decision.assert_called_once_with(
            question="Pick one?",
            context="",
            options=["A", "B"],
            decision_type="choice",
            questions=None,
            phase=None,
        )


class TestListDecisionsSerialization:
    """Tests for GET list-decisions including decision_type and questions."""

    @patch("routes.decisions.get_repo_path")
    @patch("routes.decisions.get_decision_queue")
    def test_list_includes_new_fields(self, mock_get_queue, mock_repo, client, tmp_path):
        """GET list-decisions includes decision_type and questions in response."""
        mock_repo.return_value = tmp_path
        mock_queue = MagicMock()

        questions = [{"id": "q-1", "question": "Why?", "answer": ""}]
        pipeline = _make_pipeline(
            decisions=[
                _make_decision(
                    decision_id="d1",
                    decision_type="phase_gate",
                ),
                _make_decision(
                    decision_id="d2",
                    decision_type="feedback",
                    questions=questions,
                ),
            ]
        )
        mock_queue._load_pipeline.return_value = pipeline
        mock_get_queue.return_value = mock_queue

        response = client.get("/api/v1/pipelines/test-pipeline/decisions")

        assert response.status_code == 200
        data = response.get_json()
        decisions = data["data"]["decisions"]
        assert len(decisions) == 2

        assert decisions[0]["decision_type"] == "phase_gate"
        assert decisions[0]["questions"] == []

        assert decisions[1]["decision_type"] == "feedback"
        assert decisions[1]["questions"] == questions

    @patch("routes.decisions.get_repo_path")
    @patch("routes.decisions.get_decision_queue")
    def test_get_single_includes_new_fields(self, mock_get_queue, mock_repo, client, tmp_path):
        """GET single decision includes decision_type and questions."""
        mock_repo.return_value = tmp_path
        mock_queue = MagicMock()

        questions = [{"id": "q-1", "question": "Why?", "answer": ""}]
        mock_queue.get_decision.return_value = _make_decision(
            decision_type="feedback",
            questions=questions,
        )
        mock_get_queue.return_value = mock_queue

        response = client.get("/api/v1/pipelines/test-pipeline/decisions/decision-1")

        assert response.status_code == 200
        data = response.get_json()
        decision = data["data"]["decision"]
        assert decision["decision_type"] == "feedback"
        assert decision["questions"] == questions

    @patch("routes.decisions.get_repo_path")
    @patch("routes.decisions.get_decision_queue")
    def test_list_default_type_for_old_decisions(self, mock_get_queue, mock_repo, client, tmp_path):
        """Decisions without explicit type serialize as 'choice' with empty questions."""
        mock_repo.return_value = tmp_path
        mock_queue = MagicMock()

        # Simulate an old-style decision (defaults applied by Pydantic)
        pipeline = _make_pipeline(
            decisions=[
                _make_decision(decision_id="d1"),
            ]
        )
        mock_queue._load_pipeline.return_value = pipeline
        mock_get_queue.return_value = mock_queue

        response = client.get("/api/v1/pipelines/test-pipeline/decisions")

        assert response.status_code == 200
        data = response.get_json()
        decisions = data["data"]["decisions"]
        assert decisions[0]["decision_type"] == "choice"
        assert decisions[0]["questions"] == []


class TestDecisionTypeValidation:
    """Tests for decision_type validation in the POST create-decision endpoint."""

    @patch("routes.decisions.get_repo_path")
    def test_invalid_decision_type_returns_400(self, mock_repo, client, tmp_path):
        """POST with an unrecognized decision_type returns 400."""
        mock_repo.return_value = tmp_path

        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions",
            json={
                "question": "Approve the plan?",
                "decision_type": "phase_gat",  # typo
            },
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data["success"] is False
        assert "Invalid decision_type" in data["message"]
        assert "phase_gat" in data["message"]

    @patch("routes.decisions.get_repo_path")
    def test_empty_string_decision_type_returns_400(self, mock_repo, client, tmp_path):
        """POST with empty string decision_type returns 400."""
        mock_repo.return_value = tmp_path

        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions",
            json={
                "question": "Approve the plan?",
                "decision_type": "",
            },
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data["success"] is False

    @patch("routes.decisions.get_repo_path")
    def test_arbitrary_string_decision_type_returns_400(self, mock_repo, client, tmp_path):
        """POST with arbitrary decision_type returns 400 with valid types listed."""
        mock_repo.return_value = tmp_path

        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions",
            json={
                "question": "Approve?",
                "decision_type": "chocie",  # typo of 'choice'
            },
        )

        assert response.status_code == 400
        data = response.get_json()
        assert "phase_gate" in data["message"]
        assert "choice" in data["message"]
        assert "feedback" in data["message"]

    @patch("routes.decisions.get_repo_path")
    @patch("routes.decisions.get_decision_queue")
    def test_valid_decision_types_accepted(self, mock_get_queue, mock_repo, client, tmp_path):
        """POST with each valid decision_type succeeds."""
        mock_repo.return_value = tmp_path
        mock_queue = MagicMock()
        mock_get_queue.return_value = mock_queue

        for dtype in ("phase_gate", "choice", "feedback"):
            mock_queue.queue_decision.return_value = _make_decision(decision_type=dtype)
            response = client.post(
                "/api/v1/pipelines/test-pipeline/decisions",
                json={
                    "question": "Test?",
                    "decision_type": dtype,
                },
            )
            assert response.status_code == 200, f"Expected 200 for decision_type={dtype}"

    @patch("routes.decisions.get_repo_path")
    @patch("routes.decisions.get_decision_queue")
    def test_missing_decision_type_defaults_to_choice(
        self, mock_get_queue, mock_repo, client, tmp_path
    ):
        """POST without decision_type defaults to 'choice' and succeeds."""
        mock_repo.return_value = tmp_path
        mock_queue = MagicMock()
        mock_decision = _make_decision()
        mock_queue.queue_decision.return_value = mock_decision
        mock_get_queue.return_value = mock_queue

        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions",
            json={"question": "Pick one?"},
        )

        assert response.status_code == 200
        # Verify default 'choice' was passed to queue_decision
        call_kwargs = mock_queue.queue_decision.call_args
        assert call_kwargs[1]["decision_type"] == "choice"


class TestQueueDecisionValidation:
    """Tests for POST create-decision input validation and error handling."""

    @patch("routes.decisions.get_repo_path")
    def test_missing_question_returns_400(self, mock_repo, client, tmp_path):
        """POST without question field returns 400."""
        mock_repo.return_value = tmp_path

        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions",
            json={"options": ["A", "B"]},
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data["success"] is False
        assert "question" in data["message"].lower()

    @patch("routes.decisions.get_repo_path")
    def test_empty_body_returns_400(self, mock_repo, client, tmp_path):
        """POST with empty body returns 400."""
        mock_repo.return_value = tmp_path

        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions",
            content_type="application/json",
            data="{}",
        )

        assert response.status_code == 400

    @patch("routes.decisions.get_repo_path")
    @patch("routes.decisions.get_decision_queue")
    def test_create_with_choice_type_and_options(self, mock_get_queue, mock_repo, client, tmp_path):
        """POST with decision_type='choice' and explicit options."""
        mock_repo.return_value = tmp_path
        mock_queue = MagicMock()
        mock_decision = _make_decision(
            decision_type="choice",
        )
        mock_queue.queue_decision.return_value = mock_decision
        mock_get_queue.return_value = mock_queue

        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions",
            json={
                "question": "Which approach?",
                "decision_type": "choice",
                "options": ["REST", "GraphQL", "gRPC"],
            },
        )

        assert response.status_code == 200
        mock_queue.queue_decision.assert_called_once_with(
            question="Which approach?",
            context="",
            options=["REST", "GraphQL", "gRPC"],
            decision_type="choice",
            questions=None,
            phase=None,
        )


class TestResolveDecisionEndpoint:
    """Tests for POST resolve-decision endpoint."""

    @patch("routes.decisions.get_repo_path")
    @patch("routes.decisions.get_decision_queue")
    def test_resolve_success(self, mock_get_queue, mock_repo, client, tmp_path):
        """POST resolve returns resolved decision."""
        mock_repo.return_value = tmp_path
        mock_queue = MagicMock()

        resolved_decision = _make_decision(
            status="resolved",
            resolution='{"action": "approve"}',
        )
        mock_queue.resolve_decision.return_value = resolved_decision
        mock_get_queue.return_value = mock_queue

        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions/decision-1/resolve",
            json={"resolution": '{"action": "approve"}'},
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        assert data["data"]["decision"]["status"] == "resolved"

    @patch("routes.decisions.get_repo_path")
    def test_resolve_missing_resolution_returns_400(self, mock_repo, client, tmp_path):
        """POST resolve without resolution field returns 400."""
        mock_repo.return_value = tmp_path

        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions/decision-1/resolve",
            json={},
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data["success"] is False

    @patch("routes.decisions.get_repo_path")
    @patch("routes.decisions.get_decision_queue")
    def test_resolve_not_found_returns_404(self, mock_get_queue, mock_repo, client, tmp_path):
        """POST resolve for non-existent decision returns 404."""
        from decision_queue import DecisionNotFoundError

        mock_repo.return_value = tmp_path
        mock_queue = MagicMock()
        mock_queue.resolve_decision.side_effect = DecisionNotFoundError("Not found")
        mock_get_queue.return_value = mock_queue

        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions/decision-99/resolve",
            json={"resolution": "approve"},
        )

        assert response.status_code == 404

    @patch("routes.decisions.get_repo_path")
    @patch("routes.decisions.get_decision_queue")
    def test_resolve_already_resolved_returns_409(
        self, mock_get_queue, mock_repo, client, tmp_path
    ):
        """POST resolve for already-resolved decision returns 409."""
        from decision_queue import DecisionAlreadyResolvedError

        mock_repo.return_value = tmp_path
        mock_queue = MagicMock()
        mock_queue.resolve_decision.side_effect = DecisionAlreadyResolvedError("Already resolved")
        mock_get_queue.return_value = mock_queue

        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions/decision-1/resolve",
            json={"resolution": "approve"},
        )

        assert response.status_code == 409


class TestCancelDecisionEndpoint:
    """Tests for POST cancel-decision endpoint."""

    @patch("routes.decisions.get_repo_path")
    @patch("routes.decisions.get_decision_queue")
    def test_cancel_success(self, mock_get_queue, mock_repo, client, tmp_path):
        """POST cancel returns cancelled decision."""
        mock_repo.return_value = tmp_path
        mock_queue = MagicMock()

        cancelled_decision = _make_decision(status="cancelled")
        # Override status to CANCELLED
        from models import DecisionStatus

        cancelled_decision.status = DecisionStatus.CANCELLED
        mock_queue.cancel_decision.return_value = cancelled_decision
        mock_get_queue.return_value = mock_queue

        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions/decision-1/cancel",
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        assert data["data"]["decision"]["status"] == "cancelled"

    @patch("routes.decisions.get_repo_path")
    @patch("routes.decisions.get_decision_queue")
    def test_cancel_not_found_returns_404(self, mock_get_queue, mock_repo, client, tmp_path):
        """POST cancel for non-existent decision returns 404."""
        from decision_queue import DecisionNotFoundError

        mock_repo.return_value = tmp_path
        mock_queue = MagicMock()
        mock_queue.cancel_decision.side_effect = DecisionNotFoundError("Not found")
        mock_get_queue.return_value = mock_queue

        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions/decision-99/cancel",
        )

        assert response.status_code == 404


class TestGetDecisionEndpointErrors:
    """Tests for GET single decision error handling."""

    @patch("routes.decisions.get_repo_path")
    @patch("routes.decisions.get_decision_queue")
    def test_get_not_found_returns_404(self, mock_get_queue, mock_repo, client, tmp_path):
        """GET non-existent decision returns 404."""
        from decision_queue import DecisionNotFoundError

        mock_repo.return_value = tmp_path
        mock_queue = MagicMock()
        mock_queue.get_decision.side_effect = DecisionNotFoundError("Not found")
        mock_get_queue.return_value = mock_queue

        response = client.get(
            "/api/v1/pipelines/test-pipeline/decisions/decision-99",
        )

        assert response.status_code == 404


class TestQueueStatusEndpoint:
    """Tests for GET queue-status endpoint."""

    @patch("routes.decisions.get_repo_path")
    @patch("routes.decisions.get_decision_queue")
    def test_status_success(self, mock_get_queue, mock_repo, client, tmp_path):
        """GET queue status returns counts."""
        mock_repo.return_value = tmp_path
        mock_queue = MagicMock()
        mock_queue.get_queue_status.return_value = {
            "pipeline_id": "test-pipeline",
            "total_decisions": 3,
            "pending": 1,
            "resolved": 2,
            "pending_decisions": [
                {"id": "d3", "question": "Pending?", "created_at": "2024-01-01T00:00:00"},
            ],
        }
        mock_get_queue.return_value = mock_queue

        response = client.get(
            "/api/v1/pipelines/test-pipeline/decisions/status",
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        assert data["data"]["total_decisions"] == 3
        assert data["data"]["pending"] == 1


class TestListDecisionsFiltering:
    """Tests for GET list-decisions with pending_only filter."""

    @patch("routes.decisions.get_repo_path")
    @patch("routes.decisions.get_decision_queue")
    def test_pending_only_filter(self, mock_get_queue, mock_repo, client, tmp_path):
        """GET with pending_only=true returns only pending decisions."""
        mock_repo.return_value = tmp_path
        mock_queue = MagicMock()

        pending = _make_decision(decision_id="d1", status="pending")
        mock_queue.get_pending_decisions.return_value = [pending]
        mock_get_queue.return_value = mock_queue

        response = client.get(
            "/api/v1/pipelines/test-pipeline/decisions?pending_only=true",
        )

        assert response.status_code == 200
        data = response.get_json()
        assert len(data["data"]["decisions"]) == 1
        assert data["data"]["decisions"][0]["status"] == "pending"
        mock_queue.get_pending_decisions.assert_called_once()
