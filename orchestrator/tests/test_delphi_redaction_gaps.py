"""Gap tests for Delphi redaction of CONSENSUS_PROPOSE messages.

Covers edge cases and gaps identified beyond the existing test suite
in test_messages.py, specifically targeting the redaction-based Delphi
filter introduced to fix the deadlock in issue #1522 / PR #1525.

Gaps covered:
- Redaction clears after NACK (not just ACK)
- Dual-role agent (tester: producer + reviewer) polling
- Re-proposal resets redaction for reviewers
- Empty metadata dict on PROPOSE message
- Redaction with since_id pagination
- Multiple reviewers with different evaluation states
- Redacted message still counts in message count
- Redaction of proposals from multiple producers in one poll
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


def _make_tracker_and_store(edges, agents, proposals=None):
    """Helper to create a tracker and message store with given config.

    Args:
        edges: List of ReviewEdge instances.
        agents: List of agent role strings to register.
        proposals: List of (role, payload) tuples for proposals to record.

    Returns:
        (tracker, store) tuple.
    """
    from peer_consensus import PeerConsensusTracker
    from review_graph import ReviewGraph

    graph = ReviewGraph(edges)
    tracker = PeerConsensusTracker("test-pipeline", graph, cooldown_seconds=0)
    for agent in agents:
        tracker.register_agent(agent)

    if proposals:
        for role, payload in proposals:
            tracker.handle_propose(role, payload)

    store = MessageStore()
    return tracker, store


def _poll_messages(client, app, store, tracker, role, since_id=None):
    """Helper to poll messages with Delphi filtering applied.

    Returns:
        Parsed JSON response data dict.
    """
    url = f"/api/v1/pipelines/test-pipeline/messages?role={role}"
    if since_id:
        url += f"&since_id={since_id}"

    with app.test_request_context():
        with (
            patch("routes.messages.get_message_store", return_value=store),
            patch("routes.messages.get_state_store_for_pipeline") as mock_get_store_for_pipeline,
            patch("peer_consensus.get_peer_consensus_tracker", return_value=tracker),
        ):
            mock_get_store_for_pipeline.return_value = (MagicMock(), _make_pipeline_mock())
            resp = client.get(url)
            return json.loads(resp.data)


class TestDelphiRedactionAfterNACK:
    """NACK should also clear redaction (has_reviewed returns True for NACK)."""

    def test_propose_visible_after_nack(self, client, app):
        """After a reviewer NACKs, redaction should be lifted."""
        from review_graph import ReviewCriticality, ReviewEdge

        tracker, store = _make_tracker_and_store(
            edges=[ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL)],
            agents=["coder", "reviewer_code"],
            proposals=[
                (
                    "coder",
                    {
                        "summary": "Implemented auth",
                        "artifacts": ["src/auth.py"],
                        "commit_sha": "abc123",
                    },
                )
            ],
        )

        # Reviewer NACKs (not ACKs)
        tracker.handle_nack(
            "reviewer_code",
            "coder",
            {
                "reason": "Missing error handling",
                "artifact_references": ["src/auth.py"],
            },
        )

        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal from coder",
                body="Full self-assessment body",
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

        data = _poll_messages(client, app, store, tracker, "reviewer_code")

        # After NACK, proposal should be fully visible (not redacted)
        assert data["data"]["count"] == 1
        msg = data["data"]["messages"][0]
        assert msg["body"] == "Full self-assessment body"
        assert msg["metadata"]["payload"]["summary"] == "Implemented auth"
        assert "delphi_redacted" not in msg["metadata"]


class TestDualRoleRedaction:
    """Tester is both producer and reviewer of coder in implement phase."""

    def test_dual_role_sees_redacted_coder_proposal(self, client, app):
        """Tester (reviewer of coder) should see redacted coder PROPOSE
        before evaluating, even though tester is also a producer."""
        from review_graph import ReviewCriticality, ReviewEdge

        tracker, store = _make_tracker_and_store(
            edges=[
                ReviewEdge("tester", "coder", ReviewCriticality.CRITICAL),
                ReviewEdge("reviewer_code", "tester", ReviewCriticality.CRITICAL),
            ],
            agents=["coder", "tester", "reviewer_code"],
            proposals=[
                (
                    "coder",
                    {
                        "summary": "Implemented feature",
                        "artifacts": ["src/feature.py"],
                        "commit_sha": "abc123",
                    },
                )
            ],
        )

        # Also add tester's own proposal (tester is a producer too)
        tracker.handle_propose(
            "tester",
            {
                "summary": "Added tests",
                "artifacts": ["tests/test_feature.py"],
                "commit_sha": "def456",
            },
        )

        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal from coder",
                body="Coder self-assessment",
                metadata={
                    "payload": {
                        "summary": "Implemented feature",
                        "artifacts": ["src/feature.py"],
                        "version": 1,
                        "commit_sha": "abc123",
                    }
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
                    "payload": {
                        "summary": "Added tests",
                        "artifacts": ["tests/test_feature.py"],
                        "version": 1,
                        "commit_sha": "def456",
                    }
                },
            )
        )

        data = _poll_messages(client, app, store, tracker, "tester")

        # Tester should see both proposals
        assert data["data"]["count"] == 2

        msgs = {m["from_role"]: m for m in data["data"]["messages"]}

        # Coder proposal: redacted (tester reviews coder, hasn't evaluated yet)
        coder_msg = msgs["coder"]
        assert coder_msg["body"] == ""
        assert coder_msg["metadata"]["delphi_redacted"] is True
        assert "summary" not in coder_msg["metadata"]["payload"]

        # Tester's own proposal: NOT redacted (tester doesn't review tester)
        tester_msg = msgs["tester"]
        assert tester_msg["body"] == "Tester self-assessment"
        assert "delphi_redacted" not in tester_msg["metadata"]

    def test_dual_role_sees_full_coder_proposal_after_ack(self, client, app):
        """After tester ACKs coder, tester should see full coder PROPOSE."""
        from review_graph import ReviewCriticality, ReviewEdge

        tracker, store = _make_tracker_and_store(
            edges=[
                ReviewEdge("tester", "coder", ReviewCriticality.CRITICAL),
            ],
            agents=["coder", "tester"],
            proposals=[
                (
                    "coder",
                    {
                        "summary": "Implemented feature",
                        "artifacts": ["src/feature.py"],
                        "commit_sha": "abc123",
                    },
                )
            ],
        )

        # Tester ACKs the coder
        tracker.handle_ack(
            "tester",
            "coder",
            {"artifact_references": ["src/feature.py"]},
        )

        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal from coder",
                body="Full coder assessment",
                metadata={
                    "payload": {
                        "summary": "Implemented feature",
                        "artifacts": ["src/feature.py"],
                        "version": 1,
                        "commit_sha": "abc123",
                    }
                },
            )
        )

        data = _poll_messages(client, app, store, tracker, "tester")

        assert data["data"]["count"] == 1
        msg = data["data"]["messages"][0]
        assert msg["body"] == "Full coder assessment"
        assert "delphi_redacted" not in msg["metadata"]


class TestRedactionWithEmptyMetadata:
    """Edge case: PROPOSE message with empty metadata dict."""

    def test_empty_metadata_redaction(self, client, app):
        """PROPOSE with empty metadata should still get delphi_redacted flag."""
        from review_graph import ReviewCriticality, ReviewEdge

        tracker, store = _make_tracker_and_store(
            edges=[ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL)],
            agents=["coder", "reviewer_code"],
            proposals=[
                (
                    "coder",
                    {
                        "summary": "Fix",
                        "artifacts": ["src/fix.py"],
                        "commit_sha": "abc123",
                    },
                )
            ],
        )

        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal from coder",
                body="Some body",
                metadata={},
            )
        )

        data = _poll_messages(client, app, store, tracker, "reviewer_code")

        assert data["data"]["count"] == 1
        msg = data["data"]["messages"][0]
        assert msg["body"] == ""
        assert msg["metadata"]["delphi_redacted"] is True
        # No payload key since metadata was empty
        assert "payload" not in msg["metadata"]


class TestRedactionWithSinceId:
    """Redaction should work correctly with since_id pagination."""

    def test_redaction_applied_after_since_id_filter(self, client, app):
        """Messages retrieved via since_id should still be Delphi-redacted."""
        from review_graph import ReviewCriticality, ReviewEdge

        tracker, store = _make_tracker_and_store(
            edges=[ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL)],
            agents=["coder", "reviewer_code"],
            proposals=[
                (
                    "coder",
                    {
                        "summary": "Implemented auth",
                        "artifacts": ["src/auth.py"],
                        "commit_sha": "abc123",
                    },
                )
            ],
        )

        # Add an initial status message (not a proposal)
        first_msg = Message(
            pipeline_id="test-pipeline",
            from_role="orchestrator",
            to_role="all",
            message_type=MessageType.STATUS,
            subject="Phase started",
        )
        store.add_message(first_msg)

        # Add the proposal after
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal from coder",
                body="Should be redacted",
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

        # Poll with since_id to skip the first message
        data = _poll_messages(client, app, store, tracker, "reviewer_code", since_id=first_msg.id)

        # Should get only the proposal, and it should be redacted
        assert data["data"]["count"] == 1
        msg = data["data"]["messages"][0]
        assert msg["message_type"] == "CONSENSUS_PROPOSE"
        assert msg["body"] == ""
        assert msg["metadata"]["delphi_redacted"] is True


class TestMultipleReviewersPartialEvaluation:
    """Multiple reviewers with different evaluation states."""

    def test_one_reviewer_evaluated_one_not(self, client, app):
        """When two reviewers exist, each should see redaction based on
        their own evaluation state, not the other reviewer's."""
        from review_graph import ReviewCriticality, ReviewEdge

        tracker, store = _make_tracker_and_store(
            edges=[
                ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
                ReviewEdge("reviewer_contract", "coder", ReviewCriticality.CRITICAL),
            ],
            agents=["coder", "reviewer_code", "reviewer_contract"],
            proposals=[
                (
                    "coder",
                    {
                        "summary": "Implemented auth",
                        "artifacts": ["src/auth.py"],
                        "commit_sha": "abc123",
                    },
                )
            ],
        )

        # Only reviewer_contract has ACKed
        tracker.handle_ack(
            "reviewer_contract",
            "coder",
            {"artifact_references": ["src/auth.py"]},
        )

        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal from coder",
                body="Full assessment",
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

        # reviewer_code: has NOT evaluated -> should see redacted
        data_code = _poll_messages(client, app, store, tracker, "reviewer_code")
        assert data_code["data"]["count"] == 1
        msg_code = data_code["data"]["messages"][0]
        assert msg_code["body"] == ""
        assert msg_code["metadata"]["delphi_redacted"] is True

        # reviewer_contract: HAS evaluated -> should see full
        data_contract = _poll_messages(client, app, store, tracker, "reviewer_contract")
        assert data_contract["data"]["count"] == 1
        msg_contract = data_contract["data"]["messages"][0]
        assert msg_contract["body"] == "Full assessment"
        assert "delphi_redacted" not in msg_contract["metadata"]


