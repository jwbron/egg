"""Tests for message endpoints and Delphi visibility filtering.

Covers the BRC-specific message filtering (Delphi ordering) and
long-polling behavior in the messages route.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

# Add orchestrator to path
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

from message_store import Message, MessageStore, MessageType, reset_message_store
from routes.messages import messages_bp
from state_store import InvalidPipelineIdError


@pytest.fixture
def app():
    """Create a test Flask app with the messages blueprint."""
    app = Flask(__name__)
    app.register_blueprint(messages_bp)
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    """Create a test client."""
    return app.test_client()


@pytest.fixture(autouse=True)
def _reset_store():
    """Reset message store singleton between tests."""
    reset_message_store()
    yield
    reset_message_store()


def _make_pipeline_mock():
    """Create a mock pipeline for state store."""
    pipeline = MagicMock()
    pipeline.current_phase.value = "implement"
    return pipeline


class TestDelphiFiltering:
    """Test Delphi visibility: CONSENSUS_PROPOSE withheld from reviewers
    who haven't submitted their independent ACK/NACK."""

    def test_propose_redacted_for_unreviewed_reviewer(self, client, app):
        """Reviewer should see a redacted PROPOSE before submitting evaluation.

        Instead of dropping the message entirely (old behavior that caused
        deadlocks), the Delphi filter now sends a redacted copy with body
        cleared, payload summary stripped, and delphi_redacted=True.
        """
        from peer_consensus import PeerConsensusTracker
        from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

        graph = ReviewGraph(
            [
                ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ]
        )
        tracker = PeerConsensusTracker("test-pipeline", graph, cooldown_seconds=0)
        tracker.register_agent("coder")
        tracker.register_agent("reviewer_code")

        # Coder proposes
        tracker.handle_propose(
            "coder",
            {
                "summary": "Implemented auth",
                "artifacts": ["src/auth.py"],
                "commit_sha": "abc123",
            },
        )

        # Add PROPOSE message to store with body and rich metadata
        store = MessageStore()
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal from coder",
                body="Detailed self-assessment of my implementation",
                metadata={
                    "payload": {
                        "summary": "Implemented auth",
                        "artifacts": ["src/auth.py"],
                        "version": 1,
                        "commit_sha": "abc123",
                    }
                },
            )
        )

        with app.test_request_context():
            with (
                patch("routes.messages.get_message_store", return_value=store),
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
                patch("peer_consensus.get_peer_consensus_tracker", return_value=tracker),
            ):
                mock_get_store_for_pipeline.return_value = (MagicMock(), _make_pipeline_mock())

                resp = client.get("/api/v1/pipelines/test-pipeline/messages?role=reviewer_code")
                data = json.loads(resp.data)

                # PROPOSE should be present (not dropped) but redacted
                assert data["data"]["count"] == 1
                msg = data["data"]["messages"][0]
                assert msg["body"] == ""
                assert msg["metadata"]["delphi_redacted"] is True
                # Payload should only contain version and commit_sha
                assert msg["metadata"]["payload"]["version"] == 1
                assert msg["metadata"]["payload"]["commit_sha"] == "abc123"
                assert "summary" not in msg["metadata"]["payload"]
                assert "artifacts" not in msg["metadata"]["payload"]

    def test_propose_visible_after_reviewer_evaluates(self, client, app):
        """Reviewer should see PROPOSE after they've submitted ACK/NACK."""
        from peer_consensus import PeerConsensusTracker
        from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

        graph = ReviewGraph(
            [
                ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ]
        )
        tracker = PeerConsensusTracker("test-pipeline", graph, cooldown_seconds=0)
        tracker.register_agent("coder")
        tracker.register_agent("reviewer_code")

        tracker.handle_propose(
            "coder",
            {
                "summary": "Implemented auth",
                "artifacts": ["src/auth.py"],
                "commit_sha": "abc123",
            },
        )

        # Reviewer ACKs (evaluates)
        tracker.handle_ack(
            "reviewer_code",
            "coder",
            {
                "artifact_references": ["src/auth.py"],
            },
        )

        store = MessageStore()
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal from coder",
                body="Detailed self-assessment of my implementation",
                metadata={
                    "payload": {
                        "summary": "Implemented auth",
                        "artifacts": ["src/auth.py"],
                        "version": 1,
                        "commit_sha": "abc123",
                    }
                },
            )
        )

        with app.test_request_context():
            with (
                patch("routes.messages.get_message_store", return_value=store),
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
                patch("peer_consensus.get_peer_consensus_tracker", return_value=tracker),
            ):
                mock_get_store_for_pipeline.return_value = (MagicMock(), _make_pipeline_mock())

                resp = client.get("/api/v1/pipelines/test-pipeline/messages?role=reviewer_code")
                data = json.loads(resp.data)

                # PROPOSE should now be visible with full unredacted content
                assert data["data"]["count"] == 1
                msg = data["data"]["messages"][0]
                assert msg["body"] == "Detailed self-assessment of my implementation"
                assert msg["metadata"]["payload"]["summary"] == "Implemented auth"
                assert msg["metadata"]["payload"]["artifacts"] == ["src/auth.py"]
                assert "delphi_redacted" not in msg["metadata"]

    def test_non_reviewer_sees_propose(self, client, app):
        """Non-reviewer agents should see PROPOSE immediately."""
        from peer_consensus import PeerConsensusTracker
        from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

        graph = ReviewGraph(
            [
                ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ]
        )
        tracker = PeerConsensusTracker("test-pipeline", graph, cooldown_seconds=0)
        tracker.register_agent("coder")
        tracker.register_agent("reviewer_code")

        tracker.handle_propose(
            "coder",
            {
                "summary": "Implemented auth",
                "artifacts": ["src/auth.py"],
                "commit_sha": "abc123",
            },
        )

        store = MessageStore()
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal from coder",
            )
        )

        with app.test_request_context():
            with (
                patch("routes.messages.get_message_store", return_value=store),
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
                patch("peer_consensus.get_peer_consensus_tracker", return_value=tracker),
            ):
                mock_get_store_for_pipeline.return_value = (MagicMock(), _make_pipeline_mock())

                # documenter is not a reviewer in this graph
                resp = client.get("/api/v1/pipelines/test-pipeline/messages?role=documenter")
                data = json.loads(resp.data)

                # Non-reviewer should see the broadcast
                assert data["data"]["count"] == 1

    def test_propose_redacted_preserves_header(self, client, app):
        """Redacted PROPOSE should preserve from_role, subject, version, commit_sha."""
        from peer_consensus import PeerConsensusTracker
        from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

        graph = ReviewGraph(
            [
                ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ]
        )
        tracker = PeerConsensusTracker("test-pipeline", graph, cooldown_seconds=0)
        tracker.register_agent("coder")
        tracker.register_agent("reviewer_code")

        tracker.handle_propose(
            "coder",
            {
                "summary": "Implemented auth",
                "artifacts": ["src/auth.py"],
                "commit_sha": "abc123",
            },
        )

        store = MessageStore()
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal from coder",
                body="Sensitive self-assessment details",
                metadata={
                    "payload": {
                        "summary": "Implemented auth module",
                        "artifacts": ["src/auth.py"],
                        "version": 2,
                        "commit_sha": "def456",
                    }
                },
            )
        )

        with app.test_request_context():
            with (
                patch("routes.messages.get_message_store", return_value=store),
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
                patch("peer_consensus.get_peer_consensus_tracker", return_value=tracker),
            ):
                mock_get_store_for_pipeline.return_value = (MagicMock(), _make_pipeline_mock())

                resp = client.get("/api/v1/pipelines/test-pipeline/messages?role=reviewer_code")
                data = json.loads(resp.data)

                assert data["data"]["count"] == 1
                msg = data["data"]["messages"][0]

                # Header fields preserved
                assert msg["from_role"] == "coder"
                assert msg["subject"] == "Proposal from coder"
                assert msg["message_type"] == "CONSENSUS_PROPOSE"
                assert msg["to_role"] == "all"
                assert msg["pipeline_id"] == "test-pipeline"

                # Payload: version and commit_sha preserved
                assert msg["metadata"]["payload"]["version"] == 2
                assert msg["metadata"]["payload"]["commit_sha"] == "def456"

                # Body and sensitive payload fields stripped
                assert msg["body"] == ""
                assert "summary" not in msg["metadata"]["payload"]
                assert "artifacts" not in msg["metadata"]["payload"]
                assert msg["metadata"]["delphi_redacted"] is True

    def test_propose_redacted_without_payload_in_metadata(self, client, app):
        """Redacted PROPOSE should work when metadata has no payload key."""
        from peer_consensus import PeerConsensusTracker
        from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

        graph = ReviewGraph(
            [
                ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ]
        )
        tracker = PeerConsensusTracker("test-pipeline", graph, cooldown_seconds=0)
        tracker.register_agent("coder")
        tracker.register_agent("reviewer_code")

        tracker.handle_propose(
            "coder",
            {
                "summary": "Implemented auth",
                "artifacts": ["src/auth.py"],
                "commit_sha": "abc123",
            },
        )

        # Message with no payload in metadata
        store = MessageStore()
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal from coder",
                body="Some body text",
                metadata={"custom_key": "custom_value"},
            )
        )

        with app.test_request_context():
            with (
                patch("routes.messages.get_message_store", return_value=store),
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
                patch("peer_consensus.get_peer_consensus_tracker", return_value=tracker),
            ):
                mock_get_store_for_pipeline.return_value = (MagicMock(), _make_pipeline_mock())

                resp = client.get("/api/v1/pipelines/test-pipeline/messages?role=reviewer_code")
                data = json.loads(resp.data)

                # Should still get the redacted message
                assert data["data"]["count"] == 1
                msg = data["data"]["messages"][0]
                assert msg["body"] == ""
                assert msg["metadata"]["delphi_redacted"] is True
                # Original custom_key should be preserved
                assert msg["metadata"]["custom_key"] == "custom_value"
                # No payload key since original didn't have one
                assert "payload" not in msg["metadata"]

    def test_redacted_propose_does_not_mutate_original(self, client, app):
        """Redaction should not modify the original message in the store."""
        from peer_consensus import PeerConsensusTracker
        from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

        graph = ReviewGraph(
            [
                ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ]
        )
        tracker = PeerConsensusTracker("test-pipeline", graph, cooldown_seconds=0)
        tracker.register_agent("coder")
        tracker.register_agent("reviewer_code")

        tracker.handle_propose(
            "coder",
            {
                "summary": "Implemented auth",
                "artifacts": ["src/auth.py"],
                "commit_sha": "abc123",
            },
        )

        original_body = "Original self-assessment body"
        original_metadata = {
            "payload": {
                "summary": "Implemented auth",
                "artifacts": ["src/auth.py"],
                "version": 1,
                "commit_sha": "abc123",
            }
        }

        store = MessageStore()
        original_msg = Message(
            pipeline_id="test-pipeline",
            from_role="coder",
            to_role="all",
            message_type=MessageType.CONSENSUS_PROPOSE,
            subject="Proposal from coder",
            body=original_body,
            metadata=original_metadata,
        )
        store.add_message(original_msg)

        with app.test_request_context():
            with (
                patch("routes.messages.get_message_store", return_value=store),
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
                patch("peer_consensus.get_peer_consensus_tracker", return_value=tracker),
            ):
                mock_get_store_for_pipeline.return_value = (MagicMock(), _make_pipeline_mock())

                # First call: reviewer gets redacted
                resp = client.get("/api/v1/pipelines/test-pipeline/messages?role=reviewer_code")
                data = json.loads(resp.data)
                assert data["data"]["count"] == 1
                assert data["data"]["messages"][0]["body"] == ""

        # Verify original message in store is NOT mutated
        assert original_msg.body == original_body
        assert "summary" in original_msg.metadata["payload"]
        assert "delphi_redacted" not in original_msg.metadata

    def test_unassigned_reviewer_sees_full_propose(self, client, app):
        """Reviewer not assigned to this producer should see full PROPOSE."""
        from peer_consensus import PeerConsensusTracker
        from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

        graph = ReviewGraph(
            [
                # reviewer_code reviews coder, but NOT documenter
                ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ]
        )
        tracker = PeerConsensusTracker("test-pipeline", graph, cooldown_seconds=0)
        tracker.register_agent("coder")
        tracker.register_agent("reviewer_code")

        store = MessageStore()
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="documenter",
                to_role="all",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal from documenter",
                body="Full documentation details",
                metadata={"payload": {"summary": "Updated docs", "version": 1}},
            )
        )

        with app.test_request_context():
            with (
                patch("routes.messages.get_message_store", return_value=store),
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
                patch("peer_consensus.get_peer_consensus_tracker", return_value=tracker),
            ):
                mock_get_store_for_pipeline.return_value = (MagicMock(), _make_pipeline_mock())

                resp = client.get("/api/v1/pipelines/test-pipeline/messages?role=reviewer_code")
                data = json.loads(resp.data)

                # reviewer_code is not assigned to documenter, so full message visible
                assert data["data"]["count"] == 1
                msg = data["data"]["messages"][0]
                assert msg["body"] == "Full documentation details"
                assert "delphi_redacted" not in msg["metadata"]

    def test_multiple_proposals_mixed_redaction(self, client, app):
        """Reviewer should see redacted PROPOSE from assigned producer and full
        PROPOSE from unassigned producer in the same poll."""
        from peer_consensus import PeerConsensusTracker
        from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

        graph = ReviewGraph(
            [
                ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
                # reviewer_code does NOT review tester
            ]
        )
        tracker = PeerConsensusTracker("test-pipeline", graph, cooldown_seconds=0)
        tracker.register_agent("coder")
        tracker.register_agent("reviewer_code")

        tracker.handle_propose(
            "coder",
            {
                "summary": "Code implementation",
                "artifacts": ["src/auth.py"],
                "commit_sha": "code123",
            },
        )

        store = MessageStore()
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal from coder",
                body="Coder self-assessment",
                metadata={
                    "payload": {"summary": "Code impl", "version": 1, "commit_sha": "code123"}
                },
            )
        )
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="tester",
                to_role="all",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal from tester",
                body="Tester self-assessment",
                metadata={
                    "payload": {"summary": "Test results", "version": 1, "commit_sha": "test123"}
                },
            )
        )

        with app.test_request_context():
            with (
                patch("routes.messages.get_message_store", return_value=store),
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
                patch("peer_consensus.get_peer_consensus_tracker", return_value=tracker),
            ):
                mock_get_store_for_pipeline.return_value = (MagicMock(), _make_pipeline_mock())

                resp = client.get("/api/v1/pipelines/test-pipeline/messages?role=reviewer_code")
                data = json.loads(resp.data)

                # Both proposals visible
                assert data["data"]["count"] == 2

                msgs = {m["from_role"]: m for m in data["data"]["messages"]}

                # Coder proposal: redacted (reviewer_code reviews coder)
                coder_msg = msgs["coder"]
                assert coder_msg["body"] == ""
                assert coder_msg["metadata"]["delphi_redacted"] is True

                # Tester proposal: full (reviewer_code does NOT review tester)
                tester_msg = msgs["tester"]
                assert tester_msg["body"] == "Tester self-assessment"
                assert "delphi_redacted" not in tester_msg["metadata"]

    def test_no_filtering_without_role(self, client, app):
        """Messages without role filter should not be Delphi-filtered."""
        store = MessageStore()
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal from coder",
            )
        )

        with app.test_request_context():
            with (
                patch("routes.messages.get_message_store", return_value=store),
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
            ):
                mock_get_store_for_pipeline.return_value = (MagicMock(), _make_pipeline_mock())

                resp = client.get("/api/v1/pipelines/test-pipeline/messages")
                data = json.loads(resp.data)

                assert data["data"]["count"] == 1


