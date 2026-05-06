"""Tests for message endpoints and Delphi visibility filtering.

Covers the BRC-specific message filtering (Delphi ordering) and
long-polling behavior in the messages route.
"""

import json
import sys
import time
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
    """Reset the message store between tests.

    Heartbeat coordinator reset is handled by ``_reset_heartbeat_coordinator``
    in ``conftest.py`` (autouse-scoped to all orchestrator tests) so the
    cleanup is shared across files instead of drifting per-test-module.
    """
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

    def test_wait_clamped_to_poll_max_wait(self, client, app, monkeypatch):
        """Wait should be capped by EGG_MESSAGE_POLL_MAX_WAIT.

        The route clamps any client-supplied ``wait`` to the configured
        cap before calling ``MessageStore.get_messages``.  We verify the
        clamp empirically by lowering the cap to 2s and sending
        ``wait=999`` — the request must return in well under 999s.
        Without the clamp, this test would block on ``cv.wait`` for the
        full 999-second budget (issue #1928).
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
                mock_get_store_for_pipeline.return_value = (MagicMock(), _make_pipeline_mock())

                start = _t.monotonic()
                resp = client.get("/api/v1/pipelines/test-pipeline/messages?wait=999")
                elapsed = _t.monotonic() - start

                assert resp.status_code == 200
                assert elapsed < 5, f"wait=999 with cap=2 took {elapsed:.1f}s; clamp not applied"

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


class TestSinceIdStaleSignal:
    """Issue #2464 — ``/messages`` and ``/messages/wait`` advertise a
    structured ``since_id_stale: true`` flag when the cursor was unknown,
    so consumers (sandbox CLI cursor file, agent ``wait_loop``) can drop
    cached cursors instead of feeding the dead value back forever.
    """

    def test_messages_carries_flag_on_stale_cursor(self, client, app):
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
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )

                resp = client.get(
                    "/api/v1/pipelines/test-pipeline/messages?since_id=cursor-the-store-never-saw"
                )
                data = json.loads(resp.data)

                assert resp.status_code == 200
                assert data["data"].get("since_id_stale") is True
                # Full-history fallback still in effect.
                assert data["data"]["count"] == 1

    def test_messages_omits_flag_for_fresh_cursor(self, client, app):
        """Pin the byte-shape contract: responses with no staleness DO
        NOT carry a ``since_id_stale`` field at all (instead of
        ``false``), so legacy consumers see byte-identical output."""
        store = MessageStore()
        first = Message(
            pipeline_id="test-pipeline",
            from_role="coder",
            to_role="all",
            message_type=MessageType.PROGRESS,
            subject="first",
        )
        store.add_message(first)
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.PROGRESS,
                subject="second",
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

                resp = client.get(f"/api/v1/pipelines/test-pipeline/messages?since_id={first.id}")
                data = json.loads(resp.data)

                assert resp.status_code == 200
                assert "since_id_stale" not in data["data"]

    def test_messages_omits_flag_when_no_since_id(self, client, app):
        """No cursor passed → the flag is irrelevant and must be absent."""
        store = MessageStore()
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.PROGRESS,
                subject="x",
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

                resp = client.get("/api/v1/pipelines/test-pipeline/messages")
                data = json.loads(resp.data)

                assert resp.status_code == 200
                assert "since_id_stale" not in data["data"]

    def test_messages_wait_carries_flag_on_stale_cursor(self, client, app):
        """``/messages/wait`` mirrors the same flag — agent ``wait_loop``
        relies on it to drop ``inner["since"]`` mid-loop after a phase
        clear."""
        store = MessageStore()
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.OVERSEER_ALERT,
                subject="alert",
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
                    "?for=OVERSEER_ALERT&timeout=1"
                    "&since_id=cursor-the-store-never-saw"
                )
                data = json.loads(resp.data)

                assert resp.status_code == 200
                assert data["data"].get("since_id_stale") is True

    def test_messages_wait_omits_flag_for_fresh_cursor(self, client, app):
        """Pin the byte-shape contract for ``/messages/wait`` on the
        fresh-cursor path: when the supplied ``since_id`` resolves
        cleanly the response must omit ``since_id_stale`` entirely
        (instead of carrying ``false``). A future divergence between
        ``/messages`` and ``/messages/wait`` field handling would
        otherwise slip through — see reviewer note #6 on PR #2485."""
        store = MessageStore()
        first = Message(
            pipeline_id="test-pipeline",
            from_role="coder",
            to_role="all",
            message_type=MessageType.PROGRESS,
            subject="first",
        )
        store.add_message(first)
        # Add a matching second message so the wait returns immediately
        # via the fast-path filter rather than blocking on the timeout.
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_CONFIRMED,
                subject="match",
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
                    f"?for=CONSENSUS_CONFIRMED&timeout=2&since_id={first.id}"
                )
                data = json.loads(resp.data)

                assert resp.status_code == 200
                assert data["data"]["matched"] is True
                assert "since_id_stale" not in data["data"]

    def test_messages_wait_omits_flag_when_no_since_id(self, client, app):
        """No ``since_id`` parameter → ``from_tip=True`` semantics,
        ``meta.since_id_stale`` is irrelevant by construction, and the
        field must be absent from the response (mirrors the
        ``/messages`` no-cursor case)."""
        import threading
        import time as _t

        store = MessageStore()

        def _add_after_delay() -> None:
            _t.sleep(0.15)
            store.add_message(
                Message(
                    pipeline_id="test-pipeline",
                    from_role="coder",
                    to_role="all",
                    message_type=MessageType.CONSENSUS_CONFIRMED,
                    subject="match",
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
                t = threading.Thread(target=_add_after_delay)
                t.start()
                try:
                    resp = client.get(
                        "/api/v1/pipelines/test-pipeline/messages/wait"
                        "?for=CONSENSUS_CONFIRMED&timeout=3"
                    )
                finally:
                    t.join(timeout=2)
                data = json.loads(resp.data)

                assert resp.status_code == 200
                assert data["data"]["matched"] is True
                assert "since_id_stale" not in data["data"]


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
        """A matching message added *after* the wait starts unblocks it.

        Issue #1925: the wait endpoint now starts from the stream tip,
        so pre-existing messages do NOT satisfy a cursor-less wait. This
        test uses a background thread to inject the match while the
        endpoint is blocking.
        """
        import threading
        import time as _t

        store = MessageStore()

        def _add_after_delay() -> None:
            _t.sleep(0.15)
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
                t = threading.Thread(target=_add_after_delay)
                t.start()
                try:
                    resp = client.get(
                        "/api/v1/pipelines/test-pipeline/messages/wait"
                        "?for=CONSENSUS_CONFIRMED&timeout=3"
                    )
                finally:
                    t.join(timeout=2)
                assert resp.status_code == 200
                data = json.loads(resp.data)
                assert data["data"]["count"] == 1
                assert data["data"]["matched"] is True
                assert data["data"]["messages"][0]["message_type"] == "CONSENSUS_CONFIRMED"

    def test_wait_repeatable_for_param(self, client, app):
        """Multiple --for types act as an OR filter."""
        import threading
        import time as _t

        store = MessageStore()

        def _add_after_delay() -> None:
            _t.sleep(0.15)
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
                t = threading.Thread(target=_add_after_delay)
                t.start()
                try:
                    resp = client.get(
                        "/api/v1/pipelines/test-pipeline/messages/wait"
                        "?for=CONSENSUS_CONFIRMED&for=CONSENSUS_RE_REVIEW"
                        "&timeout=3"
                    )
                finally:
                    t.join(timeout=2)
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
        import threading
        import time as _t

        store = MessageStore()

        def _add_after_delay() -> None:
            _t.sleep(0.15)
            # Wrong-sender message first — must NOT unblock the wait.
            store.add_message(
                Message(
                    pipeline_id="test-pipeline",
                    from_role="documenter",
                    to_role="all",
                    message_type=MessageType.CONSENSUS_CONFIRMED,
                    subject="docs confirmed",
                )
            )
            _t.sleep(0.1)
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
                t = threading.Thread(target=_add_after_delay)
                t.start()
                try:
                    resp = client.get(
                        "/api/v1/pipelines/test-pipeline/messages/wait"
                        "?for=CONSENSUS_CONFIRMED&from=coder&timeout=3"
                    )
                finally:
                    t.join(timeout=2)
                assert resp.status_code == 200
                data = json.loads(resp.data)
                assert data["data"]["count"] == 1
                assert data["data"]["messages"][0]["from_role"] == "coder"

    def test_wait_ignores_pre_existing_messages(self, client, app):
        """Issue #1925 regression: a cursor-less wait must NOT return a
        matching message that was added before the call started.

        Before the fix, repeated wait-loop invocations re-matched the same
        already-seen CONSENSUS_CONFIRMED on every call because the store
        scanned from stream tail = 0. After the fix, the wait endpoint
        starts from the stream tip, so pre-existing messages are ignored.
        """
        store = MessageStore()
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="documenter",
                to_role="all",
                message_type=MessageType.CONSENSUS_CONFIRMED,
                subject="already seen — must not match",
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
                    "?for=CONSENSUS_CONFIRMED&timeout=1"
                )
                assert resp.status_code == 200
                data = json.loads(resp.data)
                assert data["data"]["count"] == 0
                assert data["data"]["matched"] is False

    def test_wait_honors_explicit_since_id(self, client, app):
        """Passing ``since_id=<id>`` opts out of from_tip behavior and
        returns matching messages added after that cursor.

        This is the race-safety pattern callers use when they've already
        observed an event and want to wait for the NEXT event after it.
        """
        store = MessageStore()
        cursor = store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="documenter",
                to_role="all",
                message_type=MessageType.PROGRESS,
                subject="cursor anchor",
            )
        )
        # A matching message added AFTER the cursor but BEFORE the wait
        # call — with since_id passed, this must still be returned.
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_CONFIRMED,
                subject="after cursor",
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
                    f"?for=CONSENSUS_CONFIRMED&since_id={cursor.id}&timeout=1"
                )
                assert resp.status_code == 200
                data = json.loads(resp.data)
                assert data["data"]["count"] == 1
                assert data["data"]["messages"][0]["subject"] == "after cursor"

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

    def test_wait_returns_cursor_on_match(self, client, app):
        """Issue #1995: wait response includes the ID of the last
        delivered message so the caller can thread it on the next call."""
        import threading
        import time as _t

        store = MessageStore()

        def _add_after_delay() -> None:
            _t.sleep(0.15)
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
                t = threading.Thread(target=_add_after_delay)
                t.start()
                try:
                    resp = client.get(
                        "/api/v1/pipelines/test-pipeline/messages/wait"
                        "?for=CONSENSUS_CONFIRMED&timeout=3"
                    )
                finally:
                    t.join(timeout=2)
                assert resp.status_code == 200
                data = json.loads(resp.data)
                assert data["data"]["matched"] is True
                # Cursor must equal the ID of the last delivered message so
                # the next wait can resume strictly after it.
                assert data["data"]["cursor"] == data["data"]["messages"][-1]["id"]

    def test_wait_returns_cursor_on_timeout(self, client, app):
        """Issue #1995: on timeout with no match the server still returns
        a cursor (current stream tip) so the next wait can pick up
        anything that arrived while the caller was round-tripping."""
        store = MessageStore()
        # Seed one non-matching message so the stream has a tip.
        seeded = store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="documenter",
                to_role="all",
                message_type=MessageType.PROGRESS,
                subject="ignore me",
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
                    "?for=CONSENSUS_CONFIRMED&timeout=1"
                )
                assert resp.status_code == 200
                data = json.loads(resp.data)
                assert data["data"]["matched"] is False
                assert data["data"]["cursor"] == seeded.id

    def test_wait_returns_null_cursor_when_stream_empty(self, client, app):
        """Stream has never had a message → cursor is null. Next call
        may safely omit ``since_id`` and the server will snap to a fresh
        tip as before."""
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
                assert resp.status_code == 200
                data = json.loads(resp.data)
                assert data["data"]["matched"] is False
                assert data["data"]["cursor"] is None

    def test_wait_cursor_threading_closes_between_call_race(self, client, app):
        """Issue #1995 regression: reproduces the BRC deadlock scenario.

        Timeline:
          1. Reviewer A ACKs → wait #1 returns with messages=[ack_a],
             cursor=ack_a.id.
          2. Reviewer B ACKs *between* wait #1 returning and wait #2
             starting. Without cursor threading, wait #2 would snap to
             a new tip past ack_b and deadlock.
          3. Producer threads cursor=ack_a.id as since_id on wait #2;
             ack_b is delivered immediately.
        """
        store = MessageStore()

        ack_a = store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="reviewer_agent_design",
                to_role="all",
                message_type=MessageType.CONSENSUS_ACK,
                subject="ack-a",
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
                # Wait #1: caller passes since_id=<pre-existing tip> so
                # the pre-existing ack_a is delivered.  Here we thread
                # a since_id of an unknown id so the server falls back
                # to "return full history", which delivers ack_a.
                resp1 = client.get(
                    "/api/v1/pipelines/test-pipeline/messages/wait"
                    "?for=CONSENSUS_ACK&timeout=1&since_id=unknown-sentinel"
                )
                assert resp1.status_code == 200
                data1 = json.loads(resp1.data)
                assert data1["data"]["matched"] is True
                cursor1 = data1["data"]["cursor"]
                assert cursor1 == ack_a.id

                # Simulate the between-calls race: ack_b lands right now.
                ack_b = store.add_message(
                    Message(
                        pipeline_id="test-pipeline",
                        from_role="reviewer_refine",
                        to_role="all",
                        message_type=MessageType.CONSENSUS_ACK,
                        subject="ack-b",
                    )
                )

                # Wait #2: caller threads cursor1 as since_id. Without
                # cursor threading this call would deadlock (bug #1995);
                # with it, ack_b is delivered immediately.
                resp2 = client.get(
                    "/api/v1/pipelines/test-pipeline/messages/wait"
                    f"?for=CONSENSUS_ACK&timeout=1&since_id={cursor1}"
                )
                assert resp2.status_code == 200
                data2 = json.loads(resp2.data)
                assert data2["data"]["matched"] is True
                assert data2["data"]["messages"][0]["id"] == ack_b.id
                assert data2["data"]["cursor"] == ack_b.id

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


