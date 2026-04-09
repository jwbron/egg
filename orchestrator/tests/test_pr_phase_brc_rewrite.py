"""Tests for PR-phase BRC history re-write safety net (#1599).

The PR phase should re-write BRC history for all completed phases
before creating the PR, as a safety net for cases where per-phase
pushes failed silently.
"""

import sys
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

# Mock heavy dependencies that pipelines.py imports at module level
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

from message_store import Message, MessageStore, MessageType
from models import PipelineStatus


def _make_brc_message(
    pipeline_id="issue-42",
    from_role="coder",
    message_type=MessageType.CONSENSUS_PROPOSE,
    subject="Proposal",
    body="test body",
    phase="implement",
    timestamp=None,
):
    """Create a BRC message for testing."""
    return Message(
        pipeline_id=pipeline_id,
        from_role=from_role,
        to_role="all",
        message_type=message_type,
        subject=subject,
        body=body,
        phase=phase,
        timestamp=timestamp or datetime(2026, 4, 8, 12, 0, 0, tzinfo=UTC),
        metadata={},
    )


class TestPrPhaseBrcRewrite:
    """Tests for BRC history re-write in the PR phase path."""

    def test_writes_history_for_completed_phases(self, tmp_path):
        """_write_brc_history is called for each phase with COMPLETE status."""
        from routes.pipelines import _write_brc_history

        messages_by_phase = {}
        for phase in ["refine", "plan", "implement"]:
            messages_by_phase[phase] = [
                _make_brc_message(
                    pipeline_id="issue-42",
                    from_role="coder",
                    message_type=MessageType.CONSENSUS_PROPOSE,
                    subject=f"Proposal for {phase}",
                    body=f"{phase} work",
                    phase=phase,
                ),
                _make_brc_message(
                    pipeline_id="issue-42",
                    from_role="coder",
                    message_type=MessageType.CONSENSUS_CONFIRMED,
                    subject=f"Confirmed for {phase}",
                    body="",
                    phase=phase,
                ),
            ]

        all_messages = []
        for msgs in messages_by_phase.values():
            all_messages.extend(msgs)

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = all_messages

        with patch("message_store.get_message_store", return_value=mock_store):
            for phase in ["refine", "plan", "implement"]:
                _write_brc_history(tmp_path, "issue-42", phase, 42)

        history_dir = tmp_path / ".egg-state" / "brc-history"
        assert (history_dir / "42-refine.md").exists()
        assert (history_dir / "42-plan.md").exists()
        assert (history_dir / "42-implement.md").exists()

    def test_skips_non_complete_phases(self, tmp_path):
        """Phases with FAILED or RUNNING status are not written."""
        from routes.pipelines import _write_brc_history

        # Simulate the PR-phase logic: only iterate completed phases
        phase_statuses = {
            "refine": PipelineStatus.COMPLETE,
            "plan": PipelineStatus.COMPLETE,
            "implement": PipelineStatus.FAILED,
        }

        messages = [
            _make_brc_message(phase="refine"),
            _make_brc_message(phase="plan"),
            _make_brc_message(phase="implement"),
        ]

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            for phase_name, status in phase_statuses.items():
                if status == PipelineStatus.COMPLETE:
                    _write_brc_history(tmp_path, "issue-42", phase_name, 42)

        history_dir = tmp_path / ".egg-state" / "brc-history"
        assert (history_dir / "42-refine.md").exists()
        assert (history_dir / "42-plan.md").exists()
        # implement was FAILED, so not written
        assert not (history_dir / "42-implement.md").exists()

    def test_idempotent_rewrite(self, tmp_path):
        """Re-writing BRC history overwrites existing files safely."""
        from routes.pipelines import _write_brc_history

        messages = [
            _make_brc_message(
                phase="implement",
                subject="First proposal",
                body="first version",
            ),
        ]

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        history_file = tmp_path / ".egg-state" / "brc-history" / "42-implement.md"
        first_content = history_file.read_text()

        # Add more messages and re-write
        messages.append(
            _make_brc_message(
                phase="implement",
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_ACK,
                subject="ACK",
                body="looks good",
            ),
        )
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        second_content = history_file.read_text()
        assert second_content != first_content
        assert "CONSENSUS_ACK" in second_content

    def test_brc_error_does_not_propagate(self, tmp_path):
        """Errors during BRC history write are caught gracefully."""
        from routes.pipelines import _write_brc_history

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.side_effect = Exception("Store unavailable")

        with patch("message_store.get_message_store", return_value=mock_store):
            # Should not raise
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        history_dir = tmp_path / ".egg-state" / "brc-history"
        if history_dir.exists():
            assert list(history_dir.iterdir()) == []


