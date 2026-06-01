"""Tests for #2915: auto-populate contract at implement start.

When a pipeline enters implement phase with an empty contract, the orchestrator
should attempt to populate it from the plan draft before spawning agents.
"""

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

from gateway_client import PushResult
from routes.pipelines import (
    ForestValidationError,
    PipelinePhase,
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
        "current_phase": PipelinePhase.IMPLEMENT,
        "branch": "test-branch",
        "base_branch": "main",
        "gateway_mode": "public",
    }


def _make_gateway(
    push_ok: bool = True,
    category: str | None = None,
    detail: str | None = None,
) -> MagicMock:
    gateway = MagicMock()
    gateway.push_worktree_branch.return_value = PushResult(
        ok=push_ok, category=category, detail=detail
    )
    return gateway


def _invoke(state, **overrides):
    """Helper to call the SUT with kwargs derived from the fixture state."""
    kwargs = {
        "worktree_repo_path": overrides.pop("worktree_repo_path"),
        "pipeline_id": state["pipeline_id"],
        "pipeline_mode": state["pipeline_mode"],
        "issue_number": state["issue_number"],
        "current_phase": state["current_phase"],
        "pipeline_branch": state["branch"],
        "gateway": overrides.pop("gateway", _make_gateway()),
        "gateway_mode": state["gateway_mode"],
        "base_branch": state["base_branch"],
    }
    kwargs.update(overrides)
    return _auto_populate_contract_at_implement_start(**kwargs)


def test_auto_populate_empty_contract_success(mock_pipeline_state):
    """Test successful auto-population of an empty contract."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree_repo_path = Path(tmpdir)
        gateway = _make_gateway()

        mock_populate_result = PopulateResult(
            outcome=PopulateOutcome.POPULATED,
            slice_count=3,
            task_count=5,
        )

        with (
            patch("routes.pipelines._populate_contract_from_plan") as mock_populate,
            patch("routes.pipelines._commit_statefiles_to_worktree"),
        ):
            mock_populate.return_value = mock_populate_result

            result = _invoke(
                mock_pipeline_state,
                worktree_repo_path=worktree_repo_path,
                gateway=gateway,
            )

        assert result == 3
        # Verify gateway_mode and base_branch were forwarded.
        gateway.push_worktree_branch.assert_called_once()
        push_kwargs = gateway.push_worktree_branch.call_args.kwargs
        assert push_kwargs["mode"] == "public"
        assert push_kwargs["base_branch"] == "main"
        assert push_kwargs["branch"] == "test-branch"


def test_auto_populate_empty_contract_populate_fails(mock_pipeline_state):
    """Test auto-populate when populate raises an exception."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree_repo_path = Path(tmpdir)

        with patch("routes.pipelines._populate_contract_from_plan") as mock_populate:
            mock_populate.side_effect = Exception("Parse error")

            result = _invoke(mock_pipeline_state, worktree_repo_path=worktree_repo_path)

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

            result = _invoke(mock_pipeline_state, worktree_repo_path=worktree_repo_path)

        assert result == 0


def test_auto_populate_empty_contract_not_populated_outcome(mock_pipeline_state):
    """Test auto-populate when populate returns a non-POPULATED outcome."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree_repo_path = Path(tmpdir)

        mock_populate_result = PopulateResult(
            outcome=PopulateOutcome.EMPTY_RESULT,
            slice_count=0,
            task_count=0,
        )

        with patch("routes.pipelines._populate_contract_from_plan") as mock_populate:
            mock_populate.return_value = mock_populate_result

            result = _invoke(mock_pipeline_state, worktree_repo_path=worktree_repo_path)

        assert result == 0


def test_auto_populate_empty_contract_populated_but_empty(mock_pipeline_state):
    """Test auto-populate when populate returns POPULATED but with 0 slices."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree_repo_path = Path(tmpdir)

        mock_populate_result = PopulateResult(
            outcome=PopulateOutcome.POPULATED,
            slice_count=0,
            task_count=0,
        )

        with patch("routes.pipelines._populate_contract_from_plan") as mock_populate:
            mock_populate.return_value = mock_populate_result

            result = _invoke(mock_pipeline_state, worktree_repo_path=worktree_repo_path)

        assert result == 0


def test_auto_populate_empty_contract_commit_fails(mock_pipeline_state):
    """Test auto-populate when commit raises (should return 0)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree_repo_path = Path(tmpdir)

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

            result = _invoke(mock_pipeline_state, worktree_repo_path=worktree_repo_path)

        assert result == 0


def test_auto_populate_empty_contract_commit_returned_false(mock_pipeline_state):
    """Cover the ``not _committed`` branch: commit short-circuited with no commit."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree_repo_path = Path(tmpdir)

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
            # _commit_statefiles_to_worktree returns False when nothing was staged
            # (no .egg-state directory, no prefix match, or nothing staged after add).
            mock_commit.return_value = False

            result = _invoke(mock_pipeline_state, worktree_repo_path=worktree_repo_path)

        assert result == 0


def test_auto_populate_empty_contract_push_transport_failure(mock_pipeline_state):
    """Test auto-populate when push raises a transport exception (non-fatal)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree_repo_path = Path(tmpdir)

        mock_populate_result = PopulateResult(
            outcome=PopulateOutcome.POPULATED,
            slice_count=3,
            task_count=5,
        )

        gateway = MagicMock()
        gateway.push_worktree_branch.side_effect = Exception("Push transport failed")

        with (
            patch("routes.pipelines._populate_contract_from_plan") as mock_populate,
            patch("routes.pipelines._commit_statefiles_to_worktree"),
        ):
            mock_populate.return_value = mock_populate_result

            result = _invoke(
                mock_pipeline_state,
                worktree_repo_path=worktree_repo_path,
                gateway=gateway,
            )

        # Push transport failure does not abort — contract is committed locally.
        assert result == 3


def test_auto_populate_empty_contract_push_rejected_by_gateway(mock_pipeline_state):
    """Test auto-populate when gateway returns PushResult(ok=False) (non-fatal)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree_repo_path = Path(tmpdir)

        mock_populate_result = PopulateResult(
            outcome=PopulateOutcome.POPULATED,
            slice_count=3,
            task_count=5,
        )

        gateway = _make_gateway(push_ok=False, category="non_fast_forward", detail="...")

        with (
            patch("routes.pipelines._populate_contract_from_plan") as mock_populate,
            patch("routes.pipelines._commit_statefiles_to_worktree"),
        ):
            mock_populate.return_value = mock_populate_result

            result = _invoke(
                mock_pipeline_state,
                worktree_repo_path=worktree_repo_path,
                gateway=gateway,
            )

        # Gateway-rejected push does not abort — contract is committed locally.
        assert result == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
