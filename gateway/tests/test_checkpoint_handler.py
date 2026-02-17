"""Tests for checkpoint_handler module - per-commit and session-end checkpoint creation."""

import subprocess
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

# Import from conftest-loaded modules
from checkpoint_handler import (
    _resolve_agent_type,
    _resolve_github_token,
    capture_session_end_checkpoint,
    get_commits_in_push,
)
from session_manager import Session, _hash_token


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
        from egg_contracts.checkpoints import CheckpointV2, SessionMetadata, TriggerType

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
            repo_path,
            commit_sha,
            branch,
            session=None,
            issue_number=None,
            pipeline_phase=None,
            push_sha=None,
        ):
            now = datetime.now(UTC)
            return CheckpointV2(
                id=f"ckpt-{commit_sha[:12]}",
                trigger_type=TriggerType.COMMIT,
                commit_sha=commit_sha,
                session_id="test",
                session=SessionMetadata(session_id="test", started_at=now),
                created_at=now,
                session_started_at=now,
                push_sha=push_sha,
                branch=branch,
            )

        mock_handler.capture_checkpoint.side_effect = create_checkpoint
        mock_handler.store_checkpoint_v2.return_value = True

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
        assert mock_handler.store_checkpoint_v2.call_count == 3

    @patch("checkpoint_handler.get_commits_in_push")
    @patch("checkpoint_handler.get_checkpoint_handler")
    def test_push_sha_set_on_all_checkpoints(self, mock_get_handler, mock_get_commits):
        """Test that push_sha is set to the tip commit for all checkpoints."""
        from egg_contracts.checkpoints import CheckpointV2, SessionMetadata, TriggerType

        # Mock 2 commits
        commit1 = "1111111111111111111111111111111111111111"
        commit2 = "2222222222222222222222222222222222222222"
        mock_get_commits.return_value = [commit1, commit2]

        # Track push_sha values
        captured_push_shas = []

        def capture_and_record(
            repo_path,
            commit_sha,
            branch,
            session=None,
            issue_number=None,
            pipeline_phase=None,
            push_sha=None,
        ):
            captured_push_shas.append(push_sha)
            now = datetime.now(UTC)
            return CheckpointV2(
                id=f"ckpt-{commit_sha[:12]}",
                trigger_type=TriggerType.COMMIT,
                commit_sha=commit_sha,
                session_id="test",
                session=SessionMetadata(session_id="test", started_at=now),
                created_at=now,
                session_started_at=now,
                push_sha=push_sha,
                branch=branch,
            )

        mock_handler = MagicMock()
        mock_handler.capture_checkpoint.side_effect = capture_and_record
        mock_handler.store_checkpoint_v2.return_value = True
        mock_get_handler.return_value = mock_handler

        import checkpoint_handler

        checkpoint_handler.capture_and_store_checkpoints_for_push(
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
        from egg_contracts.checkpoints import CheckpointV2, SessionMetadata, TriggerType

        # Mock 3 commits
        commit1 = "1111111111111111111111111111111111111111"
        commit2 = "2222222222222222222222222222222222222222"
        commit3 = "3333333333333333333333333333333333333333"
        mock_get_commits.return_value = [commit1, commit2, commit3]

        call_count = [0]

        def capture_with_failure(
            repo_path,
            commit_sha,
            branch,
            session=None,
            issue_number=None,
            pipeline_phase=None,
            push_sha=None,
        ):
            call_count[0] += 1
            if call_count[0] == 2:
                raise Exception("Simulated failure")
            now = datetime.now(UTC)
            return CheckpointV2(
                id=f"ckpt-{commit_sha[:12]}",
                trigger_type=TriggerType.COMMIT,
                commit_sha=commit_sha,
                session_id="test",
                session=SessionMetadata(session_id="test", started_at=now),
                created_at=now,
                session_started_at=now,
                push_sha=push_sha,
                branch=branch,
            )

        mock_handler = MagicMock()
        mock_handler.capture_checkpoint.side_effect = capture_with_failure
        mock_handler.store_checkpoint_v2.return_value = True
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
        assert mock_handler.store_checkpoint_v2.call_count == 2

    @patch("checkpoint_handler.get_commits_in_push")
    @patch("checkpoint_handler.get_checkpoint_handler")
    def test_async_store_uses_thread(self, mock_get_handler, mock_get_commits):
        """Test that async_store=True stores in a background thread."""
        from egg_contracts.checkpoints import CheckpointV2, SessionMetadata, TriggerType

        commit1 = "1111111111111111111111111111111111111111"
        mock_get_commits.return_value = [commit1]

        mock_handler = MagicMock()
        now = datetime.now(UTC)
        mock_handler.capture_checkpoint.return_value = CheckpointV2(
            id="ckpt-1111111111111",
            trigger_type=TriggerType.COMMIT,
            commit_sha=commit1,
            session_id="test",
            session=SessionMetadata(session_id="test", started_at=now),
            created_at=now,
            session_started_at=now,
            branch="main",
        )
        mock_get_handler.return_value = mock_handler

        import checkpoint_handler

        with patch("checkpoint_handler.threading.Thread") as mock_thread:
            mock_thread_instance = MagicMock()
            mock_thread.return_value = mock_thread_instance

            checkpoint_handler.capture_and_store_checkpoints_for_push(
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
            mock_handler.store_checkpoint_v2.assert_not_called()

    @patch("checkpoint_handler.get_commits_in_push")
    @patch("checkpoint_handler.get_checkpoint_handler")
    def test_checkpoint_returns_none_excluded(self, mock_get_handler, mock_get_commits):
        """Test that checkpoints returning None are excluded from result."""
        from egg_contracts.checkpoints import CheckpointV2, SessionMetadata, TriggerType

        commit1 = "1111111111111111111111111111111111111111"
        commit2 = "2222222222222222222222222222222222222222"
        mock_get_commits.return_value = [commit1, commit2]

        call_count = [0]

        def sometimes_returns_none(
            repo_path,
            commit_sha,
            branch,
            session=None,
            issue_number=None,
            pipeline_phase=None,
            push_sha=None,
        ):
            call_count[0] += 1
            if call_count[0] == 1:
                return None
            now = datetime.now(UTC)
            return CheckpointV2(
                id=f"ckpt-{commit_sha[:12]}",
                trigger_type=TriggerType.COMMIT,
                commit_sha=commit_sha,
                session_id="test",
                session=SessionMetadata(session_id="test", started_at=now),
                created_at=now,
                session_started_at=now,
                push_sha=push_sha,
                branch=branch,
            )

        mock_handler = MagicMock()
        mock_handler.capture_checkpoint.side_effect = sometimes_returns_none
        mock_handler.store_checkpoint_v2.return_value = True
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


def _make_test_session(
    container_id="test-container",
    agent_role="coder",
    phase="implement",
    issue_number=530,
    pr_number=42,
    pipeline_id=None,
):
    """Create a test Session for checkpoint tests."""
    now = datetime.now(UTC)
    return Session(
        session_token="test-token",
        session_token_hash=_hash_token("test-token"),
        container_id=container_id,
        container_ip="172.18.0.5",
        mode="private",
        created_at=now - timedelta(hours=1),
        last_seen=now,
        expires_at=now + timedelta(hours=23),
        agent_role=agent_role,
        phase=phase,
        issue_number=issue_number,
        pr_number=pr_number,
        pipeline_id=pipeline_id,
    )


class TestCaptureSessionEndCheckpoint:
    """Tests for capture_session_end_checkpoint function."""

    def test_returns_none_when_disabled(self):
        """Returns (None, None) when checkpoints are disabled."""
        import checkpoint_handler

        original = checkpoint_handler.CHECKPOINT_ENABLED
        checkpoint_handler.CHECKPOINT_ENABLED = False

        try:
            from egg_contracts.checkpoints import SessionStatus

            session = _make_test_session()
            result = capture_session_end_checkpoint(
                session=session,
                session_status=SessionStatus.COMPLETED,
            )
            assert result == (None, None)
        finally:
            checkpoint_handler.CHECKPOINT_ENABLED = original

    @patch("checkpoint_handler.get_checkpoint_handler")
    def test_completed_session_creates_checkpoint(self, mock_get_handler):
        """Session-end with COMPLETED status creates a checkpoint."""
        from egg_contracts.checkpoints import (
            CheckpointV2,
            SessionMetadata,
            SessionStatus,
            TriggerType,
        )

        now = datetime.now(UTC)
        mock_handler = MagicMock()
        mock_handler.capture_session_end_checkpoint.return_value = CheckpointV2(
            id="ckpt-abc123def456",
            trigger_type=TriggerType.SESSION_END,
            session_status=SessionStatus.COMPLETED,
            session_id="test-container",
            session=SessionMetadata(session_id="test-container", started_at=now),
            created_at=now,
            session_started_at=now,
        )
        mock_handler.store_checkpoint_v2.return_value = True
        mock_get_handler.return_value = mock_handler

        session = _make_test_session()
        checkpoint, event = capture_session_end_checkpoint(
            session=session,
            session_status=SessionStatus.COMPLETED,
            repo_path="/home/egg/repos/test-repo",
            async_store=False,
        )

        assert checkpoint is not None
        assert checkpoint.trigger_type == TriggerType.SESSION_END
        assert checkpoint.session_status == SessionStatus.COMPLETED
        assert event is None  # sync store

    @patch("checkpoint_handler.get_checkpoint_handler")
    def test_expired_session_creates_checkpoint(self, mock_get_handler):
        """Session-end with EXPIRED status creates a checkpoint."""
        from egg_contracts.checkpoints import (
            CheckpointV2,
            SessionMetadata,
            SessionStatus,
            TriggerType,
        )

        now = datetime.now(UTC)
        mock_handler = MagicMock()
        mock_handler.capture_session_end_checkpoint.return_value = CheckpointV2(
            id="ckpt-abc123def456",
            trigger_type=TriggerType.SESSION_END,
            session_status=SessionStatus.EXPIRED,
            session_id="test-container",
            session=SessionMetadata(session_id="test-container", started_at=now),
            created_at=now,
            session_started_at=now,
        )
        mock_get_handler.return_value = mock_handler

        session = _make_test_session()
        checkpoint, event = capture_session_end_checkpoint(
            session=session,
            session_status=SessionStatus.EXPIRED,
            repo_path="/home/egg/repos/test-repo",
            async_store=False,
        )

        assert checkpoint is not None
        assert checkpoint.session_status == SessionStatus.EXPIRED

    @patch("checkpoint_handler.get_checkpoint_handler")
    def test_failed_session_creates_checkpoint(self, mock_get_handler):
        """Session-end with FAILED status creates a checkpoint."""
        from egg_contracts.checkpoints import (
            CheckpointV2,
            SessionMetadata,
            SessionStatus,
            TriggerType,
        )

        now = datetime.now(UTC)
        mock_handler = MagicMock()
        mock_handler.capture_session_end_checkpoint.return_value = CheckpointV2(
            id="ckpt-abc123def456",
            trigger_type=TriggerType.SESSION_END,
            session_status=SessionStatus.FAILED,
            session_id="test-container",
            session=SessionMetadata(session_id="test-container", started_at=now),
            created_at=now,
            session_started_at=now,
        )
        mock_get_handler.return_value = mock_handler

        session = _make_test_session()
        checkpoint, event = capture_session_end_checkpoint(
            session=session,
            session_status=SessionStatus.FAILED,
            repo_path="/home/egg/repos/test-repo",
            async_store=False,
        )

        assert checkpoint is not None
        assert checkpoint.session_status == SessionStatus.FAILED

    @patch("checkpoint_handler.get_checkpoint_handler")
    def test_async_store_returns_completion_event(self, mock_get_handler):
        """Async store returns a completion event that is eventually set."""
        from egg_contracts.checkpoints import (
            CheckpointV2,
            SessionMetadata,
            SessionStatus,
            TriggerType,
        )

        now = datetime.now(UTC)
        mock_handler = MagicMock()
        mock_handler.capture_session_end_checkpoint.return_value = CheckpointV2(
            id="ckpt-abc123def456",
            trigger_type=TriggerType.SESSION_END,
            session_status=SessionStatus.COMPLETED,
            session_id="test-container",
            session=SessionMetadata(session_id="test-container", started_at=now),
            created_at=now,
            session_started_at=now,
        )
        mock_handler.store_checkpoint_v2.return_value = True
        mock_get_handler.return_value = mock_handler

        session = _make_test_session()
        checkpoint, event = capture_session_end_checkpoint(
            session=session,
            session_status=SessionStatus.COMPLETED,
            repo_path="/home/egg/repos/test-repo",
            async_store=True,
        )

        assert checkpoint is not None
        assert event is not None
        # Wait for async storage to complete
        event.wait(timeout=5)
        assert event.is_set()
        mock_handler.store_checkpoint_v2.assert_called_once()

    @patch("checkpoint_handler.get_checkpoint_handler")
    def test_capture_failure_returns_none(self, mock_get_handler):
        """Returns (None, None) when handler fails to capture."""
        from egg_contracts.checkpoints import SessionStatus

        mock_handler = MagicMock()
        mock_handler.capture_session_end_checkpoint.return_value = None
        mock_get_handler.return_value = mock_handler

        session = _make_test_session()
        checkpoint, event = capture_session_end_checkpoint(
            session=session,
            session_status=SessionStatus.COMPLETED,
        )

        assert checkpoint is None
        assert event is None

    @patch("checkpoint_handler.get_checkpoint_handler")
    def test_no_repo_path_returns_checkpoint_without_event(self, mock_get_handler):
        """When no repo_path is available, returns checkpoint but no event."""
        from egg_contracts.checkpoints import (
            CheckpointV2,
            SessionMetadata,
            SessionStatus,
            TriggerType,
        )

        now = datetime.now(UTC)
        mock_handler = MagicMock()
        mock_handler.capture_session_end_checkpoint.return_value = CheckpointV2(
            id="ckpt-abc123def456",
            trigger_type=TriggerType.SESSION_END,
            session_status=SessionStatus.COMPLETED,
            session_id="test-container",
            session=SessionMetadata(session_id="test-container", started_at=now),
            created_at=now,
            session_started_at=now,
        )
        mock_get_handler.return_value = mock_handler

        session = _make_test_session()

        # Patch Path to make repos dir not exist
        with patch("checkpoint_handler.Path") as mock_path:
            mock_repos_base = MagicMock()
            mock_repos_base.exists.return_value = False
            mock_path.return_value = mock_repos_base

            checkpoint, event = capture_session_end_checkpoint(
                session=session,
                session_status=SessionStatus.COMPLETED,
                repo_path=None,
            )

            assert checkpoint is not None
            assert event is None

    @patch("checkpoint_handler._get_checkpoint_repo_for_path")
    @patch("checkpoint_handler.get_checkpoint_handler")
    def test_explicit_checkpoint_repo_skips_auto_detection(
        self, mock_get_handler, mock_auto_detect
    ):
        """When checkpoint_repo is passed explicitly, auto-detection is skipped."""
        from egg_contracts.checkpoints import (
            CheckpointV2,
            SessionMetadata,
            SessionStatus,
            TriggerType,
        )

        now = datetime.now(UTC)
        mock_handler = MagicMock()
        mock_handler.capture_session_end_checkpoint.return_value = CheckpointV2(
            id="ckpt-abc123def456",
            trigger_type=TriggerType.SESSION_END,
            session_status=SessionStatus.COMPLETED,
            session_id="test-container",
            session=SessionMetadata(session_id="test-container", started_at=now),
            created_at=now,
            session_started_at=now,
        )
        mock_handler.store_checkpoint_v2.return_value = True
        mock_get_handler.return_value = mock_handler

        session = _make_test_session()
        checkpoint, event = capture_session_end_checkpoint(
            session=session,
            session_status=SessionStatus.COMPLETED,
            repo_path="/home/egg/repos/test-repo",
            checkpoint_repo="jwbron/egg-checkpoints",
            async_store=False,
        )

        assert checkpoint is not None
        # Auto-detection should NOT be called when checkpoint_repo is explicit
        mock_auto_detect.assert_not_called()
        # store_checkpoint_v2 should receive the explicit checkpoint_repo
        call_kwargs = mock_handler.store_checkpoint_v2.call_args
        assert call_kwargs[1].get("checkpoint_repo") == "jwbron/egg-checkpoints" or (
            len(call_kwargs[0]) > 2 and call_kwargs[0][2] == "jwbron/egg-checkpoints"
        )

    @patch("checkpoint_handler._get_checkpoint_repo_for_path")
    @patch("checkpoint_handler.get_checkpoint_handler")
    def test_auto_detection_used_when_checkpoint_repo_is_none(
        self, mock_get_handler, mock_auto_detect
    ):
        """When checkpoint_repo is None, auto-detection is attempted."""
        from egg_contracts.checkpoints import (
            CheckpointV2,
            SessionMetadata,
            SessionStatus,
            TriggerType,
        )

        mock_auto_detect.return_value = "jwbron/egg-checkpoints"

        now = datetime.now(UTC)
        mock_handler = MagicMock()
        mock_handler.capture_session_end_checkpoint.return_value = CheckpointV2(
            id="ckpt-abc123def456",
            trigger_type=TriggerType.SESSION_END,
            session_status=SessionStatus.COMPLETED,
            session_id="test-container",
            session=SessionMetadata(session_id="test-container", started_at=now),
            created_at=now,
            session_started_at=now,
        )
        mock_handler.store_checkpoint_v2.return_value = True
        mock_get_handler.return_value = mock_handler

        session = _make_test_session()
        checkpoint, event = capture_session_end_checkpoint(
            session=session,
            session_status=SessionStatus.COMPLETED,
            repo_path="/home/egg/repos/test-repo",
            checkpoint_repo=None,
            async_store=False,
        )

        assert checkpoint is not None
        mock_auto_detect.assert_called_once_with("/home/egg/repos/test-repo")
        # store_checkpoint_v2 should receive the auto-detected checkpoint_repo
        call_kwargs = mock_handler.store_checkpoint_v2.call_args
        assert call_kwargs[1].get("checkpoint_repo") == "jwbron/egg-checkpoints"


class TestStoreCheckpointV2GitOps:
    """Tests for store_checkpoint_v2 git operations (force-fetch, branch cleanup)."""

    @patch("checkpoint_handler.get_checkpoint_handler")
    def test_force_fetch_prefix_used(self, mock_get_handler):
        """Force-fetch (+) prefix is used when branch exists on remote."""
        import checkpoint_handler

        handler = checkpoint_handler.CheckpointHandler(github_token="test-token")

        # Track all _run_git calls
        git_calls = []

        def track_run_git(cwd, args, **kwargs):
            git_calls.append((cwd, args, kwargs))
            # Return success for all commands
            return MagicMock(returncode=0, stdout="", stderr="")

        handler._run_git = track_run_git
        handler._branch_exists = MagicMock(return_value=True)

        from egg_contracts.checkpoints import (
            CheckpointV2,
            SessionMetadata,
            TriggerType,
        )

        now = datetime.now(UTC)
        checkpoint = CheckpointV2(
            id="ckpt-a1b2c3d4e5f67890",
            trigger_type=TriggerType.COMMIT,
            session_id="test-container",
            commit_sha="abc123def456789012345678901234567890abcd",
            session=SessionMetadata(session_id="test-container", started_at=now),
            created_at=now,
            session_started_at=now,
        )

        # Will fail at some point during the process but we just need to
        # verify the fetch call uses + prefix
        try:
            handler.store_checkpoint_v2(checkpoint, "/fake/repo")
        except Exception:
            pass

        # Find the fetch call
        fetch_calls = [c for c in git_calls if "fetch" in c[1]]
        assert len(fetch_calls) >= 1
        fetch_args = fetch_calls[0][1]
        # The refspec should have + prefix for force-update
        refspec = fetch_args[-1]
        assert refspec.startswith("+"), f"Expected force-fetch prefix, got: {refspec}"

    @patch("checkpoint_handler.get_checkpoint_handler")
    def test_stale_branch_deleted_before_orphan(self, mock_get_handler):
        """Stale local branch is deleted before creating orphan for new remote."""
        import checkpoint_handler

        handler = checkpoint_handler.CheckpointHandler(github_token="test-token")

        git_calls = []

        def track_run_git(cwd, args, **kwargs):
            git_calls.append((cwd, args, kwargs))
            return MagicMock(returncode=0, stdout="", stderr="")

        handler._run_git = track_run_git
        handler._branch_exists = MagicMock(return_value=False)

        from egg_contracts.checkpoints import (
            CheckpointV2,
            SessionMetadata,
            TriggerType,
        )

        now = datetime.now(UTC)
        checkpoint = CheckpointV2(
            id="ckpt-a1b2c3d4e5f67890",
            trigger_type=TriggerType.COMMIT,
            session_id="test-container",
            commit_sha="abc123def456789012345678901234567890abcd",
            session=SessionMetadata(session_id="test-container", started_at=now),
            created_at=now,
            session_started_at=now,
        )

        try:
            handler.store_checkpoint_v2(
                checkpoint, "/fake/repo", checkpoint_repo="jwbron/egg-checkpoints"
            )
        except Exception:
            pass

        # Find branch -D call (should come before orphan checkout)
        branch_delete_calls = [c for c in git_calls if "branch" in c[1] and "-D" in c[1]]
        orphan_calls = [c for c in git_calls if "checkout" in c[1] and "--orphan" in c[1]]

        assert len(branch_delete_calls) >= 1, "Expected branch -D call for stale cleanup"
        assert len(orphan_calls) >= 1, "Expected orphan checkout call"

        # branch -D should come before orphan checkout
        branch_idx = git_calls.index(branch_delete_calls[0])
        orphan_idx = git_calls.index(orphan_calls[0])
        assert branch_idx < orphan_idx, "branch -D should precede orphan checkout"

        # branch -D should use check=False (non-fatal if branch doesn't exist)
        assert branch_delete_calls[0][2].get("check") is False


class TestResolvePipelineId:
    """Tests for CheckpointHandler._resolve_pipeline_id."""

    def test_from_session(self):
        """Pipeline ID resolved from session."""
        from checkpoint_handler import CheckpointHandler

        handler = CheckpointHandler()
        session = _make_test_session(pipeline_id="issue-42")
        assert handler._resolve_pipeline_id(session) == "issue-42"

    @patch.dict("os.environ", {"EGG_PIPELINE_ID": "issue-99"})
    def test_from_env(self):
        """Pipeline ID falls back to EGG_PIPELINE_ID env var."""
        from checkpoint_handler import CheckpointHandler

        handler = CheckpointHandler()
        session = _make_test_session(pipeline_id=None)
        assert handler._resolve_pipeline_id(session) == "issue-99"

    @patch.dict("os.environ", {}, clear=True)
    def test_returns_none(self):
        """Returns None when no pipeline ID available."""
        from checkpoint_handler import CheckpointHandler

        handler = CheckpointHandler()
        session = _make_test_session(pipeline_id=None)
        # Clear the env var explicitly
        import os

        os.environ.pop("EGG_PIPELINE_ID", None)
        assert handler._resolve_pipeline_id(session) is None

    def test_none_session(self):
        """Returns None when session is None."""
        from checkpoint_handler import CheckpointHandler

        handler = CheckpointHandler()
        with patch.dict("os.environ", {}, clear=True):
            import os

            os.environ.pop("EGG_PIPELINE_ID", None)
            assert handler._resolve_pipeline_id(None) is None

    def test_session_takes_precedence_over_env(self):
        """Session pipeline_id takes precedence over env var."""
        from checkpoint_handler import CheckpointHandler

        handler = CheckpointHandler()
        session = _make_test_session(pipeline_id="from-session")
        with patch.dict("os.environ", {"EGG_PIPELINE_ID": "from-env"}):
            assert handler._resolve_pipeline_id(session) == "from-session"


class TestResolveAgentType:
    """Tests for _resolve_agent_type with expanded role mappings."""

    def test_known_roles(self):
        """Known roles map to their correct AgentType."""
        from egg_contracts.checkpoints import AgentType

        assert _resolve_agent_type("coder") == AgentType.CODER
        assert _resolve_agent_type("tester") == AgentType.TESTER
        assert _resolve_agent_type("documenter") == AgentType.DOCUMENTER
        assert _resolve_agent_type("integrator") == AgentType.INTEGRATOR
        assert _resolve_agent_type("reviewer") == AgentType.REVIEWER

    def test_new_orchestrator_roles(self):
        """New orchestrator roles map correctly."""
        from egg_contracts.checkpoints import AgentType

        assert _resolve_agent_type("architect") == AgentType.ARCHITECT
        assert _resolve_agent_type("task_planner") == AgentType.TASK_PLANNER
        assert _resolve_agent_type("risk_analyst") == AgentType.RISK_ANALYST
        assert _resolve_agent_type("refiner") == AgentType.REFINER
        assert _resolve_agent_type("checker") == AgentType.CHECKER

    def test_reviewer_subtypes(self):
        """Reviewer subtypes all map to REVIEWER."""
        from egg_contracts.checkpoints import AgentType

        assert _resolve_agent_type("reviewer_unified") == AgentType.REVIEWER
        assert _resolve_agent_type("reviewer_code") == AgentType.REVIEWER
        assert _resolve_agent_type("reviewer_contract") == AgentType.REVIEWER
        assert _resolve_agent_type("reviewer_agent_design") == AgentType.REVIEWER
        assert _resolve_agent_type("reviewer_refine") == AgentType.REVIEWER
        assert _resolve_agent_type("reviewer_plan") == AgentType.REVIEWER

    def test_case_insensitive(self):
        """Role matching is case-insensitive."""
        from egg_contracts.checkpoints import AgentType

        assert _resolve_agent_type("CODER") == AgentType.CODER
        assert _resolve_agent_type("Architect") == AgentType.ARCHITECT

    def test_unknown_role(self):
        """Unknown roles map to UNKNOWN."""
        from egg_contracts.checkpoints import AgentType

        assert _resolve_agent_type("some_new_role") == AgentType.UNKNOWN

    def test_none_role(self):
        """None role maps to UNKNOWN."""
        from egg_contracts.checkpoints import AgentType

        assert _resolve_agent_type(None) == AgentType.UNKNOWN

    def test_empty_role(self):
        """Empty string maps to UNKNOWN."""
        from egg_contracts.checkpoints import AgentType

        assert _resolve_agent_type("") == AgentType.UNKNOWN


class TestResolveGithubToken:
    """Tests for _resolve_github_token helper."""

    @patch("checkpoint_handler.get_token_for_repo")
    @patch("checkpoint_handler.subprocess.run")
    def test_resolves_token_from_remote(self, mock_run, mock_get_token):
        """Resolves a fresh token from the git remote URL."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/owner/repo.git\n",
        )
        mock_get_token.return_value = ("fresh-token-123", "bot", "")

        token = _resolve_github_token("/some/repo/path")

        assert token == "fresh-token-123"
        mock_get_token.assert_called_once_with("owner/repo")

    @patch("checkpoint_handler.subprocess.run")
    def test_returns_none_on_git_failure(self, mock_run):
        """Returns None when git remote command fails."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="not a git repo",
        )

        token = _resolve_github_token("/not/a/repo")
        assert token is None

    @patch("checkpoint_handler.get_token_for_repo")
    @patch("checkpoint_handler.subprocess.run")
    def test_returns_none_when_token_unavailable(self, mock_run, mock_get_token):
        """Returns None when get_token_for_repo can't get a token."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/owner/repo.git\n",
        )
        mock_get_token.return_value = (None, "bot", "Token not available")

        token = _resolve_github_token("/some/repo/path")
        assert token is None

    @patch("checkpoint_handler.subprocess.run")
    def test_returns_none_on_non_github_remote(self, mock_run):
        """Returns None for non-GitHub remotes."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://gitlab.com/owner/repo.git\n",
        )

        token = _resolve_github_token("/some/repo/path")
        assert token is None

    @patch("checkpoint_handler.subprocess.run")
    def test_handles_exception_gracefully(self, mock_run):
        """Returns None on unexpected exceptions."""
        mock_run.side_effect = Exception("Unexpected error")

        token = _resolve_github_token("/some/repo/path")
        assert token is None


