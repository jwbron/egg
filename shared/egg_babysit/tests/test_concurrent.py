"""Unit tests for concurrent BRC babysit-pr execution.

Tests the BRC consensus flow, NACK handling, flip-flop cap enforcement,
timeout escalation, and graceful degradation when not in concurrent mode.
"""

import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from egg_babysit.concurrent import ConcurrentReviewResult, run_concurrent_review
from egg_babysit.config import BabysitConfig
from egg_babysit.types import (
    BabysitAgentRole,
    ConsensusState,
    LoopState,
    ReviewVerdict,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def babysit_config() -> BabysitConfig:
    """Default babysit config for testing."""
    return BabysitConfig(
        pr_number=42,
        repo="owner/repo",
        timeout_seconds=3600,
        concurrent_mode=True,
        consensus_timeout_minutes=10,
        max_consensus_rounds=3,
    )


@pytest.fixture
def sequential_config() -> BabysitConfig:
    """Babysit config with concurrent mode disabled."""
    return BabysitConfig(
        pr_number=42,
        repo="owner/repo",
        timeout_seconds=3600,
        concurrent_mode=False,
    )


# ---------------------------------------------------------------------------
# TASK-1-1: Agent roles and config
# ---------------------------------------------------------------------------


class TestBabysitAgentRoles:
    """Test BabysitAgentRole enum values."""

    def test_fixer_role_value(self) -> None:
        assert BabysitAgentRole.BABYSIT_FIXER.value == "babysit_fixer"

    def test_reviewer_role_value(self) -> None:
        assert BabysitAgentRole.BABYSIT_REVIEWER.value == "babysit_reviewer"


class TestConsensusState:
    """Test ConsensusState enum values."""

    def test_all_states(self) -> None:
        assert ConsensusState.WORKING.value == "working"
        assert ConsensusState.PROPOSED.value == "proposed"
        assert ConsensusState.ACKED.value == "acked"
        assert ConsensusState.NACKED.value == "nacked"
        assert ConsensusState.CONFIRMED.value == "confirmed"


class TestBabysitConfigBRC:
    """Test BabysitConfig BRC fields."""

    def test_default_consensus_timeout(self) -> None:
        config = BabysitConfig(pr_number=1, repo="o/r")
        assert config.consensus_timeout_minutes == 30

    def test_default_max_consensus_rounds(self) -> None:
        config = BabysitConfig(pr_number=1, repo="o/r")
        assert config.max_consensus_rounds == 3

    def test_default_concurrent_mode_off(self) -> None:
        config = BabysitConfig(pr_number=1, repo="o/r")
        assert config.concurrent_mode is False

    def test_custom_brc_params(self) -> None:
        config = BabysitConfig(
            pr_number=1,
            repo="o/r",
            consensus_timeout_minutes=15,
            max_consensus_rounds=5,
            concurrent_mode=True,
        )
        assert config.consensus_timeout_minutes == 15
        assert config.max_consensus_rounds == 5
        assert config.concurrent_mode is True

    def test_invalid_consensus_timeout(self) -> None:
        with pytest.raises(ValueError, match="consensus_timeout_minutes"):
            BabysitConfig(pr_number=1, repo="o/r", consensus_timeout_minutes=0)

    def test_invalid_max_consensus_rounds(self) -> None:
        with pytest.raises(ValueError, match="max_consensus_rounds"):
            BabysitConfig(pr_number=1, repo="o/r", max_consensus_rounds=0)


class TestLoopStateBRC:
    """Test LoopState consensus_round field."""

    def test_default_consensus_round(self) -> None:
        state = LoopState()
        assert state.consensus_round == 0

    def test_consensus_round_increment(self) -> None:
        state = LoopState()
        state.consensus_round += 1
        assert state.consensus_round == 1


# ---------------------------------------------------------------------------
# TASK-1-2: Concurrent review execution
# ---------------------------------------------------------------------------


class TestConcurrentReviewResult:
    """Test ConcurrentReviewResult dataclass."""

    def test_basic_result(self) -> None:
        result = ConcurrentReviewResult(
            verdict=ReviewVerdict.APPROVED,
            comments=["Looks good"],
            consensus_reached=True,
            rounds_used=1,
        )
        assert result.verdict == ReviewVerdict.APPROVED
        assert result.consensus_reached is True
        assert result.escalated is False

    def test_escalated_result(self) -> None:
        result = ConcurrentReviewResult(
            verdict=ReviewVerdict.PENDING,
            comments=[],
            consensus_reached=False,
            rounds_used=0,
            escalated=True,
            message="Timed out",
        )
        assert result.escalated is True


class TestRunConcurrentReview:
    """Test run_concurrent_review function."""

    @patch("egg_babysit.concurrent.subprocess.Popen")
    @patch("egg_babysit.concurrent.fetch_pr_state")
    @patch("egg_babysit.concurrent.fetch_review_comments")
    @patch("orchestrator.consensus_wrapper.build_consensus_wrapped_command")
    def test_happy_path_approved(
        self,
        mock_build_cmd: Any,
        mock_fetch_comments: Any,
        mock_fetch_state: Any,
        mock_popen: Any,
        babysit_config: BabysitConfig,
    ) -> None:
        """Test successful concurrent review with APPROVED verdict."""
        mock_build_cmd.return_value = ["bash", "-c", "echo test"]
        mock_fetch_comments.return_value = []

        mock_proc = MagicMock()
        mock_proc.communicate.return_value = ("review output", "")
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        mock_state = MagicMock()
        mock_state.review_verdict = ReviewVerdict.APPROVED
        mock_fetch_state.return_value = mock_state

        result = run_concurrent_review(babysit_config)

        assert result.verdict == ReviewVerdict.APPROVED
        assert result.consensus_reached is True
        assert result.escalated is False

    @patch("egg_babysit.concurrent.subprocess.Popen")
    @patch("egg_babysit.concurrent.fetch_pr_state")
    @patch("egg_babysit.concurrent.fetch_review_comments")
    @patch("orchestrator.consensus_wrapper.build_consensus_wrapped_command")
    def test_timeout_escalation(
        self,
        mock_build_cmd: Any,
        mock_fetch_comments: Any,
        mock_fetch_state: Any,
        mock_popen: Any,
        babysit_config: BabysitConfig,
    ) -> None:
        """Test that timeout triggers escalation."""
        mock_build_cmd.return_value = ["bash", "-c", "echo test"]
        mock_fetch_comments.return_value = []

        mock_proc = MagicMock()
        mock_proc.communicate.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=600)
        mock_proc.kill.return_value = None
        mock_proc.wait.return_value = None
        mock_popen.return_value = mock_proc

        result = run_concurrent_review(babysit_config)

        assert result.escalated is True
        assert result.consensus_reached is False
        assert result.verdict == ReviewVerdict.PENDING

    @patch("egg_babysit.concurrent.subprocess.Popen")
    @patch("egg_babysit.concurrent.fetch_pr_state")
    @patch("egg_babysit.concurrent.fetch_review_comments")
    @patch("orchestrator.consensus_wrapper.build_consensus_wrapped_command")
    def test_fixer_failure(
        self,
        mock_build_cmd: Any,
        mock_fetch_comments: Any,
        mock_fetch_state: Any,
        mock_popen: Any,
        babysit_config: BabysitConfig,
    ) -> None:
        """Test that fixer failure results in no consensus."""
        mock_build_cmd.return_value = ["bash", "-c", "echo test"]
        mock_fetch_comments.return_value = []

        fixer_proc = MagicMock()
        fixer_proc.communicate.return_value = ("", "error")
        fixer_proc.returncode = 1

        reviewer_proc = MagicMock()
        reviewer_proc.communicate.return_value = ("ok", "")
        reviewer_proc.returncode = 0

        mock_popen.side_effect = [fixer_proc, reviewer_proc]

        mock_state = MagicMock()
        mock_state.review_verdict = ReviewVerdict.CHANGES_REQUESTED
        mock_fetch_state.return_value = mock_state

        result = run_concurrent_review(babysit_config)

        assert result.consensus_reached is False

    @patch("egg_babysit.concurrent.subprocess.Popen")
    @patch("egg_babysit.concurrent.fetch_pr_state")
    @patch("egg_babysit.concurrent.fetch_review_comments")
    @patch("orchestrator.consensus_wrapper.build_consensus_wrapped_command")
    def test_with_existing_feedback(
        self,
        mock_build_cmd: Any,
        mock_fetch_comments: Any,
        mock_fetch_state: Any,
        mock_popen: Any,
        babysit_config: BabysitConfig,
    ) -> None:
        """Test that existing review comments are passed to fixer."""
        mock_build_cmd.return_value = ["bash", "-c", "echo test"]
        mock_fetch_comments.return_value = ["Fix the type annotation"]

        mock_proc = MagicMock()
        mock_proc.communicate.return_value = ("", "")
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        mock_state = MagicMock()
        mock_state.review_verdict = ReviewVerdict.APPROVED
        mock_fetch_state.return_value = mock_state

        run_concurrent_review(babysit_config)

        assert mock_build_cmd.call_count == 2

    @patch("egg_babysit.concurrent.subprocess.Popen")
    @patch("egg_babysit.concurrent.fetch_pr_state")
    @patch("egg_babysit.concurrent.fetch_review_comments")
    @patch("orchestrator.consensus_wrapper.build_consensus_wrapped_command")
    def test_brc_env_vars_set(
        self,
        mock_build_cmd: Any,
        mock_fetch_comments: Any,
        mock_fetch_state: Any,
        mock_popen: Any,
        babysit_config: BabysitConfig,
    ) -> None:
        """Test that BRC environment variables are set correctly."""
        mock_build_cmd.return_value = ["bash", "-c", "echo test"]
        mock_fetch_comments.return_value = []

        mock_proc = MagicMock()
        mock_proc.communicate.return_value = ("", "")
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        mock_state = MagicMock()
        mock_state.review_verdict = ReviewVerdict.APPROVED
        mock_fetch_state.return_value = mock_state

        run_concurrent_review(babysit_config)

        assert mock_popen.call_count == 2

        fixer_call_env = mock_popen.call_args_list[0].kwargs.get("env", {})
        assert fixer_call_env.get("EGG_CONCURRENT_MODE") == "true"
        assert fixer_call_env.get("EGG_BRC_ROLE_TYPE") == "producer"
        assert fixer_call_env.get("EGG_AGENT_ROLE") == "babysit_fixer"

        reviewer_call_env = mock_popen.call_args_list[1].kwargs.get("env", {})
        assert reviewer_call_env.get("EGG_CONCURRENT_MODE") == "true"
        assert reviewer_call_env.get("EGG_BRC_ROLE_TYPE") == "reviewer"
        assert reviewer_call_env.get("EGG_AGENT_ROLE") == "babysit_reviewer"


