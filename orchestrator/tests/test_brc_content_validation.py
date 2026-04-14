"""Tests for BRC minimum-content enforcement (issue #1716).

Covers:
- ``_validate_brc_content`` unit tests: empty, whitespace-only, <50-char,
  boilerplate, and substantive inputs.
- Per-handler integration tests that validation failure returns HTTP 400
  with no message-store write, and that success still writes normally.
- Re-propose path (with ``changed_artifacts``) is also validated.
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
    """Create a mock pipeline."""
    from models import Pipeline

    return Pipeline(
        id="issue-42",
        issue_number=42,
        repo="owner/repo",
        branch="egg/issue-42",
    )


# A content string guaranteed to be >=50 chars and non-boilerplate
SUBSTANTIVE_REASON = (
    "Reviewed auth module thoroughly: input validation covers SQL injection, "
    "XSS, and CSRF edge cases. Error handling returns actionable messages. "
    "Test coverage is comprehensive with boundary conditions covered."
)

SUBSTANTIVE_SUMMARY = (
    "Implemented authentication module with login, logout, session management. "
    "Added input validation, rate limiting, and comprehensive error handling. "
    "All acceptance criteria from task-1-1 and task-1-2 are satisfied."
)

# Short strings that should be rejected (< 50 chars)
SHORT_CONTENT = "Fixed the bug"
EMPTY_CONTENT = ""
WHITESPACE_CONTENT = "   \n\t  "

# Boilerplate strings that should be rejected
BOILERPLATE_STRINGS = [
    "lgtm",
    "LGTM",
    "Lgtm",
    "looks good",
    "Looks Good",
    "LOOKS GOOD",
    "no issues",
    "No Issues",
    "NO ISSUES",
    "approved",
    "Approved",
    "APPROVED",
    "ok",
    "OK",
    "Ok",
]


# ---------------------------------------------------------------------------
# Unit tests for _validate_brc_content
# ---------------------------------------------------------------------------


class TestValidateBrcContent:
    """Unit tests for the _validate_brc_content validator."""

    def _get_validator(self):
        """Import the validator function."""
        from routes.signals import _validate_brc_content

        return _validate_brc_content

    def test_empty_string_returns_error(self):
        """Empty string should be rejected."""
        validate = self._get_validator()
        result = validate("", "summary")
        assert result is not None
        assert "empty" in result.lower() or "content" in result.lower()

    def test_whitespace_only_returns_error(self):
        """Whitespace-only string should be rejected."""
        validate = self._get_validator()
        result = validate("   \n\t  ", "reason")
        assert result is not None

    def test_short_string_returns_error(self):
        """String shorter than 50 chars should be rejected."""
        validate = self._get_validator()
        result = validate("This is too short to be substantive", "summary")
        assert result is not None
        assert "50" in result or "short" in result.lower() or "minimum" in result.lower()

    def test_exactly_50_chars_passes(self):
        """String of exactly 50 chars should pass (boundary)."""
        validate = self._get_validator()
        content = "A" * 50
        result = validate(content, "summary")
        assert result is None

    def test_49_chars_fails(self):
        """String of 49 chars should fail (boundary)."""
        validate = self._get_validator()
        content = "A" * 49
        result = validate(content, "summary")
        assert result is not None

    @pytest.mark.parametrize("boilerplate", BOILERPLATE_STRINGS)
    def test_boilerplate_returns_error(self, boilerplate):
        """Known boilerplate phrases should be rejected regardless of length."""
        validate = self._get_validator()
        result = validate(boilerplate, "reason")
        assert result is not None
        assert "boilerplate" in result.lower() or "substantive" in result.lower()

    def test_substantive_reason_passes(self):
        """A substantive, detailed reason should pass."""
        validate = self._get_validator()
        result = validate(SUBSTANTIVE_REASON, "reason")
        assert result is None

    def test_substantive_summary_passes(self):
        """A substantive, detailed summary should pass."""
        validate = self._get_validator()
        result = validate(SUBSTANTIVE_SUMMARY, "summary")
        assert result is None

    def test_kind_appears_in_error_message(self):
        """The 'kind' parameter should appear in the error message for context."""
        validate = self._get_validator()
        result = validate("", "summary")
        assert result is not None
        assert "summary" in result.lower()

        result = validate("", "reason")
        assert result is not None
        assert "reason" in result.lower()

    def test_boilerplate_with_padding_still_rejected(self):
        """Boilerplate padded with spaces should still be rejected."""
        validate = self._get_validator()
        result = validate("  lgtm  ", "reason")
        assert result is not None

    def test_long_non_boilerplate_passes(self):
        """A long, non-boilerplate string should pass."""
        validate = self._get_validator()
        content = (
            "After careful review of the implementation, I found the error "
            "handling to be robust with proper input validation."
        )
        assert len(content) >= 50
        result = validate(content, "reason")
        assert result is None


# ---------------------------------------------------------------------------
# Handler-level tests: propose
# ---------------------------------------------------------------------------


class TestProposeContentValidation:
    """handle_consensus_propose_signal rejects empty/short/boilerplate summaries."""

    def _call_propose(self, app, mock_pipeline, summary, changed_artifacts=None):
        """Helper to call handle_consensus_propose_signal."""
        data = {
            "agent_role": "coder",
            "payload": {
                "summary": summary,
                "attestation": {},
                "artifacts": ["src/auth.py"],
                "commit_sha": "abc123",
            },
        }
        if changed_artifacts:
            data["changed_artifacts"] = changed_artifacts

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_msg_store = MagicMock()

        with app.app_context():
            from routes.signals import handle_consensus_propose_signal

            with (
                patch("routes.signals.get_state_store", return_value=mock_store),
                patch("routes.signals.resolve_worktree_path", return_value=Path("/tmp")),
                patch("routes.signals.get_message_store", return_value=mock_msg_store),
                patch("routes.signals._verify_commit_on_branch", return_value=True),
                patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
            ):
                # Mock the tracker
                mock_tracker = MagicMock()
                mock_tracker.handle_propose.return_value = {"status": "proposed", "version": 1, "stale_reviewers": []}
                mock_tracker.handle_re_propose.return_value = {"status": "proposed", "version": 2, "stale_reviewers": []}
                with patch("routes.signals.get_peer_consensus_tracker", return_value=mock_tracker):
                    response, status_code = handle_consensus_propose_signal(
                        "issue-42", data, Path("/tmp/repo")
                    )

        return response, status_code, mock_msg_store, mock_tracker

    def test_empty_summary_returns_400(self, app, mock_pipeline):
        """Empty summary should be rejected with 400."""
        response, status_code, msg_store, tracker = self._call_propose(
            app, mock_pipeline, ""
        )
        assert status_code == 400
        data = json.loads(response.data)
        assert data["success"] is False
        # Message store should NOT have been written to
        msg_store.add_message.assert_not_called()
        # Tracker should NOT have been mutated
        tracker.handle_propose.assert_not_called()

    def test_short_summary_returns_400(self, app, mock_pipeline):
        """Summary shorter than 50 chars should be rejected."""
        response, status_code, msg_store, tracker = self._call_propose(
            app, mock_pipeline, "Too short"
        )
        assert status_code == 400
        msg_store.add_message.assert_not_called()
        tracker.handle_propose.assert_not_called()

    def test_boilerplate_summary_returns_400(self, app, mock_pipeline):
        """Boilerplate summary should be rejected."""
        response, status_code, msg_store, tracker = self._call_propose(
            app, mock_pipeline, "looks good"
        )
        assert status_code == 400
        msg_store.add_message.assert_not_called()

    def test_substantive_summary_succeeds(self, app, mock_pipeline):
        """Substantive summary should pass and write to message store."""
        response, status_code, msg_store, tracker = self._call_propose(
            app, mock_pipeline, SUBSTANTIVE_SUMMARY
        )
        assert status_code == 200
        msg_store.add_message.assert_called()
        tracker.handle_propose.assert_called_once()

    def test_repropose_with_empty_summary_returns_400(self, app, mock_pipeline):
        """Re-propose (changed_artifacts) must also validate summary."""
        response, status_code, msg_store, tracker = self._call_propose(
            app,
            mock_pipeline,
            "",
            changed_artifacts=["src/auth.py"],
        )
        assert status_code == 400
        msg_store.add_message.assert_not_called()
        # handle_re_propose should NOT have been called
        tracker.handle_re_propose.assert_not_called()

    def test_repropose_with_substantive_summary_succeeds(self, app, mock_pipeline):
        """Re-propose with substantive summary should succeed."""
        response, status_code, msg_store, tracker = self._call_propose(
            app,
            mock_pipeline,
            SUBSTANTIVE_SUMMARY,
            changed_artifacts=["src/auth.py"],
        )
        assert status_code == 200
        tracker.handle_re_propose.assert_called_once()


# ---------------------------------------------------------------------------
# Handler-level tests: ACK
# ---------------------------------------------------------------------------


class TestAckContentValidation:
    """handle_consensus_ack_signal rejects empty/short/boilerplate reasons."""

    def _call_ack(self, app, mock_pipeline, reason):
        """Helper to call handle_consensus_ack_signal."""
        data = {
            "agent_role": "reviewer_code",
            "producer_role": "coder",
            "payload": {
                "reason": reason,
                "artifact_references": ["src/auth.py"],
            },
        }

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_msg_store = MagicMock()

        with app.app_context():
            from routes.signals import handle_consensus_ack_signal

            mock_tracker = MagicMock()
            mock_tracker.handle_ack.return_value = {"status": "acked", "version": 1, "fully_acked": False}
            with (
                patch("routes.signals.get_state_store", return_value=mock_store),
                patch("routes.signals.resolve_worktree_path", return_value=Path("/tmp")),
                patch("routes.signals.get_message_store", return_value=mock_msg_store),
                patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
                patch("routes.signals.get_peer_consensus_tracker", return_value=mock_tracker),
            ):
                response, status_code = handle_consensus_ack_signal(
                    "issue-42", data, Path("/tmp/repo")
                )

        return response, status_code, mock_msg_store, mock_tracker

    def test_empty_reason_returns_400(self, app, mock_pipeline):
        """Empty reason should be rejected."""
        response, status_code, msg_store, tracker = self._call_ack(
            app, mock_pipeline, ""
        )
        assert status_code == 400
        msg_store.add_message.assert_not_called()
        tracker.handle_ack.assert_not_called()

    def test_short_reason_returns_400(self, app, mock_pipeline):
        """Short reason should be rejected."""
        response, status_code, msg_store, tracker = self._call_ack(
            app, mock_pipeline, "Looks fine"
        )
        assert status_code == 400
        msg_store.add_message.assert_not_called()

    def test_boilerplate_reason_returns_400(self, app, mock_pipeline):
        """Boilerplate reason should be rejected."""
        response, status_code, msg_store, tracker = self._call_ack(
            app, mock_pipeline, "LGTM"
        )
        assert status_code == 400
        msg_store.add_message.assert_not_called()

    def test_substantive_reason_succeeds(self, app, mock_pipeline):
        """Substantive reason should pass and write to message store."""
        response, status_code, msg_store, tracker = self._call_ack(
            app, mock_pipeline, SUBSTANTIVE_REASON
        )
        assert status_code == 200
        msg_store.add_message.assert_called()
        tracker.handle_ack.assert_called_once()


# ---------------------------------------------------------------------------
# Handler-level tests: NACK
# ---------------------------------------------------------------------------


class TestNackContentValidation:
    """handle_consensus_nack_signal rejects empty/short/boilerplate reasons."""

    def _call_nack(self, app, mock_pipeline, reason):
        """Helper to call handle_consensus_nack_signal."""
        data = {
            "agent_role": "reviewer_code",
            "producer_role": "coder",
            "payload": {
                "reason": reason,
                "artifact_references": ["src/auth.py"],
            },
        }

        mock_msg_store = MagicMock()

        with app.app_context():
            from routes.signals import handle_consensus_nack_signal

            mock_tracker = MagicMock()
            mock_tracker.handle_nack.return_value = {"status": "nacked", "reason": reason, "revision_count": 1}
            with (
                patch("routes.signals.get_message_store", return_value=mock_msg_store),
                patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
                patch("routes.signals.get_peer_consensus_tracker", return_value=mock_tracker),
            ):
                response, status_code = handle_consensus_nack_signal(
                    "issue-42", data, Path("/tmp/repo")
                )

        return response, status_code, mock_msg_store, mock_tracker

    def test_empty_reason_returns_400(self, app, mock_pipeline):
        """Empty NACK reason should be rejected."""
        response, status_code, msg_store, tracker = self._call_nack(
            app, mock_pipeline, ""
        )
        assert status_code == 400
        msg_store.add_message.assert_not_called()
        tracker.handle_nack.assert_not_called()

    def test_short_reason_returns_400(self, app, mock_pipeline):
        """Short NACK reason should be rejected."""
        response, status_code, msg_store, tracker = self._call_nack(
            app, mock_pipeline, "Bad code"
        )
        assert status_code == 400
        msg_store.add_message.assert_not_called()

    def test_substantive_reason_succeeds(self, app, mock_pipeline):
        """Substantive NACK reason should pass and write to message store."""
        substantive_nack = (
            "The authentication module is missing input validation for the "
            "login endpoint. SQL injection is possible via the username field. "
            "Please add parameterized queries and input sanitization."
        )
        response, status_code, msg_store, tracker = self._call_nack(
            app, mock_pipeline, substantive_nack
        )
        assert status_code == 200
        msg_store.add_message.assert_called()
        tracker.handle_nack.assert_called_once()


# ---------------------------------------------------------------------------
# Handler-level tests: WITHDRAW
# ---------------------------------------------------------------------------


class TestWithdrawContentValidation:
    """handle_consensus_withdraw_signal rejects empty/short/boilerplate reasons."""

    def _call_withdraw(self, app, mock_pipeline, reason):
        """Helper to call handle_consensus_withdraw_signal."""
        data = {
            "agent_role": "coder",
            "reason": reason,
        }

        mock_msg_store = MagicMock()

        with app.app_context():
            from routes.signals import handle_consensus_withdraw_signal

            mock_tracker = MagicMock()
            mock_tracker.handle_withdraw.return_value = {"status": "withdrawn"}
            with (
                patch("routes.signals.get_message_store", return_value=mock_msg_store),
                patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
                patch("routes.signals.get_peer_consensus_tracker", return_value=mock_tracker),
            ):
                response, status_code = handle_consensus_withdraw_signal(
                    "issue-42", data, Path("/tmp/repo")
                )

        return response, status_code, mock_msg_store, mock_tracker

    def test_empty_reason_returns_400(self, app, mock_pipeline):
        """Empty withdraw reason should be rejected."""
        response, status_code, msg_store, tracker = self._call_withdraw(
            app, mock_pipeline, ""
        )
        assert status_code == 400
        msg_store.add_message.assert_not_called()
        tracker.handle_withdraw.assert_not_called()

    def test_short_reason_returns_400(self, app, mock_pipeline):
        """Short withdraw reason should be rejected."""
        response, status_code, msg_store, tracker = self._call_withdraw(
            app, mock_pipeline, "Giving up"
        )
        assert status_code == 400
        msg_store.add_message.assert_not_called()

    def test_substantive_reason_succeeds(self, app, mock_pipeline):
        """Substantive withdraw reason should pass and write to message store."""
        substantive_withdraw = (
            "Withdrawing proposal because the initial design approach for "
            "authentication conflicts with the existing session management "
            "architecture. Need to redesign to use JWT tokens instead."
        )
        response, status_code, msg_store, tracker = self._call_withdraw(
            app, mock_pipeline, substantive_withdraw
        )
        assert status_code == 200
        msg_store.add_message.assert_called()
        tracker.handle_withdraw.assert_called_once()


# ---------------------------------------------------------------------------
# Edge cases and additional coverage
# ---------------------------------------------------------------------------


class TestValidationEdgeCases:
    """Edge cases for content validation across handlers."""

    def test_propose_whitespace_summary_returns_400(self, app, mock_pipeline):
        """Whitespace-only summary should be rejected like empty."""
        data = {
            "agent_role": "coder",
            "payload": {
                "summary": "   \n\t  ",
                "attestation": {},
                "artifacts": [],
                "commit_sha": "abc123",
            },
        }
        mock_tracker = MagicMock()
        mock_tracker.handle_propose.return_value = {"status": "proposed", "version": 1, "stale_reviewers": []}
        mock_msg_store = MagicMock()

        with app.app_context():
            from routes.signals import handle_consensus_propose_signal

            with (
                patch("routes.signals.get_state_store") as mock_get_store,
                patch("routes.signals.resolve_worktree_path", return_value=Path("/tmp")),
                patch("routes.signals.get_message_store", return_value=mock_msg_store),
                patch("routes.signals._verify_commit_on_branch", return_value=True),
                patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
                patch("routes.signals.get_peer_consensus_tracker", return_value=mock_tracker),
            ):
                mock_store = MagicMock()
                mock_store.load_pipeline.return_value = mock_pipeline
                mock_get_store.return_value = mock_store
                response, status_code = handle_consensus_propose_signal(
                    "issue-42", data, Path("/tmp/repo")
                )

        assert status_code == 400
        mock_msg_store.add_message.assert_not_called()

    def test_ack_boilerplate_no_issues_returns_400(self, app, mock_pipeline):
        """'no issues' as ACK reason should be rejected as boilerplate."""
        data = {
            "agent_role": "reviewer_code",
            "producer_role": "coder",
            "payload": {
                "reason": "no issues",
                "artifact_references": ["src/auth.py"],
            },
        }
        mock_tracker = MagicMock()
        mock_msg_store = MagicMock()

        with app.app_context():
            from routes.signals import handle_consensus_ack_signal

            with (
                patch("routes.signals.get_message_store", return_value=mock_msg_store),
                patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
                patch("routes.signals.get_peer_consensus_tracker", return_value=mock_tracker),
            ):
                response, status_code = handle_consensus_ack_signal(
                    "issue-42", data, Path("/tmp/repo")
                )

        assert status_code == 400
        mock_msg_store.add_message.assert_not_called()
        mock_tracker.handle_ack.assert_not_called()