class TestProducerPendingConfirmGuard:
    """Reject ``wait --for CONSENSUS_CONFIRMED`` from producers in
    WORKING/PROPOSED state (#2064).

    A producer's own confirm is part of what generates the global
    CONSENSUS_CONFIRMED signal — waiting on it before having confirmed
    is a circular dependency that would deadlock until the overseer's
    heartbeat-stall detector intervened minutes later. The guard turns
    that silent deadlock into an immediate, actionable 400.
    """

    @pytest.fixture
    def implement_tracker(self):
        """Build a tracker matching the implement-phase shape that
        triggered #2064: documenter as producer with one ADVISORY
        reviewer, plus coder/tester/reviewers."""
        from peer_consensus import PeerConsensusTracker
        from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph

        graph = ReviewGraph(
            [
                ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
                ReviewEdge("reviewer_code", "tester", ReviewCriticality.CRITICAL),
                ReviewEdge("reviewer_code", "documenter", ReviewCriticality.ADVISORY),
                ReviewEdge("reviewer_contract", "coder", ReviewCriticality.CRITICAL),
                # tester reviews coder in the default implement graph; this
                # edge makes tester genuinely dual-role (producer + reviewer)
                # so test_dual_role_tester_in_proposed_blocked locks the
                # dual-role contract, not just the tester-as-producer case.
                ReviewEdge("tester", "coder", ReviewCriticality.CRITICAL),
            ]
        )
        tracker = PeerConsensusTracker("test-pipeline", graph, cooldown_seconds=0)
        for role in ("coder", "tester", "documenter", "reviewer_code", "reviewer_contract"):
            tracker.register_agent(role)
        return tracker

    def _wait(self, client, *, role: str, for_types: list[str] | None = None):
        types = for_types or ["CONSENSUS_CONFIRMED"]
        qs = "&".join(f"for={t}" for t in types) + f"&role={role}&timeout=1"
        return client.get(f"/api/v1/pipelines/test-pipeline/messages/wait?{qs}")

    def test_producer_in_working_blocked_on_consensus_confirmed(
        self, client, app, implement_tracker
    ):
        """Producer in WORKING (never proposed) cannot wait on
        CONSENSUS_CONFIRMED — it must propose and confirm first."""
        with app.test_request_context():
            with (
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
                patch(
                    "peer_consensus.get_peer_consensus_tracker",
                    return_value=implement_tracker,
                ),
            ):
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                resp = self._wait(client, role="documenter")
                assert resp.status_code == 400
                msg = json.loads(resp.data)["message"]
                assert "documenter" in msg
                assert "CONSENSUS_CONFIRMED" in msg
                assert "mcp__brc__confirm" in msg
                assert "#2064" in msg

    def test_producer_in_proposed_blocked_on_consensus_confirmed(
        self, client, app, implement_tracker
    ):
        """Reproduces the issue-1965 documenter case: PROPOSED but
        not CONFIRMED, attempting to STAY ALIVE on CONSENSUS_CONFIRMED."""
        implement_tracker.handle_propose(
            "documenter",
            {
                "summary": "Wrote docs for the new fan-out feature, covering "
                "thresholds, partitioning, parent cross-partition consistency, "
                "and the parallelism config knob.",
                "artifacts": ["docs/guides/concurrent-execution.md"],
                "commit_sha": "abc1234",
            },
        )
        with app.test_request_context():
            with (
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
                patch(
                    "peer_consensus.get_peer_consensus_tracker",
                    return_value=implement_tracker,
                ),
            ):
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                resp = self._wait(
                    client,
                    role="documenter",
                    for_types=["CONSENSUS_CONFIRMED", "CONSENSUS_RE_REVIEW", "OVERSEER_ALERT"],
                )
                assert resp.status_code == 400
                msg = json.loads(resp.data)["message"]
                assert "pending_acks" in msg

    def test_producer_in_confirmed_passes(self, client, app, implement_tracker):
        """A producer that has actually confirmed may legitimately wait
        on CONSENSUS_CONFIRMED — that's the post-confirm STAY ALIVE
        pattern the producer-lifecycle prompt prescribes."""
        # Set up: every producer proposes, every reviewer ACKs the
        # critical edges, then documenter (advisory-only) confirms.
        for producer in ("coder", "tester", "documenter"):
            implement_tracker.handle_propose(
                producer,
                {
                    "summary": (
                        f"Stub proposal from {producer} so the global guard "
                        "passes — every producer must propose before any "
                        "agent can confirm consensus."
                    ),
                    "artifacts": [f"path/{producer}.py"],
                    "commit_sha": "abc1234",
                },
            )
        for producer in ("coder", "tester"):
            implement_tracker.handle_ack(
                "reviewer_code",
                producer,
                {"artifact_references": [f"path/{producer}.py"]},
            )
        implement_tracker.handle_ack(
            "reviewer_contract",
            "coder",
            {"artifact_references": ["path/coder.py"]},
        )
        # documenter's only reviewer is advisory, so it's already fully ACKed
        result = implement_tracker.handle_confirmed("documenter")
        assert result["status"] == "confirmed"

        with app.test_request_context():
            with (
                patch("routes.messages.get_message_store", return_value=MessageStore()),
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
                patch(
                    "peer_consensus.get_peer_consensus_tracker",
                    return_value=implement_tracker,
                ),
            ):
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                resp = self._wait(client, role="documenter")
                assert resp.status_code == 200

    def test_dual_role_tester_in_proposed_blocked(self, client, app, implement_tracker):
        """Dual-role tester (producer of its own artifacts + reviewer of
        coder, per the implement graph) is still blocked by the guard
        while in PROPOSED — its producer phase has not yet transitioned
        to CONFIRMED, so the deadlock condition still holds even though
        the agent also has a reviewer phase. Locks the helper's contract
        for the dual-role case."""
        implement_tracker.handle_propose(
            "tester",
            {
                "summary": (
                    "Added integration tests covering the new fan-out "
                    "thresholds, partition boundaries, and parent "
                    "cross-partition consistency invariants."
                ),
                "artifacts": ["orchestrator/tests/test_fan_out.py"],
                "commit_sha": "abc1234",
            },
        )
        with app.test_request_context():
            with (
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
                patch(
                    "peer_consensus.get_peer_consensus_tracker",
                    return_value=implement_tracker,
                ),
            ):
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                resp = self._wait(client, role="tester")
                assert resp.status_code == 400
                msg = json.loads(resp.data)["message"]
                assert "tester" in msg
                assert "CONSENSUS_CONFIRMED" in msg
                assert "#2064" in msg

    def test_reviewer_only_role_passes(self, client, app, implement_tracker):
        """Reviewer-only roles may wait on CONSENSUS_CONFIRMED at any
        time — they have no producer-side confirm of their own to
        block consensus on."""
        with app.test_request_context():
            with (
                patch("routes.messages.get_message_store", return_value=MessageStore()),
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
                patch(
                    "peer_consensus.get_peer_consensus_tracker",
                    return_value=implement_tracker,
                ),
            ):
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                resp = self._wait(client, role="reviewer_contract")
                assert resp.status_code == 200

    def test_other_for_types_pass_for_unconfirmed_producer(self, client, app, implement_tracker):
        """Producers in WORKING/PROPOSED can still wait on the events
        they legitimately need — CONSENSUS_ACK from reviewers,
        CONSENSUS_PROPOSE from peers, OVERSEER_ALERT, etc."""
        with app.test_request_context():
            with (
                patch("routes.messages.get_message_store", return_value=MessageStore()),
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
                patch(
                    "peer_consensus.get_peer_consensus_tracker",
                    return_value=implement_tracker,
                ),
            ):
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                resp = self._wait(
                    client,
                    role="documenter",
                    for_types=["CONSENSUS_ACK", "CONSENSUS_NACK", "OVERSEER_ALERT"],
                )
                assert resp.status_code == 200

    def test_no_tracker_passes(self, client, app):
        """When no consensus tracker is registered (e.g. test fixtures,
        non-BRC pipelines, post-orchestrator-restart before reconstruction),
        the guard short-circuits rather than wedging the wait endpoint."""
        with app.test_request_context():
            with (
                patch("routes.messages.get_message_store", return_value=MessageStore()),
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
                patch("peer_consensus.get_peer_consensus_tracker", return_value=None),
            ):
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                resp = self._wait(client, role="documenter")
                assert resp.status_code == 200

    def test_no_role_passes(self, client, app, implement_tracker):
        """Calls without a role parameter (e.g. broadcast snapshots) cannot
        be evaluated by the guard and must pass through."""
        with app.test_request_context():
            with (
                patch("routes.messages.get_message_store", return_value=MessageStore()),
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
                patch(
                    "peer_consensus.get_peer_consensus_tracker",
                    return_value=implement_tracker,
                ),
            ):
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                resp = client.get(
                    "/api/v1/pipelines/test-pipeline/messages/wait"
                    "?for=CONSENSUS_CONFIRMED&timeout=1"
                )
                assert resp.status_code == 200


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


