"""Gap tests for egg_babysit.loop — signal handling, concurrent push, full flows."""

import signal
from unittest.mock import MagicMock, patch

from egg_babysit.config import BabysitConfig
from egg_babysit.loop import BabysitLoop
from egg_babysit.steps.conflict import StepResult
from egg_babysit.steps.review import ReviewStepResult
from egg_babysit.types import (
    BabysitExitReason,
    BabysitStep,
    CICheckResult,
    CICheckStatus,
    PRState,
    ReviewVerdict,
)


def _make_pr_state(**overrides):
    defaults = {
        "number": 42,
        "title": "Test PR",
        "state": "open",
        "merged": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "head_sha": "abc123",
        "base_branch": "main",
        "head_branch": "feature",
        "ci_checks": [],
        "review_comments": [],
    }
    defaults.update(overrides)
    return PRState(**defaults)


class TestSignalHandling:
    """Test BabysitLoop signal handler installation and cancellation."""

    @patch("egg_babysit.loop.get_full_pr_state")
    @patch("egg_babysit.loop.wait_for_ci")
    @patch("egg_babysit.loop.subprocess.run")
    def test_sigterm_cancels_loop(self, mock_subprocess, mock_ci, mock_get_state):
        """SIGTERM sets _cancelled flag and loop exits with CANCELLED."""
        config = BabysitConfig(
            pr_number=42,
            repo="owner/repo",
            timeout_seconds=600,
            max_iterations=10,
            poll_interval_seconds=1,
        )

        # First call: open PR, second call: simulate signal on CI wait
        mock_get_state.return_value = _make_pr_state()

        def cancel_on_ci_wait(*args, **kwargs):
            # Simulate SIGTERM during CI wait
            loop._cancelled = True
            return (CICheckStatus.PENDING, [])

        mock_ci.side_effect = cancel_on_ci_wait

        loop = BabysitLoop(config)
        result = loop.run()

        # Should exit with CANCELLED on the next iteration check
        assert result.exit_reason == BabysitExitReason.CANCELLED
        assert "termination" in result.message.lower() or "cancelled" in result.message.lower()

    def test_signal_handlers_installed_and_restored(self):
        """Signal handlers are installed on run() and restored after."""
        config = BabysitConfig(pr_number=42, repo="owner/repo", timeout_seconds=1)
        loop = BabysitLoop(config)

        original_sigterm = signal.getsignal(signal.SIGTERM)
        original_sigint = signal.getsignal(signal.SIGINT)

        with patch("egg_babysit.loop.get_full_pr_state") as mock_state:
            mock_state.return_value = _make_pr_state(merged=True)
            loop.run()

        # After run, handlers should be restored
        assert signal.getsignal(signal.SIGTERM) == original_sigterm
        assert signal.getsignal(signal.SIGINT) == original_sigint


class TestConcurrentPushDetection:
    """Test that HEAD SHA changes reset retry counts."""

    @patch("egg_babysit.loop.fix_failed_checks")
    @patch("egg_babysit.loop.wait_for_ci")
    @patch("egg_babysit.loop.get_full_pr_state")
    @patch("egg_babysit.loop.subprocess.run")
    def test_head_sha_change_resets_retries(
        self, mock_subprocess, mock_get_state, mock_ci, mock_fix
    ):
        """When HEAD SHA changes between iterations, retry counts are cleared."""
        config = BabysitConfig(
            pr_number=42,
            repo="owner/repo",
            max_iterations=3,
            timeout_seconds=600,
            poll_interval_seconds=1,
        )

        failing_check = CICheckResult(
            name="lint", status=CICheckStatus.FAILING, conclusion="FAILURE"
        )

        # Iteration 1: sha=aaa, CI failing, fix succeeds but CI still failing
        # Iteration 2: sha=bbb (changed!), CI failing, retries should be reset
        # Iteration 3: max iterations reached
        states = [
            _make_pr_state(head_sha="aaa"),
            _make_pr_state(head_sha="bbb"),  # SHA changed
            _make_pr_state(head_sha="bbb"),
        ]
        mock_get_state.side_effect = states
        mock_ci.return_value = (CICheckStatus.FAILING, [failing_check])
        mock_fix.return_value = StepResult(success=False, message="Fix failed")

        loop = BabysitLoop(config)
        result = loop.run()

        assert result.exit_reason == BabysitExitReason.MAX_ITERATIONS
        # Retry counts should have been cleared when SHA changed
        assert loop.state.retry_counts == {}


