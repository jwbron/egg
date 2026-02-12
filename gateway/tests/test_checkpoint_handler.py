"""Tests for checkpoint_handler module - per-commit checkpoint creation."""

import subprocess
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch, call

import pytest

# Import from conftest-loaded modules
from checkpoint_handler import (
    get_commits_in_push,
    capture_and_store_checkpoints_for_push,
    CheckpointHandler,
)


class TestGetCommitsInPush:
    """Tests for get_commits_in_push function."""

    def test_new_branch_returns_single_commit(self):
        """Test that pushing a new branch returns only the tip commit."""
        null_sha = "0" * 40
        new_sha = "abc123def456789012345678901234567890abcd"

        commits = get_commits_in_push("/some/repo", null_sha, new_sha)
        assert commits == [new_sha]

    def test_empty_old_sha_returns_single_commit(self):
        """Test that empty old_sha is treated like null SHA."""
        new_sha = "abc123def456789012345678901234567890abcd"

        commits = get_commits_in_push("/some/repo", "", new_sha)
        assert commits == [new_sha]

    @patch("checkpoint_handler.subprocess.run")
    def test_single_commit_push(self, mock_run):
        """Test pushing a single commit."""
        old_sha = "1111111111111111111111111111111111111111"
        new_sha = "2222222222222222222222222222222222222222"

        # Mock git rev-list returning single commit
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=f"{new_sha}\n",
        )

        commits = get_commits_in_push("/repo", old_sha, new_sha)

        assert len(commits) == 1
        assert commits[0] == new_sha

        # Verify git rev-list was called correctly
        mock_run.assert_called_once()
        args = mock_run.call_args
        assert "rev-list" in args[0][0]
        assert "--reverse" in args[0][0]
        assert f"{old_sha}..{new_sha}" in args[0][0]

    @patch("checkpoint_handler.subprocess.run")
    def test_multi_commit_push(self, mock_run):
        """Test pushing multiple commits returns them in chronological order."""
        old_sha = "0000000000000000000000000000000000000000"
        commit1 = "1111111111111111111111111111111111111111"
        commit2 = "2222222222222222222222222222222222222222"
        commit3 = "3333333333333333333333333333333333333333"
        new_sha = commit3

        # Mock git rev-list returning multiple commits (oldest first due to --reverse)
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=f"{commit1}\n{commit2}\n{commit3}\n",
        )

        # For new branch (old_sha is null), we don't call rev-list
        # Let's use a non-null old_sha
        old_sha = "0000000000000000000000000000000000000001"

        commits = get_commits_in_push("/repo", old_sha, new_sha)

        assert len(commits) == 3
        # Chronological order (oldest first)
        assert commits == [commit1, commit2, commit3]

    @patch("checkpoint_handler.subprocess.run")
    def test_rev_list_failure_falls_back_to_new_sha(self, mock_run):
        """Test that git rev-list failure falls back to new_sha."""
        old_sha = "1111111111111111111111111111111111111111"
        new_sha = "2222222222222222222222222222222222222222"

        # Mock git rev-list failing
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="error: some git error",
        )

        commits = get_commits_in_push("/repo", old_sha, new_sha)
        assert commits == [new_sha]

    @patch("checkpoint_handler.subprocess.run")
    def test_rev_list_empty_falls_back_to_new_sha(self, mock_run):
        """Test that empty rev-list output falls back to new_sha."""
        old_sha = "1111111111111111111111111111111111111111"
        new_sha = "2222222222222222222222222222222222222222"

        # Mock git rev-list returning empty
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="",
        )

        commits = get_commits_in_push("/repo", old_sha, new_sha)
        assert commits == [new_sha]

    @patch("checkpoint_handler.subprocess.run")
    def test_rev_list_timeout_falls_back_to_new_sha(self, mock_run):
        """Test that git rev-list timeout falls back to new_sha."""
        old_sha = "1111111111111111111111111111111111111111"
        new_sha = "2222222222222222222222222222222222222222"

        # Mock git rev-list timing out
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=30)

        commits = get_commits_in_push("/repo", old_sha, new_sha)
        assert commits == [new_sha]

    @patch("checkpoint_handler.subprocess.run")
    def test_rev_list_exception_falls_back_to_new_sha(self, mock_run):
        """Test that any exception falls back to new_sha."""
        old_sha = "1111111111111111111111111111111111111111"
        new_sha = "2222222222222222222222222222222222222222"

        # Mock git rev-list raising an exception
        mock_run.side_effect = Exception("Unexpected error")

        commits = get_commits_in_push("/repo", old_sha, new_sha)
        assert commits == [new_sha]


