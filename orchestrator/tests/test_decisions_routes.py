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

from models import PipelinePhase  # noqa: E402


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


def _mock_store_for_pipeline(tmp_path):
    """Return a (store, pipeline) tuple suitable for mocking get_state_store_for_pipeline."""
    return (MagicMock(repo_path=tmp_path), MagicMock())


class TestCreateDecisionWithType:
    """Tests for POST create-decision with decision_type and questions."""

    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_create_with_decision_type_feedback(
        self, mock_get_queue, mock_get_store_for_pipeline, client, tmp_path
    ):
        """POST with decision_type='feedback' and questions creates correct decision."""
        mock_get_store_for_pipeline.return_value = _mock_store_for_pipeline(tmp_path)
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

    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_create_with_decision_type_phase_gate(
        self, mock_get_queue, mock_get_store_for_pipeline, client, tmp_path
    ):
        """POST with decision_type='phase_gate' creates correct decision."""
        mock_get_store_for_pipeline.return_value = _mock_store_for_pipeline(tmp_path)
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

    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_create_without_new_fields_defaults(
        self, mock_get_queue, mock_get_store_for_pipeline, client, tmp_path
    ):
        """POST without decision_type and questions applies defaults."""
        mock_get_store_for_pipeline.return_value = _mock_store_for_pipeline(tmp_path)
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

    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_list_includes_new_fields(
        self, mock_get_queue, mock_get_store_for_pipeline, client, tmp_path
    ):
        """GET list-decisions includes decision_type and questions in response."""
        mock_get_store_for_pipeline.return_value = _mock_store_for_pipeline(tmp_path)
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

    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_get_single_includes_new_fields(
        self, mock_get_queue, mock_get_store_for_pipeline, client, tmp_path
    ):
        """GET single decision includes decision_type and questions."""
        mock_get_store_for_pipeline.return_value = _mock_store_for_pipeline(tmp_path)
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

    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_list_default_type_for_old_decisions(
        self, mock_get_queue, mock_get_store_for_pipeline, client, tmp_path
    ):
        """Decisions without explicit type serialize as 'choice' with empty questions."""
        mock_get_store_for_pipeline.return_value = _mock_store_for_pipeline(tmp_path)
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

    def test_invalid_decision_type_returns_400(self, client, tmp_path):
        """POST with an unrecognized decision_type returns 400."""
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

    def test_empty_string_decision_type_returns_400(self, client, tmp_path):
        """POST with empty string decision_type returns 400."""
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

    def test_arbitrary_string_decision_type_returns_400(self, client, tmp_path):
        """POST with arbitrary decision_type returns 400 with valid types listed."""
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

    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_valid_decision_types_accepted(
        self, mock_get_queue, mock_get_store_for_pipeline, client, tmp_path
    ):
        """POST with each valid decision_type succeeds."""
        mock_get_store_for_pipeline.return_value = _mock_store_for_pipeline(tmp_path)
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

    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_missing_decision_type_defaults_to_choice(
        self, mock_get_queue, mock_get_store_for_pipeline, client, tmp_path
    ):
        """POST without decision_type defaults to 'choice' and succeeds."""
        mock_get_store_for_pipeline.return_value = _mock_store_for_pipeline(tmp_path)
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


class TestPhaseValidation:
    """Tests for phase validation in the POST create-decision endpoint."""

    def test_invalid_phase_returns_400(self, client, tmp_path):
        """POST with an unrecognized phase returns 400."""
        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions",
            json={
                "question": "Approve?",
                "phase": "implment",  # typo
            },
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data["success"] is False
        assert "Invalid phase" in data["message"]
        assert "implment" in data["message"]

    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_valid_phases_accepted(
        self, mock_get_queue, mock_get_store_for_pipeline, client, tmp_path
    ):
        """POST with each valid phase succeeds."""
        mock_get_store_for_pipeline.return_value = _mock_store_for_pipeline(tmp_path)
        mock_queue = MagicMock()
        mock_get_queue.return_value = mock_queue

        for phase in ("refine", "plan", "implement", "pr"):
            mock_queue.queue_decision.return_value = _make_decision()
            response = client.post(
                "/api/v1/pipelines/test-pipeline/decisions",
                json={
                    "question": "Test?",
                    "phase": phase,
                },
            )
            assert response.status_code == 200, f"Expected 200 for phase={phase}"
            call_kwargs = mock_queue.queue_decision.call_args
            assert call_kwargs[1]["phase"] == PipelinePhase(phase), (
                f"Expected PipelinePhase enum for phase={phase}, got {call_kwargs[1]['phase']!r}"
            )

    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_missing_phase_defaults_to_none(
        self, mock_get_queue, mock_get_store_for_pipeline, client, tmp_path
    ):
        """POST without phase defaults to None."""
        mock_get_store_for_pipeline.return_value = _mock_store_for_pipeline(tmp_path)
        mock_queue = MagicMock()
        mock_queue.queue_decision.return_value = _make_decision()
        mock_get_queue.return_value = mock_queue

        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions",
            json={"question": "Pick one?"},
        )

        assert response.status_code == 200
        call_kwargs = mock_queue.queue_decision.call_args
        assert call_kwargs[1]["phase"] is None


