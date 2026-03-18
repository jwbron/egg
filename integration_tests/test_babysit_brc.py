"""Integration tests for babysit BRC pipeline lifecycle.

Tests pipeline creation with mode=babysit, BRC agent registration,
review graph initialization, and pipeline completion signaling.
"""

import pytest
from egg_babysit.config import BabysitConfig
from egg_babysit.types import (
    BabysitAgentRole,
    BabysitExitReason,
    BabysitResult,
    BabysitStep,
    ConsensusState,
    LoopState,
)


@pytest.mark.integration
class TestBabysitBRCPipelineCreation:
    """Test babysit pipeline creation with BRC configuration."""

    def test_babysit_config_concurrent_mode(self):
        """BabysitConfig accepts concurrent_mode parameter."""
        config = BabysitConfig(
            pr_number=100,
            repo="owner/repo",
            concurrent_mode=True,
            consensus_timeout_minutes=15,
            max_consensus_rounds=5,
        )
        assert config.concurrent_mode is True
        assert config.consensus_timeout_minutes == 15
        assert config.max_consensus_rounds == 5

    def test_babysit_config_sequential_fallback(self):
        """BabysitConfig defaults to sequential mode."""
        config = BabysitConfig(pr_number=100, repo="owner/repo")
        assert config.concurrent_mode is False
        assert config.consensus_timeout_minutes == 30
        assert config.max_consensus_rounds == 3


@pytest.mark.integration
class TestBabysitBRCAgentRegistration:
    """Test BRC agent roles and review graph configuration."""

    def test_agent_roles_defined(self):
        """BabysitAgentRole has fixer and reviewer roles."""
        assert BabysitAgentRole.BABYSIT_FIXER.value == "babysit_fixer"
        assert BabysitAgentRole.BABYSIT_REVIEWER.value == "babysit_reviewer"

    def test_consensus_states_defined(self):
        """ConsensusState has all BRC states."""
        states = [s.value for s in ConsensusState]
        assert "working" in states
        assert "proposed" in states
        assert "acked" in states
        assert "nacked" in states
        assert "confirmed" in states

    def test_loop_state_tracks_consensus(self):
        """LoopState tracks consensus round."""
        state = LoopState()
        assert state.consensus_round == 0
        state.consensus_round = 3
        assert state.consensus_round == 3


@pytest.mark.integration
class TestBabysitBRCResultTypes:
    """Test BRC result types are properly structured."""

    def test_babysit_result_with_concurrent_fields(self):
        """BabysitResult works with all fields."""
        result = BabysitResult(
            exit_reason=BabysitExitReason.READY_TO_MERGE,
            iterations=2,
            duration_seconds=120.0,
            last_step=BabysitStep.REVIEW,
            message="PR approved via BRC consensus",
        )
        assert result.exit_reason == BabysitExitReason.READY_TO_MERGE
        assert result.message == "PR approved via BRC consensus"

    def test_concurrent_review_result_import(self):
        """ConcurrentReviewResult can be imported and instantiated."""
        from egg_babysit.concurrent import ConcurrentReviewResult
        from egg_babysit.types import ReviewVerdict

        result = ConcurrentReviewResult(
            verdict=ReviewVerdict.APPROVED,
            comments=["LGTM"],
            consensus_reached=True,
            rounds_used=1,
        )
        assert result.consensus_reached is True


@pytest.mark.integration
class TestBabysitBRCComments:
    """Test status comment management types."""

    def test_status_comment_marker(self):
        """Status comment marker is defined."""
        from egg_babysit.comments import STATUS_COMMENT_MARKER

        assert "egg-status-comment" in STATUS_COMMENT_MARKER

    def test_status_comment_dataclass(self):
        """StatusComment dataclass works."""
        from egg_babysit.comments import StatusComment

        comment = StatusComment(
            id="MDEyOklzc3VlQ29tbWVudDE=",
            body="<!-- egg-status-comment -->\nReview complete",
            author_login="egg-bot",
            created_at="2024-01-01T00:00:00Z",
        )
        assert comment.is_minimized is False
        assert "egg-status-comment" in comment.body
