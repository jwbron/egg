"""Tests for _commit_statefiles_to_worktree pipeline scoping (#1390).

Verifies that when a pipeline_identifier is provided, only state files
belonging to that pipeline are staged and committed — preventing
concurrent pipelines from leaking state into each other's PRs.
"""

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

from routes.pipelines import _commit_statefiles_to_worktree


def _make_run_side_effect(*, diff_has_changes: bool = True):
    """Create a side_effect for subprocess.run that simulates git behavior.

    When *diff_has_changes* is True, the ``git diff --cached --quiet``
    call returns non-zero (meaning there are staged changes to commit).
    """

    def _side_effect(cmd, **kwargs):
        result = MagicMock()
        # git diff --cached --quiet returns 0 when nothing staged, 1 when staged
        if "--quiet" in cmd:
            result.returncode = 1 if diff_has_changes else 0
        else:
            result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    return _side_effect


class TestCommitStatefilesScoping:
    """Scoped staging: only files matching pipeline_identifier are committed."""

    def test_scoped_stages_only_matching_files(self, tmp_path: Path):
        """With pipeline_identifier=42, only files containing '42' are staged."""
        # Create state files for pipeline 42 and pipeline 99
        for subdir in ("contracts", "drafts", "reviews", "agent-outputs"):
            d = tmp_path / ".egg-state" / subdir
            d.mkdir(parents=True, exist_ok=True)

        (tmp_path / ".egg-state" / "contracts" / "42.json").write_text("{}")
        (tmp_path / ".egg-state" / "drafts" / "42-plan.md").write_text("plan")
        (tmp_path / ".egg-state" / "reviews" / "42-implement-code-review.json").write_text("{}")
        (tmp_path / ".egg-state" / "contracts" / "99.json").write_text("{}")
        (tmp_path / ".egg-state" / "drafts" / "99-plan.md").write_text("other plan")

        with patch("subprocess.run", side_effect=_make_run_side_effect()) as mock_run:
            _commit_statefiles_to_worktree(tmp_path, "scoped commit", pipeline_identifier=42)

        # Find the git add call
        add_call = None
        for c in mock_run.call_args_list:
            cmd = c[0][0]
            if "add" in cmd and "--" in cmd:
                add_call = cmd
                break

        assert add_call is not None, "Expected a git add call"

        # The add call should contain paths with '42' but NOT '99'
        add_paths = add_call[add_call.index("--") + 1 :]
        add_paths_str = " ".join(add_paths)
        assert "42" in add_paths_str
        assert "99" not in add_paths_str

    def test_scoped_no_matching_files_is_noop(self, tmp_path: Path):
        """When no files match the identifier, nothing is committed."""
        d = tmp_path / ".egg-state" / "contracts"
        d.mkdir(parents=True)
        (d / "99.json").write_text("{}")

        with patch("subprocess.run") as mock_run:
            _commit_statefiles_to_worktree(tmp_path, "noop commit", pipeline_identifier=42)

        # subprocess.run should never be called (no matching files)
        mock_run.assert_not_called()

    def test_none_identifier_stages_everything(self, tmp_path: Path):
        """With pipeline_identifier=None, all .egg-state/ files are staged (fallback)."""
        d = tmp_path / ".egg-state" / "contracts"
        d.mkdir(parents=True)
        (d / "42.json").write_text("{}")
        (d / "99.json").write_text("{}")

        with patch("subprocess.run", side_effect=_make_run_side_effect()) as mock_run:
            _commit_statefiles_to_worktree(tmp_path, "unscoped commit", pipeline_identifier=None)

        # The git add call should use the broad ".egg-state/" path
        add_call = mock_run.call_args_list[0]
        cmd = add_call[0][0]
        assert ".egg-state/" in cmd

    def test_idempotent_when_nothing_staged(self, tmp_path: Path):
        """When diff --cached --quiet returns 0, no commit is created."""
        d = tmp_path / ".egg-state" / "contracts"
        d.mkdir(parents=True)
        (d / "42.json").write_text("{}")

        with patch(
            "subprocess.run", side_effect=_make_run_side_effect(diff_has_changes=False)
        ) as mock_run:
            _commit_statefiles_to_worktree(tmp_path, "idempotent", pipeline_identifier=42)

        # Should have add + diff calls, but NOT a commit call
        commit_calls = [c for c in mock_run.call_args_list if "commit" in c[0][0]]
        assert len(commit_calls) == 0

    def test_string_pipeline_identifier(self, tmp_path: Path):
        """Works with string pipeline IDs (no issue number)."""
        d = tmp_path / ".egg-state" / "contracts"
        d.mkdir(parents=True)
        (d / "pipe-abc.json").write_text("{}")
        (d / "pipe-xyz.json").write_text("{}")

        with patch("subprocess.run", side_effect=_make_run_side_effect()) as mock_run:
            _commit_statefiles_to_worktree(
                tmp_path, "scoped string", pipeline_identifier="pipe-abc"
            )

        add_call = None
        for c in mock_run.call_args_list:
            cmd = c[0][0]
            if "add" in cmd and "--" in cmd:
                add_call = cmd
                break

        assert add_call is not None
        add_paths = add_call[add_call.index("--") + 1 :]
        add_paths_str = " ".join(add_paths)
        assert "pipe-abc" in add_paths_str
        assert "pipe-xyz" not in add_paths_str

    def test_no_state_dir_is_noop(self, tmp_path: Path):
        """When .egg-state/ doesn't exist, nothing happens."""
        with patch("subprocess.run") as mock_run:
            _commit_statefiles_to_worktree(tmp_path, "no dir", pipeline_identifier=42)

        mock_run.assert_not_called()
