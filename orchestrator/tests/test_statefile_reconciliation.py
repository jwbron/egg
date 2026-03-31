"""Tests for statefile reconciliation: _ensure_statefiles_on_branch."""

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
    mode: str | None = None,
) -> MagicMock:
    """Create a mock Pipeline object."""
    pipeline = MagicMock()
    pipeline.id = pipeline_id
    pipeline.issue_number = issue_number
    pipeline.repo = repo
    pipeline.prompt = prompt
    pipeline.mode = mode
    return pipeline


class TestEnsureStatefilesOnBranch:
    """Tests for _ensure_statefiles_on_branch."""

    def test_returns_true_when_contract_exists(self, tmp_path: Path):
        """When the contract file already exists, return True without re-creating."""
        pipeline = _make_pipeline()
        # Create the expected contract file
        contract_dir = tmp_path / ".egg-state" / "contracts"
        contract_dir.mkdir(parents=True)
        contract_file = contract_dir / "42.json"
        contract_file.write_text("{}")

        result = _ensure_statefiles_on_branch(tmp_path, pipeline)

        assert result is True

    def test_recreates_contract_when_missing_issue_number(self, tmp_path: Path):
        """When the contract file is missing and pipeline has issue_number, re-create it."""
        pipeline = _make_pipeline(issue_number=42)

        with (
            patch("egg_contracts.loader.create_contract") as mock_create,
            patch("routes.pipelines._populate_contract_from_plan") as mock_populate,
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
        mock_populate.assert_called_once_with(
            tmp_path,
            pipeline.id,
            pipeline.mode or "local",
            42,
        )
        mock_commit.assert_called_once_with(
            tmp_path,
            "Restore missing contract for 42",
            pipeline_identifier=42,
        )

    def test_recreates_contract_when_missing_pipeline_id(self, tmp_path: Path):
        """When no issue_number, uses pipeline_id to create contract."""
        pipeline = _make_pipeline(issue_number=None, pipeline_id="pipe-abc")

        with (
            patch("egg_contracts.loader.create_contract") as mock_create,
            patch("routes.pipelines._populate_contract_from_plan") as mock_populate,
            patch("routes.pipelines._commit_statefiles_to_worktree") as mock_commit,
        ):
            result = _ensure_statefiles_on_branch(tmp_path, pipeline)

        assert result is True
        mock_create.assert_called_once_with(
            pipeline_id="pipe-abc",
            title="test prompt",
            repo_root=tmp_path,
        )
        mock_populate.assert_called_once_with(
            tmp_path,
            "pipe-abc",
            pipeline.mode or "local",
            None,
        )
        mock_commit.assert_called_once_with(
            tmp_path,
            "Restore missing contract for pipe-abc",
            pipeline_identifier="pipe-abc",
        )

    def test_canonical_path_guard_prevents_recreation(self, tmp_path: Path):
        """When hardcoded path is missing but canonical path exists, skip recreation."""
        pipeline = _make_pipeline()
        # Do NOT create the hardcoded contract file — simulate path drift where
        # get_contract_path resolves to a different location that does exist.
        alt_dir = tmp_path / ".egg-state" / "alt-contracts"
        alt_dir.mkdir(parents=True)
        alt_contract = alt_dir / "42.json"
        alt_contract.write_text("{}")

        with (
            patch("egg_contracts.loader.get_contract_path", return_value=alt_contract),
            patch("egg_contracts.loader.create_contract") as mock_create,
        ):
            result = _ensure_statefiles_on_branch(tmp_path, pipeline)

        assert result is True
        mock_create.assert_not_called()

    def test_returns_false_when_restoration_fails(self, tmp_path: Path):
        """When contract re-creation raises an exception, return False."""
        pipeline = _make_pipeline()

        with patch(
            "egg_contracts.loader.create_contract",
            side_effect=RuntimeError("disk full"),
        ):
            result = _ensure_statefiles_on_branch(tmp_path, pipeline)

        assert result is False