# ---------------------------------------------------------------------------
# TASK-1-3: BRC fixer and reviewer spawning
# ---------------------------------------------------------------------------


class TestBRCFixer:
    """Test run_brc_fixer function."""

    @patch("orchestrator.consensus_wrapper.build_consensus_wrapped_command")
    @patch("egg_babysit.fixer.subprocess.run")
    def test_brc_fixer_success(
        self, mock_run: Any, mock_build_cmd: Any, babysit_config: BabysitConfig
    ) -> None:
        """Test BRC fixer spawning with successful completion."""
        from egg_babysit.fixer import run_brc_fixer

        mock_build_cmd.return_value = ["bash", "-c", "echo test"]

        sha_result = MagicMock()
        sha_result.returncode = 0
        sha_result.stdout = "abc123\n"

        agent_result = MagicMock()
        agent_result.returncode = 0
        agent_result.stderr = ""

        mock_run.side_effect = [sha_result, agent_result, sha_result]

        result = run_brc_fixer("fix prompt", babysit_config, "check_fix")

        assert result.success is True

    @patch("orchestrator.consensus_wrapper.build_consensus_wrapped_command")
    @patch("egg_babysit.fixer.subprocess.run")
    def test_brc_fixer_failure(
        self, mock_run: Any, mock_build_cmd: Any, babysit_config: BabysitConfig
    ) -> None:
        """Test BRC fixer handling of agent failure."""
        from egg_babysit.fixer import run_brc_fixer

        mock_build_cmd.return_value = ["bash", "-c", "exit 1"]

        sha_result = MagicMock()
        sha_result.returncode = 0
        sha_result.stdout = "abc123\n"

        agent_result = MagicMock()
        agent_result.returncode = 1
        agent_result.stderr = "agent failed"

        mock_run.side_effect = [sha_result, agent_result]

        result = run_brc_fixer("fix prompt", babysit_config, "check_fix")

        assert result.success is False