class TestRedactedMessageCount:
    """Redacted messages should still be counted in response."""

    def test_redacted_messages_included_in_count(self, client, app):
        """Redacted proposals should still appear in message count
        (unlike the old withholding behavior that dropped them entirely)."""
        from review_graph import ReviewCriticality, ReviewEdge

        tracker, store = _make_tracker_and_store(
            edges=[ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL)],
            agents=["coder", "reviewer_code"],
            proposals=[
                (
                    "coder",
                    {
                        "summary": "Implemented auth",
                        "artifacts": ["src/auth.py"],
                        "commit_sha": "abc123",
                    },
                )
            ],
        )

        # Add a regular status message
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="orchestrator",
                to_role="all",
                message_type=MessageType.STATUS,
                subject="Phase started",
            )
        )
        # Add proposal
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal from coder",
                body="Self-assessment",
                metadata={
                    "payload": {
                        "summary": "Implemented auth",
                        "version": 1,
                        "commit_sha": "abc123",
                    }
                },
            )
        )

        data = _poll_messages(client, app, store, tracker, "reviewer_code")

        # Both messages should be present (status + redacted proposal)
        assert data["data"]["count"] == 2

        # Find the proposal
        proposal = next(
            m for m in data["data"]["messages"] if m["message_type"] == "CONSENSUS_PROPOSE"
        )
        assert proposal["metadata"]["delphi_redacted"] is True
        # Status message should be unaffected
        status = next(m for m in data["data"]["messages"] if m["message_type"] == "STATUS")
        assert "delphi_redacted" not in status["metadata"]