class TestFullFlows:
    """Test full loop flows through multiple steps."""

    @patch("egg_babysit.loop.address_feedback")
    @patch("egg_babysit.loop.run_review")
    @patch("egg_babysit.loop.wait_for_ci")
    @patch("egg_babysit.loop.get_full_pr_state")
    @patch("egg_babysit.loop.subprocess.run")
    def test_ci_pass_review_changes_feedback_flow(
        self,
        mock_subprocess,
        mock_get_state,
        mock_ci,
        mock_review,
        mock_feedback,
    ):
        """Full flow: CI passes → review requests changes → feedback addressed → loop continues."""
        config = BabysitConfig(
            pr_number=42,
            repo="owner/repo",
            max_iterations=3,
            timeout_seconds=600,
            poll_interval_seconds=1,
        )

        # Iteration 1: CI passing, review requests changes, feedback addressed
        # Iteration 2: CI passing, approved → READY_TO_MERGE
        mock_get_state.side_effect = [
            _make_pr_state(),  # Iteration 1 start
            _make_pr_state(),  # Iteration 1 re-fetch after CI
            _make_pr_state(),  # Iteration 2 start
            _make_pr_state(review_verdict=ReviewVerdict.APPROVED),  # Iteration 2 re-fetch
        ]
        mock_ci.return_value = (CICheckStatus.PASSING, [])
        mock_review.return_value = ReviewStepResult(
            verdict=ReviewVerdict.CHANGES_REQUESTED,
            comments=["Fix the bug"],
            success=True,
        )
        mock_feedback.return_value = StepResult(success=True, message="Fixed")

        loop = BabysitLoop(config)
        result = loop.run()

        assert result.exit_reason == BabysitExitReason.READY_TO_MERGE
        assert mock_feedback.call_count == 1
        assert loop.state.feedback_rounds == 1

    @patch("egg_babysit.loop.escalate")
    @patch("egg_babysit.loop.wait_for_ci")
    @patch("egg_babysit.loop.get_full_pr_state")
    @patch("egg_babysit.loop.subprocess.run")
    def test_stale_ci_checks_escalate(
        self, mock_subprocess, mock_get_state, mock_ci, mock_escalate
    ):
        """Stale CI checks cause escalation."""
        config = BabysitConfig(
            pr_number=42,
            repo="owner/repo",
            max_iterations=5,
            timeout_seconds=600,
            poll_interval_seconds=1,
        )

        mock_get_state.return_value = _make_pr_state()
        mock_ci.return_value = (CICheckStatus.STALE, [])

        loop = BabysitLoop(config)
        result = loop.run()

        assert result.exit_reason == BabysitExitReason.ESCALATED
        assert "stale" in result.message.lower()
        mock_escalate.assert_called_once()

    @patch("egg_babysit.loop.run_review")
    @patch("egg_babysit.loop.wait_for_ci")
    @patch("egg_babysit.loop.get_full_pr_state")
    @patch("egg_babysit.loop.subprocess.run")
    def test_review_comment_only_continues(
        self, mock_subprocess, mock_get_state, mock_ci, mock_review
    ):
        """Review with COMMENTED verdict (no changes requested) continues the loop."""
        config = BabysitConfig(
            pr_number=42,
            repo="owner/repo",
            max_iterations=2,
            timeout_seconds=600,
            poll_interval_seconds=1,
        )

        mock_get_state.side_effect = [
            _make_pr_state(),  # Iteration 1
            _make_pr_state(),  # Re-fetch after CI
            _make_pr_state(),  # Iteration 2
            _make_pr_state(),  # Re-fetch after CI
        ]
        mock_ci.return_value = (CICheckStatus.PASSING, [])
        mock_review.return_value = ReviewStepResult(
            verdict=ReviewVerdict.COMMENTED,
            comments=["Looks interesting"],
            success=True,
        )

        loop = BabysitLoop(config)
        result = loop.run()

        assert result.exit_reason == BabysitExitReason.MAX_ITERATIONS
        assert mock_review.call_count == 2

    @patch("egg_babysit.loop.get_full_pr_state")
    @patch("egg_babysit.loop.subprocess.run")
    def test_pr_state_fetch_returns_none_exits_error(self, mock_subprocess, mock_get_state):
        """When _fetch_pr_state returns None (exception), loop exits with ERROR."""
        config = BabysitConfig(
            pr_number=42,
            repo="owner/repo",
            max_iterations=5,
            timeout_seconds=600,
            poll_interval_seconds=1,
        )

        # The first call returns None because get_full_pr_state raises
        mock_get_state.side_effect = Exception("API unavailable")

        loop = BabysitLoop(config)
        result = loop.run()

        assert result.exit_reason == BabysitExitReason.ERROR

    @patch("egg_babysit.loop.fix_failed_checks")
    @patch("egg_babysit.loop.wait_for_ci")
    @patch("egg_babysit.loop.get_full_pr_state")
    @patch("egg_babysit.loop.subprocess.run")
    def test_fix_checks_then_ci_still_failing_retries(
        self, mock_subprocess, mock_get_state, mock_ci, mock_fix
    ):
        """After fixing checks, if CI is still not passing, loop continues."""
        config = BabysitConfig(
            pr_number=42,
            repo="owner/repo",
            max_iterations=2,
            timeout_seconds=600,
            poll_interval_seconds=1,
        )

        failing_check = CICheckResult(
            name="test", status=CICheckStatus.FAILING, conclusion="FAILURE"
        )

        mock_get_state.return_value = _make_pr_state()
        # First wait_for_ci: failing, second (after fix): still failing
        mock_ci.side_effect = [
            (CICheckStatus.FAILING, [failing_check]),
            (CICheckStatus.FAILING, [failing_check]),
            (CICheckStatus.FAILING, [failing_check]),
            (CICheckStatus.FAILING, [failing_check]),
        ]
        mock_fix.return_value = StepResult(success=True, message="Fixed lint")

        loop = BabysitLoop(config)
        result = loop.run()

        assert result.exit_reason == BabysitExitReason.MAX_ITERATIONS


