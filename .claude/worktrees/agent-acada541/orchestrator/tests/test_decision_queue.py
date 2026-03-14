"""
Tests for orchestrator/decision_queue.py - DecisionQueue operations.

Covers: queue_decision, resolve_decision, cancel_decision,
get_pending_decisions, get_decision, get_queue_status, handler notification,
error cases (DecisionNotFoundError, DecisionAlreadyResolvedError),
and the get_decision_queue factory.
"""

from unittest.mock import patch

import pytest
from decision_queue import (
    DecisionAlreadyResolvedError,
    DecisionNotFoundError,
    DecisionQueue,
    get_decision_queue,
)
from models import DecisionStatus, Pipeline, PipelinePhase

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_pipeline():
    """Create a fresh Pipeline."""
    return Pipeline(
        id="test-pipeline",
        issue_number=42,
        repo="owner/repo",
        branch="egg/test",
    )


@pytest.fixture
def queue(tmp_path, mock_pipeline):
    """Create a DecisionQueue with mocked state store."""
    q = DecisionQueue(pipeline_id="test-pipeline", repo_path=tmp_path)

    # Replace internal state methods with in-memory store
    _state = {"pipeline": mock_pipeline}

    def _load():
        return _state["pipeline"]

    def _save(pipeline):
        _state["pipeline"] = pipeline

    q._load_pipeline = _load
    q._save_pipeline = _save
    q._state = _state  # Expose for assertions

    return q


# ---------------------------------------------------------------------------
# queue_decision
# ---------------------------------------------------------------------------


class TestQueueDecision:
    """Tests for DecisionQueue.queue_decision()."""

    def test_basic_queue(self, queue):
        """Queue a simple decision with question and options."""
        decision = queue.queue_decision(
            question="Which database?",
            options=["PostgreSQL", "MongoDB"],
        )

        assert decision.id == "decision-1"
        assert decision.question == "Which database?"
        assert decision.options == ["PostgreSQL", "MongoDB"]
        assert decision.status == DecisionStatus.PENDING
        assert decision.decision_type == "choice"

    def test_queue_with_decision_type(self, queue):
        """Queue with explicit decision_type."""
        decision = queue.queue_decision(
            question="Approve the plan?",
            options=["approve", "request changes"],
            decision_type="phase_gate",
        )

        assert decision.decision_type == "phase_gate"

    def test_queue_with_questions(self, queue):
        """Queue a feedback decision with structured questions."""
        questions = [
            {"id": "q-1", "question": "Volume?", "answer": ""},
            {"id": "q-2", "question": "Performance?", "answer": ""},
        ]
        decision = queue.queue_decision(
            question="Provide feedback",
            decision_type="feedback",
            questions=questions,
        )

        assert decision.decision_type == "feedback"
        assert len(decision.questions) == 2
        assert decision.questions[0]["id"] == "q-1"

    def test_queue_with_context(self, queue):
        """Queue a decision with context attached."""
        decision = queue.queue_decision(
            question="Review this",
            context="# Draft\nContent here",
        )

        assert decision.context == "# Draft\nContent here"

    def test_queue_increments_id(self, queue):
        """Each queued decision gets an incrementing ID."""
        d1 = queue.queue_decision(question="First?")
        d2 = queue.queue_decision(question="Second?")
        d3 = queue.queue_decision(question="Third?")

        assert d1.id == "decision-1"
        assert d2.id == "decision-2"
        assert d3.id == "decision-3"

    def test_queue_persists_to_pipeline(self, queue):
        """Queued decision is persisted in pipeline state."""
        queue.queue_decision(question="Stored?")

        pipeline = queue._load_pipeline()
        assert len(pipeline.decisions) == 1
        assert pipeline.decisions[0].question == "Stored?"

    def test_queue_notifies_handlers(self, queue):
        """Queued decision notifies registered handlers."""
        received = []
        queue.add_handler(lambda d: received.append(d))

        decision = queue.queue_decision(question="Notify?")

        assert len(received) == 1
        assert received[0].id == decision.id

    def test_handler_error_does_not_prevent_queue(self, queue):
        """Handler exception does not prevent decision from being queued."""
        queue.add_handler(lambda d: (_ for _ in ()).throw(RuntimeError("boom")))

        decision = queue.queue_decision(question="Despite error?")
        assert decision.status == DecisionStatus.PENDING

    def test_queue_defaults(self, queue):
        """Queue with minimal params uses sensible defaults."""
        decision = queue.queue_decision(question="Minimal?")

        assert decision.options == []
        assert decision.decision_type == "choice"
        assert decision.questions == []
        assert decision.context == ""

    def test_queue_auto_infers_phase_from_pipeline(self, queue):
        """When phase=None, queue_decision infers phase from pipeline.current_phase."""
        # Set the pipeline's current phase
        pipeline = queue._load_pipeline()
        pipeline.current_phase = PipelinePhase.IMPLEMENT
        queue._save_pipeline(pipeline)

        decision = queue.queue_decision(question="Approve this?")

        assert decision.phase == PipelinePhase.IMPLEMENT

    def test_queue_explicit_phase_not_overridden(self, queue):
        """When phase is explicitly provided, pipeline.current_phase is not used."""
        pipeline = queue._load_pipeline()
        pipeline.current_phase = PipelinePhase.IMPLEMENT
        queue._save_pipeline(pipeline)

        decision = queue.queue_decision(question="Approve?", phase=PipelinePhase.PLAN)

        assert decision.phase == PipelinePhase.PLAN