class TestStoreCheckpointWithToken:
    """Tests for store_checkpoint_v2 with explicit github_token parameter."""

    def test_explicit_token_used_in_run_git(self):
        """Explicit github_token is passed through to _run_git."""
        import checkpoint_handler

        handler = checkpoint_handler.CheckpointHandler(github_token="old-stale-token")

        git_calls = []

        def track_run_git(cwd, args, **kwargs):
            git_calls.append((cwd, args, kwargs))
            return MagicMock(returncode=0, stdout="", stderr="")

        handler._run_git = track_run_git
        handler._branch_exists = MagicMock(return_value=True)

        from egg_contracts.checkpoints import (
            CheckpointV2,
            SessionMetadata,
            TriggerType,
        )

        now = datetime.now(UTC)
        checkpoint = CheckpointV2(
            id="ckpt-a1b2c3d4e5f67890",
            trigger_type=TriggerType.COMMIT,
            session_id="test-container",
            commit_sha="abc123def456789012345678901234567890abcd",
            session=SessionMetadata(session_id="test-container", started_at=now),
            created_at=now,
            session_started_at=now,
        )

        try:
            handler.store_checkpoint_v2(checkpoint, "/fake/repo", github_token="fresh-token-456")
        except Exception:
            pass

        # Verify _branch_exists was called with the fresh token
        handler._branch_exists.assert_called_once()
        call_kwargs = handler._branch_exists.call_args
        assert call_kwargs[1].get("github_token") == "fresh-token-456"

        # Find network-facing git calls (fetch, push) and verify they got the token
        fetch_calls = [c for c in git_calls if "fetch" in c[1]]
        for call in fetch_calls:
            assert call[2].get("github_token") == "fresh-token-456"


