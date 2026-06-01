"""Tests for #2915: auto-populate contract at implement start.

When a pipeline enters implement phase with an empty contract, the orchestrator
should attempt to populate it from the plan draft before spawning agents.
"""

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add orchestrator to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from routes.pipelines import (
    ForestValidationError,
    PopulateOutcome,
    PopulateResult,
    _auto_populate_contract_at_implement_start,
)


@pytest.fixture
def mock_pipeline_state():
    """Create a mock pipeline state for testing."""
    return {
        "pipeline_id": "test-pipeline-123",
        "issue_number": 123,
        "pipeline_mode": "plan",
        "current_phase": "implement",
        "branch": "test-branch",
    }


def test_auto_populate_empty_contract_success(mock_pipeline_state):
    """Test successful auto-population of an empty contract."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree_repo_path = Path(tmpdir)

        # Mock successful populate
        mock_populate_result = PopulateResult(
            outcome=PopulateOutcome.POPULATED,
            slice_count=3,
            task_count=5,
        )

        with (
            patch("routes.pipelines._populate_contract_from_plan") as mock_populate,
            patch("routes.pipelines._commit_statefiles_to_worktree"),
            patch("gateway.client.GatewayClient"),
        ):
            mock_populate.return_value = mock_populate_result

            result = _auto_populate_contract_at_implement_start(
                worktree_repo_path=worktree_repo_path,
                pipeline_id=mock_pipeline_state["pipeline_id"],
                pipeline_mode=mock_pipeline_state["pipeline_mode"],
                issue_number=mock_pipeline_state["issue_number"],
                current_phase=mock_pipeline_state["current_phase"],
                pipeline_branch=mock_pipeline_state["branch"],
            )

        # Should return slice count
        assert result == 3


def test_auto_populate_empty_contract_populate_fails(mock_pipeline_state):
    """Test auto-populate when populate raises an exception."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree_repo_path = Path(tmpdir)

        with patch("routes.pipelines._populate_contract_from_plan") as mock_populate:
            mock_populate.side_effect = Exception("Parse error")

            result = _auto_populate_contract_at_implement_start(
                worktree_repo_path=worktree_repo_path,
                pipeline_id=mock_pipeline_state["pipeline_id"],
                pipeline_mode=mock_pipeline_state["pipeline_mode"],
                issue_number=mock_pipeline_state["issue_number"],
                current_phase=mock_pipeline_state["current_phase"],
                pipeline_branch=mock_pipeline_state["branch"],
            )

        # Should return 0 on failure
        assert result == 0


def test_auto_populate_empty_contract_forest_validation_error(mock_pipeline_state):
    """Test auto-populate when populate raises a ForestValidationError."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree_repo_path = Path(tmpdir)

        with patch("routes.pipelines._populate_contract_from_plan") as mock_populate:
            mock_populate.side_effect = ForestValidationError(
                "Invalid forest structure",
                errors=["Slice 1 has multiple parents"],
            )

            result = _auto_populate_contract_at_implement_start(
                worktree_repo_path=worktree_repo_path,
                pipeline_id=mock_pipeline_state["pipeline_id"],
                pipeline_mode=mock_pipeline_state["pipeline_mode"],
                issue_number=mock_pipeline_state["issue_number"],
                current_phase=mock_pipeline_state["current_phase"],
                pipeline_branch=mock_pipeline_state["branch"],
            )

        # Should return 0 on forest validation error
        assert result == 0


def test_auto_populate_empty_contract_not_populated_outcome(mock_pipeline_state):
    """Test auto-populate when populate returns a non-POPULATED outcome."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree_repo_path = Path(tmpdir)

        # Mock empty result
        mock_populate_result = PopulateResult(
            outcome=PopulateOutcome.EMPTY_RESULT,
            slice_count=0,
            task_count=0,
        )

        with patch("routes.pipelines._populate_contract_from_plan") as mock_populate:
            mock_populate.return_value = mock_populate_result

            result = _auto_populate_contract_at_implement_start(
                worktree_repo_path=worktree_repo_path,
                pipeline_id=mock_pipeline_state["pipeline_id"],
                pipeline_mode=mock_pipeline_state["pipeline_mode"],
                issue_number=mock_pipeline_state["issue_number"],
                current_phase=mock_pipeline_state["current_phase"],
                pipeline_branch=mock_pipeline_state["branch"],
            )

        # Should return 0 when outcome is not POPULATED
        assert result == 0


