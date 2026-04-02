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
                assert tester_msg["metadata"].get("delphi_redacted") is not True

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