class TestCaptureAndStoreCheckpointsForPush:
    """Tests for capture_and_store_checkpoints_for_push function."""

    def test_returns_empty_when_disabled(self):
        """Test that function returns empty list when checkpoints are disabled."""
        import checkpoint_handler

        original = checkpoint_handler.CHECKPOINT_ENABLED
        checkpoint_handler.CHECKPOINT_ENABLED = False

        try:
            result = checkpoint_handler.capture_and_store_checkpoints_for_push(
                repo_path="/repo",
                old_sha="0" * 40,
                new_sha="abc123",
                branch="main",
            )
            assert result == []
        finally:
            checkpoint_handler.CHECKPOINT_ENABLED = original

    @patch("checkpoint_handler.get_commits_in_push")
    def test_returns_empty_for_no_commits(self, mock_get_commits):
        """Test that empty commit list returns empty checkpoint list."""
        mock_get_commits.return_value = []

        import checkpoint_handler

        result = checkpoint_handler.capture_and_store_checkpoints_for_push(
            repo_path="/repo",
            old_sha="old",
            new_sha="new",
            branch="main",
        )
        assert result == []

    @patch("checkpoint_handler.get_commits_in_push")
    @patch("checkpoint_handler.get_checkpoint_handler")
    def test_creates_checkpoint_for_each_commit(self, mock_get_handler, mock_get_commits):
        """Test that a checkpoint is created for each commit in the push."""
        from egg_contracts.checkpoints import Checkpoint, SessionMetadata

        # Mock 3 commits
        commit1 = "1111111111111111111111111111111111111111"
        commit2 = "2222222222222222222222222222222222222222"
        commit3 = "3333333333333333333333333333333333333333"
        mock_get_commits.return_value = [commit1, commit2, commit3]

        # Mock handler
        mock_handler = MagicMock()
        mock_get_handler.return_value = mock_handler

        # Create mock checkpoints
        def create_checkpoint(
            repo_path, commit_sha, branch, session=None, issue_number=None, pipeline_phase=None, push_sha=None
        ):
            return Checkpoint(
                id=f"ckpt-{commit_sha[:12]}",
                commit_sha=commit_sha,
                session=SessionMetadata(session_id="test", started_at=datetime.now(UTC)),
                created_at=datetime.now(UTC),
                push_sha=push_sha,
                branch=branch,
            )

        mock_handler.capture_checkpoint.side_effect = create_checkpoint
        mock_handler.store_checkpoint.return_value = True

        import checkpoint_handler

        checkpoints = checkpoint_handler.capture_and_store_checkpoints_for_push(
            repo_path="/repo",
            old_sha="0" * 40,
            new_sha=commit3,
            branch="main",
            async_store=False,
        )

        # Should have 3 checkpoints
        assert len(checkpoints) == 3
        assert mock_handler.capture_checkpoint.call_count == 3
        assert mock_handler.store_checkpoint.call_count == 3

    @patch("checkpoint_handler.get_commits_in_push")
    @patch("checkpoint_handler.get_checkpoint_handler")
    def test_push_sha_set_on_all_checkpoints(self, mock_get_handler, mock_get_commits):
        """Test that push_sha is set to the tip commit for all checkpoints."""
        from egg_contracts.checkpoints import Checkpoint, SessionMetadata

        # Mock 2 commits
        commit1 = "1111111111111111111111111111111111111111"
        commit2 = "2222222222222222222222222222222222222222"
        mock_get_commits.return_value = [commit1, commit2]

        # Track push_sha values
        captured_push_shas = []

        def capture_and_record(
            repo_path, commit_sha, branch, session=None, issue_number=None, pipeline_phase=None, push_sha=None
        ):
            captured_push_shas.append(push_sha)
            return Checkpoint(
                id=f"ckpt-{commit_sha[:12]}",
                commit_sha=commit_sha,
                session=SessionMetadata(session_id="test", started_at=datetime.now(UTC)),
                created_at=datetime.now(UTC),
                push_sha=push_sha,
                branch=branch,
            )

        mock_handler = MagicMock()
        mock_handler.capture_checkpoint.side_effect = capture_and_record
        mock_handler.store_checkpoint.return_value = True
        mock_get_handler.return_value = mock_handler

        import checkpoint_handler

        checkpoints = checkpoint_handler.capture_and_store_checkpoints_for_push(
            repo_path="/repo",
            old_sha="0" * 40,
            new_sha=commit2,
            branch="main",
            async_store=False,
        )

        # All push_sha values should be commit2 (the tip)
        assert all(ps == commit2 for ps in captured_push_shas)
        assert len(captured_push_shas) == 2

    @patch("checkpoint_handler.get_commits_in_push")
    @patch("checkpoint_handler.get_checkpoint_handler")
    def test_continues_on_individual_checkpoint_failure(self, mock_get_handler, mock_get_commits):
        """Test that failure to capture one checkpoint doesn't stop others."""
        from egg_contracts.checkpoints import Checkpoint, SessionMetadata

        # Mock 3 commits
        commit1 = "1111111111111111111111111111111111111111"
        commit2 = "2222222222222222222222222222222222222222"
        commit3 = "3333333333333333333333333333333333333333"
        mock_get_commits.return_value = [commit1, commit2, commit3]

        call_count = [0]

        def capture_with_failure(
            repo_path, commit_sha, branch, session=None, issue_number=None, pipeline_phase=None, push_sha=None
        ):
            call_count[0] += 1
            if call_count[0] == 2:
                raise Exception("Simulated failure")
            return Checkpoint(
                id=f"ckpt-{commit_sha[:12]}",
                commit_sha=commit_sha,
                session=SessionMetadata(session_id="test", started_at=datetime.now(UTC)),
                created_at=datetime.now(UTC),
                push_sha=push_sha,
                branch=branch,
            )

        mock_handler = MagicMock()
        mock_handler.capture_checkpoint.side_effect = capture_with_failure
        mock_handler.store_checkpoint.return_value = True
        mock_get_handler.return_value = mock_handler

        import checkpoint_handler

        checkpoints = checkpoint_handler.capture_and_store_checkpoints_for_push(
            repo_path="/repo",
            old_sha="0" * 40,
            new_sha=commit3,
            branch="main",
            async_store=False,
        )

        # Should have 2 checkpoints (first and third succeeded)
        assert len(checkpoints) == 2
        assert call_count[0] == 3
        assert mock_handler.store_checkpoint.call_count == 2

    @patch("checkpoint_handler.get_commits_in_push")
    @patch("checkpoint_handler.get_checkpoint_handler")
    def test_async_store_uses_thread(self, mock_get_handler, mock_get_commits):
        """Test that async_store=True stores in a background thread."""
        from egg_contracts.checkpoints import Checkpoint, SessionMetadata

        commit1 = "1111111111111111111111111111111111111111"
        mock_get_commits.return_value = [commit1]

        mock_handler = MagicMock()
        mock_handler.capture_checkpoint.return_value = Checkpoint(
            id="ckpt-1111111111111",
            commit_sha=commit1,
            session=SessionMetadata(session_id="test", started_at=datetime.now(UTC)),
            created_at=datetime.now(UTC),
            branch="main",
        )
        mock_get_handler.return_value = mock_handler

        import checkpoint_handler

        with patch("checkpoint_handler.threading.Thread") as mock_thread:
            mock_thread_instance = MagicMock()
            mock_thread.return_value = mock_thread_instance

            checkpoints = checkpoint_handler.capture_and_store_checkpoints_for_push(
                repo_path="/repo",
                old_sha="0" * 40,
                new_sha=commit1,
                branch="main",
                async_store=True,  # Use async
            )

            # Thread should have been created and started
            mock_thread.assert_called_once()
            mock_thread_instance.start.assert_called_once()

            # store_checkpoint should NOT have been called directly (it's in the thread)
            mock_handler.store_checkpoint.assert_not_called()

    @patch("checkpoint_handler.get_commits_in_push")
    @patch("checkpoint_handler.get_checkpoint_handler")
    def test_checkpoint_returns_none_excluded(self, mock_get_handler, mock_get_commits):
        """Test that checkpoints returning None are excluded from result."""
        from egg_contracts.checkpoints import Checkpoint, SessionMetadata

        commit1 = "1111111111111111111111111111111111111111"
        commit2 = "2222222222222222222222222222222222222222"
        mock_get_commits.return_value = [commit1, commit2]

        call_count = [0]

        def sometimes_returns_none(
            repo_path, commit_sha, branch, session=None, issue_number=None, pipeline_phase=None, push_sha=None
        ):
            call_count[0] += 1
            if call_count[0] == 1:
                return None
            return Checkpoint(
                id=f"ckpt-{commit_sha[:12]}",
                commit_sha=commit_sha,
                session=SessionMetadata(session_id="test", started_at=datetime.now(UTC)),
                created_at=datetime.now(UTC),
                push_sha=push_sha,
                branch=branch,
            )

        mock_handler = MagicMock()
        mock_handler.capture_checkpoint.side_effect = sometimes_returns_none
        mock_handler.store_checkpoint.return_value = True
        mock_get_handler.return_value = mock_handler

        import checkpoint_handler

        checkpoints = checkpoint_handler.capture_and_store_checkpoints_for_push(
            repo_path="/repo",
            old_sha="0" * 40,
            new_sha=commit2,
            branch="main",
            async_store=False,
        )

        # Only second commit's checkpoint should be returned
        assert len(checkpoints) == 1
        assert checkpoints[0].commit_sha == commit2