def test_auto_populate_empty_contract_populated_but_empty(mock_pipeline_state):
    """Test auto-populate when populate returns POPULATED but with 0 slices."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree_repo_path = Path(tmpdir)

        # Mock populated but empty slices
        mock_populate_result = PopulateResult(
            outcome=PopulateOutcome.POPULATED,
            slice_count=0,
            task_count=0,
        )

        with patch("routes.pipelines._populate_contract_from_plan") as mock_populate:
            mock_populate.return_value = mock_populate_result

            result = _auto_populate_contract_at_implement_start(
                worktree_repo_path=worktree_repo_path,
                pipeline_id=mock_pipeline_state["pipeline_id"],
                pipeline_mode=mock_pipeline_state["pipeline_mode"],
                issue_number=mock_pipeline_state["issue_number"],
                current_phase=mock_pipeline_state["current_phase"],
                pipeline_branch=mock_pipeline_state["branch"],
            )

        # Should return 0 when slice count is 0
        assert result == 0


def test_auto_populate_empty_contract_commit_fails(mock_pipeline_state):
    """Test auto-populate when commit fails (should return 0)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree_repo_path = Path(tmpdir)

        # Mock successful populate
        mock_populate_result = PopulateResult(
            outcome=PopulateOutcome.POPULATED,
            slice_count=3,
            task_count=5,
        )

        with (
            patch("routes.pipelines._populate_contract_from_plan") as mock_populate,
            patch("routes.pipelines._commit_statefiles_to_worktree") as mock_commit,
        ):
            mock_populate.return_value = mock_populate_result
            mock_commit.side_effect = Exception("Commit failed")

            result = _auto_populate_contract_at_implement_start(
                worktree_repo_path=worktree_repo_path,
                pipeline_id=mock_pipeline_state["pipeline_id"],
                pipeline_mode=mock_pipeline_state["pipeline_mode"],
                issue_number=mock_pipeline_state["issue_number"],
                current_phase=mock_pipeline_state["current_phase"],
                pipeline_branch=mock_pipeline_state["branch"],
            )

        # Should return 0 when commit fails
        assert result == 0


def test_auto_populate_empty_contract_push_fails(mock_pipeline_state):
    """Test auto-populate when push fails (should still return slice count)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree_repo_path = Path(tmpdir)

        # Mock successful populate
        mock_populate_result = PopulateResult(
            outcome=PopulateOutcome.POPULATED,
            slice_count=3,
            task_count=5,
        )

        with (
            patch("routes.pipelines._populate_contract_from_plan") as mock_populate,
            patch("routes.pipelines._commit_statefiles_to_worktree"),
            patch("gateway.client.GatewayClient") as mock_gateway_class,
        ):
            mock_populate.return_value = mock_populate_result
            mock_gateway = MagicMock()
            mock_gateway.push_worktrees_branch.side_effect = Exception("Push failed")
            mock_gateway_class.return_value = mock_gateway

            result = _auto_populate_contract_at_implement_start(
                worktree_repo_path=worktree_repo_path,
                pipeline_id=mock_pipeline_state["pipeline_id"],
                pipeline_mode=mock_pipeline_state["pipeline_mode"],
                issue_number=mock_pipeline_state["issue_number"],
                current_phase=mock_pipeline_state["current_phase"],
                pipeline_branch=mock_pipeline_state["branch"],
            )

        # Should still return slice count even if push fails
        # (contract is already committed locally)
        assert result == 3


def test_auto_populate_empty_contract_no_commit_needed(mock_pipeline_state):
    """Test auto-populate when commit returns True but nothing was committed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree_repo_path = Path(tmpdir)

        # Mock successful populate
        mock_populate_result = PopulateResult(
            outcome=PopulateOutcome.POPULATED,
            slice_count=3,
            task_count=5,
        )

        with (
            patch("routes.pipelines._populate_contract_from_plan") as mock_populate,
            patch("routes.pipelines._commit_statefiles_to_worktree") as mock_commit,
            patch("gateway.client.GatewayClient"),
        ):
            mock_populate.return_value = mock_populate_result
            # _commit_statefiles_to_worktree returns True (success)
            mock_commit.return_value = True

            result = _auto_populate_contract_at_implement_start(
                worktree_repo_path=worktree_repo_path,
                pipeline_id=mock_pipeline_state["pipeline_id"],
                pipeline_mode=mock_pipeline_state["pipeline_mode"],
                issue_number=mock_pipeline_state["issue_number"],
                current_phase=mock_pipeline_state["current_phase"],
                pipeline_branch=mock_pipeline_state["branch"],
            )

        # Should return slice count
        assert result == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