class TestLongPolling:
    """Test long-polling wait parameter on message poll endpoint."""

    def test_wait_parameter_parsed(self, client, app):
        """Wait parameter should be accepted and capped at 60."""
        store = MessageStore()

        with app.test_request_context():
            with (
                patch("routes.messages.get_message_store", return_value=store),
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
            ):
                mock_get_store_for_pipeline.return_value = (MagicMock(), _make_pipeline_mock())

                # wait=0 should work (no blocking)
                resp = client.get("/api/v1/pipelines/test-pipeline/messages?wait=0")
                assert resp.status_code == 200

    def test_wait_negative_clamped_to_zero(self, client, app):
        """Negative wait should be clamped to 0."""
        store = MessageStore()

        with app.test_request_context():
            with (
                patch("routes.messages.get_message_store", return_value=store),
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
            ):
                mock_get_store_for_pipeline.return_value = (MagicMock(), _make_pipeline_mock())

                resp = client.get("/api/v1/pipelines/test-pipeline/messages?wait=-5")
                assert resp.status_code == 200

    def test_wait_capped_at_sixty(self, client, app):
        """Wait should be capped at 60 seconds."""
        store = MessageStore()

        with app.test_request_context():
            with (
                patch("routes.messages.get_message_store", return_value=store),
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
            ):
                mock_get_store_for_pipeline.return_value = (MagicMock(), _make_pipeline_mock())

                resp = client.get("/api/v1/pipelines/test-pipeline/messages?wait=999")
                assert resp.status_code == 200

    def test_wait_invalid_defaults_to_zero(self, client, app):
        """Invalid wait parameter should default to 0."""
        store = MessageStore()

        with app.test_request_context():
            with (
                patch("routes.messages.get_message_store", return_value=store),
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
            ):
                mock_get_store_for_pipeline.return_value = (MagicMock(), _make_pipeline_mock())

                resp = client.get("/api/v1/pipelines/test-pipeline/messages?wait=abc")
                assert resp.status_code == 200

    def test_in_memory_fallback_without_wait(self, client, app):
        """In-memory store (no wait support) should fall back gracefully."""
        store = MessageStore()
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.PROGRESS,
                subject="test",
            )
        )

        with app.test_request_context():
            with (
                patch("routes.messages.get_message_store", return_value=store),
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
            ):
                mock_get_store_for_pipeline.return_value = (MagicMock(), _make_pipeline_mock())

                # In-memory store doesn't support wait, should fall back
                resp = client.get("/api/v1/pipelines/test-pipeline/messages?wait=5")
                data = json.loads(resp.data)

                assert resp.status_code == 200
                assert data["data"]["count"] == 1