# ---------------------------------------------------------------------------
# get_pending_decisions
# ---------------------------------------------------------------------------


class TestGetPendingDecisions:
    """Tests for DecisionQueue.get_pending_decisions()."""

    def test_empty_queue(self, queue):
        """No pending decisions in empty queue."""
        assert queue.get_pending_decisions() == []

    def test_filters_resolved(self, queue):
        """Only pending decisions returned, not resolved."""
        queue.queue_decision(question="First?")
        queue.queue_decision(question="Second?")
        queue.resolve_decision("decision-1", "Done")

        pending = queue.get_pending_decisions()
        assert len(pending) == 1
        assert pending[0].id == "decision-2"

    def test_filters_cancelled(self, queue):
        """Only pending decisions returned, not cancelled."""
        queue.queue_decision(question="First?")
        queue.queue_decision(question="Second?")
        queue.cancel_decision("decision-1")

        pending = queue.get_pending_decisions()
        assert len(pending) == 1
        assert pending[0].id == "decision-2"


# ---------------------------------------------------------------------------
# get_decision
# ---------------------------------------------------------------------------


class TestGetDecision:
    """Tests for DecisionQueue.get_decision()."""

    def test_found(self, queue):
        """Retrieve a decision by ID."""
        queue.queue_decision(question="Find me?")
        decision = queue.get_decision("decision-1")
        assert decision.question == "Find me?"

    def test_not_found(self, queue):
        """Non-existent decision raises DecisionNotFoundError."""
        with pytest.raises(DecisionNotFoundError, match="decision-999"):
            queue.get_decision("decision-999")


# ---------------------------------------------------------------------------
# resolve_decision
# ---------------------------------------------------------------------------


class TestResolveDecision:
    """Tests for DecisionQueue.resolve_decision()."""

    def test_resolve_pending(self, queue):
        """Resolve a pending decision."""
        queue.queue_decision(question="Resolve me?")
        resolved = queue.resolve_decision("decision-1", "Approved")

        assert resolved.status == DecisionStatus.RESOLVED
        assert resolved.resolution == "Approved"
        assert resolved.resolved_at is not None

    def test_resolve_json_payload(self, queue):
        """Resolve with JSON string payload."""
        import json

        queue.queue_decision(question="Resolve me?")
        payload = json.dumps({"action": "approve", "feedback": "Looks good"})
        resolved = queue.resolve_decision("decision-1", payload)

        parsed = json.loads(resolved.resolution)
        assert parsed["action"] == "approve"

    def test_resolve_not_found(self, queue):
        """Resolve non-existent decision raises DecisionNotFoundError."""
        with pytest.raises(DecisionNotFoundError):
            queue.resolve_decision("decision-999", "Nope")

    def test_resolve_already_resolved(self, queue):
        """Resolve an already-resolved decision raises DecisionAlreadyResolvedError."""
        queue.queue_decision(question="Resolve twice?")
        queue.resolve_decision("decision-1", "First")

        with pytest.raises(DecisionAlreadyResolvedError, match="already"):
            queue.resolve_decision("decision-1", "Second")

    def test_resolve_persists(self, queue):
        """Resolution is persisted in pipeline state."""
        queue.queue_decision(question="Persist?")
        queue.resolve_decision("decision-1", "Done")

        pipeline = queue._load_pipeline()
        assert pipeline.decisions[0].status == DecisionStatus.RESOLVED
        assert pipeline.decisions[0].resolution == "Done"