class TestEmitProgress:
    """Test _emit_progress method."""

    def test_emit_progress_success(self):
        """Progress emission on success."""
        config = BabysitConfig(pr_number=42, repo="owner/repo")
        loop = BabysitLoop(config)

        with patch("egg_babysit.loop.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            loop._emit_progress("test_step", True)
            call_args = mock_run.call_args[0][0]
            assert "complete" in call_args

    def test_emit_progress_failure(self):
        """Progress emission on failure uses 'blocked' state."""
        config = BabysitConfig(pr_number=42, repo="owner/repo")
        loop = BabysitLoop(config)

        with patch("egg_babysit.loop.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            loop._emit_progress("test_step", False)
            call_args = mock_run.call_args[0][0]
            assert "blocked" in call_args

    def test_emit_progress_handles_file_not_found(self):
        """FileNotFoundError (no egg-orch) is silently handled."""
        config = BabysitConfig(pr_number=42, repo="owner/repo")
        loop = BabysitLoop(config)

        with patch("egg_babysit.loop.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("egg-orch not found")
            # Should not raise
            loop._emit_progress("test_step", True)

    def test_emit_progress_handles_generic_exception(self):
        """Generic exceptions in progress emission are silently handled."""
        config = BabysitConfig(pr_number=42, repo="owner/repo")
        loop = BabysitLoop(config)

        with patch("egg_babysit.loop.subprocess.run") as mock_run:
            mock_run.side_effect = RuntimeError("unexpected")
            # Should not raise
            loop._emit_progress("test_step", True)


class TestLoopInternalMethods:
    """Test internal BabysitLoop methods."""

    def test_elapsed_increases(self):
        """_elapsed returns increasing time."""
        config = BabysitConfig(pr_number=42, repo="owner/repo")
        loop = BabysitLoop(config)

        elapsed = loop._elapsed()
        assert elapsed >= 0

    def test_is_timed_out_false_initially(self):
        """Loop should not be timed out initially."""
        config = BabysitConfig(pr_number=42, repo="owner/repo", timeout_seconds=3600)
        loop = BabysitLoop(config)

        assert loop._is_timed_out() is False

    @patch("egg_babysit.loop.time.monotonic")
    def test_is_timed_out_true_after_timeout(self, mock_time):
        """Loop should be timed out when elapsed >= timeout."""
        mock_time.side_effect = [0.0, 10000.0]
        config = BabysitConfig(pr_number=42, repo="owner/repo", timeout_seconds=10)
        loop = BabysitLoop(config)

        assert loop._is_timed_out() is True

    def test_set_step_changes_state(self):
        """_set_step updates current_step."""
        config = BabysitConfig(pr_number=42, repo="owner/repo")
        loop = BabysitLoop(config)

        loop._set_step(BabysitStep.WAIT_CI)
        assert loop.state.current_step == BabysitStep.WAIT_CI

    def test_set_step_noop_same_step(self):
        """_set_step is a noop when step hasn't changed."""
        config = BabysitConfig(pr_number=42, repo="owner/repo")
        loop = BabysitLoop(config)

        loop._set_step(BabysitStep.CHECK_CONFLICTS)
        assert loop.state.current_step == BabysitStep.CHECK_CONFLICTS

    def test_result_captures_state(self):
        """_result builds a BabysitResult from current state."""
        config = BabysitConfig(pr_number=42, repo="owner/repo")
        loop = BabysitLoop(config)
        loop.state.iteration = 3
        loop._set_step(BabysitStep.REVIEW)

        result = loop._result(BabysitExitReason.TIMEOUT, message="timed out")

        assert result.exit_reason == BabysitExitReason.TIMEOUT
        assert result.iterations == 3
        assert result.last_step == BabysitStep.REVIEW
        assert result.message == "timed out"
        assert result.duration_seconds >= 0

    def test_update_activity_changes_timestamp(self):
        """_update_activity modifies the last_activity_at timestamp."""
        config = BabysitConfig(pr_number=42, repo="owner/repo")
        loop = BabysitLoop(config)

        loop._update_activity()
        # Timestamp should be updated (could be same if running fast, but should not be empty)
        assert loop.state.last_activity_at != ""