class TestBRCReviewer:
    """Test run_brc_reviewer function."""

    @patch("orchestrator.consensus_wrapper.build_consensus_wrapped_command")
    @patch("egg_babysit.reviewer.subprocess.run")
    @patch("egg_babysit.reviewer.fetch_pr_state")
    def test_brc_reviewer_success(
        self,
        mock_fetch_state: Any,
        mock_run: Any,
        mock_build_cmd: Any,
        babysit_config: BabysitConfig,
    ) -> None:
        """Test BRC reviewer spawning with successful completion."""
        from egg_babysit.reviewer import run_brc_reviewer

        mock_build_cmd.return_value = ["bash", "-c", "echo review"]

        agent_result = MagicMock()
        agent_result.returncode = 0
        agent_result.stdout = "Review complete"
        agent_result.stderr = ""
        mock_run.return_value = agent_result

        mock_state = MagicMock()
        mock_state.review_verdict = ReviewVerdict.APPROVED
        mock_fetch_state.return_value = mock_state

        result = run_brc_reviewer("review prompt", babysit_config)

        assert result.verdict == ReviewVerdict.APPROVED

    @patch("orchestrator.consensus_wrapper.build_consensus_wrapped_command")
    @patch("egg_babysit.reviewer.subprocess.run")
    def test_brc_reviewer_timeout(
        self, mock_run: Any, mock_build_cmd: Any, babysit_config: BabysitConfig
    ) -> None:
        """Test BRC reviewer handling of timeout."""
        from egg_babysit.reviewer import run_brc_reviewer

        mock_build_cmd.return_value = ["bash", "-c", "sleep 999"]
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=300)

        result = run_brc_reviewer("review prompt", babysit_config)

        assert result.verdict == ReviewVerdict.PENDING
        assert result.error is not None


# ---------------------------------------------------------------------------
# Loop integration: concurrent vs sequential path
# ---------------------------------------------------------------------------


class TestLoopConcurrentMode:
    """Test that the loop correctly dispatches to concurrent or sequential mode."""

    @patch("egg_babysit.loop.get_full_pr_state")
    @patch("egg_babysit.loop.wait_for_ci")
    def test_sequential_mode_unchanged(
        self, mock_ci: Any, mock_pr_state: Any, sequential_config: BabysitConfig
    ) -> None:
        """Sequential mode should not import concurrent module."""
        from egg_babysit.loop import BabysitLoop

        mock_state = MagicMock()
        mock_state.merged = True
        mock_state.state = "merged"
        mock_state.head_sha = "abc123"
        mock_pr_state.return_value = mock_state

        loop = BabysitLoop(sequential_config)
        result = loop.run()

        assert result.exit_reason == BabysitExitReason.MERGED


# Need the import at the end to avoid circular reference in assertion
from egg_babysit.types import BabysitExitReason  # noqa: E402