class TestQueueDecisionValidation:
    """Tests for POST create-decision input validation and error handling."""

    def test_missing_question_returns_400(self, client, tmp_path):
        """POST without question field returns 400."""
        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions",
            json={"options": ["A", "B"]},
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data["success"] is False
        assert "question" in data["message"].lower()

    def test_empty_body_returns_400(self, client, tmp_path):
        """POST with empty body returns 400."""
        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions",
            content_type="application/json",
            data="{}",
        )

        assert response.status_code == 400

    @pytest.mark.parametrize(
        "raw_body",
        ["[1, 2, 3]", '"a string body"', "42", "true"],
        ids=["array", "string", "number", "bool"],
    )
    def test_non_object_json_body_returns_400(self, client, tmp_path, raw_body):
        """Fix for #2656: non-object JSON bodies must 400, not 500.

        Previously ``data = request.get_json() or {}`` left a list /
        scalar in ``data`` and the subsequent ``data.get("question")``
        raised ``AttributeError`` → 500. The handler now rejects
        non-dict bodies before any ``.get`` call.
        """
        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions",
            content_type="application/json",
            data=raw_body,
        )
        assert response.status_code == 400, response.data
        body = response.get_json()
        assert body["success"] is False

    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_create_with_choice_type_and_options(
        self, mock_get_queue, mock_get_store_for_pipeline, client, tmp_path
    ):
        """POST with decision_type='choice' and explicit options."""
        mock_get_store_for_pipeline.return_value = _mock_store_for_pipeline(tmp_path)
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

    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_resolve_success(self, mock_get_queue, mock_get_store_for_pipeline, client, tmp_path):
        """POST resolve returns resolved decision."""
        mock_get_store_for_pipeline.return_value = _mock_store_for_pipeline(tmp_path)
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

    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_resolve_dict_resolution_serialized_to_json_string(
        self, mock_get_queue, mock_get_store_for_pipeline, client, tmp_path
    ):
        """POST resolve with dict resolution serializes it to a JSON string (#1635)."""
        import json

        mock_get_store_for_pipeline.return_value = _mock_store_for_pipeline(tmp_path)
        mock_queue = MagicMock()

        resolved_decision = _make_decision(
            status="resolved",
            resolution='{"action": "select", "selected": "approve"}',
        )
        mock_queue.resolve_decision.return_value = resolved_decision
        mock_get_queue.return_value = mock_queue

        # Send resolution as a dict (not a string) — this is the bug trigger
        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions/decision-1/resolve",
            json={"resolution": {"action": "select", "selected": "approve"}},
        )

        assert response.status_code == 200
        # The route handler should have serialized the dict to a JSON string
        call_args = mock_queue.resolve_decision.call_args
        actual_resolution = call_args[0][1]
        assert isinstance(actual_resolution, str)
        assert json.loads(actual_resolution) == {"action": "select", "selected": "approve"}

    def test_resolve_missing_resolution_returns_400(self, client, tmp_path):
        """POST resolve without resolution field returns 400."""
        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions/decision-1/resolve",
            json={},
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data["success"] is False

    @pytest.mark.parametrize(
        "raw_body",
        ["[1, 2, 3]", '"a string body"', "42", "true"],
        ids=["array", "string", "number", "bool"],
    )
    def test_resolve_non_object_json_body_returns_400(self, client, tmp_path, raw_body):
        """Resolve mirrors the queue_decision #2656 fix: non-dict body → 400."""
        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions/decision-1/resolve",
            content_type="application/json",
            data=raw_body,
        )
        assert response.status_code == 400, response.data
        body = response.get_json()
        assert body["success"] is False

    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_resolve_not_found_returns_404(
        self, mock_get_queue, mock_get_store_for_pipeline, client, tmp_path
    ):
        """POST resolve for non-existent decision returns 404."""
        from decision_queue import DecisionNotFoundError

        mock_get_store_for_pipeline.return_value = _mock_store_for_pipeline(tmp_path)
        mock_queue = MagicMock()
        mock_queue.resolve_decision.side_effect = DecisionNotFoundError("Not found")
        mock_get_queue.return_value = mock_queue

        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions/decision-99/resolve",
            json={"resolution": "approve"},
        )

        assert response.status_code == 404

    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_resolve_already_resolved_returns_409(
        self, mock_get_queue, mock_get_store_for_pipeline, client, tmp_path
    ):
        """POST resolve for already-resolved decision returns 409."""
        from decision_queue import DecisionAlreadyResolvedError

        mock_get_store_for_pipeline.return_value = _mock_store_for_pipeline(tmp_path)
        mock_queue = MagicMock()
        mock_queue.resolve_decision.side_effect = DecisionAlreadyResolvedError("Already resolved")
        mock_get_queue.return_value = mock_queue

        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions/decision-1/resolve",
            json={"resolution": "approve"},
        )

        assert response.status_code == 409