class TestSinceIdRecovery:
    """A stale ``since_id`` (e.g., surviving a phase-boundary ``clear()`` or
    fed from ``brc_state.last_message_id`` after anchor recovery) previously
    caused polls to silently return empty. Verify the cursor degrades to
    full-history replay and that targeted directed delivery still works end
    to end through the HTTP layer."""

    def test_stale_since_id_returns_all_messages(self, client, app):
        store = MessageStore()
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="overseer",
                to_role="architect",
                message_type=MessageType.STATUS,
                subject="Signal completion required",
            )
        )

        with app.test_request_context():
            with (
                patch("routes.messages.get_message_store", return_value=store),
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
            ):
                mock_get_store_for_pipeline.return_value = (MagicMock(), _make_pipeline_mock())

                resp = client.get(
                    "/api/v1/pipelines/test-pipeline/messages"
                    "?role=architect&since_id=nonexistent-cursor-xyz"
                )
                data = json.loads(resp.data)

                assert resp.status_code == 200
                assert data["data"]["count"] == 1
                assert data["data"]["messages"][0]["from_role"] == "overseer"
                assert data["data"]["messages"][0]["to_role"] == "architect"

    def test_known_since_id_still_returns_only_newer(self, client, app):
        store = MessageStore()
        first = Message(
            pipeline_id="test-pipeline",
            from_role="coder",
            to_role="all",
            message_type=MessageType.PROGRESS,
            subject="first",
        )
        second = Message(
            pipeline_id="test-pipeline",
            from_role="coder",
            to_role="all",
            message_type=MessageType.PROGRESS,
            subject="second",
        )
        store.add_message(first)
        store.add_message(second)

        with app.test_request_context():
            with (
                patch("routes.messages.get_message_store", return_value=store),
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
            ):
                mock_get_store_for_pipeline.return_value = (MagicMock(), _make_pipeline_mock())

                resp = client.get(f"/api/v1/pipelines/test-pipeline/messages?since_id={first.id}")
                data = json.loads(resp.data)

                assert resp.status_code == 200
                assert data["data"]["count"] == 1
                assert data["data"]["messages"][0]["subject"] == "second"