class TestAdvisoryEdgeRedaction:
    """Advisory review edges should also trigger Delphi redaction."""

    def test_advisory_reviewer_sees_redacted_proposal(self, client, app):
        """A reviewer with an ADVISORY edge should still see redacted
        proposals until they've evaluated."""
        from review_graph import ReviewCriticality, ReviewEdge

        tracker, store = _make_tracker_and_store(
            edges=[
                ReviewEdge("reviewer_code", "documenter", ReviewCriticality.ADVISORY),
            ],
            agents=["documenter", "reviewer_code"],
            proposals=[
                (
                    "documenter",
                    {
                        "summary": "Updated docs",
                        "artifacts": ["docs/api.md"],
                        "commit_sha": "doc123",
                    },
                )
            ],
        )

        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="documenter",
                to_role="all",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal from documenter",
                body="Documentation self-assessment",
                metadata={
                    "payload": {
                        "summary": "Updated docs",
                        "artifacts": ["docs/api.md"],
                        "version": 1,
                        "commit_sha": "doc123",
                    }
                },
            )
        )

        data = _poll_messages(client, app, store, tracker, "reviewer_code")

        assert data["data"]["count"] == 1
        msg = data["data"]["messages"][0]
        # Advisory edge still triggers redaction
        assert msg["body"] == ""
        assert msg["metadata"]["delphi_redacted"] is True