class TestResolveEmitsEvent:
    """Tests that resolving a decision emits DECISION_RESOLVED event."""

    @patch("routes.decisions.emit_event")
    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_resolve_emits_decision_resolved_event(
        self, mock_get_queue, mock_get_store_for_pipeline, mock_emit, client, tmp_path
    ):
        """Resolving a decision emits EventType.DECISION_RESOLVED."""
        from events import EventType

        mock_get_store_for_pipeline.return_value = _mock_store_for_pipeline(tmp_path)
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
        mock_emit.assert_called_once_with(
            EventType.DECISION_RESOLVED,
            pipeline_id="test-pipeline",
            data={
                "decision_id": "decision-1",
                "resolution": '{"action": "approve"}',
            },
        )

    @patch("routes.decisions.emit_event", side_effect=Exception("event bus down"))
    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_resolve_succeeds_even_if_event_emission_fails(
        self, mock_get_queue, mock_get_store_for_pipeline, mock_emit, client, tmp_path
    ):
        """Event emission failure does not break the resolve endpoint."""
        mock_get_store_for_pipeline.return_value = _mock_store_for_pipeline(tmp_path)
        mock_queue = MagicMock()
        resolved_decision = _make_decision(
            status="resolved",
            resolution="approve",
        )
        mock_queue.resolve_decision.return_value = resolved_decision
        mock_get_queue.return_value = mock_queue

        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions/decision-1/resolve",
            json={"resolution": "approve"},
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True


