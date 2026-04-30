"""Tests for checkpoint_handler module - checkpoint creation and session-end.

Note: the module-level ``_stub_bare_repo_lock`` autouse fixture below replaces
the cross-process flock primitive with a no-op for every test in this file.
Tests added here that need to exercise the real ``bare_repo_lock`` path
(rather than just ``_get_store_lock``'s in-process serialization) must opt out
explicitly or live in ``shared/tests/test_cross_process_lock.py`` /
``orchestrator/tests/test_state_store.py`` instead.
"""

import contextlib
import subprocess
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

# Import from conftest-loaded modules
from checkpoint_handler import (
    _extract_repo_from_remote,
    _resolve_agent_type,
    _resolve_github_token,
    capture_session_end_checkpoint,
)
from session_manager import Session, _hash_token


@pytest.fixture(autouse=True)
def _stub_bare_repo_lock(monkeypatch):
    """Replace ``bare_repo_lock`` with a no-op for these unit tests.

    The cross-process flock primitive (#2311) requires a real
    ``<repo>/.git/`` to exist so it can ``mkdir`` the sentinel and
    ``os.open`` an fd.  These tests pass sentinel paths like
    ``/fake/repo`` which would fail the mkdir.  Cross-process behaviour
    is covered by ``shared/tests/test_cross_process_lock.py`` and the
    worktree integration test — here we only need ``_get_store_lock``'s
    in-process serialization to work.

    Tests that need to observe ``bare_repo_lock`` acquire/release order
    (e.g. #2332's regression that the flock is *not* held across the
    fetch-retry window) override this with their own recording stand-in.
    """
    import checkpoint_handler

    @contextlib.contextmanager
    def _noop(repo_path):
        yield

    monkeypatch.setattr(checkpoint_handler, "bare_repo_lock", _noop)


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
                new_sha="abc123",
                branch="main",
            )
            assert result == []
        finally:
            checkpoint_handler.CHECKPOINT_ENABLED = original

    @patch("checkpoint_handler.capture_and_store_checkpoint")
    def test_creates_single_checkpoint_per_push(self, mock_capture):
        """Test that exactly one checkpoint is created per push, using new_sha."""
        from egg_contracts.checkpoints import CheckpointV2, SessionMetadata, TriggerType

        new_sha = "3333333333333333333333333333333333333333"
        now = datetime.now(UTC)
        mock_capture.return_value = CheckpointV2(
            id="ckpt-333333333333",
            trigger_type=TriggerType.COMMIT,
            commit_sha=new_sha,
            session_id="test",
            session=SessionMetadata(session_id="test", started_at=now),
            created_at=now,
            session_started_at=now,
            push_sha=new_sha,
            branch="main",
        )

        import checkpoint_handler

        checkpoints = checkpoint_handler.capture_and_store_checkpoints_for_push(
            repo_path="/repo",
            new_sha=new_sha,
            branch="main",
            async_store=False,
        )

        # Exactly one checkpoint for the push tip, regardless of commit count
        assert len(checkpoints) == 1
        assert checkpoints[0].commit_sha == new_sha
        mock_capture.assert_called_once()
        call_kwargs = mock_capture.call_args[1]
        assert call_kwargs["commit_sha"] == new_sha
        assert call_kwargs["push_sha"] == new_sha

    @patch("checkpoint_handler.capture_and_store_checkpoint")
    def test_multi_commit_push_still_one_checkpoint(self, mock_capture):
        """Multi-commit push creates only one checkpoint (for the tip)."""
        from egg_contracts.checkpoints import CheckpointV2, SessionMetadata, TriggerType

        new_sha = "3333333333333333333333333333333333333333"
        now = datetime.now(UTC)
        mock_capture.return_value = CheckpointV2(
            id="ckpt-333333333333",
            trigger_type=TriggerType.COMMIT,
            commit_sha=new_sha,
            session_id="test",
            session=SessionMetadata(session_id="test", started_at=now),
            created_at=now,
            session_started_at=now,
            push_sha=new_sha,
            branch="main",
        )

        import checkpoint_handler

        checkpoints = checkpoint_handler.capture_and_store_checkpoints_for_push(
            repo_path="/repo",
            new_sha=new_sha,
            branch="main",
            async_store=False,
        )

        assert len(checkpoints) == 1
        mock_capture.assert_called_once()

    @patch("checkpoint_handler.capture_and_store_checkpoint")
    def test_async_store_passes_through(self, mock_capture):
        """Test that async_store=True is passed through to capture_and_store_checkpoint."""
        from egg_contracts.checkpoints import CheckpointV2, SessionMetadata, TriggerType

        new_sha = "1111111111111111111111111111111111111111"
        now = datetime.now(UTC)
        mock_capture.return_value = CheckpointV2(
            id="ckpt-1111111111111",
            trigger_type=TriggerType.COMMIT,
            commit_sha=new_sha,
            session_id="test",
            session=SessionMetadata(session_id="test", started_at=now),
            created_at=now,
            session_started_at=now,
            branch="main",
        )

        import checkpoint_handler

        checkpoint_handler.capture_and_store_checkpoints_for_push(
            repo_path="/repo",
            new_sha=new_sha,
            branch="main",
            async_store=True,
        )

        mock_capture.assert_called_once()
        assert mock_capture.call_args[1]["async_store"] is True

    @patch("checkpoint_handler.capture_and_store_checkpoint")
    def test_returns_empty_when_capture_returns_none(self, mock_capture):
        """Returns empty list when capture_and_store_checkpoint returns None."""
        mock_capture.return_value = None

        import checkpoint_handler

        checkpoints = checkpoint_handler.capture_and_store_checkpoints_for_push(
            repo_path="/repo",
            new_sha="2222222222222222222222222222222222222222",
            branch="main",
            async_store=False,
        )

        assert checkpoints == []


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
            checkpoint_repo="owner/repo-checkpoints",
            async_store=False,
        )

        assert checkpoint is not None
        # Auto-detection should NOT be called when checkpoint_repo is explicit
        mock_auto_detect.assert_not_called()
        # store_checkpoint_v2 should receive the explicit checkpoint_repo
        call_kwargs = mock_handler.store_checkpoint_v2.call_args
        assert call_kwargs[1].get("checkpoint_repo") == "owner/repo-checkpoints" or (
            len(call_kwargs[0]) > 2 and call_kwargs[0][2] == "owner/repo-checkpoints"
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

        mock_auto_detect.return_value = "owner/repo-checkpoints"

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
        assert call_kwargs[1].get("checkpoint_repo") == "owner/repo-checkpoints"


class TestAutoCommitShaInCheckpoint:
    """Tests for auto_commit_sha propagation to session-end checkpoint metadata."""

    @patch("checkpoint_handler.get_proxy_buffer_path")
    def test_auto_commit_sha_propagated_to_checkpoint(self, mock_buffer_path):
        """Session.auto_commit_sha is set as commit_sha on CheckpointV2."""
        from checkpoint_handler import CheckpointHandler
        from egg_contracts.checkpoints import SessionStatus

        mock_buffer_path.return_value = MagicMock(exists=MagicMock(return_value=False))

        handler = CheckpointHandler()
        session = _make_test_session()
        session.auto_commit_sha = "abc1234"

        checkpoint = handler.capture_session_end_checkpoint(
            session=session,
            session_status=SessionStatus.COMPLETED,
        )

        assert checkpoint is not None
        assert checkpoint.commit_sha == "abc1234"

    @patch("checkpoint_handler.get_proxy_buffer_path")
    def test_none_auto_commit_sha_leaves_checkpoint_commit_sha_none(self, mock_buffer_path):
        """When auto_commit_sha is None, checkpoint.commit_sha is None."""
        from checkpoint_handler import CheckpointHandler
        from egg_contracts.checkpoints import SessionStatus

        mock_buffer_path.return_value = MagicMock(exists=MagicMock(return_value=False))

        handler = CheckpointHandler()
        session = _make_test_session()
        # auto_commit_sha defaults to None

        checkpoint = handler.capture_session_end_checkpoint(
            session=session,
            session_status=SessionStatus.COMPLETED,
        )

        assert checkpoint is not None
        assert checkpoint.commit_sha is None


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
                checkpoint, "/fake/repo", checkpoint_repo="owner/repo-checkpoints"
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


class TestStoreCheckpointV2Concurrency:
    """Tests for per-repo serialization of store_checkpoint_v2 (issue #2069)."""

    def _make_checkpoint(self):
        from egg_contracts.checkpoints import (
            CheckpointV2,
            SessionMetadata,
            TriggerType,
        )

        now = datetime.now(UTC)
        return CheckpointV2(
            id="ckpt-a1b2c3d4e5f67890",
            trigger_type=TriggerType.COMMIT,
            session_id="test-container",
            commit_sha="abc123def456789012345678901234567890abcd",
            session=SessionMetadata(session_id="test-container", started_at=now),
            created_at=now,
            session_started_at=now,
        )

    def test_worktree_prune_called_in_cleanup(self):
        """Cleanup runs `git worktree prune` to drop stale worktree entries."""
        import checkpoint_handler

        # Reset the per-repo lock dict so this test is isolated.
        checkpoint_handler._store_locks.clear()

        handler = checkpoint_handler.CheckpointHandler(github_token="test-token")

        git_calls = []

        def track_run_git(cwd, args, **kwargs):
            git_calls.append((cwd, args, kwargs))
            return MagicMock(returncode=0, stdout="", stderr="")

        handler._run_git = track_run_git
        handler._branch_exists = MagicMock(return_value=True)

        try:
            handler.store_checkpoint_v2(self._make_checkpoint(), "/fake/repo")
        except Exception:
            pass

        prune_calls = [c for c in git_calls if c[1][:2] == ["worktree", "prune"]]
        assert len(prune_calls) == 1, "Expected exactly one `worktree prune` call"
        assert prune_calls[0][0] == "/fake/repo"
        assert prune_calls[0][2].get("check") is False

    def test_worktree_prune_runs_when_worktree_add_fails(self):
        """If `worktree add` itself raises, cleanup still runs `worktree prune`."""
        import checkpoint_handler

        checkpoint_handler._store_locks.clear()

        handler = checkpoint_handler.CheckpointHandler(github_token="test-token")

        git_calls = []

        def track_run_git(cwd, args, **kwargs):
            git_calls.append((cwd, args, kwargs))
            if args[:2] == ["worktree", "add"]:
                raise checkpoint_handler.CheckpointError("simulated worktree add failure")
            return MagicMock(returncode=0, stdout="", stderr="")

        handler._run_git = track_run_git
        handler._branch_exists = MagicMock(return_value=True)

        # store_checkpoint_v2 swallows exceptions and returns False; we just
        # need it to complete and run the finally block.
        result = handler.store_checkpoint_v2(self._make_checkpoint(), "/fake/repo")
        assert result is False

        prune_calls = [c for c in git_calls if c[1][:2] == ["worktree", "prune"]]
        assert len(prune_calls) == 1, (
            "Expected `worktree prune` to run even when `worktree add` fails"
        )
        assert prune_calls[0][0] == "/fake/repo"

    def test_concurrent_stores_on_same_repo_serialized(self):
        """Two threads storing into the same repo_path run sequentially."""
        import threading
        import time

        import checkpoint_handler

        # Reset the per-repo lock dict so this test is isolated.
        checkpoint_handler._store_locks.clear()

        handler = checkpoint_handler.CheckpointHandler(github_token="test-token")

        in_flight = 0
        max_in_flight = 0
        observe_lock = threading.Lock()

        def track_run_git(cwd, args, **kwargs):
            nonlocal in_flight, max_in_flight
            with observe_lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            time.sleep(0.05)
            with observe_lock:
                in_flight -= 1
            return MagicMock(returncode=0, stdout="", stderr="")

        handler._run_git = track_run_git
        handler._branch_exists = MagicMock(return_value=True)

        def run_store():
            try:
                handler.store_checkpoint_v2(
                    self._make_checkpoint(),
                    "/fake/repo",
                    checkpoint_repo="owner/repo-checkpoints",
                )
            except Exception:
                pass

        threads = [threading.Thread(target=run_store) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # If serialization works, only one thread is ever inside _run_git
        # at a time for the same repo_path.
        assert max_in_flight == 1, f"Expected serialized git calls, saw {max_in_flight} concurrent"

    def test_concurrent_stores_on_different_repos_not_serialized(self):
        """Different repo_paths take different locks and run in parallel."""
        import threading
        import time

        import checkpoint_handler

        checkpoint_handler._store_locks.clear()

        handler = checkpoint_handler.CheckpointHandler(github_token="test-token")

        in_flight = 0
        max_in_flight = 0
        observe_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def track_run_git(cwd, args, **kwargs):
            nonlocal in_flight, max_in_flight
            with observe_lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            # Synchronize so both threads are guaranteed to overlap if
            # the locks are independent.
            try:
                barrier.wait(timeout=2.0)
            except threading.BrokenBarrierError:
                pass
            time.sleep(0.05)
            with observe_lock:
                in_flight -= 1
            return MagicMock(returncode=0, stdout="", stderr="")

        handler._run_git = track_run_git
        handler._branch_exists = MagicMock(return_value=True)

        def run_store(repo_path):
            try:
                handler.store_checkpoint_v2(self._make_checkpoint(), repo_path)
            except Exception:
                pass

        t1 = threading.Thread(target=run_store, args=("/fake/repo-a",))
        t2 = threading.Thread(target=run_store, args=("/fake/repo-b",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert max_in_flight >= 2, (
            "Expected concurrent execution across different repos, "
            f"saw max_in_flight={max_in_flight}"
        )

    def test_concurrent_stores_with_shared_checkpoint_repo_serialized(self):
        """Different source repos pushing to the same checkpoint_repo serialize.

        Regression test for #2316: before the target-keyed lock, two source
        repos targeting the shared ``egg/checkpoints/v2`` branch in
        ``jwbron/egg-checkpoints`` could push concurrently, and the second
        writer would see a non-FF rejection.
        """
        import threading
        import time

        import checkpoint_handler

        checkpoint_handler._store_locks.clear()

        handler = checkpoint_handler.CheckpointHandler(github_token="test-token")

        in_flight = 0
        max_in_flight = 0
        observe_lock = threading.Lock()
        # Force overlap if the lock disappears: a regression that drops
        # the destination-keyed lock would let both threads into
        # ``_run_git`` concurrently, the barrier would release them
        # together, and ``max_in_flight`` would jump to 2. With the
        # lock in place only one thread reaches the barrier; the wait
        # times out, ``BrokenBarrierError`` is caught, and serialization
        # is still observed.
        barrier = threading.Barrier(2)

        def track_run_git(cwd, args, **kwargs):
            nonlocal in_flight, max_in_flight
            with observe_lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            try:
                barrier.wait(timeout=2.0)
            except threading.BrokenBarrierError:
                pass
            time.sleep(0.05)
            with observe_lock:
                in_flight -= 1
            return MagicMock(returncode=0, stdout="", stderr="")

        handler._run_git = track_run_git
        handler._branch_exists = MagicMock(return_value=True)

        def run_store(repo_path):
            try:
                handler.store_checkpoint_v2(
                    self._make_checkpoint(),
                    repo_path,
                    checkpoint_repo="jwbron/egg-checkpoints",
                )
            except Exception:
                pass

        t1 = threading.Thread(target=run_store, args=("/fake/repo-a",))
        t2 = threading.Thread(target=run_store, args=("/fake/repo-b",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert max_in_flight == 1, (
            "Expected serialization across source repos when checkpoint_repo "
            f"is shared, saw max_in_flight={max_in_flight}"
        )

    def test_bare_repo_lock_not_held_across_fetch_retry(self, monkeypatch):
        """Cross-process flock is released around the fetch-retry window.

        Regression for #2332. Holding ``bare_repo_lock`` across the
        fetch's 3 × 45s retry window blocks every state-store commit
        and other gateway worktree op against the same bare repo.
        Fetch doesn't write ``.git/config``, so it doesn't need
        cross-process serialization — the flock should only wrap the
        ops that touch the bare repo's ``.git/`` (worktree add,
        worktree remove/prune).
        """
        import checkpoint_handler

        checkpoint_handler._store_locks.clear()

        flock_depth = [0]
        # Per-call list of (git_args, depth_at_entry).
        observations: list[tuple[list[str], int]] = []

        @contextlib.contextmanager
        def recording_flock(_repo_path):
            flock_depth[0] += 1
            try:
                yield
            finally:
                flock_depth[0] -= 1

        monkeypatch.setattr(checkpoint_handler, "bare_repo_lock", recording_flock)
        monkeypatch.setattr(checkpoint_handler.time, "sleep", lambda _s: None)

        handler = checkpoint_handler.CheckpointHandler(github_token="test-token")

        fetch_attempts = [0]

        def track_run_git(cwd, args, **kwargs):
            observations.append((list(args), flock_depth[0]))
            if args[:1] == ["fetch"]:
                fetch_attempts[0] += 1
                # Fail the first two fetches to drive the retry loop;
                # let the third succeed so the rest of the op runs.
                if fetch_attempts[0] < 3:
                    raise checkpoint_handler.CheckpointError("simulated fetch failure")
            return MagicMock(returncode=0, stdout="", stderr="")

        handler._run_git = track_run_git
        handler._branch_exists = MagicMock(return_value=True)

        result = handler.store_checkpoint_v2(self._make_checkpoint(), "/fake/repo")
        assert result is True

        fetch_observations = [depth for args, depth in observations if args[:1] == ["fetch"]]
        assert len(fetch_observations) == 3, (
            f"Expected 3 fetch attempts, observed {len(fetch_observations)}"
        )
        assert all(d == 0 for d in fetch_observations), (
            f"bare_repo_lock must not be held during fetch retry, saw depths {fetch_observations}"
        )

        # Sanity: `worktree add` and the cleanup `worktree remove` /
        # `worktree prune` must run *under* the flock — that's the
        # whole point of keeping the flock at all.
        worktree_observations = [
            (args[:2], depth)
            for args, depth in observations
            if args[:2] in (["worktree", "add"], ["worktree", "remove"], ["worktree", "prune"])
        ]
        assert worktree_observations, "Expected worktree git ops to be observed"
        assert all(depth >= 1 for _args, depth in worktree_observations), (
            f"Worktree ops must run under bare_repo_lock, saw {worktree_observations}"
        )

    def test_bare_repo_lock_not_held_across_regenerate_fetch(self, monkeypatch):
        """Cross-process flock is also released around the regenerate-path fetch.

        Companion to ``test_bare_repo_lock_not_held_across_fetch_retry``: the
        non-FF push retry runs a second ``fetch +CHECKPOINT_BRANCH:CHECKPOINT_BRANCH``
        from a separate code path (``checkpoint_handler.py:1079-1084``). It has
        the same fetch-timeout pathology — holding the flock across it would
        block every state-store commit and worktree op against the same bare
        repo for up to ~60s. A regression that wrapped the regenerate fetch
        in ``bare_repo_lock`` would not be caught by the initial-fetch test.
        """
        import checkpoint_handler

        checkpoint_handler._store_locks.clear()

        flock_depth = [0]
        observations: list[tuple[list[str], int]] = []

        @contextlib.contextmanager
        def recording_flock(_repo_path):
            flock_depth[0] += 1
            try:
                yield
            finally:
                flock_depth[0] -= 1

        monkeypatch.setattr(checkpoint_handler, "bare_repo_lock", recording_flock)
        monkeypatch.setattr(checkpoint_handler.time, "sleep", lambda _s: None)

        handler = checkpoint_handler.CheckpointHandler(github_token="test-token")

        push_count = [0]

        def track_run_git(cwd, args, **kwargs):
            observations.append((list(args), flock_depth[0]))
            if "push" in args:
                push_count[0] += 1
                if push_count[0] == 1:
                    raise checkpoint_handler.CheckpointError(
                        "Git command failed: ! [rejected] non-fast-forward"
                    )
            return MagicMock(returncode=0, stdout="", stderr="")

        handler._run_git = track_run_git
        handler._branch_exists = MagicMock(return_value=True)

        result = handler.store_checkpoint_v2(self._make_checkpoint(), "/fake/repo")
        assert result is True

        push_calls = [args for args, _ in observations if "push" in args]
        assert len(push_calls) == 2, (
            f"Expected 2 push attempts (initial + regenerate retry), got {len(push_calls)}"
        )

        # Both the initial fetch and the regenerate-path fetch must run
        # outside the flock. Pin the count to exactly 2 (initial + regenerate)
        # so a regression that adds a redundant fetch *inside* the flock
        # — masked by an at-depth-0 fetch elsewhere — is caught.
        fetch_observations = [depth for args, depth in observations if args[:1] == ["fetch"]]
        assert len(fetch_observations) == 2, (
            f"Expected exactly 2 fetches (initial + regenerate), observed {len(fetch_observations)}"
        )
        assert all(d == 0 for d in fetch_observations), (
            "bare_repo_lock must not be held during any fetch (including the "
            f"regenerate-path retry), saw depths {fetch_observations}"
        )

    def test_bare_repo_lock_wraps_branch_d_in_orphan_path(self, monkeypatch):
        """``branch -D`` and the orphan-path ``worktree add`` run under the flock.

        Companion to ``test_bare_repo_lock_not_held_across_fetch_retry``: that
        test mocks ``_branch_exists=True`` so the orphan path's ``branch -D``
        is never executed. A regression that moved ``branch -D`` outside the
        flock window (``checkpoint_handler.py:978-987``) — or moved
        ``checkout --orphan`` *inside* it — would not be caught. This test
        drives the orphan path explicitly.
        """
        import checkpoint_handler

        checkpoint_handler._store_locks.clear()

        flock_depth = [0]
        observations: list[tuple[list[str], int]] = []

        @contextlib.contextmanager
        def recording_flock(_repo_path):
            flock_depth[0] += 1
            try:
                yield
            finally:
                flock_depth[0] -= 1

        monkeypatch.setattr(checkpoint_handler, "bare_repo_lock", recording_flock)
        monkeypatch.setattr(checkpoint_handler.time, "sleep", lambda _s: None)

        handler = checkpoint_handler.CheckpointHandler(github_token="test-token")

        def track_run_git(cwd, args, **kwargs):
            observations.append((list(args), flock_depth[0]))
            return MagicMock(returncode=0, stdout="", stderr="")

        handler._run_git = track_run_git
        handler._branch_exists = MagicMock(return_value=False)

        result = handler.store_checkpoint_v2(self._make_checkpoint(), "/fake/repo")
        assert result is True

        # Both bare-repo writers in the orphan path must run under the flock.
        branch_d_observations = [
            depth for args, depth in observations if args[:2] == ["branch", "-D"]
        ]
        assert branch_d_observations, "Expected `branch -D` to run in the orphan path"
        assert all(d >= 1 for d in branch_d_observations), (
            f"`branch -D` must run under bare_repo_lock, saw depths {branch_d_observations}"
        )

        worktree_add_observations = [
            depth for args, depth in observations if args[:2] == ["worktree", "add"]
        ]
        assert worktree_add_observations, "Expected `worktree add` to run in the orphan path"
        assert all(d >= 1 for d in worktree_add_observations), (
            f"`worktree add` must run under bare_repo_lock, saw depths {worktree_add_observations}"
        )

        # ``branch -D`` must run *before* ``worktree add`` in the orphan path.
        # Running ``worktree add`` against a still-existing branch would fail
        # at the git layer in production. The two calls share the same flock
        # window, so the only way to keep them safe is the explicit ordering
        # at ``checkpoint_handler.py:978-987``. A regression that swapped them
        # would still pass the depth check above; this assertion catches that.
        observed_args = [args for args, _ in observations]
        first_branch_d = next(
            i for i, args in enumerate(observed_args) if args[:2] == ["branch", "-D"]
        )
        first_worktree_add = next(
            i for i, args in enumerate(observed_args) if args[:2] == ["worktree", "add"]
        )
        assert first_branch_d < first_worktree_add, (
            "`branch -D` must run before `worktree add` in the orphan path "
            f"(saw branch -D at index {first_branch_d}, worktree add at index {first_worktree_add})"
        )

        # ``checkout --orphan`` runs inside the temp worktree, not the bare
        # repo, so it must run *outside* the flock. A regression that nested
        # it inside the flock window would inflate the cross-process critical
        # section unnecessarily.
        orphan_observations = [
            depth for args, depth in observations if args[:2] == ["checkout", "--orphan"]
        ]
        assert orphan_observations, "Expected `checkout --orphan` to run in the orphan path"
        assert all(d == 0 for d in orphan_observations), (
            f"`checkout --orphan` must run outside bare_repo_lock, saw depths {orphan_observations}"
        )


class TestStoreCheckpointV2RemoteTarget:
    """Tests for store_checkpoint_v2 remote URL resolution (issue #1767).

    The gateway pod has no SSH config, so origin URLs inherited from the host
    clone as SSH must be rewritten to HTTPS before any fetch/push.
    """

    def _run(self, checkpoint_repo, remote_url):
        import checkpoint_handler
        from egg_contracts.checkpoints import (
            CheckpointV2,
            SessionMetadata,
            TriggerType,
        )

        handler = checkpoint_handler.CheckpointHandler(github_token="test-token")
        git_calls = []

        def track_run_git(cwd, args, **kwargs):
            git_calls.append((cwd, args, kwargs))
            return MagicMock(returncode=0, stdout="", stderr="")

        handler._run_git = track_run_git
        handler._branch_exists = MagicMock(return_value=True)

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

        with patch(
            "checkpoint_handler.resolve_remote_url",
            return_value=(remote_url, None),
        ):
            try:
                handler.store_checkpoint_v2(
                    checkpoint, "/fake/repo", checkpoint_repo=checkpoint_repo
                )
            except Exception:
                pass

        fetch_calls = [c for c in git_calls if "fetch" in c[1]]
        assert fetch_calls, "Expected a git fetch call"
        # The fetch target is the second arg after 'fetch'
        return fetch_calls[0][1][1]

    def test_ssh_origin_rewritten_to_https(self):
        """SSH-form origin URL is rewritten to HTTPS before fetch/push."""
        target = self._run(
            checkpoint_repo=None,
            remote_url="git@github.com:owner/repo.git",
        )
        assert target == "https://github.com/owner/repo.git"

    def test_ssh_url_form_rewritten_to_https(self):
        """ssh:// URL form is rewritten to HTTPS."""
        target = self._run(
            checkpoint_repo=None,
            remote_url="ssh://git@github.com/owner/repo.git",
        )
        assert target == "https://github.com/owner/repo.git"

    def test_https_origin_uses_remote_name(self):
        """HTTPS origin keeps the remote name (credential helper works)."""
        target = self._run(
            checkpoint_repo=None,
            remote_url="https://github.com/owner/repo.git",
        )
        assert target == "origin"

    def test_checkpoint_repo_overrides_origin(self):
        """Explicit checkpoint_repo is used regardless of origin URL."""
        target = self._run(
            checkpoint_repo="owner/other-repo",
            remote_url="git@github.com:owner/repo.git",
        )
        assert target == "https://github.com/owner/other-repo.git"

    def test_resolve_error_falls_back_to_remote_name(self):
        """When resolve_remote_url returns an error, fall back to bare remote name."""
        import checkpoint_handler
        from egg_contracts.checkpoints import (
            CheckpointV2,
            SessionMetadata,
            TriggerType,
        )

        handler = checkpoint_handler.CheckpointHandler(github_token="test-token")
        git_calls = []

        def track_run_git(cwd, args, **kwargs):
            git_calls.append((cwd, args, kwargs))
            return MagicMock(returncode=0, stdout="", stderr="")

        handler._run_git = track_run_git
        handler._branch_exists = MagicMock(return_value=True)

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

        with patch(
            "checkpoint_handler.resolve_remote_url",
            return_value=("", "fatal: No such remote 'origin'"),
        ):
            try:
                handler.store_checkpoint_v2(checkpoint, "/fake/repo", checkpoint_repo=None)
            except Exception:
                pass

        fetch_calls = [c for c in git_calls if "fetch" in c[1]]
        assert fetch_calls, "Expected a git fetch call"
        assert fetch_calls[0][1][1] == "origin"


class TestFetchAndReadIndexRemoteTarget:
    """Tests that fetch_and_read_index resolves SSH origins to HTTPS (#1767)."""

    def _get_fetch_target(self, checkpoint_repo, remote_url, resolve_error=None):
        import checkpoint_handler

        handler = checkpoint_handler.CheckpointHandler(github_token="test-token")
        git_calls = []

        def track_run_git(cwd, args, **kwargs):
            git_calls.append((cwd, args, kwargs))
            return MagicMock(returncode=0, stdout="", stderr="")

        handler._run_git = track_run_git
        handler._branch_exists = MagicMock(return_value=True)

        resolve_return = ("", resolve_error) if resolve_error else (remote_url, None)
        with patch(
            "checkpoint_handler.resolve_remote_url",
            return_value=resolve_return,
        ):
            try:
                handler.fetch_and_read_index("/fake/repo", checkpoint_repo=checkpoint_repo)
            except Exception:
                pass

        fetch_calls = [c for c in git_calls if "fetch" in c[1]]
        assert fetch_calls, "Expected a git fetch call"
        return fetch_calls[0][1][1]

    def test_ssh_origin_rewritten_to_https(self):
        target = self._get_fetch_target(None, "git@github.com:owner/repo.git")
        assert target == "https://github.com/owner/repo.git"

    def test_https_origin_uses_remote_name(self):
        target = self._get_fetch_target(None, "https://github.com/owner/repo.git")
        assert target == "origin"

    def test_checkpoint_repo_overrides_origin(self):
        target = self._get_fetch_target("owner/other", "git@github.com:owner/repo.git")
        assert target == "https://github.com/owner/other.git"


class TestEnsureRefRemoteTarget:
    """Tests that ensure_ref resolves SSH origins to HTTPS (#1767)."""

    def _get_fetch_target(self, checkpoint_repo, remote_url, resolve_error=None):
        import checkpoint_handler

        handler = checkpoint_handler.CheckpointHandler(github_token="test-token")
        git_calls = []

        def track_run_git(cwd, args, **kwargs):
            git_calls.append((cwd, args, kwargs))
            return MagicMock(returncode=0, stdout="", stderr="")

        handler._run_git = track_run_git
        handler._branch_exists = MagicMock(return_value=True)

        resolve_return = ("", resolve_error) if resolve_error else (remote_url, None)
        with patch(
            "checkpoint_handler.resolve_remote_url",
            return_value=resolve_return,
        ):
            try:
                handler.ensure_ref("/fake/repo", checkpoint_repo=checkpoint_repo)
            except Exception:
                pass

        fetch_calls = [c for c in git_calls if "fetch" in c[1]]
        assert fetch_calls, "Expected a git fetch call"
        return fetch_calls[0][1][1]

    def test_ssh_origin_rewritten_to_https(self):
        target = self._get_fetch_target(None, "git@github.com:owner/repo.git")
        assert target == "https://github.com/owner/repo.git"

    def test_https_origin_uses_remote_name(self):
        target = self._get_fetch_target(None, "https://github.com/owner/repo.git")
        assert target == "origin"


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
        assert _resolve_agent_type("reviewer") == AgentType.REVIEWER

    def test_new_orchestrator_roles(self):
        """New orchestrator roles map correctly."""
        from egg_contracts.checkpoints import AgentType

        assert _resolve_agent_type("architect") == AgentType.ARCHITECT
        assert _resolve_agent_type("task_planner") == AgentType.TASK_PLANNER
        assert _resolve_agent_type("risk_analyst") == AgentType.RISK_ANALYST
        assert _resolve_agent_type("refiner") == AgentType.REFINER
        # checker role removed from gateway; maps to UNKNOWN
        assert _resolve_agent_type("checker") == AgentType.UNKNOWN

    def test_reviewer_subtypes(self):
        """Reviewer subtypes all map to REVIEWER."""
        from egg_contracts.checkpoints import AgentType

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


class TestExtractRepoFromRemote:
    """Tests for _extract_repo_from_remote helper."""

    @patch("checkpoint_handler.subprocess.run")
    def test_https_url(self, mock_run):
        """Extracts owner/repo from HTTPS remote URL."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/owner/repo.git\n",
        )
        assert _extract_repo_from_remote("/some/repo") == "owner/repo"

    @patch("checkpoint_handler.subprocess.run")
    def test_https_url_without_git_suffix(self, mock_run):
        """Extracts owner/repo from HTTPS URL without .git suffix."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/owner/repo\n",
        )
        assert _extract_repo_from_remote("/some/repo") == "owner/repo"

    @patch("checkpoint_handler.subprocess.run")
    def test_ssh_url(self, mock_run):
        """Extracts owner/repo from SSH remote URL."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="git@github.com:owner/repo.git\n",
        )
        assert _extract_repo_from_remote("/some/repo") == "owner/repo"

    @patch("checkpoint_handler.subprocess.run")
    def test_ssh_url_without_git_suffix(self, mock_run):
        """Extracts owner/repo from SSH URL without .git suffix."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="git@github.com:entireio/cli\n",
        )
        assert _extract_repo_from_remote("/some/repo") == "entireio/cli"

    @patch("checkpoint_handler.subprocess.run")
    def test_non_github_url(self, mock_run):
        """Returns None for non-GitHub remote URLs."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://gitlab.com/owner/repo.git\n",
        )
        assert _extract_repo_from_remote("/some/repo") is None

    @patch("checkpoint_handler.subprocess.run")
    def test_git_command_failure(self, mock_run):
        """Returns None when git command fails."""
        mock_run.return_value = MagicMock(
            returncode=128,
            stdout="",
        )
        assert _extract_repo_from_remote("/some/repo") is None

    @patch("checkpoint_handler.subprocess.run")
    def test_empty_output(self, mock_run):
        """Returns None when git returns empty output."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="  \n",
        )
        assert _extract_repo_from_remote("/some/repo") is None

    @patch("checkpoint_handler.subprocess.run")
    def test_subprocess_exception(self, mock_run):
        """Returns None when subprocess raises an exception."""
        mock_run.side_effect = OSError("No such file or directory")
        assert _extract_repo_from_remote("/some/repo") is None

    @patch("checkpoint_handler.subprocess.run")
    def test_timeout_exception(self, mock_run):
        """Returns None on subprocess timeout."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=10)
        assert _extract_repo_from_remote("/some/repo") is None

    @patch("checkpoint_handler.subprocess.run")
    def test_passes_repo_path_as_cwd(self, mock_run):
        """Passes repo_path as cwd to subprocess."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/owner/repo.git\n",
        )
        _extract_repo_from_remote("/home/egg/repos/myrepo")
        mock_run.assert_called_once()
        assert mock_run.call_args[1]["cwd"] == "/home/egg/repos/myrepo"

    @patch("checkpoint_handler.subprocess.run")
    def test_dotted_repo_name_https(self, mock_run):
        """Extracts owner/repo when repo name contains dots."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/my-org/some.project.git\n",
        )
        assert _extract_repo_from_remote("/some/repo") == "my-org/some.project"

    @patch("checkpoint_handler.subprocess.run")
    def test_dotted_repo_name_without_git_suffix(self, mock_run):
        """Extracts owner/repo when repo name contains dots and no .git suffix."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/my-org/some.project\n",
        )
        assert _extract_repo_from_remote("/some/repo") == "my-org/some.project"


class TestResolveRepo:
    """Tests for CheckpointHandler._resolve_repo."""

    @patch("checkpoint_handler._extract_repo_from_remote")
    def test_from_repo_path(self, mock_extract):
        """Resolves repo from repo_path."""
        from checkpoint_handler import CheckpointHandler

        mock_extract.return_value = "owner/repo"
        handler = CheckpointHandler()
        result = handler._resolve_repo("/home/egg/repos/egg", None)
        assert result == "owner/repo"
        mock_extract.assert_called_once_with("/home/egg/repos/egg")

    @patch("checkpoint_handler._extract_repo_from_remote")
    def test_fallback_to_session_last_repo_path(self, mock_extract):
        """Falls back to session.last_repo_path when repo_path extraction fails."""
        from checkpoint_handler import CheckpointHandler

        mock_extract.side_effect = lambda path: (
            "entireio/cli" if path == "/home/egg/repos/cli" else None
        )
        handler = CheckpointHandler()
        session = _make_test_session()
        session.last_repo_path = "/home/egg/repos/cli"
        result = handler._resolve_repo("/home/egg/repos/egg", session)
        assert result == "entireio/cli"

    @patch("checkpoint_handler._extract_repo_from_remote")
    def test_no_duplicate_extraction_when_paths_match(self, mock_extract):
        """Does not try the same path twice when repo_path == session.last_repo_path."""
        from checkpoint_handler import CheckpointHandler

        mock_extract.return_value = None
        handler = CheckpointHandler()
        session = _make_test_session()
        session.last_repo_path = "/home/egg/repos/egg"
        result = handler._resolve_repo("/home/egg/repos/egg", session)
        assert result is None
        mock_extract.assert_called_once_with("/home/egg/repos/egg")

    @patch("checkpoint_handler._extract_repo_from_remote")
    def test_none_repo_path_with_session(self, mock_extract):
        """Uses session.last_repo_path when repo_path is None."""
        from checkpoint_handler import CheckpointHandler

        mock_extract.return_value = "owner/repo"
        handler = CheckpointHandler()
        session = _make_test_session()
        session.last_repo_path = "/home/egg/repos/egg"
        result = handler._resolve_repo(None, session)
        assert result == "owner/repo"
        mock_extract.assert_called_once_with("/home/egg/repos/egg")

    @patch("checkpoint_handler._extract_repo_from_remote")
    def test_none_repo_path_none_session(self, mock_extract):
        """Returns None when both repo_path and session are None."""
        from checkpoint_handler import CheckpointHandler

        handler = CheckpointHandler()
        result = handler._resolve_repo(None, None)
        assert result is None
        mock_extract.assert_not_called()

    @patch("checkpoint_handler._extract_repo_from_remote")
    def test_session_without_last_repo_path(self, mock_extract):
        """Returns None when repo_path fails and session has no last_repo_path."""
        from checkpoint_handler import CheckpointHandler

        mock_extract.return_value = None
        handler = CheckpointHandler()
        session = _make_test_session()
        session.last_repo_path = None
        result = handler._resolve_repo(None, session)
        assert result is None
        mock_extract.assert_not_called()


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


class TestSessionEndCaptureTimeout:
    """Tests for SESSION_END_CAPTURE_TIMEOUT constant."""

    def test_timeout_is_180_seconds(self):
        """Timeout should be 180s to cover worst-case git operations."""
        import checkpoint_handler

        assert checkpoint_handler.SESSION_END_CAPTURE_TIMEOUT == 180


class TestNonDaemonThreads:
    """Tests for non-daemon checkpoint store threads."""

    @patch("checkpoint_handler.get_checkpoint_handler")
    def test_session_end_thread_is_non_daemon(self, mock_get_handler):
        """Session-end checkpoint store thread should be non-daemon."""
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

        import threading

        created_threads = []
        original_init = threading.Thread.__init__

        def tracking_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            created_threads.append(self)

        with patch.object(threading.Thread, "__init__", tracking_init):
            checkpoint, event = capture_session_end_checkpoint(
                session=session,
                session_status=SessionStatus.COMPLETED,
                repo_path="/home/egg/repos/test-repo",
                async_store=True,
            )

        # Find the store thread (it's the one with our target function)
        store_threads = [t for t in created_threads if not t.daemon]
        assert len(store_threads) >= 1, "Expected at least one non-daemon thread"

        if event is not None:
            event.wait(timeout=5)


class TestFetchRetryInStore:
    """Tests for fetch retry logic in store_checkpoint_v2."""

    def test_fetch_retries_on_timeout(self):
        """Fetch retries up to 3 times on TimeoutExpired."""
        import checkpoint_handler

        handler = checkpoint_handler.CheckpointHandler(github_token="test-token")

        git_calls = []
        call_count = 0

        def track_run_git(cwd, args, **kwargs):
            nonlocal call_count
            git_calls.append((cwd, args, kwargs))
            if "fetch" in args:
                call_count += 1
                if call_count < 3:
                    raise subprocess.TimeoutExpired(cmd="git fetch", timeout=45)
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

        # Will succeed on 3rd attempt; may fail later but we check fetch retries
        try:
            handler.store_checkpoint_v2(checkpoint, "/fake/repo")
        except Exception:
            pass

        fetch_calls = [c for c in git_calls if "fetch" in c[1]]
        assert len(fetch_calls) == 3, f"Expected 3 fetch attempts, got {len(fetch_calls)}"

    def test_fetch_retries_on_checkpoint_error(self):
        """Fetch retries on CheckpointError (e.g., network failures)."""
        import checkpoint_handler

        handler = checkpoint_handler.CheckpointHandler(github_token="test-token")

        git_calls = []
        call_count = 0

        def track_run_git(cwd, args, **kwargs):
            nonlocal call_count
            git_calls.append((cwd, args, kwargs))
            if "fetch" in args:
                call_count += 1
                if call_count < 3:
                    raise checkpoint_handler.CheckpointError(
                        "Git command failed: fatal: fetch failed"
                    )
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
            handler.store_checkpoint_v2(checkpoint, "/fake/repo")
        except Exception:
            pass

        fetch_calls = [c for c in git_calls if "fetch" in c[1]]
        assert len(fetch_calls) == 3, f"Expected 3 fetch attempts, got {len(fetch_calls)}"

    def test_fetch_does_not_retry_on_unexpected_error(self):
        """Fetch does not retry on unexpected errors (e.g., OSError)."""
        import checkpoint_handler

        handler = checkpoint_handler.CheckpointHandler(github_token="test-token")

        git_calls = []

        def track_run_git(cwd, args, **kwargs):
            git_calls.append((cwd, args, kwargs))
            if "fetch" in args:
                raise OSError("Unexpected filesystem error")
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
            handler.store_checkpoint_v2(checkpoint, "/fake/repo")
        except Exception:
            pass

        fetch_calls = [c for c in git_calls if "fetch" in c[1]]
        assert len(fetch_calls) == 1, f"Expected 1 fetch attempt (no retry), got {len(fetch_calls)}"


class TestPushRetryInStore:
    """Tests for push retry logic in store_checkpoint_v2."""

    def _make_handler_and_checkpoint(self):
        """Create a handler and checkpoint for push retry tests."""
        import checkpoint_handler

        handler = checkpoint_handler.CheckpointHandler(github_token="test-token")
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
        return handler, checkpoint

    @patch("time.sleep")
    def test_push_succeeds_on_first_attempt(self, mock_sleep):
        """Push succeeds on first attempt — no retry triggered."""
        handler, checkpoint = self._make_handler_and_checkpoint()
        git_calls = []

        def track_run_git(cwd, args, **kwargs):
            git_calls.append((cwd, args, kwargs))
            return MagicMock(returncode=0, stdout="", stderr="")

        handler._run_git = track_run_git

        result = handler.store_checkpoint_v2(checkpoint, "/fake/repo")
        assert result is True, "Expected store to return True on first-attempt success"

        push_calls = [c for c in git_calls if "push" in c[1]]
        assert len(push_calls) == 1, f"Expected 1 push attempt, got {len(push_calls)}"
        mock_sleep.assert_not_called()

    @patch("time.sleep")
    def test_push_retries_on_non_fast_forward(self, mock_sleep):
        """Push fails with non-fast-forward, regenerate against fresh tip succeeds."""
        import checkpoint_handler

        handler, checkpoint = self._make_handler_and_checkpoint()
        git_calls = []
        push_count = 0

        def track_run_git(cwd, args, **kwargs):
            nonlocal push_count
            git_calls.append((cwd, args, kwargs))
            if "push" in args:
                push_count += 1
                if push_count == 1:
                    raise checkpoint_handler.CheckpointError(
                        "Git command failed: ! [rejected] non-fast-forward"
                    )
            return MagicMock(returncode=0, stdout="", stderr="")

        handler._run_git = track_run_git

        result = handler.store_checkpoint_v2(checkpoint, "/fake/repo")
        assert result is True, "Expected store to return True after successful retry"

        push_calls = [c for c in git_calls if "push" in c[1]]
        assert len(push_calls) == 2, f"Expected 2 push attempts, got {len(push_calls)}"
        # Verify regenerate flow ran between pushes:
        # checkout --detach + fetch + reset --hard + re-add + re-commit
        first_push_idx = git_calls.index(push_calls[0])
        post_push = git_calls[first_push_idx + 1 :]
        detach_after_push = [c for c in post_push if "checkout" in c[1] and "--detach" in c[1]]
        fetch_after_push = [c for c in post_push if "fetch" in c[1]]
        reset_after_push = [c for c in post_push if "reset" in c[1]]
        commit_after_push = [c for c in post_push if "commit" in c[1]]
        assert len(detach_after_push) >= 1, (
            "Expected checkout --detach before fetch so the local "
            "CHECKPOINT_BRANCH ref is updatable from the orphan path"
        )
        assert len(fetch_after_push) >= 1, "Expected fetch after failed push"
        assert len(reset_after_push) >= 1, "Expected reset --hard after failed push"
        assert len(commit_after_push) >= 1, "Expected re-commit after regenerate"
        # No rebase under the new strategy
        assert not [c for c in git_calls if "rebase" in c[1]], (
            "rebase should not be invoked under the regenerate strategy"
        )

    @patch("time.sleep")
    def test_push_raises_after_max_attempts(self, mock_sleep):
        """Push fails with non-fast-forward on all 3 attempts, then raises."""
        import checkpoint_handler

        handler, checkpoint = self._make_handler_and_checkpoint()
        git_calls = []

        def track_run_git(cwd, args, **kwargs):
            git_calls.append((cwd, args, kwargs))
            if "push" in args:
                raise checkpoint_handler.CheckpointError(
                    "Git command failed: ! [rejected] non-fast-forward"
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        handler._run_git = track_run_git

        # store_checkpoint_v2 catches all exceptions and returns False
        result = handler.store_checkpoint_v2(checkpoint, "/fake/repo")
        assert result is False, "Expected store to return False after max push attempts"

        push_calls = [c for c in git_calls if "push" in c[1]]
        assert len(push_calls) == 3, f"Expected 3 push attempts, got {len(push_calls)}"

    @patch("time.sleep")
    def test_push_does_not_retry_on_non_matching_error(self, mock_sleep):
        """Push fails with auth error — no retry, returns False immediately."""
        import checkpoint_handler

        handler, checkpoint = self._make_handler_and_checkpoint()
        git_calls = []

        def track_run_git(cwd, args, **kwargs):
            git_calls.append((cwd, args, kwargs))
            if "push" in args:
                raise checkpoint_handler.CheckpointError(
                    "Git command failed: fatal: Authentication failed"
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        handler._run_git = track_run_git

        result = handler.store_checkpoint_v2(checkpoint, "/fake/repo")
        assert result is False, "Expected store to return False on auth failure"

        push_calls = [c for c in git_calls if "push" in c[1]]
        assert len(push_calls) == 1, f"Expected 1 push attempt (no retry), got {len(push_calls)}"

    @patch("time.sleep")
    def test_push_fails_when_fetch_in_retry_fails(self, mock_sleep):
        """Fetch within the retry loop fails — returns False."""
        import checkpoint_handler

        handler, checkpoint = self._make_handler_and_checkpoint()
        git_calls = []
        push_count = 0
        push_failed = False

        def track_run_git(cwd, args, **kwargs):
            nonlocal push_count, push_failed
            git_calls.append((cwd, args, kwargs))
            if "push" in args:
                push_count += 1
                if push_count == 1:
                    push_failed = True
                    raise checkpoint_handler.CheckpointError(
                        "Git command failed: ! [rejected] non-fast-forward"
                    )
            if "fetch" in args and push_failed:
                raise checkpoint_handler.CheckpointError(
                    "Git command failed: fatal: Could not read from remote"
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        handler._run_git = track_run_git

        result = handler.store_checkpoint_v2(checkpoint, "/fake/repo")
        assert result is False, "Expected store to return False on fetch failure"

        push_calls = [c for c in git_calls if "push" in c[1]]
        assert len(push_calls) == 1, (
            f"Expected 1 push attempt before fetch failure, got {len(push_calls)}"
        )

    @patch("time.sleep")
    def test_push_fails_when_regenerate_commit_fails(self, mock_sleep):
        """Regenerate-step commit failure during retry surfaces as False."""
        import checkpoint_handler

        handler, checkpoint = self._make_handler_and_checkpoint()
        git_calls = []
        push_count = 0
        push_failed = False

        def track_run_git(cwd, args, **kwargs):
            nonlocal push_count, push_failed
            git_calls.append((cwd, args, kwargs))
            if "push" in args:
                push_count += 1
                if push_count == 1:
                    push_failed = True
                    raise checkpoint_handler.CheckpointError(
                        "Git command failed: ! [rejected] non-fast-forward"
                    )
            # Fail the re-commit during the regenerate step.
            if push_failed and args[:1] == ["commit"]:
                raise checkpoint_handler.CheckpointError(
                    "Git command failed: nothing to commit, working tree clean"
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        handler._run_git = track_run_git

        result = handler.store_checkpoint_v2(checkpoint, "/fake/repo")
        assert result is False, "Expected store to return False on regenerate-commit failure"

        push_calls = [c for c in git_calls if "push" in c[1]]
        assert len(push_calls) == 1, (
            f"Expected 1 push attempt before regenerate failure, got {len(push_calls)}"
        )


class TestMissingBufferWarning:
    """Tests for missing transcript buffer warnings."""

    @patch("checkpoint_handler.get_proxy_buffer_path")
    @patch("checkpoint_handler.logger")
    def test_session_end_warns_on_missing_buffer_for_long_session(
        self, mock_logger, mock_get_path, tmp_path
    ):
        """Warning is logged when buffer is missing for a session > 10 seconds."""
        from checkpoint_handler import CheckpointHandler

        # Create a fake buffer path that doesn't exist
        fake_path = tmp_path / "nonexistent.jsonl"
        mock_get_path.return_value = fake_path

        handler = CheckpointHandler.__new__(CheckpointHandler)
        handler._github_token = "fake-token"
        handler._checkpoint_repo = "owner/repo"
        handler._redaction_enabled = False

        # Session that ran for 1 hour (well over 10 seconds)
        session = _make_test_session(container_id="test-missing-buffer")

        from egg_contracts.checkpoints import SessionStatus

        handler.capture_session_end_checkpoint(
            session=session,
            session_status=SessionStatus.COMPLETED,
        )

        # Should have logged a warning about the missing buffer
        warning_calls = [
            call
            for call in mock_logger.warning.call_args_list
            if "missing" in str(call).lower() and "buffer" in str(call).lower()
        ]
        assert len(warning_calls) >= 1, (
            f"Expected warning about missing buffer, got warnings: "
            f"{[str(c) for c in mock_logger.warning.call_args_list]}"
        )