class TestNonProposeMessagesNotRedacted:
    """Non-CONSENSUS_PROPOSE messages should never be redacted."""

    @pytest.mark.parametrize(
        "message_type",
        [
            MessageType.CONSENSUS_ACK,
            MessageType.CONSENSUS_NACK,
            MessageType.CONSENSUS_CONFIRMED,
            MessageType.CONSENSUS_RE_REVIEW,
            MessageType.STATUS,
            MessageType.PROGRESS,
        ],
    )
    def test_non_propose_message_unredacted(self, client, app, message_type):
        """Only CONSENSUS_PROPOSE should be redacted."""
        from review_graph import ReviewCriticality, ReviewEdge

        tracker, store = _make_tracker_and_store(
            edges=[ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL)],
            agents=["coder", "reviewer_code"],
            proposals=[
                (
                    "coder",
                    {
                        "summary": "Fix",
                        "artifacts": ["src/fix.py"],
                        "commit_sha": "abc123",
                    },
                )
            ],
        )

        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=message_type,
                subject=f"Test {message_type}",
                body="Full body content",
                metadata={"detail": "should not be redacted"},
            )
        )

        data = _poll_messages(client, app, store, tracker, "reviewer_code")

        assert data["data"]["count"] == 1
        msg = data["data"]["messages"][0]
        assert msg["body"] == "Full body content"
        assert "delphi_redacted" not in msg["metadata"]


class TestRedactionPayloadFieldPreservation:
    """Verify exactly which payload fields survive redaction."""

    def test_only_version_and_commit_sha_preserved(self, client, app):
        """Redacted payload should ONLY contain version and commit_sha."""
        from review_graph import ReviewCriticality, ReviewEdge

        tracker, store = _make_tracker_and_store(
            edges=[ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL)],
            agents=["coder", "reviewer_code"],
            proposals=[
                (
                    "coder",
                    {
                        "summary": "Big feature",
                        "artifacts": ["src/big.py"],
                        "commit_sha": "big123",
                    },
                )
            ],
        )

        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal from coder",
                body="Detailed assessment",
                metadata={
                    "payload": {
                        "summary": "Big feature impl",
                        "artifacts": ["src/big.py", "src/helper.py"],
                        "commit_sha": "big123",
                        "version": 3,
                        "attestation": {
                            "commit_shas": ["big123"],
                            "files_changed": ["src/big.py"],
                        },
                        "extra_field": "should be stripped",
                    }
                },
            )
        )

        data = _poll_messages(client, app, store, tracker, "reviewer_code")

        msg = data["data"]["messages"][0]
        payload = msg["metadata"]["payload"]

        # Only these two should be present
        assert set(payload.keys()) == {"version", "commit_sha"}
        assert payload["version"] == 3
        assert payload["commit_sha"] == "big123"