class TestSessionEndCheckpointTokenResolution:
    """Tests for fresh token resolution in capture_session_end_checkpoint."""

    @patch("checkpoint_handler._resolve_github_token")
    @patch("checkpoint_handler._get_checkpoint_repo_for_path")
    @patch("checkpoint_handler.get_checkpoint_handler")
    def test_resolves_fresh_token_when_none_provided(
        self, mock_get_handler, mock_get_ckpt_repo, mock_resolve_token
    ):
        """Resolves a fresh token when no github_token is provided."""
        from egg_contracts.checkpoints import (
            CheckpointV2,
            SessionMetadata,
            SessionStatus,
            TriggerType,
        )

        mock_resolve_token.return_value = "fresh-resolved-token"
        mock_get_ckpt_repo.return_value = None

        now = datetime.now(UTC)
        mock_handler = MagicMock()
        mock_handler.capture_session_end_checkpoint.return_value = CheckpointV2(
            id="ckpt-abc123def456",
            trigger_type=TriggerType.SESSION_END,
            session_status=SessionStatus.COMPLETED,
            session_id="test-container",
            session=SessionMetadata(session_id="test-container", started_at=now),
            created_at=now,
            session_started_at=now,
        )
        mock_handler.store_checkpoint_v2.return_value = True
        mock_get_handler.return_value = mock_handler

        session = _make_test_session()
        checkpoint, event = capture_session_end_checkpoint(
            session=session,
            session_status=SessionStatus.COMPLETED,
            repo_path="/home/egg/repos/test-repo",
            github_token=None,
            async_store=False,
        )

        assert checkpoint is not None
        mock_resolve_token.assert_called_once_with("/home/egg/repos/test-repo")
        # Verify the resolved token was passed to store_checkpoint_v2
        call_kwargs = mock_handler.store_checkpoint_v2.call_args
        assert call_kwargs[1].get("github_token") == "fresh-resolved-token"

    @patch("checkpoint_handler._resolve_github_token")
    @patch("checkpoint_handler._get_checkpoint_repo_for_path")
    @patch("checkpoint_handler.get_checkpoint_handler")
    def test_skips_resolution_when_token_provided(
        self, mock_get_handler, mock_get_ckpt_repo, mock_resolve_token
    ):
        """Does not resolve a fresh token when github_token is explicitly provided."""
        from egg_contracts.checkpoints import (
            CheckpointV2,
            SessionMetadata,
            SessionStatus,
            TriggerType,
        )

        mock_get_ckpt_repo.return_value = None

        now = datetime.now(UTC)
        mock_handler = MagicMock()
        mock_handler.capture_session_end_checkpoint.return_value = CheckpointV2(
            id="ckpt-abc123def456",
            trigger_type=TriggerType.SESSION_END,
            session_status=SessionStatus.COMPLETED,
            session_id="test-container",
            session=SessionMetadata(session_id="test-container", started_at=now),
            created_at=now,
            session_started_at=now,
        )
        mock_handler.store_checkpoint_v2.return_value = True
        mock_get_handler.return_value = mock_handler

        session = _make_test_session()
        checkpoint, event = capture_session_end_checkpoint(
            session=session,
            session_status=SessionStatus.COMPLETED,
            repo_path="/home/egg/repos/test-repo",
            github_token="explicit-token",
            async_store=False,
        )

        assert checkpoint is not None
        # Should NOT resolve a fresh token since one was provided
        mock_resolve_token.assert_not_called()
        # The explicit token should be passed through
        call_kwargs = mock_handler.store_checkpoint_v2.call_args
        assert call_kwargs[1].get("github_token") == "explicit-token"