class TestHeartbeatRoute:
    """POST /api/v1/pipelines/<id>/heartbeat — plan TASK-3-2 and TASK-3-4.

    Non-blocking items from reviewer_code NACK: add coverage for the
    dedicated heartbeat route so the dedup + rate-limit plumbing has a
    regression guard. Without these tests, a future refactor could
    silently break the 429 response shape or the (state, waiting_on)
    dedup and only surface in prod.
    """

    def test_heartbeat_route_accepts_valid_payload(self, client, app):
        """Happy path: POST a valid heartbeat, get 200 + non-deduped."""
        with app.test_request_context():
            with patch(
                "routes.messages.get_state_store_for_pipeline"
            ) as mock_get_store_for_pipeline:
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                # Use a unique role so the global coordinator state from
                # a prior test doesn't trigger dedup.
                resp = client.post(
                    "/api/v1/pipelines/test-pipeline/heartbeat",
                    json={"from_role": "heartbeat-route-role-a", "state": "WORKING"},
                )
                assert resp.status_code == 200
                data = json.loads(resp.data)
                assert data["success"] is True
                assert data["data"]["deduped"] is False

    def test_heartbeat_route_dedups_repeat_state(self, client, app):
        """Plan TASK-3-2: repeated (state, waiting_on) tuples dedupe."""
        with app.test_request_context():
            with patch(
                "routes.messages.get_state_store_for_pipeline"
            ) as mock_get_store_for_pipeline:
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                # Use a unique role for this test.
                role = "heartbeat-route-role-b"
                resp1 = client.post(
                    "/api/v1/pipelines/test-pipeline/heartbeat",
                    json={"from_role": role, "state": "WORKING"},
                )
                assert resp1.status_code == 200
                assert json.loads(resp1.data)["data"]["deduped"] is False

                # Second identical call MUST dedupe.
                resp2 = client.post(
                    "/api/v1/pipelines/test-pipeline/heartbeat",
                    json={"from_role": role, "state": "WORKING"},
                )
                assert resp2.status_code == 200
                assert json.loads(resp2.data)["data"]["deduped"] is True

    def test_heartbeat_route_does_not_dedupe_waiting_for_event(self, client, app):
        """Issue #2036: ``WAITING_FOR_EVENT`` is a liveness keep-alive
        emitted by the ``message_wait_loop`` handler while it's blocked
        (driving ``egg-orch message wait-loop``). Periodic identical
        beats are exactly the signal the overseer's stall detector
        needs — so this state MUST skip the ``(state, waiting_on)``
        dedup filter even when consecutive posts are byte-for-byte
        identical.

        If a future refactor re-enables dedup for this state, every
        agent blocked in a wait-loop would once again stop emitting
        heartbeats after the first beat, and the overseer would resume
        firing the false-positive stall alerts described in #2036.
        """
        with app.test_request_context():
            with patch(
                "routes.messages.get_state_store_for_pipeline"
            ) as mock_get_store_for_pipeline:
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                role = "wait-loop-liveness-role"
                for _ in range(3):
                    resp = client.post(
                        "/api/v1/pipelines/test-pipeline/heartbeat",
                        json={"from_role": role, "state": "WAITING_FOR_EVENT"},
                    )
                    assert resp.status_code == 200
                    assert json.loads(resp.data)["data"]["deduped"] is False

    def test_heartbeat_route_requires_from_role(self, client, app):
        """Missing from_role -> 400."""
        with app.test_request_context():
            with patch("routes.messages.get_state_store_for_pipeline"):
                resp = client.post(
                    "/api/v1/pipelines/test-pipeline/heartbeat",
                    json={"state": "WORKING"},
                )
                assert resp.status_code == 400

    def test_heartbeat_route_rejects_invalid_state(self, client, app):
        """Invalid state values rejected with 400."""
        with app.test_request_context():
            with patch("routes.messages.get_state_store_for_pipeline"):
                resp = client.post(
                    "/api/v1/pipelines/test-pipeline/heartbeat",
                    json={"from_role": "coder", "state": "BOGUS"},
                )
                assert resp.status_code == 400
                assert b"state must be one of" in resp.data

    def test_heartbeat_route_waiting_on_role_requires_waiting_on(self, client, app):
        """state=WAITING_ON_ROLE must include waiting_on."""
        with app.test_request_context():
            with patch("routes.messages.get_state_store_for_pipeline"):
                resp = client.post(
                    "/api/v1/pipelines/test-pipeline/heartbeat",
                    json={"from_role": "coder", "state": "WAITING_ON_ROLE"},
                )
                assert resp.status_code == 400
                assert b"waiting_on" in resp.data

    def test_heartbeat_rate_limit_429_response_shape(self, client, app, monkeypatch):
        """Plan TASK-3-4: exceeding EGG_HEARTBEAT_RATE_LIMIT returns 429
        with retry_after in the body.

        Response-shape pin: body MUST include ``retry_after`` (integer
        seconds) so clients can back off deterministically. Without
        this, a misbehaving agent could hot-loop on a rejected
        heartbeat and saturate the waitress pool.
        """
        # Set a very low rate limit so we can blow through it cheaply.
        monkeypatch.setenv("EGG_HEARTBEAT_RATE_LIMIT", "2")
        # Reset the heartbeat coordinator so this test's rate-limit
        # window starts empty.
        import heartbeat as _hb

        _hb._coordinator = None  # type: ignore[attr-defined]

        with app.test_request_context():
            with patch(
                "routes.messages.get_state_store_for_pipeline"
            ) as mock_get_store_for_pipeline:
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                # Need heartbeats that are NOT deduped by (state, waiting_on).
                # Alternate states so each passes the dedup filter; then
                # we exceed the rate limit on the third call.
                role = "rate-limit-role-unique"
                # First heartbeat: WORKING (allowed, non-dup).
                r1 = client.post(
                    "/api/v1/pipelines/test-pipeline/heartbeat",
                    json={"from_role": role, "state": "WORKING"},
                )
                assert r1.status_code == 200
                # Second: PROPOSED (allowed, non-dup).
                r2 = client.post(
                    "/api/v1/pipelines/test-pipeline/heartbeat",
                    json={"from_role": role, "state": "PROPOSED"},
                )
                assert r2.status_code == 200
                # Third: IDLE should trip the limit of 2/min.
                r3 = client.post(
                    "/api/v1/pipelines/test-pipeline/heartbeat",
                    json={"from_role": role, "state": "IDLE"},
                )
                assert r3.status_code == 429
                body = json.loads(r3.data)
                # Required fields per plan TASK-3-4.
                assert body["success"] is False
                assert "retry_after" in body
                assert isinstance(body["retry_after"], int)
                # retry_after should be in [0, 60] for a per-minute window.
                assert 0 <= body["retry_after"] <= 60

    def test_heartbeat_route_accepts_optional_since_field(self, client, app):
        """Optional ``since`` field passed through into message metadata."""
        with app.test_request_context():
            with patch(
                "routes.messages.get_state_store_for_pipeline"
            ) as mock_get_store_for_pipeline:
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                resp = client.post(
                    "/api/v1/pipelines/test-pipeline/heartbeat",
                    json={
                        "from_role": "heartbeat-route-role-since",
                        "state": "WORKING",
                        "since": "2026-04-23T07:00:00Z",
                    },
                )
                assert resp.status_code == 200
                data = json.loads(resp.data)
                assert data["data"]["message"]["metadata"]["since"] == ("2026-04-23T07:00:00Z")

    def test_heartbeat_refreshes_gateway_session(self, client, app):
        """A real heartbeat fans out to the gateway to refresh the agent's session.

        Regression guard for #2068: without this, an agent in
        ``WAITING_FOR_EVENT`` for >60 min has its gateway session pruned
        as idle even though it's actively heartbeating.
        """
        with app.test_request_context():
            mock_gw_client = MagicMock()
            with (
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
                patch(
                    "gateway_client.get_gateway_client",
                    return_value=mock_gw_client,
                ),
            ):
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                resp = client.post(
                    "/api/v1/pipelines/test-pipeline/heartbeat",
                    json={
                        "from_role": "fanout-role-coder",
                        "state": "WAITING_FOR_EVENT",
                    },
                )
                assert resp.status_code == 200
                mock_gw_client.heartbeat_session_by_container.assert_called_once_with(
                    "egg-agent-test-pipeline-fanout-role-coder"
                )

    def test_heartbeat_swallows_gateway_failure(self, client, app):
        """Gateway fan-out failures must not fail the heartbeat call."""
        with app.test_request_context():
            mock_gw_client = MagicMock()
            mock_gw_client.heartbeat_session_by_container.side_effect = RuntimeError("gateway down")
            with (
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
                patch(
                    "gateway_client.get_gateway_client",
                    return_value=mock_gw_client,
                ),
            ):
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                resp = client.post(
                    "/api/v1/pipelines/test-pipeline/heartbeat",
                    json={
                        "from_role": "fanout-role-tester",
                        "state": "WORKING",
                    },
                )
                assert resp.status_code == 200

    def test_heartbeat_fan_out_fires_on_deduped_state(self, client, app):
        """Deduped heartbeats still refresh the gateway session.

        Reviewer NB3 (#2068): the original fix only ran the fan-out
        *after* dedup, so an agent stuck in ``WORKING`` through a long
        compute (e.g. a slow ``make test``) would emit identical
        heartbeats that were dropped before refreshing the session.  The
        fan-out now runs *between* dedup and rate-limit (see
        ``routes/messages.py``): the dedup early-return invokes it so
        unchanged-state heartbeats still count as gateway-session
        liveness, while rate-limited heartbeats do not amplify into the
        gateway.

        The ``_GATEWAY_FANOUT_MIN_INTERVAL_SECONDS`` cooldown (#2076 NB2)
        is patched to ``0`` here so the test stays focused on the
        dedup-fan-out invariant; the throttle's caps are pinned by
        ``test_heartbeat_fan_out_throttle_*`` below.
        """
        with app.test_request_context():
            mock_gw_client = MagicMock()
            with (
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
                patch(
                    "gateway_client.get_gateway_client",
                    return_value=mock_gw_client,
                ),
                patch(
                    "routes.messages._GATEWAY_FANOUT_MIN_INTERVAL_SECONDS",
                    0.0,
                ),
            ):
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                # First WORKING heartbeat — recorded, not deduped.
                resp1 = client.post(
                    "/api/v1/pipelines/test-pipeline/heartbeat",
                    json={
                        "from_role": "fanout-dedup-role",
                        "state": "WORKING",
                    },
                )
                assert resp1.status_code == 200
                # Second identical heartbeat — dedup gate should drop it.
                resp2 = client.post(
                    "/api/v1/pipelines/test-pipeline/heartbeat",
                    json={
                        "from_role": "fanout-dedup-role",
                        "state": "WORKING",
                    },
                )
                assert resp2.status_code == 200
                assert json.loads(resp2.data)["data"]["deduped"] is True
                # Both calls fan out — that's the bug fix.
                assert mock_gw_client.heartbeat_session_by_container.call_count == 2

    @pytest.mark.parametrize(
        "from_role,expected_container_id",
        [
            ("reviewer_refine", "egg-agent-test-pipeline-reviewer-refine"),
            ("reviewer_code", "egg-agent-test-pipeline-reviewer-code"),
            ("reviewer_agent_design", "egg-agent-test-pipeline-reviewer-agent-design"),
            ("task_planner", "egg-agent-test-pipeline-task-planner"),
            ("conflict_resolver", "egg-agent-test-pipeline-conflict-resolver"),
            ("coder", "egg-agent-test-pipeline-coder"),
        ],
    )
    def test_heartbeat_fan_out_normalizes_underscores_to_hyphens(
        self, client, app, from_role, expected_container_id
    ):
        """Fan-out container_id must mirror kubernetes_spawner's role hyphenation.

        Reviewer blocker (#2068 follow-up): k8s names are RFC-1123
        labels (no underscores), so ``kubernetes_spawner.JOB_NAME_FORMAT``
        is filled with ``role.replace("_", "-")``.  ``from_role`` arrives
        from ``EGG_AGENT_ROLE`` which is the underscore form, so the
        fan-out must apply the same normalization — otherwise reviewer
        roles like ``reviewer_refine`` build a container_id that never
        matches the registered session and the gateway returns 404,
        making the fan-out a silent no-op for exactly the BRC reviewer
        roles that #2068 most acutely affects.
        """
        with app.test_request_context():
            mock_gw_client = MagicMock()
            with (
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
                patch(
                    "gateway_client.get_gateway_client",
                    return_value=mock_gw_client,
                ),
            ):
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                resp = client.post(
                    "/api/v1/pipelines/test-pipeline/heartbeat",
                    json={
                        "from_role": from_role,
                        "state": "WAITING_FOR_EVENT",
                    },
                )
                assert resp.status_code == 200
                mock_gw_client.heartbeat_session_by_container.assert_called_once_with(
                    expected_container_id
                )

    @pytest.mark.parametrize(
        "from_role,slice_id,expected_container_id",
        [
            (
                "reviewer_contract",
                "slice-2",
                "egg-agent-test-pipeline-slice-2-reviewer-contract",
            ),
            (
                "reviewer_code_holistic",
                "slice-12",
                "egg-agent-test-pipeline-slice-12-reviewer-code-holistic",
            ),
            ("tester", "slice-5", "egg-agent-test-pipeline-slice-5-tester"),
            ("coder", "slice-1", "egg-agent-test-pipeline-slice-1-coder"),
        ],
    )
    def test_heartbeat_fan_out_includes_slice_id(
        self, client, app, from_role, slice_id, expected_container_id
    ):
        """Slice-scoped heartbeats fan out to the slice-scoped container_id (#2451).

        Slice-DAG agents register gateway sessions under
        ``egg-agent-{pid}-{slice_id}-{role}`` per
        ``kubernetes_spawner.JOB_NAME_FORMAT_SLICE``. The orchestrator
        must thread the agent's ``slice_id`` (forwarded via the
        heartbeat body from ``EGG_SLICE_ID``) into the fan-out's
        container_id, otherwise every slice-scoped reviewer/tester
        emits warnings ("Session not found for container") and the
        gateway session never has its idle timer refreshed.
        """
        with app.test_request_context():
            mock_gw_client = MagicMock()
            with (
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
                patch(
                    "gateway_client.get_gateway_client",
                    return_value=mock_gw_client,
                ),
            ):
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                resp = client.post(
                    "/api/v1/pipelines/test-pipeline/heartbeat",
                    json={
                        "from_role": from_role,
                        "state": "WAITING_FOR_EVENT",
                        "slice_id": slice_id,
                    },
                )
                assert resp.status_code == 200
                mock_gw_client.heartbeat_session_by_container.assert_called_once_with(
                    expected_container_id
                )

    @pytest.mark.parametrize(
        "bad_slice_id",
        [
            "phase-2",  # legacy contract migration shape
            "../escape",  # path traversal
            "; rm -rf /",  # shell metacharacters
            "slice-2/extra",  # path separator after a valid prefix
            "slice-",  # missing index
            "Slice-2",  # case mismatch
            "",  # empty string disguised as set
            "slice-2 ",  # trailing whitespace
        ],
    )
    def test_heartbeat_rejects_invalid_slice_id(self, client, app, bad_slice_id):
        """Malformed ``slice_id`` is rejected with 400 rather than smuggled into the fan-out.

        Mirrors the canonical ``slice-<N>`` regex enforced at every
        gateway-facing seam (#2403, ``slice_id_validation``). A path
        separator, shell metacharacter, or other contract-foreign value
        must not reach the container_id construction below — covering
        both real-world legacy payloads (``phase-2``) and obviously
        malicious values (``../escape``, ``; rm -rf /``) so the
        security intent of the regex is explicit in the test corpus.
        """
        with app.test_request_context():
            mock_gw_client = MagicMock()
            with (
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
                patch(
                    "gateway_client.get_gateway_client",
                    return_value=mock_gw_client,
                ),
            ):
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                resp = client.post(
                    "/api/v1/pipelines/test-pipeline/heartbeat",
                    json={
                        "from_role": "tester",
                        "state": "WORKING",
                        "slice_id": bad_slice_id,
                    },
                )
                # Empty-string ``slice_id`` is treated as "no slice" by
                # the orchestrator's ``extract_slice_id`` (matches the
                # agent-side ``or`` fallback) — the request still
                # succeeds but pipeline-level fan-out semantics apply.
                if bad_slice_id == "":
                    assert resp.status_code == 200
                    return
                assert resp.status_code == 400, (
                    f"slice_id={bad_slice_id!r} must reject with 400; got {resp.status_code}"
                )
                mock_gw_client.heartbeat_session_by_container.assert_not_called()

    def test_heartbeat_sibling_slices_do_not_share_throttle(self, client, app):
        """Sibling slices with the same role each fan out independently (#2451).

        The dedup-amplification cap (#2076 NB2) is keyed per role to
        bound a single hot-looping agent. Without slice-scoping the
        throttle key, slice-2 reviewer-code's fan-out would suppress
        slice-3 reviewer-code's fan-out for 30 s, leaving slice-3's
        gateway session unrefreshed even though they are independent
        agents in independent pods.
        """
        with app.test_request_context():
            mock_gw_client = MagicMock()
            with (
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
                patch(
                    "gateway_client.get_gateway_client",
                    return_value=mock_gw_client,
                ),
                patch(
                    "routes.messages._GATEWAY_FANOUT_MIN_INTERVAL_SECONDS",
                    300.0,
                ),
            ):
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                for slice_id in ("slice-2", "slice-3"):
                    resp = client.post(
                        "/api/v1/pipelines/test-pipeline/heartbeat",
                        json={
                            "from_role": "reviewer_code",
                            "state": "WORKING",
                            "slice_id": slice_id,
                        },
                    )
                    assert resp.status_code == 200
                assert mock_gw_client.heartbeat_session_by_container.call_count == 2
                fan_out_calls = [
                    call.args[0]
                    for call in mock_gw_client.heartbeat_session_by_container.call_args_list
                ]
                assert fan_out_calls == [
                    "egg-agent-test-pipeline-slice-2-reviewer-code",
                    "egg-agent-test-pipeline-slice-3-reviewer-code",
                ]

    def test_heartbeat_fan_out_throttle_caps_dedup_amplification(self, client, app):
        """#2076 NB2: dedup'd hot-loops cannot amplify into the gateway.

        The dedup early-return path bypasses the per-role rate limit by
        design (#1897 NB1: dedup'd heartbeats are no-ops and must not
        consume rate budget).  Without a separate cap, a misbehaving
        agent hot-looping with identical state could fan out a gateway
        session refresh on every call.  ``_refresh_gateway_session``
        applies a per-role cooldown via
        ``HeartbeatCoordinator.should_fan_out_gateway_session`` to bound
        amplification.

        Five back-to-back identical heartbeats (1 fresh + 4 dedup'd)
        within the cooldown window MUST produce exactly one fan-out.
        """
        with app.test_request_context():
            mock_gw_client = MagicMock()
            with (
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
                patch(
                    "gateway_client.get_gateway_client",
                    return_value=mock_gw_client,
                ),
                # Cooldown well above realistic test wall-clock so all
                # five posts fall inside the same window.
                patch(
                    "routes.messages._GATEWAY_FANOUT_MIN_INTERVAL_SECONDS",
                    300.0,
                ),
            ):
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                role = "fanout-throttle-hotloop-role"
                for _ in range(5):
                    resp = client.post(
                        "/api/v1/pipelines/test-pipeline/heartbeat",
                        json={"from_role": role, "state": "WORKING"},
                    )
                    assert resp.status_code == 200
                assert mock_gw_client.heartbeat_session_by_container.call_count == 1

    def test_heartbeat_fan_out_throttle_resumes_after_window(self, client, app):
        """#2076 NB2: cooldown is a throttle, not a one-shot mute.

        After the per-role cooldown elapses, the next heartbeat MUST
        fan out again — otherwise a long-running agent in a single
        state would fan out exactly once and then silently age out of
        the gateway's 60-minute idle window.

        Uses a tiny cooldown + ``time.sleep`` rather than mocking
        ``time.time`` so the patch doesn't ripple through unrelated
        callers (Flask internals, gateway client) inside the request
        scope.  Uses ``WAITING_FOR_EVENT`` (dedup-exempt) so this
        exercise also pins the throttle on the post-rate-limit fan-out
        site, not just the dedup early-return.
        """
        with app.test_request_context():
            mock_gw_client = MagicMock()
            with (
                patch(
                    "routes.messages.get_state_store_for_pipeline"
                ) as mock_get_store_for_pipeline,
                patch(
                    "gateway_client.get_gateway_client",
                    return_value=mock_gw_client,
                ),
                patch(
                    "routes.messages._GATEWAY_FANOUT_MIN_INTERVAL_SECONDS",
                    0.05,
                ),
            ):
                mock_get_store_for_pipeline.return_value = (
                    MagicMock(),
                    _make_pipeline_mock(),
                )
                role = "fanout-throttle-window-role"
                # First heartbeat — fans out.
                resp1 = client.post(
                    "/api/v1/pipelines/test-pipeline/heartbeat",
                    json={"from_role": role, "state": "WAITING_FOR_EVENT"},
                )
                assert resp1.status_code == 200
                assert mock_gw_client.heartbeat_session_by_container.call_count == 1

                # Inside the 50 ms cooldown — no additional fan-out.
                resp2 = client.post(
                    "/api/v1/pipelines/test-pipeline/heartbeat",
                    json={"from_role": role, "state": "WAITING_FOR_EVENT"},
                )
                assert resp2.status_code == 200
                assert mock_gw_client.heartbeat_session_by_container.call_count == 1

                # Past the cooldown — fan-out fires again.
                time.sleep(0.07)
                resp3 = client.post(
                    "/api/v1/pipelines/test-pipeline/heartbeat",
                    json={"from_role": role, "state": "WAITING_FOR_EVENT"},
                )
                assert resp3.status_code == 200
                assert mock_gw_client.heartbeat_session_by_container.call_count == 2


class TestWaitTimeoutFloorRegression:
    """Plan non-blocking: ``timeout <= 0`` is silently coerced to 1s
    in routes/messages.py (see line 382-385). Pin the current behavior
    so a future refactor doesn't accidentally return immediately on
    timeout=0 (which would make /messages/wait behave like the
    non-blocking /messages endpoint).
    """

    def test_timeout_zero_coerced_to_1s_minimum(self, client, app):
        """A caller passing timeout=0 MUST still observe blocking
        semantics for >= 1s rather than returning instantly.

        This is a surprising-but-documented behavior — the wait endpoint
        is explicitly blocking; a caller that wants non-blocking should
        use /messages instead.
        """
        import time as _t

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
                    "?for=CONSENSUS_CONFIRMED&timeout=0"
                )
                elapsed = _t.monotonic() - start
        assert resp.status_code == 200
        # Coerced to 1s — expect the call to block at least close to 1s.
        # Allow generous upper bound (3s) to tolerate slow CI.
        assert 0.8 <= elapsed <= 3.0, (
            f"timeout=0 elapsed={elapsed:.2f}s; expected ~1s floor "
            "per routes/messages.py:382-385. If this drops to ~0s, "
            "the silent floor has been removed — update the docstring."
        )