class TestContinueWithoutExcusesReviewer:
    """Tests for 'Continue without' resolution calling excuse_reviewer."""

    @patch("routes.decisions.get_peer_consensus_tracker")
    @patch("routes.decisions.emit_event")
    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_continue_without_calls_excuse_reviewer(
        self,
        mock_get_queue,
        mock_get_store_for_pipeline,
        mock_emit,
        mock_get_tracker,
        client,
        tmp_path,
    ):
        """Resolving with 'Continue without' calls excuse_reviewer for the failed role."""
        mock_get_store_for_pipeline.return_value = _mock_store_for_pipeline(tmp_path)
        mock_queue = MagicMock()
        resolved_decision = _make_decision(
            status="resolved",
            resolution="Continue without",
        )
        resolved_decision.context = "failed_role:reviewer_code"
        mock_queue.resolve_decision.return_value = resolved_decision
        mock_get_queue.return_value = mock_queue

        mock_tracker = MagicMock()
        mock_tracker.excuse_reviewer.return_value = {
            "status": "excused",
            "role": "reviewer_code",
            "affected_producers": ["coder"],
        }
        mock_get_tracker.return_value = mock_tracker

        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions/decision-1/resolve",
            json={"resolution": "Continue without"},
        )

        assert response.status_code == 200
        mock_tracker.excuse_reviewer.assert_called_once_with("reviewer_code")

    @patch("routes.decisions.get_peer_consensus_tracker")
    @patch("routes.decisions.emit_event")
    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_non_continue_without_does_not_call_excuse(
        self,
        mock_get_queue,
        mock_get_store_for_pipeline,
        mock_emit,
        mock_get_tracker,
        client,
        tmp_path,
    ):
        """Resolving with other options does not call excuse_reviewer."""
        mock_get_store_for_pipeline.return_value = _mock_store_for_pipeline(tmp_path)
        mock_queue = MagicMock()
        resolved_decision = _make_decision(
            status="resolved",
            resolution="Retry (respawn agent)",
        )
        resolved_decision.context = "failed_role:reviewer_code"
        mock_queue.resolve_decision.return_value = resolved_decision
        mock_get_queue.return_value = mock_queue

        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions/decision-1/resolve",
            json={"resolution": "Retry (respawn agent)"},
        )

        assert response.status_code == 200
        mock_get_tracker.assert_not_called()


class TestCancelDecisionEndpoint:
    """Tests for POST cancel-decision endpoint."""

    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_cancel_success(self, mock_get_queue, mock_get_store_for_pipeline, client, tmp_path):
        """POST cancel returns cancelled decision."""
        mock_get_store_for_pipeline.return_value = _mock_store_for_pipeline(tmp_path)
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

    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_cancel_not_found_returns_404(
        self, mock_get_queue, mock_get_store_for_pipeline, client, tmp_path
    ):
        """POST cancel for non-existent decision returns 404."""
        from decision_queue import DecisionNotFoundError

        mock_get_store_for_pipeline.return_value = _mock_store_for_pipeline(tmp_path)
        mock_queue = MagicMock()
        mock_queue.cancel_decision.side_effect = DecisionNotFoundError("Not found")
        mock_get_queue.return_value = mock_queue

        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions/decision-99/cancel",
        )

        assert response.status_code == 404


class TestGetDecisionEndpointErrors:
    """Tests for GET single decision error handling."""

    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_get_not_found_returns_404(
        self, mock_get_queue, mock_get_store_for_pipeline, client, tmp_path
    ):
        """GET non-existent decision returns 404."""
        from decision_queue import DecisionNotFoundError

        mock_get_store_for_pipeline.return_value = _mock_store_for_pipeline(tmp_path)
        mock_queue = MagicMock()
        mock_queue.get_decision.side_effect = DecisionNotFoundError("Not found")
        mock_get_queue.return_value = mock_queue

        response = client.get(
            "/api/v1/pipelines/test-pipeline/decisions/decision-99",
        )

        assert response.status_code == 404


class TestQueueStatusEndpoint:
    """Tests for GET queue-status endpoint."""

    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_status_success(self, mock_get_queue, mock_get_store_for_pipeline, client, tmp_path):
        """GET queue status returns counts."""
        mock_get_store_for_pipeline.return_value = _mock_store_for_pipeline(tmp_path)
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

    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_pending_only_filter(
        self, mock_get_queue, mock_get_store_for_pipeline, client, tmp_path
    ):
        """GET with pending_only=true returns only pending decisions."""
        mock_get_store_for_pipeline.return_value = _mock_store_for_pipeline(tmp_path)
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