class TestRewriteBrcHistoryForPr:
    """Integration tests for _rewrite_brc_history_for_pr.

    These exercise the actual PR-phase integration loop — verifying that
    the function iterates pipeline.phases, filters by COMPLETE status,
    and handles errors from _write_brc_history via the outer try/except.
    """

    def test_calls_write_for_completed_phases_only(self, tmp_path):
        """_write_brc_history is called only for COMPLETE phases."""
        from routes.pipelines import _rewrite_brc_history_for_pr

        # Build a mock pipeline.phases dict with mixed statuses
        phases = {
            "refine": MagicMock(status=PipelineStatus.COMPLETE),
            "plan": MagicMock(status=PipelineStatus.COMPLETE),
            "implement": MagicMock(status=PipelineStatus.FAILED),
            "review": MagicMock(status=PipelineStatus.RUNNING),
        }

        with (
            patch("routes.pipelines._write_brc_history") as mock_write,
            patch("routes.pipelines._commit_statefiles_to_worktree"),
        ):
            _rewrite_brc_history_for_pr(tmp_path, "issue-42", phases, 42)

        # Should be called exactly for refine and plan (COMPLETE), not implement or review
        assert mock_write.call_count == 2
        called_phases = {call.args[2] for call in mock_write.call_args_list}
        assert called_phases == {"refine", "plan"}

    def test_passes_correct_arguments(self, tmp_path):
        """_write_brc_history receives worktree_path, pipeline_id, phase, identifier."""
        from routes.pipelines import _rewrite_brc_history_for_pr

        phases = {
            "implement": MagicMock(status=PipelineStatus.COMPLETE),
        }

        with (
            patch("routes.pipelines._write_brc_history") as mock_write,
            patch("routes.pipelines._commit_statefiles_to_worktree"),
        ):
            _rewrite_brc_history_for_pr(tmp_path, "issue-99", phases, 1599)

        mock_write.assert_called_once_with(tmp_path, "issue-99", "implement", 1599)

    def test_commits_after_rewrite(self, tmp_path):
        """_commit_statefiles_to_worktree is called after history writes."""
        from routes.pipelines import _rewrite_brc_history_for_pr

        phases = {
            "refine": MagicMock(status=PipelineStatus.COMPLETE),
        }

        with (
            patch("routes.pipelines._write_brc_history"),
            patch("routes.pipelines._commit_statefiles_to_worktree") as mock_commit,
        ):
            _rewrite_brc_history_for_pr(tmp_path, "issue-42", phases, 42)

        mock_commit.assert_called_once_with(
            tmp_path,
            "Persist BRC history files for PR",
            pipeline_identifier=42,
        )

    def test_outer_except_catches_write_brc_history_exception(self, tmp_path):
        """PermissionError from _write_brc_history is caught by the outer handler.

        _write_brc_history handles its own internal errors (e.g. message store
        failures), but filesystem errors like PermissionError propagate out and
        must be caught by the outer try/except in the PR-phase loop.
        """
        from routes.pipelines import _rewrite_brc_history_for_pr

        phases = {
            "refine": MagicMock(status=PipelineStatus.COMPLETE),
            "plan": MagicMock(status=PipelineStatus.COMPLETE),
        }

        call_count = {"n": 0}

        def fail_on_first_phase(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise PermissionError("Read-only filesystem")

        with (
            patch(
                "routes.pipelines._write_brc_history", side_effect=fail_on_first_phase
            ) as mock_write,
            patch("routes.pipelines._commit_statefiles_to_worktree"),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            # Should not raise despite PermissionError
            _rewrite_brc_history_for_pr(tmp_path, "issue-42", phases, 42)

        # Both phases attempted (error on first doesn't skip second)
        assert mock_write.call_count == 2
        # Warning logged for the failed phase
        mock_logger.warning.assert_called()
        warning_msg = mock_logger.warning.call_args_list[0][0][0]
        assert "Failed to re-write BRC history" in warning_msg

    def test_noop_when_no_completed_phases(self, tmp_path):
        """No calls to _write_brc_history when no phases are COMPLETE."""
        from routes.pipelines import _rewrite_brc_history_for_pr

        phases = {
            "refine": MagicMock(status=PipelineStatus.RUNNING),
            "plan": MagicMock(status=PipelineStatus.PENDING),
        }

        with (
            patch("routes.pipelines._write_brc_history") as mock_write,
            patch("routes.pipelines._commit_statefiles_to_worktree") as mock_commit,
        ):
            _rewrite_brc_history_for_pr(tmp_path, "issue-42", phases, 42)

        mock_write.assert_not_called()
        # Commit is still attempted (no-op if nothing staged)
        mock_commit.assert_called_once()
