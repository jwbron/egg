"""
Tests for BRC consensus message phase propagation (issue #1580).

Verifies that:
- _resolve_pipeline_phase returns current phase or falls back to "implement"
- All consensus signal handlers set phase on every Message they create
- _write_brc_history skips messages with phase=None (pre-fix behavior)
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Mock heavy dependencies before importing modules under test
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

from message_store import Message, MessageStore, MessageType
from models import Pipeline, PipelinePhase, PipelineStatus

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _make_pipeline(
    pipeline_id="issue-42",
    issue_number=42,
    repo="owner/repo",
    branch="egg/issue-42",
    phase=PipelinePhase.IMPLEMENT,
):
    """Create a Pipeline for testing."""
    return Pipeline(
        id=pipeline_id,
        issue_number=issue_number,
        repo=repo,
        branch=branch,
        mode="issue",
        status=PipelineStatus.RUNNING,
        current_phase=phase,
    )


# Default slice_id seeded onto implement-phase BRC messages so the
# post-#2548 hard-switchover writer accepts them. Tests that need an
# unattributed (missing-slice_id) message must set ``slice_id=None``
# AND avoid passing ``metadata={"slice_id": ...}``.
_DEFAULT_IMPLEMENT_SLICE_ID = "slice-1"


def _make_brc_message(
    pipeline_id="issue-42",
    from_role="coder",
    message_type=MessageType.CONSENSUS_PROPOSE,
    subject="Proposal from coder",
    body="Implemented the feature",
    phase=None,
    timestamp=None,
    metadata=None,
    slice_id="__default__",
):
    """Create a BRC Message for testing.  Defaults to phase=None to mimic pre-fix.

    For implement-phase messages, ``metadata['slice_id']`` is auto-stamped
    to ``slice-1`` (#2548 hard switchover) unless the caller passes an
    explicit ``slice_id`` or sets the key in ``metadata`` directly.
    """
    md = dict(metadata or {})
    if slice_id == "__default__":
        if phase == "implement" and "slice_id" not in md:
            md["slice_id"] = _DEFAULT_IMPLEMENT_SLICE_ID
    elif slice_id is not None:
        md.setdefault("slice_id", slice_id)
    return Message(
        pipeline_id=pipeline_id,
        from_role=from_role,
        to_role="all",
        message_type=message_type,
        subject=subject,
        body=body,
        phase=phase,
        timestamp=timestamp or datetime(2026, 4, 8, 12, 0, 0, tzinfo=UTC),
        metadata=md,
    )


@pytest.fixture
def app():
    """Create a test Flask app with the signals blueprint."""
    from flask import Flask
    from routes.signals import signals_bp

    app = Flask(__name__)
    app.register_blueprint(signals_bp)
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def mock_pipeline():
    """Create a mock pipeline in IMPLEMENT phase."""
    return _make_pipeline()


# ---------------------------------------------------------------------------
# _resolve_pipeline_phase
# ---------------------------------------------------------------------------


class TestResolvePipelinePhase:
    """Tests for the _resolve_pipeline_phase helper."""

    def test_returns_current_phase_value(self):
        """Returns the pipeline's current_phase.value when loading succeeds."""
        from routes.signals import _resolve_pipeline_phase

        pipeline = _make_pipeline(phase=PipelinePhase.IMPLEMENT)
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline

        with patch("routes.signals.get_state_store", return_value=mock_store):
            result = _resolve_pipeline_phase("issue-42", Path("/tmp/repo"))

        assert result == "implement"

    def test_returns_plan_phase(self):
        """Returns 'plan' when the pipeline is in the plan phase."""
        from routes.signals import _resolve_pipeline_phase

        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline

        with patch("routes.signals.get_state_store", return_value=mock_store):
            result = _resolve_pipeline_phase("issue-42", Path("/tmp/repo"))

        assert result == "plan"

    def test_returns_refine_phase(self):
        """Returns 'refine' when the pipeline is in the refine phase."""
        from routes.signals import _resolve_pipeline_phase

        pipeline = _make_pipeline(phase=PipelinePhase.REFINE)
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline

        with patch("routes.signals.get_state_store", return_value=mock_store):
            result = _resolve_pipeline_phase("issue-42", Path("/tmp/repo"))

        assert result == "refine"

    def test_returns_pr_phase(self):
        """Returns 'pr' when the pipeline is in the PR phase."""
        from routes.signals import _resolve_pipeline_phase

        pipeline = _make_pipeline(phase=PipelinePhase.PR)
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline

        with patch("routes.signals.get_state_store", return_value=mock_store):
            result = _resolve_pipeline_phase("issue-42", Path("/tmp/repo"))

        assert result == "pr"

    def test_fallback_on_load_failure(self):
        """Falls back to 'implement' when state store raises an exception."""
        from routes.signals import _resolve_pipeline_phase

        mock_store = MagicMock()
        mock_store.load_pipeline.side_effect = FileNotFoundError("Pipeline not found")

        with patch("routes.signals.get_state_store", return_value=mock_store):
            result = _resolve_pipeline_phase("nonexistent", Path("/tmp/repo"))

        assert result == "implement"

    def test_fallback_on_state_store_error(self):
        """Falls back to 'implement' when get_state_store itself fails."""
        from routes.signals import _resolve_pipeline_phase

        with patch(
            "routes.signals.get_state_store",
            side_effect=RuntimeError("Store unavailable"),
        ):
            result = _resolve_pipeline_phase("issue-42", Path("/tmp/repo"))

        assert result == "implement"

    def test_fallback_on_attribute_error(self):
        """Falls back to 'implement' when pipeline lacks current_phase."""
        from routes.signals import _resolve_pipeline_phase

        mock_store = MagicMock()
        mock_pipeline = MagicMock()
        mock_pipeline.current_phase = None  # .value will raise
        mock_store.load_pipeline.return_value = mock_pipeline

        with patch("routes.signals.get_state_store", return_value=mock_store):
            result = _resolve_pipeline_phase("issue-42", Path("/tmp/repo"))

        assert result == "implement"

    def test_returns_string_not_enum(self):
        """Returns a plain string, not a PipelinePhase enum value."""
        from routes.signals import _resolve_pipeline_phase

        pipeline = _make_pipeline(phase=PipelinePhase.IMPLEMENT)
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline

        with patch("routes.signals.get_state_store", return_value=mock_store):
            result = _resolve_pipeline_phase("issue-42", Path("/tmp/repo"))

        assert isinstance(result, str)
        assert result == "implement"


# ---------------------------------------------------------------------------
# Signal handler phase propagation
# ---------------------------------------------------------------------------


class TestProposePhasePropagation:
    """handle_consensus_propose_signal sets phase on all Messages."""

    @patch("routes.signals._resolve_pipeline_phase", return_value="implement")
    @patch("routes.signals.resolve_worktree_path", return_value=Path("/tmp/wt"))
    @patch("routes.signals.get_state_store")
    def test_propose_message_has_phase(
        self,
        mock_get_store,
        mock_resolve_wt,
        mock_resolve_phase,
        app,
        mock_pipeline,
    ):
        """CONSENSUS_PROPOSE message includes the resolved phase."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store

        mock_tracker = MagicMock()
        mock_tracker.handle_propose.return_value = {"version": 1, "stale_reviewers": []}

        mock_msg_store = MagicMock()

        with (
            app.app_context(),
            patch("peer_consensus.get_peer_consensus_tracker", return_value=mock_tracker),
            patch("message_store.get_message_store", return_value=mock_msg_store),
        ):
            from routes.signals import handle_consensus_propose_signal

            response, status_code = handle_consensus_propose_signal(
                "issue-42",
                {
                    "agent_role": "coder",
                    "payload": {
                        "summary": "Implemented authentication module with JWT validation and session management",
                        "commit_sha": "abc123",
                    },
                },
                Path("/tmp/repo"),
            )

        assert status_code == 200

        # Inspect the Message passed to add_message
        assert mock_msg_store.add_message.called
        msg = mock_msg_store.add_message.call_args_list[0][0][0]
        assert msg.phase == "implement"
        assert msg.message_type == MessageType.CONSENSUS_PROPOSE

    @patch("routes.signals._resolve_pipeline_phase", return_value="implement")
    @patch("routes.signals.resolve_worktree_path", return_value=Path("/tmp/wt"))
    @patch("routes.signals.get_state_store")
    def test_re_propose_stale_reviewer_message_has_phase(
        self,
        mock_get_store,
        mock_resolve_wt,
        mock_resolve_phase,
        app,
        mock_pipeline,
    ):
        """RE_REVIEW message sent to stale reviewers includes phase."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store

        mock_tracker = MagicMock()
        mock_tracker.handle_re_propose.return_value = {
            "version": 2,
            "stale_reviewers": ["reviewer_code"],
        }

        mock_msg_store = MagicMock()

        with (
            app.app_context(),
            patch("peer_consensus.get_peer_consensus_tracker", return_value=mock_tracker),
            patch("message_store.get_message_store", return_value=mock_msg_store),
        ):
            from routes.signals import handle_consensus_propose_signal

            response, status_code = handle_consensus_propose_signal(
                "issue-42",
                {
                    "agent_role": "coder",
                    "payload": {
                        "summary": "Updated authentication module: fixed token expiry and added session refresh logic"
                    },
                    "changed_artifacts": ["file.py"],
                },
                Path("/tmp/repo"),
            )

        assert status_code == 200
        # Should have 2 messages: PROPOSE + RE_REVIEW
        assert mock_msg_store.add_message.call_count == 2

        propose_msg = mock_msg_store.add_message.call_args_list[0][0][0]
        assert propose_msg.phase == "implement"

        re_review_msg = mock_msg_store.add_message.call_args_list[1][0][0]
        assert re_review_msg.phase == "implement"
        assert re_review_msg.message_type == MessageType.CONSENSUS_RE_REVIEW


class TestAckPhasePropagation:
    """handle_consensus_ack_signal sets phase on all Messages."""

    @patch("routes.signals._resolve_pipeline_phase", return_value="implement")
    @patch("routes.signals.get_state_store")
    def test_ack_message_has_phase(
        self,
        mock_get_store,
        mock_resolve_phase,
        app,
        mock_pipeline,
    ):
        """CONSENSUS_ACK message includes the resolved phase."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store

        mock_tracker = MagicMock()
        mock_tracker.handle_ack.return_value = {"version": 1, "fully_acked": False}

        mock_msg_store = MagicMock()

        with (
            app.app_context(),
            patch("peer_consensus.get_peer_consensus_tracker", return_value=mock_tracker),
            patch("message_store.get_message_store", return_value=mock_msg_store),
        ):
            from routes.signals import handle_consensus_ack_signal

            response, status_code = handle_consensus_ack_signal(
                "issue-42",
                {
                    "agent_role": "reviewer_code",
                    "producer_role": "coder",
                    "ack_version": 1,
                    "payload": {
                        "reason": "Reviewed src/auth.py: token validation logic is correct, all branches covered by tests"
                    },
                },
                Path("/tmp/repo"),
            )

        assert status_code == 200
        msg = mock_msg_store.add_message.call_args_list[0][0][0]
        assert msg.phase == "implement"
        assert msg.message_type == MessageType.CONSENSUS_ACK

    @patch("routes.signals._resolve_pipeline_phase", return_value="implement")
    @patch("routes.signals.get_state_store")
    def test_ready_to_confirm_status_message_has_phase(
        self,
        mock_get_store,
        mock_resolve_phase,
        app,
        mock_pipeline,
    ):
        """STATUS message sent when a producer becomes ready-to-confirm
        includes phase.  The tracker reports newly-ready producers via
        ``newly_ready`` (post-#2078); the prior ``fully_acked`` flag is no
        longer the nudge gate."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store

        mock_tracker = MagicMock()
        mock_tracker.handle_ack.return_value = {
            "version": 1,
            "fully_acked": True,
            "newly_ready": [{"role": "coder", "version": 1}],
        }

        mock_msg_store = MagicMock()

        with (
            app.app_context(),
            patch("peer_consensus.get_peer_consensus_tracker", return_value=mock_tracker),
            patch("message_store.get_message_store", return_value=mock_msg_store),
        ):
            from routes.signals import handle_consensus_ack_signal

            response, status_code = handle_consensus_ack_signal(
                "issue-42",
                {
                    "agent_role": "reviewer_code",
                    "producer_role": "coder",
                    "ack_version": 1,
                    "payload": {
                        "reason": "Reviewed src/auth.py: token validation logic is correct, all branches covered by tests"
                    },
                },
                Path("/tmp/repo"),
            )

        assert status_code == 200
        # Should have 2 messages: ACK + STATUS (ready-to-confirm)
        assert mock_msg_store.add_message.call_count == 2

        ack_msg = mock_msg_store.add_message.call_args_list[0][0][0]
        assert ack_msg.phase == "implement"

        status_msg = mock_msg_store.add_message.call_args_list[1][0][0]
        assert status_msg.phase == "implement"
        assert status_msg.metadata.get("ready_to_confirm") is True


class TestNackPhasePropagation:
    """handle_consensus_nack_signal sets phase on its Message."""

    @patch("routes.signals._resolve_pipeline_phase", return_value="implement")
    @patch("routes.signals.get_state_store")
    def test_nack_message_has_phase(
        self,
        mock_get_store,
        mock_resolve_phase,
        app,
        mock_pipeline,
    ):
        """CONSENSUS_NACK message includes the resolved phase."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store

        mock_tracker = MagicMock()
        mock_tracker.handle_nack.return_value = {
            "reason": "Missing tests",
            "revision_count": 1,
        }

        mock_msg_store = MagicMock()

        with (
            app.app_context(),
            patch("peer_consensus.get_peer_consensus_tracker", return_value=mock_tracker),
            patch("message_store.get_message_store", return_value=mock_msg_store),
        ):
            from routes.signals import handle_consensus_nack_signal

            response, status_code = handle_consensus_nack_signal(
                "issue-42",
                {
                    "agent_role": "reviewer_code",
                    "producer_role": "coder",
                    "payload": {
                        "reason": "Missing unit tests for token expiry edge cases and invalid signature handling paths"
                    },
                },
                Path("/tmp/repo"),
            )

        assert status_code == 200
        msg = mock_msg_store.add_message.call_args_list[0][0][0]
        assert msg.phase == "implement"
        assert msg.message_type == MessageType.CONSENSUS_NACK


class TestWithdrawPhasePropagation:
    """handle_consensus_withdraw_signal sets phase on its Message."""

    @patch("routes.signals._resolve_pipeline_phase", return_value="implement")
    @patch("routes.signals.get_state_store")
    def test_withdraw_message_has_phase(
        self,
        mock_get_store,
        mock_resolve_phase,
        app,
        mock_pipeline,
    ):
        """CONSENSUS_WITHDRAW message includes the resolved phase."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store

        mock_tracker = MagicMock()
        mock_tracker.handle_withdraw.return_value = {"status": "withdrawn"}

        mock_msg_store = MagicMock()

        with (
            app.app_context(),
            patch("peer_consensus.get_peer_consensus_tracker", return_value=mock_tracker),
            patch("message_store.get_message_store", return_value=mock_msg_store),
        ):
            from routes.signals import handle_consensus_withdraw_signal

            response, status_code = handle_consensus_withdraw_signal(
                "issue-42",
                {
                    "agent_role": "coder",
                    "reason": "Withdrawing: discovered timing attack vulnerability in JWT comparison, need constant-time implementation",
                },
                Path("/tmp/repo"),
            )

        assert status_code == 200
        msg = mock_msg_store.add_message.call_args_list[0][0][0]
        assert msg.phase == "implement"
        assert msg.message_type == MessageType.CONSENSUS_WITHDRAW


class TestConfirmedPhasePropagation:
    """handle_consensus_confirmed_signal sets phase on its Messages."""

    @patch("routes.signals._resolve_pipeline_phase", return_value="implement")
    @patch("routes.signals._write_consensus_confirmed_marker")
    @patch("routes.signals.get_state_store")
    def test_confirmed_message_has_phase(
        self,
        mock_get_store,
        mock_write_marker,
        mock_resolve_phase,
        app,
        mock_pipeline,
    ):
        """CONSENSUS_CONFIRMED message includes the resolved phase."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store

        mock_tracker = MagicMock()
        mock_tracker.handle_confirmed.return_value = {
            "status": "confirmed",
            "consensus_reached": True,
        }

        mock_msg_store = MagicMock()

        with (
            app.app_context(),
            patch("peer_consensus.get_peer_consensus_tracker", return_value=mock_tracker),
            patch("message_store.get_message_store", return_value=mock_msg_store),
        ):
            from routes.signals import handle_consensus_confirmed_signal

            response, status_code = handle_consensus_confirmed_signal(
                "issue-42",
                {"agent_role": "coder"},
                Path("/tmp/repo"),
            )

        assert status_code == 200
        msg = mock_msg_store.add_message.call_args_list[0][0][0]
        assert msg.phase == "implement"
        assert msg.message_type == MessageType.CONSENSUS_CONFIRMED

    @patch("routes.signals._write_consensus_confirmed_marker")
    @patch("routes.signals.get_state_store")
    def test_confirmed_fallback_uses_local_phase(
        self,
        mock_get_store,
        mock_write_marker,
        app,
        mock_pipeline,
    ):
        """Fallback path uses _phase local variable (not _resolve_pipeline_phase).

        When the tracker is None and the message-bus fallback kicks in, the
        confirmed handler should use its pre-resolved _phase variable to set
        the phase on the CONSENSUS_CONFIRMED message.
        """
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store

        mock_msg_store = MagicMock()

        # Return enough CONFIRMED messages to trigger the fallback path.
        # `coder` is omitted — it's the role sending this signal, so its
        # CONFIRMED is added via the fallback's confirmed_roles.add(agent_role)
        # line.  This also verifies idempotency doesn't short-circuit the
        # fallback write when the sender has no prior CONFIRMED (#1890).
        existing_confirmed = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role=role,
                message_type=MessageType.CONSENSUS_CONFIRMED,
                subject=f"Confirmed by {role}",
                body="",
                phase="implement",
            )
            for role in ["tester", "reviewer_code", "documenter", "reviewer_contract"]
        ]
        mock_msg_store.get_messages.return_value = existing_confirmed

        mock_review_graph = MagicMock()
        mock_review_graph.all_roles.return_value = {
            "coder",
            "tester",
            "reviewer_code",
            "documenter",
            "reviewer_contract",
        }

        with (
            app.app_context(),
            patch("peer_consensus.get_peer_consensus_tracker", return_value=None),
            patch(
                "peer_consensus.reconstruct_tracker_from_messages",
                side_effect=Exception("Reconstruction failed"),
            ),
            patch("message_store.get_message_store", return_value=mock_msg_store),
            patch(
                "review_graph.get_review_graph_for_phase",
                return_value=mock_review_graph,
            ),
        ):
            from routes.signals import handle_consensus_confirmed_signal

            response, status_code = handle_consensus_confirmed_signal(
                "issue-42",
                {"agent_role": "coder"},
                Path("/tmp/repo"),
            )

        assert status_code == 200
        data = json.loads(response.data)
        assert data.get("data", {}).get("fallback") == "message_bus"

        # The fallback writes a CONFIRMED message — check it has phase
        msg = mock_msg_store.add_message.call_args_list[0][0][0]
        assert msg.phase is not None, "Fallback CONFIRMED message must have a phase"
        assert msg.phase != "unknown", "Phase must not be 'unknown'"


# ---------------------------------------------------------------------------
# Regression tests: _write_brc_history with None-phase messages
# ---------------------------------------------------------------------------


class TestWriteBrcHistoryNonePhase:
    """Regression tests for _write_brc_history with phase=None messages."""

    def test_skips_none_phase_messages(self, tmp_path):
        """_write_brc_history produces no file when all messages have phase=None.

        This documents the pre-fix behavior: without the phase field set on
        Messages, _write_brc_history filters them all out (since m.phase != phase).
        """
        from routes.pipelines import _write_brc_history

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal from coder",
                body="Did the work",
                phase=None,  # Pre-fix: phase was not set
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_ACK,
                subject="ACK from reviewer",
                body="LGTM",
                phase=None,
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_CONFIRMED,
                subject="Confirmed",
                body="",
                phase=None,
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        history_dir = tmp_path / ".egg-state" / "brc-history"
        if history_dir.exists():
            assert list(history_dir.iterdir()) == [], (
                "No file should be created when all messages have phase=None"
            )

    def test_includes_messages_with_correct_phase(self, tmp_path):
        """Messages with matching phase are included, None-phase ones excluded."""
        from routes.pipelines import _write_brc_history

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                body="Old message",
                phase=None,  # Pre-fix message
            ),
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                body="New message",
                phase="implement",  # Post-fix message
            ),
        ]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        # #2548: implement is per-slice — the aggregate file is gone.
        expected_path = (
            tmp_path
            / ".egg-state"
            / "brc-history"
            / f"42-implement-{_DEFAULT_IMPLEMENT_SLICE_ID}.md"
        )
        assert expected_path.exists()
        content = expected_path.read_text()
        assert "New message" in content
        assert "Old message" not in content


# ---------------------------------------------------------------------------
# Edge cases and boundary conditions
# ---------------------------------------------------------------------------


class TestPhasePropagationEdgeCases:
    """Edge cases for phase propagation in signal handlers."""

    @patch("routes.signals._resolve_pipeline_phase", return_value="plan")
    @patch("routes.signals.resolve_worktree_path", return_value=Path("/tmp/wt"))
    @patch("routes.signals.get_state_store")
    def test_phase_matches_actual_pipeline_phase(
        self,
        mock_get_store,
        mock_resolve_wt,
        mock_resolve_phase,
        app,
    ):
        """Phase on message matches the pipeline's actual phase, not hardcoded."""
        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = mock_store

        mock_tracker = MagicMock()
        mock_tracker.handle_propose.return_value = {"version": 1, "stale_reviewers": []}

        mock_msg_store = MagicMock()

        with (
            app.app_context(),
            patch("peer_consensus.get_peer_consensus_tracker", return_value=mock_tracker),
            patch("message_store.get_message_store", return_value=mock_msg_store),
        ):
            from routes.signals import handle_consensus_propose_signal

            handle_consensus_propose_signal(
                "issue-42",
                {
                    "agent_role": "planner",
                    "payload": {
                        "summary": "Completed architecture plan: defined API endpoints, database schema, and auth flow for user management"
                    },
                },
                Path("/tmp/repo"),
            )

        msg = mock_msg_store.add_message.call_args_list[0][0][0]
        assert msg.phase == "plan", "Phase should match the pipeline's actual phase"

    def test_resolve_phase_called_with_correct_args(self, app, mock_pipeline):
        """_resolve_pipeline_phase receives the correct pipeline_id and repo_path."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline

        mock_tracker = MagicMock()
        mock_tracker.handle_nack.return_value = {
            "reason": "Implementation has SQL injection in query builder module",
            "revision_count": 1,
        }
        mock_msg_store = MagicMock()

        with (
            app.app_context(),
            patch("routes.signals.get_state_store", return_value=mock_store),
            patch("peer_consensus.get_peer_consensus_tracker", return_value=mock_tracker),
            patch("message_store.get_message_store", return_value=mock_msg_store),
            patch(
                "routes.signals._resolve_pipeline_phase", return_value="implement"
            ) as mock_resolve,
        ):
            from routes.signals import handle_consensus_nack_signal

            handle_consensus_nack_signal(
                "issue-99",
                {
                    "agent_role": "reviewer_code",
                    "producer_role": "coder",
                    "payload": {
                        "reason": "Implementation has SQL injection in query builder module, user input is not sanitized"
                    },
                },
                Path("/tmp/my-repo"),
            )

        mock_resolve.assert_called_with("issue-99", Path("/tmp/my-repo"))
