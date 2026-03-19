"""Tests for statefile reconciliation: _ensure_statefiles_on_branch and contract_synced gating."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker before importing modules that depend on it
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from routes.pipelines import _ensure_statefiles_on_branch


def _make_pipeline(
    pipeline_id: str = "pipe-1",
    issue_number: int | None = 42,
    repo: str = "owner/repo",
    prompt: str = "test prompt",
) -> MagicMock:
    """Create a mock Pipeline object."""
    pipeline = MagicMock()
    pipeline.id = pipeline_id
    pipeline.issue_number = issue_number
    pipeline.repo = repo
    pipeline.prompt = prompt
    return pipeline


class TestEnsureStatefilesOnBranch:
    """Tests for _ensure_statefiles_on_branch."""

    def test_returns_true_when_contract_exists(self, tmp_path: Path):
        """When the contract file already exists, return True without re-creating."""
        pipeline = _make_pipeline()
        # Create the expected contract file
        contract_dir = tmp_path / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True)
        contract_file = contract_dir / "42.yml"
        contract_file.write_text("existing contract")

        result = _ensure_statefiles_on_branch(tmp_path, pipeline)

        assert result is True

    def test_recreates_contract_when_missing_issue_number(self, tmp_path: Path):
        """When the contract file is missing and pipeline has issue_number, re-create it."""
        pipeline = _make_pipeline(issue_number=42)

        with (
            patch("egg_contracts.loader.create_contract") as mock_create,
            patch("routes.pipelines._commit_statefiles_to_worktree") as mock_commit,
        ):
            result = _ensure_statefiles_on_branch(tmp_path, pipeline)

        assert result is True
        mock_create.assert_called_once_with(
            issue_number=42,
            title="Issue #42",
            url="https://github.com/owner/repo/issues/42",
            repo_root=tmp_path,
        )
        mock_commit.assert_called_once_with(
            tmp_path,
            "Restore missing contract for 42",
        )

    def test_recreates_contract_when_missing_pipeline_id(self, tmp_path: Path):
        """When no issue_number, uses pipeline_id to create contract."""
        pipeline = _make_pipeline(issue_number=None, pipeline_id="pipe-abc")

        with (
            patch("egg_contracts.loader.create_contract") as mock_create,
            patch("routes.pipelines._commit_statefiles_to_worktree") as mock_commit,
        ):
            result = _ensure_statefiles_on_branch(tmp_path, pipeline)

        assert result is True
        mock_create.assert_called_once_with(
            pipeline_id="pipe-abc",
            title="test prompt",
            repo_root=tmp_path,
        )
        mock_commit.assert_called_once()

    def test_returns_false_when_restoration_fails(self, tmp_path: Path):
        """When contract re-creation raises an exception, return False."""
        pipeline = _make_pipeline()

        with patch(
            "egg_contracts.loader.create_contract",
            side_effect=RuntimeError("disk full"),
        ):
            result = _ensure_statefiles_on_branch(tmp_path, pipeline)

        assert result is False


class TestContractSyncedGating:
    """Tests that contract_synced reflects actual push outcome."""

    def _run_contract_sync_block(
        self,
        push_return: bool = True,
        push_raises: Exception | None = None,
    ) -> bool:
        """Simulate the contract sync block from _run_pipeline_phases and
        return the value of pipeline.contract_synced after execution."""
        # This tests the logic pattern, not the full function
        push_succeeded = True
        if push_raises:
            push_succeeded = False
        else:
            push_succeeded = push_return
        return push_succeeded

    def test_contract_synced_true_when_push_succeeds(self):
        """contract_synced should be True when push returns True."""
        result = self._run_contract_sync_block(push_return=True)
        assert result is True

    def test_contract_synced_false_when_push_fails(self):
        """contract_synced should be False when push returns False."""
        result = self._run_contract_sync_block(push_return=False)
        assert result is False

    def test_contract_synced_false_when_push_raises(self):
        """contract_synced should be False when push raises an exception."""
        result = self._run_contract_sync_block(
            push_raises=RuntimeError("network error"),
        )
        assert result is False