class TestRedactionWithTargetedMessages:
    """Mix of broadcast and targeted messages with Delphi filtering."""

    def test_targeted_ack_not_affected_by_delphi(self, client, app):
        """An ACK message targeted to a producer should not be
        affected by Delphi filtering even if it mentions proposals."""
        from review_graph import ReviewCriticality, ReviewEdge

        tracker, store = _make_tracker_and_store(
            edges=[
                ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
                ReviewEdge("tester", "coder", ReviewCriticality.CRITICAL),
            ],
            agents=["coder", "tester", "reviewer_code"],
            proposals=[
                (
                    "coder",
                    {
                        "summary": "Feature",
                        "artifacts": ["src/feat.py"],
                        "commit_sha": "abc123",
                    },
                )
            ],
        )

        # Add broadcast PROPOSE (will be redacted for reviewer_code)
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal from coder",
                body="Assessment",
                metadata={
                    "payload": {
                        "summary": "Feature",
                        "version": 1,
                        "commit_sha": "abc123",
                    }
                },
            )
        )
        # Add targeted ACK to coder (not for reviewer_code, but coder polls)
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="tester",
                to_role="coder",
                message_type=MessageType.CONSENSUS_ACK,
                subject="ACK from tester",
                body="Tests pass",
            )
        )

        # Coder should see both: their PROPOSE (no edge to self) and the ACK
        data = _poll_messages(client, app, store, tracker, "coder")
        # Coder sees broadcast PROPOSE + targeted ACK
        assert data["data"]["count"] == 2
        ack = next(m for m in data["data"]["messages"] if m["message_type"] == "CONSENSUS_ACK")
        assert ack["body"] == "Tests pass"
        assert "delphi_redacted" not in ack["metadata"]


class TestReProposalResetsRedaction:
    """Re-proposal should reset redaction for reviewers who previously ACKed."""

    def test_reviewer_sees_redacted_after_reproposal(self, client, app):
        """After a producer re-proposes, a reviewer who previously ACKed
        should see redacted proposals again (their ACK was invalidated)."""
        from review_graph import ReviewCriticality, ReviewEdge

        tracker, store = _make_tracker_and_store(
            edges=[
                ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
                ReviewEdge("reviewer_contract", "coder", ReviewCriticality.CRITICAL),
            ],
            agents=["coder", "reviewer_code", "reviewer_contract"],
            proposals=[
                (
                    "coder",
                    {
                        "summary": "Implemented auth v1",
                        "artifacts": ["src/auth.py"],
                        "commit_sha": "v1abc",
                    },
                )
            ],
        )

        # reviewer_code ACKs v1
        tracker.handle_ack(
            "reviewer_code",
            "coder",
            {"artifact_references": ["src/auth.py"]},
        )

        # reviewer_contract NACKs v1, triggering a re-proposal
        tracker.handle_nack(
            "reviewer_contract",
            "coder",
            {
                "reason": "Missing error handling",
                "artifact_references": ["src/auth.py"],
            },
        )

        # Coder re-proposes (v2) — invalidates reviewer_code's ACK
        tracker.handle_re_propose(
            "coder",
            {
                "summary": "Implemented auth v2",
                "artifacts": ["src/auth.py"],
                "commit_sha": "v2def",
            },
            changed_artifacts=["src/auth.py"],
        )

        # Add the v2 proposal message to the store
        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal from coder v2",
                body="Updated self-assessment for v2",
                metadata={
                    "payload": {
                        "summary": "Implemented auth v2",
                        "artifacts": ["src/auth.py"],
                        "version": 2,
                        "commit_sha": "v2def",
                    }
                },
            )
        )

        # reviewer_code's ACK was invalidated by re-proposal, so
        # has_reviewed returns False → proposal should be redacted again
        data = _poll_messages(client, app, store, tracker, "reviewer_code")
        assert data["data"]["count"] == 1
        msg = data["data"]["messages"][0]
        assert msg["body"] == ""
        assert msg["metadata"]["delphi_redacted"] is True
        assert set(msg["metadata"]["payload"].keys()) == {"version", "commit_sha"}

    def test_reviewer_sees_full_after_re_ack(self, client, app):
        """After re-ACKing a re-proposal, the reviewer should see unredacted."""
        from review_graph import ReviewCriticality, ReviewEdge

        tracker, store = _make_tracker_and_store(
            edges=[ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL)],
            agents=["coder", "reviewer_code"],
            proposals=[
                (
                    "coder",
                    {
                        "summary": "Feature v1",
                        "artifacts": ["src/feat.py"],
                        "commit_sha": "v1abc",
                    },
                )
            ],
        )

        # ACK v1
        tracker.handle_ack(
            "reviewer_code",
            "coder",
            {"artifact_references": ["src/feat.py"]},
        )

        # Re-propose (conservative invalidation — no changed_artifacts)
        tracker.handle_re_propose(
            "coder",
            {
                "summary": "Feature v2",
                "artifacts": ["src/feat.py"],
                "commit_sha": "v2def",
            },
        )

        # Re-ACK v2
        tracker.handle_ack(
            "reviewer_code",
            "coder",
            {"artifact_references": ["src/feat.py"]},
        )

        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal from coder v2",
                body="Full v2 assessment",
                metadata={
                    "payload": {
                        "summary": "Feature v2",
                        "artifacts": ["src/feat.py"],
                        "version": 2,
                        "commit_sha": "v2def",
                    }
                },
            )
        )

        data = _poll_messages(client, app, store, tracker, "reviewer_code")
        assert data["data"]["count"] == 1
        msg = data["data"]["messages"][0]
        assert msg["body"] == "Full v2 assessment"
        assert "delphi_redacted" not in msg["metadata"]