# ---------------------------------------------------------------------------
# cancel_decision
# ---------------------------------------------------------------------------


class TestCancelDecision:
    """Tests for DecisionQueue.cancel_decision()."""

    def test_cancel_pending(self, queue):
        """Cancel a pending decision."""
        queue.queue_decision(question="Cancel me?")
        cancelled = queue.cancel_decision("decision-1")

        assert cancelled.status == DecisionStatus.CANCELLED
        assert cancelled.resolved_at is not None

    def test_cancel_already_resolved_is_noop(self, queue):
        """Cancelling already-resolved decision returns it as-is (no error)."""
        queue.queue_decision(question="Already done?")
        queue.resolve_decision("decision-1", "Done")

        result = queue.cancel_decision("decision-1")
        assert result.status == DecisionStatus.RESOLVED

    def test_cancel_not_found(self, queue):
        """Cancel non-existent decision raises DecisionNotFoundError."""
        with pytest.raises(DecisionNotFoundError):
            queue.cancel_decision("decision-999")


# ---------------------------------------------------------------------------
# get_queue_status
# ---------------------------------------------------------------------------


class TestGetQueueStatus:
    """Tests for DecisionQueue.get_queue_status()."""

    def test_empty(self, queue):
        """Empty queue status."""
        status = queue.get_queue_status()
        assert status["total_decisions"] == 0
        assert status["pending"] == 0
        assert status["resolved"] == 0
        assert status["pending_decisions"] == []

    def test_mixed_statuses(self, queue):
        """Status with mixed pending/resolved decisions."""
        queue.queue_decision(question="First?")
        queue.queue_decision(question="Second?")
        queue.queue_decision(question="Third?")
        queue.resolve_decision("decision-1", "Done")
        queue.cancel_decision("decision-2")

        status = queue.get_queue_status()
        assert status["total_decisions"] == 3
        assert status["pending"] == 1
        assert status["resolved"] == 1
        assert len(status["pending_decisions"]) == 1
        assert status["pending_decisions"][0]["id"] == "decision-3"


# ---------------------------------------------------------------------------
# Handler management
# ---------------------------------------------------------------------------


class TestHandlerManagement:
    """Tests for handler add/remove."""

    def test_add_and_remove_handler(self, queue):
        """Handler can be added and removed."""
        received = []

        def handler(d):
            received.append(d)

        queue.add_handler(handler)
        queue.queue_decision(question="With handler?")
        assert len(received) == 1

        queue.remove_handler(handler)
        queue.queue_decision(question="Without handler?")
        assert len(received) == 1  # No new notifications

    def test_remove_nonexistent_handler(self, queue):
        """Removing a handler that was never added is a no-op."""
        queue.remove_handler(lambda d: None)
        # Should not raise


# ---------------------------------------------------------------------------
# get_decision_queue factory
# ---------------------------------------------------------------------------


class TestGetDecisionQueueFactory:
    """Tests for the get_decision_queue() factory function."""

    def test_returns_same_instance(self, tmp_path):
        """Same pipeline+path returns the cached queue."""
        with patch("decision_queue._decision_queues", {}):
            q1 = get_decision_queue("pipe-1", tmp_path)
            q2 = get_decision_queue("pipe-1", tmp_path)
            assert q1 is q2

    def test_different_pipelines_return_different(self, tmp_path):
        """Different pipeline IDs return different queues."""
        with patch("decision_queue._decision_queues", {}):
            q1 = get_decision_queue("pipe-1", tmp_path)
            q2 = get_decision_queue("pipe-2", tmp_path)
            assert q1 is not q2

    def test_accepts_string_path(self, tmp_path):
        """Accepts string path in addition to Path objects."""
        with patch("decision_queue._decision_queues", {}):
            q = get_decision_queue("pipe-1", str(tmp_path))
            assert q.repo_path == tmp_path