class TestMessageStatus:
    """Test message status endpoint."""

    def test_status_returns_counts(self, client, app):
        store = MessageStore()
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.PROGRESS,
                subject="test",
            )
        )

        with app.test_request_context():
            with (
                patch("routes.messages.get_message_store", return_value=store),
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
            ):
                mock_get_store_for_pipeline.return_value = (MagicMock(), _make_pipeline_mock())

                resp = client.get("/api/v1/pipelines/test-pipeline/messages/status")
                data = json.loads(resp.data)

                assert resp.status_code == 200
                assert data["data"]["total"] == 1


class TestShellEscapeGuard:
    """Reject message sends where ``to_role`` / ``from_role`` looks like an
    unexpanded shell variable -- a footgun we hit in practice when a caller
    runs ``--to $role`` outside a context that expands the variable.
    Without this guard the message lands in the bus with a literal
    ``"$role"`` target and silently fails to deliver to any real agent."""

    def test_rejects_unexpanded_to_role(self, client, app):
        with app.test_request_context():
            resp = client.post(
                "/api/v1/pipelines/test-pipeline/messages",
                json={
                    "from_role": "overseer",
                    "to_role": "$role",
                    "message_type": "STATUS",
                },
            )
            data = json.loads(resp.data)
            assert resp.status_code == 400
            assert "unexpanded shell variable" in data["message"]

    def test_rejects_unexpanded_from_role(self, client, app):
        with app.test_request_context():
            resp = client.post(
                "/api/v1/pipelines/test-pipeline/messages",
                json={
                    "from_role": "$EGG_AGENT_ROLE",
                    "to_role": "architect",
                    "message_type": "STATUS",
                },
            )
            data = json.loads(resp.data)
            assert resp.status_code == 400
            assert "unexpanded shell variable" in data["message"]