class TestHandleRestartAgent:
    """Tests for _handle_restart_agent helper."""

    @patch("routes.decisions.emit_event")
    @patch("docker_client.get_docker_client")
    def test_happy_path_stops_container_and_emits_event(self, mock_get_docker, mock_emit):
        """Container found, stopped, and CONTAINER_STOPPED event emitted."""
        from events import EventType
        from routes.decisions import _handle_restart_agent

        mock_container = MagicMock()
        mock_container.container_id = "abc123def456"
        mock_client = MagicMock()
        mock_client.list_containers.return_value = [mock_container]
        mock_get_docker.return_value = mock_client

        _handle_restart_agent("pipeline-1", "Agent coder issue: heartbeat stall")

        mock_client.list_containers.assert_called_once_with(
            all=False,
            labels={"egg.pipeline.id": "pipeline-1", "egg.agent.role": "coder"},
        )
        mock_client.stop_container.assert_called_once_with("abc123def456", timeout=10)
        mock_emit.assert_called_once_with(
            EventType.CONTAINER_STOPPED,
            pipeline_id="pipeline-1",
            data={
                "container_id": "abc123def456",
                "agent_role": "coder",
                "reason": "hitl_restart",
            },
        )

    @patch("docker_client.get_docker_client")
    def test_no_containers_found(self, mock_get_docker):
        """No running containers for agent — logs warning, no stop called."""
        from routes.decisions import _handle_restart_agent

        mock_client = MagicMock()
        mock_client.list_containers.return_value = []
        mock_get_docker.return_value = mock_client

        _handle_restart_agent("pipeline-1", "Agent reviewer issue: progress stall")

        mock_client.stop_container.assert_not_called()

    @patch("routes.decisions.emit_event")
    @patch("docker_client.get_docker_client")
    def test_docker_stop_failure(self, mock_get_docker, mock_emit):
        """Docker stop raises exception — handled gracefully, no crash."""
        from routes.decisions import _handle_restart_agent

        mock_container = MagicMock()
        mock_container.container_id = "abc123def456"
        mock_client = MagicMock()
        mock_client.list_containers.return_value = [mock_container]
        mock_client.stop_container.side_effect = RuntimeError("Docker daemon error")
        mock_get_docker.return_value = mock_client

        # Should not raise
        _handle_restart_agent("pipeline-1", "Agent coder issue: heartbeat stall")

        mock_emit.assert_not_called()

    def test_regex_parse_failure(self):
        """Question text doesn't match expected pattern — early return."""
        from routes.decisions import _handle_restart_agent

        # Should not raise or attempt any docker operations
        _handle_restart_agent("pipeline-1", "Some unrelated question text")


