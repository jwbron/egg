"""Tests for egg_babysit.loop — main babysit loop state machine."""

from unittest.mock import patch

from egg_babysit.config import BabysitConfig
from egg_babysit.loop import BabysitLoop, babysit
from egg_babysit.types import (
    BabysitExitReason,
    BabysitStep,
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


class TestBabysitLoop:
    """Test the BabysitLoop state machine."""

    @patch("egg_babysit.loop.get_full_pr_state")
    @patch("egg_babysit.loop.subprocess.run")  # suppress egg-orch progress calls
    def test_loop_exits_on_merged_pr(self, mock_subprocess, mock_get_state, fast_config):
        """If PR is already merged, loop exits immediately."""
        mock_get_state.return_value = _make_pr_state(merged=True, state="merged")

        loop = BabysitLoop(fast_config)
        result = loop.run()

        assert result.exit_reason == BabysitExitReason.MERGED
        assert "merged" in result.message.lower()

    @patch("egg_babysit.loop.get_full_pr_state")
    @patch("egg_babysit.loop.wait_for_ci")
    @patch("egg_babysit.loop.subprocess.run")
    def test_loop_respects_max_iterations(self, mock_subprocess, mock_ci, mock_get_state):
        """Loop exits after max iterations with pending CI."""
        config = BabysitConfig(
            pr_number=42,
            repo="owner/repo",
            max_iterations=2,
            timeout_seconds=600,
            poll_interval_seconds=1,
        )
        # PR is always open, CI always pending
        mock_get_state.return_value = _make_pr_state()
        mock_ci.return_value = (CICheckStatus.PENDING, [])

        loop = BabysitLoop(config)
        result = loop.run()

        assert result.exit_reason == BabysitExitReason.MAX_ITERATIONS
        assert result.iterations == 2

    @patch("egg_babysit.loop.time.monotonic")
    @patch("egg_babysit.loop.get_full_pr_state")
    @patch("egg_babysit.loop.wait_for_ci")
    @patch("egg_babysit.loop.subprocess.run")
    def test_loop_respects_timeout(self, mock_subprocess, mock_ci, mock_get_state, mock_time):
        """Loop exits after timeout."""
        config = BabysitConfig(
            pr_number=42,
            repo="owner/repo",
            timeout_seconds=10,
            max_iterations=100,
            poll_interval_seconds=1,
        )
        # First call sets _start_time (in __init__), subsequent calls return
        # a value well past the timeout so the loop exits immediately.
        mock_time.side_effect = [0.0] + [100.0] * 20
        mock_get_state.return_value = _make_pr_state()
        mock_ci.return_value = (CICheckStatus.PENDING, [])

        loop = BabysitLoop(config)
        result = loop.run()

        assert result.exit_reason == BabysitExitReason.TIMEOUT

    @patch("egg_babysit.loop.get_full_pr_state")
    @patch("egg_babysit.loop.subprocess.run")
    def test_loop_exits_on_closed_pr(self, mock_subprocess, mock_get_state, fast_config):
        """Closed PR causes cancellation exit."""
        mock_get_state.return_value = _make_pr_state(state="closed")

        loop = BabysitLoop(fast_config)
        result = loop.run()

        assert result.exit_reason == BabysitExitReason.CANCELLED
        assert "closed" in result.message.lower()

    @patch("egg_babysit.loop.get_full_pr_state")
    @patch("egg_babysit.loop.subprocess.run")
    def test_loop_exits_on_fetch_error(self, mock_subprocess, mock_get_state, fast_config):
        """Error fetching PR state exits with ERROR."""
        mock_get_state.side_effect = Exception("Network error")

        loop = BabysitLoop(fast_config)
        result = loop.run()

        assert result.exit_reason == BabysitExitReason.ERROR

    @patch("egg_babysit.loop.run_review")
    @patch("egg_babysit.loop.wait_for_ci")
    @patch("egg_babysit.loop.get_full_pr_state")
    @patch("egg_babysit.loop.subprocess.run")
    def test_loop_step_transitions_ci_pass_to_review(
        self, mock_subprocess, mock_get_state, mock_ci, mock_review
    ):
        """CI passing triggers review step."""
        config = BabysitConfig(
            pr_number=42,
            repo="owner/repo",
            max_iterations=2,
            timeout_seconds=600,
            poll_interval_seconds=1,
        )

        # First get_full_pr_state: open PR, no conflicts
        # Second get_full_pr_state (re-fetch after CI): approved
        mock_get_state.side_effect = [
            _make_pr_state(),
            _make_pr_state(review_verdict=ReviewVerdict.APPROVED),
        ]
        mock_ci.return_value = (CICheckStatus.PASSING, [])

        loop = BabysitLoop(config)
        result = loop.run()

        # PR approved + CI passing = READY_TO_MERGE exit (not merged yet)
        assert result.exit_reason == BabysitExitReason.READY_TO_MERGE

    def test_loop_state_tracking(self, fast_config):
        """Verify LoopState is properly initialized."""
        loop = BabysitLoop(fast_config)
        assert loop.state.iteration == 0
        assert loop.state.current_step == BabysitStep.CHECK_CONFLICTS
        assert loop.state.started_at != ""
        assert loop.state.last_activity_at != ""

    @patch("egg_babysit.loop.escalate")
    @patch("egg_babysit.loop.resolve_conflicts")
    @patch("egg_babysit.loop.get_full_pr_state")
    @patch("egg_babysit.loop.subprocess.run")
    def test_loop_conflict_escalation(
        self, mock_subprocess, mock_get_state, mock_resolve, mock_escalate, fast_config
    ):
        """Conflict resolution failure escalates."""
        from egg_babysit.steps.conflict import StepResult

        mock_get_state.return_value = _make_pr_state(mergeable_state="dirty")
        mock_resolve.return_value = StepResult(
            success=False,
            message="Cannot resolve",
            escalate=True,
        )

        loop = BabysitLoop(fast_config)
        result = loop.run()

        assert result.exit_reason == BabysitExitReason.ESCALATED
        mock_escalate.assert_called_once()


class TestBabysitFunction:
    """Test the babysit() convenience function."""

    @patch("egg_babysit.loop.get_full_pr_state")
    @patch("egg_babysit.loop.subprocess.run")
    def test_babysit_creates_and_runs_loop(self, mock_subprocess, mock_get_state, fast_config):
        mock_get_state.return_value = _make_pr_state(merged=True)

        result = babysit(fast_config)

        assert result.exit_reason == BabysitExitReason.MERGED