class TestInvalidPipelineId:
    """Test that InvalidPipelineIdError returns 400 (not 404) in all message routes."""

    def test_send_message_invalid_pipeline_id_returns_400(self, client, app):
        with app.test_request_context():
            with patch(
                "routes.messages.get_state_store_for_pipeline",
                side_effect=InvalidPipelineIdError("bad-id"),
            ):
                resp = client.post(
                    "/api/v1/pipelines/bad-id/messages",
                    json={"from_role": "coder", "message_type": "PROGRESS"},
                )
                assert resp.status_code == 400

    def test_poll_messages_invalid_pipeline_id_returns_400(self, client, app):
        with app.test_request_context():
            with patch(
                "routes.messages.get_state_store_for_pipeline",
                side_effect=InvalidPipelineIdError("bad-id"),
            ):
                resp = client.get("/api/v1/pipelines/bad-id/messages")
                assert resp.status_code == 400

    def test_message_status_invalid_pipeline_id_returns_400(self, client, app):
        with app.test_request_context():
            with patch(
                "routes.messages.get_state_store_for_pipeline",
                side_effect=InvalidPipelineIdError("bad-id"),
            ):
                resp = client.get("/api/v1/pipelines/bad-id/messages/status")
                assert resp.status_code == 400


class TestHeartbeatValidation:
    """HEARTBEAT metadata validation (issue #1897).

    The server rejects malformed HEARTBEAT messages before they hit the
    message store.  Validation rules:

      * ``metadata.state`` must be one of the four enumerated values.
      * ``WAITING_ON_ROLE`` requires ``metadata.waiting_on``.
      * Bodies are free-form (only metadata is validated).
    """

    def test_valid_heartbeat_accepted(self, client, app):
        with app.test_request_context():
            with (
                patch("routes.messages.get_message_store", return_value=MessageStore()),
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
            ):
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                resp = client.post(
                    "/api/v1/pipelines/test-pipeline/messages",
                    json={
                        "from_role": "coder",
                        "message_type": "HEARTBEAT",
                        "subject": "heartbeat: WORKING",
                        "metadata": {"state": "WORKING"},
                    },
                )
                assert resp.status_code == 200

    def test_heartbeat_missing_state_rejected(self, client, app):
        with app.test_request_context():
            with patch(
                "routes.messages.get_state_store_for_pipeline"
            ) as mock_get_store_for_pipeline:
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                resp = client.post(
                    "/api/v1/pipelines/test-pipeline/messages",
                    json={
                        "from_role": "coder",
                        "message_type": "HEARTBEAT",
                        "metadata": {},
                    },
                )
                assert resp.status_code == 400
                data = json.loads(resp.data)
                assert "state" in data["message"].lower()

    def test_heartbeat_invalid_state_rejected(self, client, app):
        with app.test_request_context():
            with patch(
                "routes.messages.get_state_store_for_pipeline"
            ) as mock_get_store_for_pipeline:
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                resp = client.post(
                    "/api/v1/pipelines/test-pipeline/messages",
                    json={
                        "from_role": "coder",
                        "message_type": "HEARTBEAT",
                        "metadata": {"state": "BOGUS_STATE"},
                    },
                )
                assert resp.status_code == 400
                data = json.loads(resp.data)
                assert "BOGUS_STATE" in data["message"]

    def test_heartbeat_waiting_on_role_requires_waiting_on(self, client, app):
        with app.test_request_context():
            with patch(
                "routes.messages.get_state_store_for_pipeline"
            ) as mock_get_store_for_pipeline:
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                resp = client.post(
                    "/api/v1/pipelines/test-pipeline/messages",
                    json={
                        "from_role": "coder",
                        "message_type": "HEARTBEAT",
                        "metadata": {"state": "WAITING_ON_ROLE"},
                    },
                )
                assert resp.status_code == 400
                data = json.loads(resp.data)
                assert "waiting_on" in data["message"].lower()

    def test_heartbeat_waiting_on_role_with_target_accepted(self, client, app):
        with app.test_request_context():
            with (
                patch("routes.messages.get_message_store", return_value=MessageStore()),
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
            ):
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                resp = client.post(
                    "/api/v1/pipelines/test-pipeline/messages",
                    json={
                        "from_role": "coder",
                        "message_type": "HEARTBEAT",
                        "metadata": {
                            "state": "WAITING_ON_ROLE",
                            "waiting_on": "reviewer_code",
                        },
                    },
                )
                assert resp.status_code == 200

    def test_heartbeat_non_dict_metadata_rejected(self, client, app):
        with app.test_request_context():
            with patch(
                "routes.messages.get_state_store_for_pipeline"
            ) as mock_get_store_for_pipeline:
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                resp = client.post(
                    "/api/v1/pipelines/test-pipeline/messages",
                    json={
                        "from_role": "coder",
                        "message_type": "HEARTBEAT",
                        "metadata": "not-a-dict",
                    },
                )
                # pydantic will already reject non-dict metadata before reaching
                # our validator, so we allow either 400 (pydantic) or 400 (ours).
                assert resp.status_code == 400