class TestMultiProducerRedaction:
    """Redaction of proposals from multiple producers in one poll."""

    def test_both_proposals_redacted_for_unevaluated_reviewer(self, client, app):
        """When a reviewer reviews multiple producers, proposals from all
        unevaluated producers should be independently redacted."""
        from review_graph import ReviewCriticality, ReviewEdge

        tracker, store = _make_tracker_and_store(
            edges=[
                ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
                ReviewEdge("reviewer_code", "tester", ReviewCriticality.CRITICAL),
            ],
            agents=["coder", "tester", "reviewer_code"],
            proposals=[
                (
                    "coder",
                    {
                        "summary": "Implemented feature",
                        "artifacts": ["src/feature.py"],
                        "commit_sha": "coder123",
                    },
                ),
                (
                    "tester",
                    {
                        "summary": "Added tests",
                        "artifacts": ["tests/test_feature.py"],
                        "commit_sha": "tester456",
                    },
                ),
            ],
        )

        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal from coder",
                body="Coder self-assessment",
                metadata={
                    "payload": {
                        "summary": "Implemented feature",
                        "artifacts": ["src/feature.py"],
                        "version": 1,
                        "commit_sha": "coder123",
                    }
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
                    "payload": {
                        "summary": "Added tests",
                        "artifacts": ["tests/test_feature.py"],
                        "version": 1,
                        "commit_sha": "tester456",
                    }
                },
            )
        )

        data = _poll_messages(client, app, store, tracker, "reviewer_code")

        assert data["data"]["count"] == 2
        msgs = {m["from_role"]: m for m in data["data"]["messages"]}

        # Both should be redacted (reviewer hasn't evaluated either)
        for role in ("coder", "tester"):
            assert msgs[role]["body"] == ""
            assert msgs[role]["metadata"]["delphi_redacted"] is True

    def test_partial_evaluation_across_producers(self, client, app):
        """After ACKing one producer but not the other, only the
        unevaluated producer's proposal should remain redacted."""
        from review_graph import ReviewCriticality, ReviewEdge

        tracker, store = _make_tracker_and_store(
            edges=[
                ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
                ReviewEdge("reviewer_code", "tester", ReviewCriticality.CRITICAL),
            ],
            agents=["coder", "tester", "reviewer_code"],
            proposals=[
                (
                    "coder",
                    {
                        "summary": "Implemented feature",
                        "artifacts": ["src/feature.py"],
                        "commit_sha": "coder123",
                    },
                ),
                (
                    "tester",
                    {
                        "summary": "Added tests",
                        "artifacts": ["tests/test_feature.py"],
                        "commit_sha": "tester456",
                    },
                ),
            ],
        )

        # ACK only tester
        tracker.handle_ack(
            "reviewer_code",
            "tester",
            {"artifact_references": ["tests/test_feature.py"]},
        )

        store.add_message(
            Message(
                pipeline_id="test-pipeline",
                from_role="coder",
                to_role="all",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal from coder",
                body="Coder self-assessment",
                metadata={
                    "payload": {
                        "summary": "Implemented feature",
                        "artifacts": ["src/feature.py"],
                        "version": 1,
                        "commit_sha": "coder123",
                    }
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
                    "payload": {
                        "summary": "Added tests",
                        "artifacts": ["tests/test_feature.py"],
                        "version": 1,
                        "commit_sha": "tester456",
                    }
                },
            )
        )

        data = _poll_messages(client, app, store, tracker, "reviewer_code")

        assert data["data"]["count"] == 2
        msgs = {m["from_role"]: m for m in data["data"]["messages"]}

        # Coder: still redacted (not evaluated)
        assert msgs["coder"]["body"] == ""
        assert msgs["coder"]["metadata"]["delphi_redacted"] is True

        # Tester: unredacted (ACKed)
        assert msgs["tester"]["body"] == "Tester self-assessment"
        assert "delphi_redacted" not in msgs["tester"]["metadata"]