class TestLifecycleSecretAuth:
    """Regression coverage for the #1769 HITL auto-approval auth gap.

    The decisions/resolve and decisions/cancel routes must reject any
    caller that doesn't present the configured
    ``Authorization: Bearer <EGG_LIFECYCLE_SECRET>`` header. Agents must
    never hold this secret, so in-cluster agent pods can't bypass HITL
    phase gates.
    """

    def test_resolve_missing_header_returns_401(self, client):
        """POST /resolve with no Authorization header returns 401."""
        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions/decision-1/resolve",
            json={"resolution": "approve"},
            _lifecycle_auth=False,
        )
        assert response.status_code == 401
        assert response.get_json()["success"] is False

    def test_resolve_wrong_secret_returns_401(self, client):
        """POST /resolve with the wrong bearer token returns 401."""
        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions/decision-1/resolve",
            json={"resolution": "approve"},
            headers={"Authorization": "Bearer not-the-real-secret"},
            _lifecycle_auth=False,
        )
        assert response.status_code == 401

    def test_resolve_non_bearer_scheme_returns_401(self, client):
        """Basic auth (or any non-Bearer scheme) is rejected."""
        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions/decision-1/resolve",
            json={"resolution": "approve"},
            headers={"Authorization": "Basic dXNlcjpwYXNz"},
            _lifecycle_auth=False,
        )
        assert response.status_code == 401

    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_resolve_correct_secret_passes_through(
        self, mock_get_queue, mock_get_store_for_pipeline, client, tmp_path, lifecycle_auth_headers
    ):
        """POST /resolve with the correct bearer token reaches the handler."""
        mock_get_store_for_pipeline.return_value = _mock_store_for_pipeline(tmp_path)
        mock_queue = MagicMock()
        mock_queue.resolve_decision.return_value = _make_decision(
            status="resolved", resolution="approve"
        )
        mock_get_queue.return_value = mock_queue

        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions/decision-1/resolve",
            json={"resolution": "approve"},
            headers=lifecycle_auth_headers,
            _lifecycle_auth=False,
        )
        assert response.status_code == 200

    def test_cancel_missing_header_returns_401(self, client):
        """POST /cancel with no Authorization header returns 401."""
        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions/decision-1/cancel",
            _lifecycle_auth=False,
        )
        assert response.status_code == 401

    def test_cancel_wrong_secret_returns_401(self, client):
        """POST /cancel with the wrong bearer token returns 401."""
        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions/decision-1/cancel",
            headers={"Authorization": "Bearer not-the-real-secret"},
            _lifecycle_auth=False,
        )
        assert response.status_code == 401

    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_cancel_correct_secret_passes_through(
        self, mock_get_queue, mock_get_store_for_pipeline, client, tmp_path, lifecycle_auth_headers
    ):
        """POST /cancel with the correct bearer token reaches the handler."""
        from models import DecisionStatus

        mock_get_store_for_pipeline.return_value = _mock_store_for_pipeline(tmp_path)
        mock_queue = MagicMock()
        cancelled = _make_decision(status="cancelled")
        cancelled.status = DecisionStatus.CANCELLED
        mock_queue.cancel_decision.return_value = cancelled
        mock_get_queue.return_value = mock_queue

        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions/decision-1/cancel",
            headers=lifecycle_auth_headers,
            _lifecycle_auth=False,
        )
        assert response.status_code == 200

    def test_missing_env_secret_returns_503(self, client, monkeypatch):
        """Server with no EGG_LIFECYCLE_SECRET fails closed with 503."""
        monkeypatch.delenv("EGG_LIFECYCLE_SECRET", raising=False)
        response = client.post(
            "/api/v1/pipelines/test-pipeline/decisions/decision-1/resolve",
            json={"resolution": "approve"},
            _lifecycle_auth=False,
        )
        assert response.status_code == 503

    @patch("routes.decisions.get_state_store_for_pipeline")
    @patch("routes.decisions.get_decision_queue")
    def test_resolve_attaches_source_to_request(
        self,
        mock_get_queue,
        mock_get_store_for_pipeline,
        client,
        tmp_path,
        lifecycle_auth_headers,
    ):
        """The decorator records the X-Egg-Source value on ``request``.

        The decision route reads ``request.egg_source`` and emits it on
        its audit log line. We verify the attach directly because egg's
        structlog output bypasses pytest's caplog fixture.
        """
        from flask import Flask, request
        from lifecycle_auth import require_lifecycle_secret

        seen: dict[str, str] = {}
        app = Flask(__name__)

        @app.route("/probe", methods=["POST"])
        @require_lifecycle_secret
        def _probe():  # type: ignore[no-untyped-def]
            seen["source"] = getattr(request, "egg_source", "missing")
            return {"ok": True}, 200

        probe_client = app.test_client()
        headers = {**lifecycle_auth_headers, "X-Egg-Source": "mcp"}
        response = probe_client.post("/probe", headers=headers, _lifecycle_auth=False)
        assert response.status_code == 200
        assert seen["source"] == "mcp"


class TestLifecycleSecretAuthOtherEndpoints:
    """Smoke tests confirming non-decision lifecycle routes also require auth.

    One per category beyond resolve/cancel so the auth wiring doesn't
    silently regress for pipeline / phase / container endpoints.
    """

    def test_delete_pipeline_requires_auth(self):
        """DELETE /pipelines/<id> returns 401 without the bearer token."""
        from flask import Flask
        from routes.pipelines import pipelines_bp

        app = Flask(__name__)
        app.register_blueprint(pipelines_bp)
        client = app.test_client()
        response = client.delete("/api/v1/pipelines/test-pipeline", _lifecycle_auth=False)
        assert response.status_code == 401

    def test_advance_phase_requires_auth(self):
        """POST /phase returns 401 without the bearer token."""
        from flask import Flask
        from routes.phases import phases_bp

        app = Flask(__name__)
        app.register_blueprint(phases_bp)
        client = app.test_client()
        response = client.post(
            "/api/v1/pipelines/test-pipeline/phase",
            json={"target_phase": "plan"},
            _lifecycle_auth=False,
        )
        assert response.status_code == 401

    def test_container_stop_requires_auth(self):
        """POST /containers/<id>/stop returns 401 without the bearer token."""
        from flask import Flask
        from routes.containers import containers_bp

        app = Flask(__name__)
        app.register_blueprint(containers_bp)
        client = app.test_client()
        response = client.post(
            "/api/v1/pipelines/test-pipeline/containers/abc123/stop",
            _lifecycle_auth=False,
        )
        assert response.status_code == 401