class TestWaitEndpoint:
    """GET /api/v1/pipelines/<id>/messages/wait (issue #1897).

    Covers the happy path, the required ``for`` param, role / from filters,
    timeout clamping, and pipeline validation.
    """

    def test_wait_missing_for_returns_400(self, client, app):
        with app.test_request_context():
            with patch(
                "routes.messages.get_state_store_for_pipeline"
            ) as mock_get_store_for_pipeline:
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                resp = client.get("/api/v1/pipelines/test-pipeline/messages/wait?timeout=1")
                assert resp.status_code == 400
                data = json.loads(resp.data)
                assert "for" in data["message"].lower()

    def test_wait_returns_matched_message(self, client, app):
        store = MessageStore()
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_CONFIRMED,
                subject="done",
            )
        )
        with app.test_request_context():
            with (
                patch("routes.messages.get_message_store", return_value=store),
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
            ):
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                resp = client.get(
                    "/api/v1/pipelines/test-pipeline/messages/wait"
                    "?for=CONSENSUS_CONFIRMED&timeout=2"
                )
                assert resp.status_code == 200
                data = json.loads(resp.data)
                assert data["data"]["count"] == 1
                assert data["data"]["matched"] is True
                assert data["data"]["messages"][0]["message_type"] == "CONSENSUS_CONFIRMED"

    def test_wait_repeatable_for_param(self, client, app):
        """Multiple --for types act as an OR filter."""
        store = MessageStore()
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_RE_REVIEW,
                subject="re-review",
            )
        )
        with app.test_request_context():
            with (
                patch("routes.messages.get_message_store", return_value=store),
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
            ):
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                resp = client.get(
                    "/api/v1/pipelines/test-pipeline/messages/wait"
                    "?for=CONSENSUS_CONFIRMED&for=CONSENSUS_RE_REVIEW"
                    "&timeout=2"
                )
                assert resp.status_code == 200
                data = json.loads(resp.data)
                assert data["data"]["count"] == 1
                assert data["data"]["matched"] is True

    def test_wait_times_out_with_empty_result(self, client, app):
        """No matching message -> 200 with matched=False."""
        store = MessageStore()
        with app.test_request_context():
            with (
                patch("routes.messages.get_message_store", return_value=store),
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
            ):
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                resp = client.get(
                    "/api/v1/pipelines/test-pipeline/messages/wait"
                    "?for=CONSENSUS_CONFIRMED&timeout=1"
                )
                # Status 200 (not 408) — same as poll_messages behaviour.
                assert resp.status_code == 200
                data = json.loads(resp.data)
                assert data["data"]["count"] == 0
                assert data["data"]["matched"] is False

    def test_wait_from_filter(self, client, app):
        """`from=ROLE` drops messages from other senders."""
        store = MessageStore()
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="documenter",
                to_role="all",
                message_type=MessageType.CONSENSUS_CONFIRMED,
                subject="docs confirmed",
            )
        )
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_CONFIRMED,
                subject="coder confirmed",
            )
        )
        with app.test_request_context():
            with (
                patch("routes.messages.get_message_store", return_value=store),
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
            ):
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                resp = client.get(
                    "/api/v1/pipelines/test-pipeline/messages/wait"
                    "?for=CONSENSUS_CONFIRMED&from=coder&timeout=1"
                )
                assert resp.status_code == 200
                data = json.loads(resp.data)
                assert data["data"]["count"] == 1
                assert data["data"]["messages"][0]["from_role"] == "coder"

    def test_wait_invalid_pipeline_id_returns_400(self, client, app):
        with app.test_request_context():
            with patch(
                "routes.messages.get_state_store_for_pipeline",
                side_effect=InvalidPipelineIdError("bad-id"),
            ):
                resp = client.get(
                    "/api/v1/pipelines/bad-id/messages/wait?for=CONSENSUS_CONFIRMED&timeout=1"
                )
                assert resp.status_code == 400

    def test_wait_timeout_clamped_to_env_cap(self, client, app, monkeypatch):
        """``timeout`` is clamped by ``EGG_MESSAGE_POLL_MAX_WAIT``.

        With the cap set to 2s, a timeout=999 request must not actually
        block for 999 seconds.  We only verify here that the request returns
        promptly (<5s) — a real integration test would measure more finely.
        """
        import time as _t

        monkeypatch.setenv("EGG_MESSAGE_POLL_MAX_WAIT", "2")
        store = MessageStore()
        with app.test_request_context():
            with (
                patch("routes.messages.get_message_store", return_value=store),
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
            ):
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                start = _t.monotonic()
                resp = client.get(
                    "/api/v1/pipelines/test-pipeline/messages/wait"
                    "?for=CONSENSUS_CONFIRMED&timeout=999"
                )
                elapsed = _t.monotonic() - start
                assert resp.status_code == 200
                assert elapsed < 5  # would be 999s without clamp


