"""
Unit tests for decision API endpoints with type-aware fields.

Tests POST create-decision with decision_type and questions parameters,
POST without new fields (defaults applied), and GET list/get endpoints
include decision_type and questions in response serialization.
"""

import json
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
    from models import DecisionStatus, HITLDecision, Pipeline

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
        )

    @patch("routes.decisions.get_repo_path")
    @patch("routes.decisions.get_decision_queue")
    def test_create_with_decision_type_phase_gate(self, mock_get_queue, mock_repo, client, tmp_path):
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
        pipeline = _make_pipeline(decisions=[
            _make_decision(
                decision_id="d1",
                decision_type="phase_gate",
            ),
            _make_decision(
                decision_id="d2",
                decision_type="feedback",
                questions=questions,
            ),
        ])
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
        pipeline = _make_pipeline(decisions=[
            _make_decision(decision_id="d1"),
        ])
        mock_queue._load_pipeline.return_value = pipeline
        mock_get_queue.return_value = mock_queue

        response = client.get("/api/v1/pipelines/test-pipeline/decisions")

        assert response.status_code == 200
        data = response.get_json()
        decisions = data["data"]["decisions"]
        assert decisions[0]["decision_type"] == "choice"
        assert decisions[0]["questions"] == []
