"""
Tests for BRC minimum-content enforcement (issue #1716).

Covers:
- Unit tests for ``_validate_brc_content`` helper (empty, whitespace-only,
  <50-char, boilerplate, boundary, and substantive inputs).
- Integration tests for each BRC signal handler verifying that:
  (a) invalid content returns HTTP 400 with no message-store write or
      tracker mutation,
  (b) substantive content passes through and produces the normal 2xx
      success,
  (c) the re-propose path (``changed_artifacts`` present) is also validated.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup (mirrors existing test_signals.py pattern)
# ---------------------------------------------------------------------------

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from routes.signals import (
    _BRC_BOILERPLATE,
    _BRC_CONDITION_MIN_LEN,
    _BRC_MIN_CONTENT_LEN,
    _validate_brc_content,
)

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


# Substantive text for use across tests (>= 50 chars, not boilerplate).
_SUBSTANTIVE_SUMMARY = (
    "Implemented authentication module with JWT token validation, "
    "password hashing, and session management. All contract tasks satisfied."
)
_SUBSTANTIVE_REASON = (
    "Reviewed src/auth.py lines 10-85: token validation handles expiry, "
    "invalid signatures, and missing claims correctly. Tests cover all branches."
)


# ============================================================================
# Unit tests for _validate_brc_content
# ============================================================================


class TestValidateBrcContentEmpty:
    """Validation rejects empty or None bodies."""

    def test_empty_string_rejected(self):
        err = _validate_brc_content("", "Proposal summary")
        assert err is not None
        assert "empty" in err.lower()
        assert "Proposal summary" in err

    def test_none_rejected(self):
        err = _validate_brc_content(None, "ACK reason")  # type: ignore[arg-type]
        assert err is not None
        assert "empty" in err.lower()

    def test_whitespace_only_rejected(self):
        err = _validate_brc_content("   \t\n  ", "NACK reason")
        assert err is not None
        assert "empty" in err.lower()


class TestValidateBrcContentBoilerplate:
    """Validation rejects known boilerplate strings."""

    @pytest.mark.parametrize("text", sorted(_BRC_BOILERPLATE))
    def test_exact_boilerplate_rejected(self, text: str):
        err = _validate_brc_content(text, "ACK reason")
        assert err is not None
        assert "boilerplate" in err.lower()

    @pytest.mark.parametrize("text", sorted(_BRC_BOILERPLATE))
    def test_boilerplate_case_insensitive(self, text: str):
        # Mixed case variant
        mixed = text[0].upper() + text[1:] if len(text) > 1 else text.upper()
        err = _validate_brc_content(mixed, "ACK reason")
        assert err is not None
        assert "boilerplate" in err.lower()

    @pytest.mark.parametrize("text", sorted(_BRC_BOILERPLATE))
    def test_boilerplate_with_whitespace_padding(self, text: str):
        err = _validate_brc_content(f"  {text}  ", "Proposal summary")
        assert err is not None
        assert "boilerplate" in err.lower()

    def test_boilerplate_all_upper_rejected(self):
        err = _validate_brc_content("LGTM", "ACK reason")
        assert err is not None
        assert "boilerplate" in err.lower()

    def test_boilerplate_ok_rejected(self):
        err = _validate_brc_content("OK", "ACK reason")
        assert err is not None
        assert "boilerplate" in err.lower()


class TestValidateBrcContentTooShort:
    """Validation rejects content below the minimum length threshold."""

    def test_one_char_rejected(self):
        err = _validate_brc_content("x", "Proposal summary")
        assert err is not None
        assert "1 chars" in err
        assert f"minimum {_BRC_MIN_CONTENT_LEN}" in err

    def test_just_under_minimum_rejected(self):
        text = "a" * (_BRC_MIN_CONTENT_LEN - 1)
        err = _validate_brc_content(text, "ACK reason")
        assert err is not None
        assert f"{_BRC_MIN_CONTENT_LEN - 1} chars" in err

    def test_short_sentence_rejected(self):
        err = _validate_brc_content("Implemented the feature.", "Proposal summary")
        assert err is not None
        assert "chars" in err

    def test_whitespace_does_not_count_toward_length(self):
        # 48 real chars + leading/trailing whitespace
        padded = "   " + "a" * (_BRC_MIN_CONTENT_LEN - 2) + "   "
        err = _validate_brc_content(padded, "NACK reason")
        assert err is not None
        assert "chars" in err


class TestValidateBrcContentBoundary:
    """Exact boundary behavior at the minimum length."""

    def test_exactly_minimum_accepted(self):
        text = "a" * _BRC_MIN_CONTENT_LEN
        err = _validate_brc_content(text, "ACK reason")
        assert err is None

    def test_one_below_minimum_rejected(self):
        text = "a" * (_BRC_MIN_CONTENT_LEN - 1)
        err = _validate_brc_content(text, "ACK reason")
        assert err is not None

    def test_one_above_minimum_accepted(self):
        text = "a" * (_BRC_MIN_CONTENT_LEN + 1)
        err = _validate_brc_content(text, "Proposal summary")
        assert err is None


class TestValidateBrcContentSubstantive:
    """Validation accepts substantive content."""

    def test_long_summary_accepted(self):
        err = _validate_brc_content(_SUBSTANTIVE_SUMMARY, "Proposal summary")
        assert err is None

    def test_long_reason_accepted(self):
        err = _validate_brc_content(_SUBSTANTIVE_REASON, "ACK reason")
        assert err is None

    def test_technical_content_accepted(self):
        text = (
            "Checked src/auth.py:42-60 for SQL injection, confirmed parameterized "
            "queries throughout."
        )
        err = _validate_brc_content(text, "NACK reason")
        assert err is None


class TestValidateBrcContentKindLabel:
    """Error messages include the correct signal kind label."""

    @pytest.mark.parametrize(
        "kind",
        ["Proposal summary", "ACK reason", "NACK reason", "Withdrawal reason"],
    )
    def test_kind_in_error_message(self, kind: str):
        err = _validate_brc_content("", kind)
        assert err is not None
        assert kind in err


# ============================================================================
# Unit tests for pre-merge condition kind (#2005)
# ============================================================================


class TestValidateBrcConditionMinLength:
    """Pre-merge condition kind uses a lower minimum length threshold."""

    def test_condition_kind_uses_lower_minimum(self):
        """10-char condition passes; same text at default kind fails."""
        text = "a" * _BRC_CONDITION_MIN_LEN
        assert _validate_brc_content(text, "pre-merge condition") is None
        # Same text is below default minimum
        assert _BRC_CONDITION_MIN_LEN < _BRC_MIN_CONTENT_LEN
        err = _validate_brc_content(text, "ACK reason")
        assert err is not None
        assert "chars" in err

    def test_condition_boundary_exactly_minimum_accepted(self):
        text = "a" * _BRC_CONDITION_MIN_LEN
        assert _validate_brc_content(text, "pre-merge condition") is None

    def test_condition_boundary_one_below_minimum_rejected(self):
        text = "a" * (_BRC_CONDITION_MIN_LEN - 1)
        err = _validate_brc_content(text, "pre-merge condition")
        assert err is not None
        assert f"{_BRC_CONDITION_MIN_LEN - 1} chars" in err
        assert f"minimum {_BRC_CONDITION_MIN_LEN}" in err

    def test_condition_boundary_one_above_minimum_accepted(self):
        text = "a" * (_BRC_CONDITION_MIN_LEN + 1)
        assert _validate_brc_content(text, "pre-merge condition") is None

    def test_condition_kind_case_insensitive(self):
        """Kind matching is case-insensitive."""
        text = "a" * _BRC_CONDITION_MIN_LEN
        assert _validate_brc_content(text, "Pre-Merge Condition") is None
        assert _validate_brc_content(text, "PRE-MERGE CONDITION") is None

    def test_boilerplate_still_rejected_for_conditions(self):
        """Boilerplate check applies regardless of kind."""
        for text in sorted(_BRC_BOILERPLATE):
            err = _validate_brc_content(text, "pre-merge condition")
            assert err is not None, f"Boilerplate '{text}' was not rejected"
            assert "boilerplate" in err.lower()

    def test_default_minimum_unchanged_for_other_kinds(self):
        """Non-condition kinds still use the default 50-char minimum."""
        text = "a" * (_BRC_MIN_CONTENT_LEN - 1)
        for kind in ["Proposal summary", "ACK reason", "NACK reason", "Withdrawal reason"]:
            err = _validate_brc_content(text, kind)
            assert err is not None, f"Kind '{kind}' should reject {len(text)}-char text"
            assert f"minimum {_BRC_MIN_CONTENT_LEN}" in err


# ============================================================================
# Handler integration tests — propose
# ============================================================================


class TestProposeContentValidation:
    """Propose handler rejects invalid summaries with HTTP 400."""

    @patch("routes.signals.resolve_worktree_path", return_value=Path("/tmp/wt"))
    @patch("routes.signals.get_state_store")
    def test_empty_summary_returns_400(
        self,
        mock_get_store,
        mock_resolve_wt,
        app,
        mock_pipeline,
    ):
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store

        with app.app_context():
            from routes.signals import handle_consensus_propose_signal

            response, status_code = handle_consensus_propose_signal(
                "issue-42",
                {
                    "agent_role": "coder",
                    "payload": {
                        "summary": "",
                        "artifacts": ["src/a.py"],
                        "commit_sha": "abc1234",
                    },
                },
                Path("/tmp/repo"),
            )

        assert status_code == 400
        data = json.loads(response.data)
        assert "empty" in data["message"]

    @patch("routes.signals.resolve_worktree_path", return_value=Path("/tmp/wt"))
    @patch("routes.signals.get_state_store")
    def test_short_summary_returns_400(
        self,
        mock_get_store,
        mock_resolve_wt,
        app,
        mock_pipeline,
    ):
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store

        with app.app_context():
            from routes.signals import handle_consensus_propose_signal

            response, status_code = handle_consensus_propose_signal(
                "issue-42",
                {
                    "agent_role": "coder",
                    "payload": {
                        "summary": "impl",
                        "artifacts": ["src/a.py"],
                        "commit_sha": "abc1234",
                    },
                },
                Path("/tmp/repo"),
            )

        assert status_code == 400
        data = json.loads(response.data)
        assert "chars" in data["message"]

    @patch("routes.signals.resolve_worktree_path", return_value=Path("/tmp/wt"))
    @patch("routes.signals.get_state_store")
    def test_boilerplate_summary_returns_400(
        self,
        mock_get_store,
        mock_resolve_wt,
        app,
        mock_pipeline,
    ):
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store

        with app.app_context():
            from routes.signals import handle_consensus_propose_signal

            response, status_code = handle_consensus_propose_signal(
                "issue-42",
                {
                    "agent_role": "coder",
                    "payload": {
                        "summary": "looks good",
                        "artifacts": ["src/a.py"],
                        "commit_sha": "abc1234",
                    },
                },
                Path("/tmp/repo"),
            )

        assert status_code == 400
        data = json.loads(response.data)
        assert "boilerplate" in data["message"]

    @patch("routes.signals.subprocess.run")
    @patch("routes.signals.resolve_worktree_path", return_value=Path("/tmp/wt"))
    @patch("routes.signals.get_state_store")
    @patch("peer_consensus.get_peer_consensus_tracker")
    def test_substantive_summary_passes(
        self,
        mock_get_tracker,
        mock_get_store,
        mock_resolve_wt,
        mock_subprocess_run,
        app,
        mock_pipeline,
    ):
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store

        mock_tracker = MagicMock()
        mock_tracker.handle_propose.return_value = {
            "version": 1,
            "status": "proposed",
            "commit_sha": "abc1234",
            "reviewers": [],
            "stale_reviewers": [],
        }
        mock_get_tracker.return_value = mock_tracker

        mock_subprocess_run.side_effect = [
            MagicMock(returncode=0),  # fetch
            MagicMock(stdout="  origin/egg/issue-42\n"),  # branch --contains
        ]

        with (
            app.app_context(),
            patch("message_store.get_message_store") as mock_msg_store,
        ):
            mock_store_inst = MagicMock()
            mock_msg_store.return_value = mock_store_inst

            from routes.signals import handle_consensus_propose_signal

            response, status_code = handle_consensus_propose_signal(
                "issue-42",
                {
                    "agent_role": "coder",
                    "payload": {
                        "summary": _SUBSTANTIVE_SUMMARY,
                        "artifacts": ["src/a.py"],
                        "commit_sha": "abc1234",
                    },
                },
                Path("/tmp/repo"),
            )

        assert status_code == 200
        # Tracker should have been called
        mock_tracker.handle_propose.assert_called_once()

    @patch("routes.signals.resolve_worktree_path", return_value=Path("/tmp/wt"))
    @patch("routes.signals.get_state_store")
    def test_no_tracker_or_store_mutation_on_400(
        self,
        mock_get_store,
        mock_resolve_wt,
        app,
        mock_pipeline,
    ):
        """When content validation fails, no tracker or message store calls happen."""
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store

        with (
            app.app_context(),
            patch("peer_consensus.get_peer_consensus_tracker") as mock_get_tracker,
            patch("message_store.get_message_store") as mock_msg_store,
        ):
            from routes.signals import handle_consensus_propose_signal

            response, status_code = handle_consensus_propose_signal(
                "issue-42",
                {
                    "agent_role": "coder",
                    "payload": {
                        "summary": "too short",
                        "artifacts": ["src/a.py"],
                    },
                },
                Path("/tmp/repo"),
            )

        assert status_code == 400
        # Tracker should never be fetched
        mock_get_tracker.assert_not_called()
        # Message store should never be written to
        mock_msg_store.assert_not_called()


class TestReProposeContentValidation:
    """Re-propose path (with changed_artifacts) is also validated."""

    @patch("routes.signals.resolve_worktree_path", return_value=Path("/tmp/wt"))
    @patch("routes.signals.get_state_store")
    def test_re_propose_empty_summary_returns_400(
        self,
        mock_get_store,
        mock_resolve_wt,
        app,
        mock_pipeline,
    ):
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store

        with app.app_context():
            from routes.signals import handle_consensus_propose_signal

            response, status_code = handle_consensus_propose_signal(
                "issue-42",
                {
                    "agent_role": "coder",
                    "payload": {"summary": "", "artifacts": ["src/a.py"]},
                    "changed_artifacts": ["src/a.py"],
                },
                Path("/tmp/repo"),
            )

        assert status_code == 400
        data = json.loads(response.data)
        assert "empty" in data["message"]

    @patch("routes.signals.resolve_worktree_path", return_value=Path("/tmp/wt"))
    @patch("routes.signals.get_state_store")
    def test_re_propose_short_summary_returns_400(
        self,
        mock_get_store,
        mock_resolve_wt,
        app,
        mock_pipeline,
    ):
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store

        with app.app_context():
            from routes.signals import handle_consensus_propose_signal

            response, status_code = handle_consensus_propose_signal(
                "issue-42",
                {
                    "agent_role": "coder",
                    "payload": {
                        "summary": "Fixed it",
                        "artifacts": ["src/a.py"],
                    },
                    "changed_artifacts": ["src/a.py"],
                },
                Path("/tmp/repo"),
            )

        assert status_code == 400
        data = json.loads(response.data)
        assert "chars" in data["message"]

    @patch("routes.signals.subprocess.run")
    @patch("routes.signals.resolve_worktree_path", return_value=Path("/tmp/wt"))
    @patch("routes.signals.get_state_store")
    @patch("peer_consensus.get_peer_consensus_tracker")
    def test_re_propose_substantive_summary_passes(
        self,
        mock_get_tracker,
        mock_get_store,
        mock_resolve_wt,
        mock_subprocess_run,
        app,
        mock_pipeline,
    ):
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline
        mock_get_store.return_value = mock_store

        mock_tracker = MagicMock()
        mock_tracker.handle_re_propose.return_value = {
            "version": 2,
            "stale_reviewers": [],
        }
        mock_get_tracker.return_value = mock_tracker

        mock_subprocess_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(stdout="  origin/egg/issue-42\n"),
        ]

        with (
            app.app_context(),
            patch("message_store.get_message_store") as mock_msg_store,
        ):
            mock_store_inst = MagicMock()
            mock_msg_store.return_value = mock_store_inst

            from routes.signals import handle_consensus_propose_signal

            response, status_code = handle_consensus_propose_signal(
                "issue-42",
                {
                    "agent_role": "coder",
                    "payload": {
                        "summary": _SUBSTANTIVE_SUMMARY,
                        "artifacts": ["src/a.py"],
                        "commit_sha": "abc1234",
                    },
                    "changed_artifacts": ["src/a.py"],
                },
                Path("/tmp/repo"),
            )

        assert status_code == 200
        mock_tracker.handle_re_propose.assert_called_once()


# ============================================================================
# Handler integration tests — ACK
# ============================================================================


class TestAckContentValidation:
    """ACK handler rejects invalid reasons with HTTP 400."""

    def test_empty_reason_returns_400(self, app):
        with app.app_context():
            from routes.signals import handle_consensus_ack_signal

            response, status_code = handle_consensus_ack_signal(
                "issue-42",
                {
                    "agent_role": "reviewer_code",
                    "producer_role": "coder",
                    "ack_version": 1,
                    "payload": {"reason": ""},
                },
                Path("/tmp/repo"),
            )

        assert status_code == 400
        data = json.loads(response.data)
        assert "empty" in data["message"]

    def test_missing_reason_returns_400(self, app):
        """Payload with no reason key defaults to empty string → rejected."""
        with app.app_context():
            from routes.signals import handle_consensus_ack_signal

            response, status_code = handle_consensus_ack_signal(
                "issue-42",
                {
                    "agent_role": "reviewer_code",
                    "producer_role": "coder",
                    "ack_version": 1,
                    "payload": {},
                },
                Path("/tmp/repo"),
            )

        assert status_code == 400
        data = json.loads(response.data)
        assert "empty" in data["message"]

    def test_boilerplate_reason_returns_400(self, app):
        with app.app_context():
            from routes.signals import handle_consensus_ack_signal

            response, status_code = handle_consensus_ack_signal(
                "issue-42",
                {
                    "agent_role": "reviewer_code",
                    "producer_role": "coder",
                    "ack_version": 1,
                    "payload": {"reason": "LGTM"},
                },
                Path("/tmp/repo"),
            )

        assert status_code == 400
        data = json.loads(response.data)
        assert "boilerplate" in data["message"]

    def test_short_reason_returns_400(self, app):
        with app.app_context():
            from routes.signals import handle_consensus_ack_signal

            response, status_code = handle_consensus_ack_signal(
                "issue-42",
                {
                    "agent_role": "reviewer_code",
                    "producer_role": "coder",
                    "ack_version": 1,
                    "payload": {"reason": "Looks fine to me"},
                },
                Path("/tmp/repo"),
            )

        assert status_code == 400
        data = json.loads(response.data)
        assert "chars" in data["message"]

    def test_substantive_reason_passes(self, app):
        with app.app_context():
            from routes.signals import handle_consensus_ack_signal

            mock_tracker = MagicMock()
            mock_tracker.handle_ack.return_value = {
                "status": "acked",
                "version": 1,
                "fully_acked": False,
            }

            with (
                patch(
                    "peer_consensus.get_peer_consensus_tracker",
                    return_value=mock_tracker,
                ),
                patch("message_store.get_message_store") as mock_msg_store,
                patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
            ):
                mock_store_inst = MagicMock()
                mock_msg_store.return_value = mock_store_inst

                response, status_code = handle_consensus_ack_signal(
                    "issue-42",
                    {
                        "agent_role": "reviewer_code",
                        "producer_role": "coder",
                        "ack_version": 1,
                        "payload": {"reason": _SUBSTANTIVE_REASON},
                    },
                    Path("/tmp/repo"),
                )

        assert status_code == 200
        mock_tracker.handle_ack.assert_called_once()

    def test_no_tracker_mutation_on_400(self, app):
        """When ACK content validation fails, tracker is never called."""
        with (
            app.app_context(),
            patch("peer_consensus.get_peer_consensus_tracker") as mock_get_tracker,
            patch("message_store.get_message_store") as mock_msg_store,
        ):
            from routes.signals import handle_consensus_ack_signal

            response, status_code = handle_consensus_ack_signal(
                "issue-42",
                {
                    "agent_role": "reviewer_code",
                    "producer_role": "coder",
                    "payload": {"reason": "ok"},
                },
                Path("/tmp/repo"),
            )

        assert status_code == 400
        mock_get_tracker.assert_not_called()
        mock_msg_store.assert_not_called()


# ============================================================================
# Handler integration tests — ACK pre_merge_condition (#2005)
# ============================================================================


_SUBSTANTIVE_CONDITION = (
    "A human must run `git mv legacy/x new/x` before merging — "
    "the gateway blocks rename pushes from agent containers."
)


class TestAckPreMergeConditionValidation:
    """Conditional ACK rejects boilerplate/short ``pre_merge_condition`` values.

    Plain ACKs (no condition, empty condition, whitespace-only condition)
    must continue to pass through unaffected so the validator is
    regression-safe for non-conditional ACKs.
    """

    def _ack_with_payload(self, app, payload):
        with app.app_context():
            from routes.signals import handle_consensus_ack_signal

            mock_tracker = MagicMock()
            mock_tracker.handle_ack.return_value = {
                "status": "acked",
                "version": 1,
                "fully_acked": False,
            }

            with (
                patch(
                    "peer_consensus.get_peer_consensus_tracker",
                    return_value=mock_tracker,
                ),
                patch("message_store.get_message_store") as mock_msg_store,
                patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
            ):
                mock_msg_store.return_value = MagicMock()

                response, status_code = handle_consensus_ack_signal(
                    "issue-42",
                    {
                        "agent_role": "reviewer_code",
                        "producer_role": "coder",
                        "ack_version": 1,
                        "payload": payload,
                    },
                    Path("/tmp/repo"),
                )
            return response, status_code, mock_tracker

    def test_plain_ack_with_no_condition_key_passes(self, app):
        """Omitting ``pre_merge_condition`` entirely is a plain ACK."""
        _, status_code, tracker = self._ack_with_payload(app, {"reason": _SUBSTANTIVE_REASON})
        assert status_code == 200
        tracker.handle_ack.assert_called_once()

    def test_plain_ack_with_empty_condition_passes(self, app):
        """Empty-string ``pre_merge_condition`` is a plain ACK."""
        _, status_code, tracker = self._ack_with_payload(
            app, {"reason": _SUBSTANTIVE_REASON, "pre_merge_condition": ""}
        )
        assert status_code == 200
        tracker.handle_ack.assert_called_once()

    def test_plain_ack_with_whitespace_condition_passes(self, app):
        """Whitespace-only ``pre_merge_condition`` is a plain ACK."""
        _, status_code, tracker = self._ack_with_payload(
            app,
            {"reason": _SUBSTANTIVE_REASON, "pre_merge_condition": "   \t\n "},
        )
        assert status_code == 200
        tracker.handle_ack.assert_called_once()

    def test_short_condition_returns_400(self, app):
        response, status_code, tracker = self._ack_with_payload(
            app,
            {
                "reason": _SUBSTANTIVE_REASON,
                "pre_merge_condition": "see above",
            },
        )
        assert status_code == 400
        data = json.loads(response.data)
        assert "Pre-merge condition" in data["message"]
        assert "chars" in data["message"]
        tracker.handle_ack.assert_not_called()

    def test_short_imperative_condition_passes(self, app):
        """A 14-char condition passes under the ``_BRC_CONDITION_KINDS`` dispatch.

        ``_validate_brc_content`` uses ``_BRC_CONDITION_MIN_LEN`` (10) for
        content kinds in ``_BRC_CONDITION_KINDS`` (e.g. "pre-merge condition")
        instead of the default 50-char minimum.  This boundary value (14 chars)
        would have been rejected without the kind-based dispatch (#2005).
        """
        _, status_code, tracker = self._ack_with_payload(
            app,
            {
                "reason": _SUBSTANTIVE_REASON,
                "pre_merge_condition": "rotate API key",
            },
        )
        assert status_code == 200
        tracker.handle_ack.assert_called_once()

    def test_boilerplate_condition_returns_400(self, app):
        """The shared boilerplate set applies to conditions too."""
        response, status_code, tracker = self._ack_with_payload(
            app,
            {
                "reason": _SUBSTANTIVE_REASON,
                "pre_merge_condition": "LGTM",
            },
        )
        assert status_code == 400
        data = json.loads(response.data)
        assert "Pre-merge condition" in data["message"]
        assert "boilerplate" in data["message"]
        tracker.handle_ack.assert_not_called()

    def test_substantive_condition_passes(self, app):
        _, status_code, tracker = self._ack_with_payload(
            app,
            {
                "reason": _SUBSTANTIVE_REASON,
                "pre_merge_condition": _SUBSTANTIVE_CONDITION,
            },
        )
        assert status_code == 200
        tracker.handle_ack.assert_called_once()

    def test_resolution_without_condition_returns_400(self, app):
        """A resolution SHA on a plain ACK is rejected at the boundary (#2336).

        ``handle_consensus_ack_signal`` rejects the request with HTTP 400
        before reaching the tracker so the invariant downstream code relies
        on (resolution implies obligation) is enforced at the perimeter.
        """
        response, status_code, tracker = self._ack_with_payload(
            app,
            {
                "reason": _SUBSTANTIVE_REASON,
                "pre_merge_condition_resolved_in_diff": "abc1234",
            },
        )
        assert status_code == 400
        data = json.loads(response.data)
        assert "pre_merge_condition_resolved_in_diff" in data["message"]
        assert "requires" in data["message"]
        tracker.handle_ack.assert_not_called()

    def test_resolution_with_condition_passes(self, app):
        """Resolution SHA alongside an obligation passes through to the tracker (#2336)."""
        _, status_code, tracker = self._ack_with_payload(
            app,
            {
                "reason": _SUBSTANTIVE_REASON,
                "pre_merge_condition": _SUBSTANTIVE_CONDITION,
                "pre_merge_condition_resolved_in_diff": "abc1234",
            },
        )
        assert status_code == 200
        tracker.handle_ack.assert_called_once()


# ============================================================================
# Handler integration tests — NACK
# ============================================================================


class TestNackContentValidation:
    """NACK handler rejects invalid reasons with HTTP 400."""

    def test_empty_reason_returns_400(self, app):
        with app.app_context():
            from routes.signals import handle_consensus_nack_signal

            response, status_code = handle_consensus_nack_signal(
                "issue-42",
                {
                    "agent_role": "reviewer_code",
                    "producer_role": "coder",
                    "payload": {"reason": ""},
                },
                Path("/tmp/repo"),
            )

        assert status_code == 400
        data = json.loads(response.data)
        assert "empty" in data["message"]

    def test_short_reason_returns_400(self, app):
        with app.app_context():
            from routes.signals import handle_consensus_nack_signal

            response, status_code = handle_consensus_nack_signal(
                "issue-42",
                {
                    "agent_role": "reviewer_code",
                    "producer_role": "coder",
                    "payload": {"reason": "Missing tests"},
                },
                Path("/tmp/repo"),
            )

        assert status_code == 400
        data = json.loads(response.data)
        assert "chars" in data["message"]

    def test_boilerplate_reason_returns_400(self, app):
        with app.app_context():
            from routes.signals import handle_consensus_nack_signal

            response, status_code = handle_consensus_nack_signal(
                "issue-42",
                {
                    "agent_role": "reviewer_code",
                    "producer_role": "coder",
                    "payload": {"reason": "No issues"},
                },
                Path("/tmp/repo"),
            )

        assert status_code == 400
        data = json.loads(response.data)
        assert "boilerplate" in data["message"]

    def test_substantive_reason_passes(self, app):
        with app.app_context():
            from routes.signals import handle_consensus_nack_signal

            mock_tracker = MagicMock()
            mock_tracker.handle_nack.return_value = {
                "reason": "Detailed rejection",
                "revision_count": 1,
            }

            with (
                patch(
                    "peer_consensus.get_peer_consensus_tracker",
                    return_value=mock_tracker,
                ),
                patch("message_store.get_message_store") as mock_msg_store,
                patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
            ):
                mock_store_inst = MagicMock()
                mock_msg_store.return_value = mock_store_inst

                response, status_code = handle_consensus_nack_signal(
                    "issue-42",
                    {
                        "agent_role": "reviewer_code",
                        "producer_role": "coder",
                        "payload": {
                            "reason": (
                                "SQL injection vulnerability in auth.py line 42: "
                                "user input concatenated into query string. "
                                "Use parameterized queries instead."
                            ),
                        },
                    },
                    Path("/tmp/repo"),
                )

        assert status_code == 200
        mock_tracker.handle_nack.assert_called_once()

    def test_no_tracker_mutation_on_400(self, app):
        """When NACK content validation fails, tracker is never called."""
        with (
            app.app_context(),
            patch("peer_consensus.get_peer_consensus_tracker") as mock_get_tracker,
            patch("message_store.get_message_store") as mock_msg_store,
        ):
            from routes.signals import handle_consensus_nack_signal

            response, status_code = handle_consensus_nack_signal(
                "issue-42",
                {
                    "agent_role": "reviewer_code",
                    "producer_role": "coder",
                    "payload": {"reason": "bad"},
                },
                Path("/tmp/repo"),
            )

        assert status_code == 400
        mock_get_tracker.assert_not_called()
        mock_msg_store.assert_not_called()


# ============================================================================
# Handler integration tests — Withdraw
# ============================================================================


class TestWithdrawContentValidation:
    """Withdraw handler rejects invalid reasons with HTTP 400."""

    def test_empty_reason_returns_400(self, app):
        with app.app_context():
            from routes.signals import handle_consensus_withdraw_signal

            response, status_code = handle_consensus_withdraw_signal(
                "issue-42",
                {
                    "agent_role": "coder",
                    "reason": "",
                },
                Path("/tmp/repo"),
            )

        assert status_code == 400
        data = json.loads(response.data)
        assert "empty" in data["message"]

    def test_missing_reason_returns_400(self, app):
        """No reason key at all defaults to empty string → rejected."""
        with app.app_context():
            from routes.signals import handle_consensus_withdraw_signal

            response, status_code = handle_consensus_withdraw_signal(
                "issue-42",
                {
                    "agent_role": "coder",
                },
                Path("/tmp/repo"),
            )

        assert status_code == 400
        data = json.loads(response.data)
        assert "empty" in data["message"]

    def test_short_reason_returns_400(self, app):
        with app.app_context():
            from routes.signals import handle_consensus_withdraw_signal

            response, status_code = handle_consensus_withdraw_signal(
                "issue-42",
                {
                    "agent_role": "coder",
                    "reason": "Need to rework",
                },
                Path("/tmp/repo"),
            )

        assert status_code == 400
        data = json.loads(response.data)
        assert "chars" in data["message"]

    def test_substantive_reason_passes(self, app):
        with app.app_context():
            from routes.signals import handle_consensus_withdraw_signal

            mock_tracker = MagicMock()
            mock_tracker.handle_withdraw.return_value = {
                "status": "withdrawn",
            }

            with (
                patch(
                    "peer_consensus.get_peer_consensus_tracker",
                    return_value=mock_tracker,
                ),
                patch("message_store.get_message_store") as mock_msg_store,
                patch("routes.signals._resolve_pipeline_phase", return_value="implement"),
            ):
                mock_store_inst = MagicMock()
                mock_msg_store.return_value = mock_store_inst

                response, status_code = handle_consensus_withdraw_signal(
                    "issue-42",
                    {
                        "agent_role": "coder",
                        "reason": (
                            "Withdrawing proposal: discovered that the JWT "
                            "validation approach has a timing attack vulnerability. "
                            "Need to redesign with constant-time comparison."
                        ),
                    },
                    Path("/tmp/repo"),
                )

        assert status_code == 200
        mock_tracker.handle_withdraw.assert_called_once()

    def test_no_tracker_mutation_on_400(self, app):
        """When withdraw content validation fails, tracker is never called."""
        with (
            app.app_context(),
            patch("peer_consensus.get_peer_consensus_tracker") as mock_get_tracker,
            patch("message_store.get_message_store") as mock_msg_store,
        ):
            from routes.signals import handle_consensus_withdraw_signal

            response, status_code = handle_consensus_withdraw_signal(
                "issue-42",
                {
                    "agent_role": "coder",
                    "reason": "nah",
                },
                Path("/tmp/repo"),
            )

        assert status_code == 400
        mock_get_tracker.assert_not_called()
        mock_msg_store.assert_not_called()


# ============================================================================
# Error message quality
# ============================================================================


class TestErrorMessageActionable:
    """Error messages guide the agent to fix the issue."""

    def test_empty_error_mentions_empty(self):
        err = _validate_brc_content("", "ACK reason")
        assert err is not None
        assert "empty" in err.lower()

    def test_short_error_reports_actual_length(self):
        err = _validate_brc_content("short", "Proposal summary")
        assert err is not None
        assert "5 chars" in err

    def test_boilerplate_error_quotes_input(self):
        err = _validate_brc_content("lgtm", "ACK reason")
        assert err is not None
        assert "lgtm" in err

    def test_error_suggests_what_to_include(self):
        err = _validate_brc_content("ok", "ACK reason")
        assert err is not None
        assert "substantive rationale" in err.lower()