class TestEnvCapConfig:
    """`EGG_MESSAGE_POLL_MAX_WAIT` plumbing (issue #1897)."""

    def test_default_cap_is_60(self, monkeypatch):
        from routes.messages import DEFAULT_POLL_MAX_WAIT_SECONDS, _get_poll_max_wait

        monkeypatch.delenv("EGG_MESSAGE_POLL_MAX_WAIT", raising=False)
        assert _get_poll_max_wait() == DEFAULT_POLL_MAX_WAIT_SECONDS
        assert DEFAULT_POLL_MAX_WAIT_SECONDS == 60

    def test_env_var_overrides(self, monkeypatch):
        from routes.messages import _get_poll_max_wait

        monkeypatch.setenv("EGG_MESSAGE_POLL_MAX_WAIT", "120")
        assert _get_poll_max_wait() == 120

    def test_env_var_invalid_falls_back_to_default(self, monkeypatch):
        from routes.messages import DEFAULT_POLL_MAX_WAIT_SECONDS, _get_poll_max_wait

        monkeypatch.setenv("EGG_MESSAGE_POLL_MAX_WAIT", "not-a-number")
        assert _get_poll_max_wait() == DEFAULT_POLL_MAX_WAIT_SECONDS

    def test_env_var_zero_falls_back_to_default(self, monkeypatch):
        from routes.messages import DEFAULT_POLL_MAX_WAIT_SECONDS, _get_poll_max_wait

        monkeypatch.setenv("EGG_MESSAGE_POLL_MAX_WAIT", "0")
        assert _get_poll_max_wait() == DEFAULT_POLL_MAX_WAIT_SECONDS

    def test_env_var_negative_falls_back_to_default(self, monkeypatch):
        from routes.messages import DEFAULT_POLL_MAX_WAIT_SECONDS, _get_poll_max_wait

        monkeypatch.setenv("EGG_MESSAGE_POLL_MAX_WAIT", "-5")
        assert _get_poll_max_wait() == DEFAULT_POLL_MAX_WAIT_SECONDS

    def test_startup_warning_above_threshold(self, monkeypatch):
        """When the cap exceeds 90s, a warnings.warn names the gateway Squid
        coupling (also logs WARNING, but we assert via warnings only to avoid
        dependency on the logging configuration at test time)."""
        import warnings as _warnings

        from routes.messages import log_poll_max_wait_startup

        monkeypatch.setenv("EGG_MESSAGE_POLL_MAX_WAIT", "120")
        with _warnings.catch_warnings(record=True) as w:
            _warnings.simplefilter("always")
            log_poll_max_wait_startup()
            assert any("120s" in str(x.message) for x in w)
            assert any("gateway" in str(x.message).lower() for x in w)

    def test_startup_no_warning_below_threshold(self, monkeypatch):
        """At the default cap no warnings.warn is emitted."""
        import warnings as _warnings

        from routes.messages import log_poll_max_wait_startup

        monkeypatch.setenv("EGG_MESSAGE_POLL_MAX_WAIT", "60")
        with _warnings.catch_warnings(record=True) as w:
            _warnings.simplefilter("always")
            log_poll_max_wait_startup()
            # No warning about EGG_MESSAGE_POLL_MAX_WAIT at the 60s cap.
            assert not any("EGG_MESSAGE_POLL_MAX_WAIT" in str(x.message) for x in w)


class TestInflightLongPollGauge:
    """``egg_inflight_long_polls`` increments while blocking, decrements after.

    We call the private helpers because verifying the gauge via a real
    endpoint would require a running waitress stack.
    """

    def test_start_end_are_no_op_when_metric_unavailable(self):
        """_track_long_poll_start/end should not raise when the metric
        registry is unavailable (guarded by the ``except Exception`` block
        in the module)."""
        from routes.messages import _track_long_poll_end, _track_long_poll_start

        # Calling either should not raise even if the gauge is None.
        _track_long_poll_start()
        _track_long_poll_end()
