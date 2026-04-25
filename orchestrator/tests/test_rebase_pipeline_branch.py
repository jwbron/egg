"""Tests for _rebase_pipeline_branch_onto_base (#2098)."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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

from gateway_client import PushResult
from routes.pipelines import (
    StalePipelineBranchError,
    _rebase_pipeline_branch_onto_base,
)

_PUSH_OK = PushResult(ok=True, category="", detail="")
_PUSH_FAIL = PushResult(ok=False, category="non_fast_forward", detail="rejected")


def _make_spawner(fetch_ok: bool = True, push_ok: bool = True) -> MagicMock:
    spawner = MagicMock()
    spawner.gateway.fetch_worktree_branch.return_value = fetch_ok
    spawner.gateway.push_worktree_branch.return_value = _PUSH_OK if push_ok else _PUSH_FAIL
    return spawner


def _result(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _call(spawner: MagicMock, **overrides) -> None:
    kwargs = {
        "spawner": spawner,
        "pipeline_id": "pipe-1",
        "worktree_repo_path": Path("/tmp/repo"),
        "pipeline_branch": "egg/issue-42",
        "base_branch": "main",
        "gateway_mode": "public",
    }
    kwargs.update(overrides)
    _rebase_pipeline_branch_onto_base(**kwargs)


class TestRebasePipelineBranchOntoBase:
    def test_skips_when_pipeline_branch_empty(self):
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            _call(spawner, pipeline_branch="")
            spawner.gateway.fetch_worktree_branch.assert_not_called()
            mock_run.assert_not_called()

    def test_skips_when_base_branch_empty(self):
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            _call(spawner, base_branch="")
            spawner.gateway.fetch_worktree_branch.assert_not_called()
            mock_run.assert_not_called()

    def test_skips_when_branch_equals_base(self):
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            _call(spawner, pipeline_branch="main", base_branch="main")
            spawner.gateway.fetch_worktree_branch.assert_not_called()
            mock_run.assert_not_called()

    def test_skips_when_fetch_fails(self):
        spawner = _make_spawner(fetch_ok=False)
        with patch("routes.pipelines.subprocess.run") as mock_run:
            _call(spawner)
            mock_run.assert_not_called()
            spawner.gateway.push_worktree_branch.assert_not_called()

    def test_skips_when_origin_branch_missing(self):
        """Fresh pipeline — origin/<pipeline_branch> doesn't exist yet."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _result(returncode=128),  # rev-parse origin/<branch> fails
            ]
            _call(spawner)
            assert mock_run.call_count == 1
            spawner.gateway.push_worktree_branch.assert_not_called()

    def test_skips_when_origin_base_missing(self):
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _result(returncode=0),  # rev-parse origin/<branch> ok
                _result(returncode=128),  # rev-parse origin/<base> fails
            ]
            _call(spawner)
            assert mock_run.call_count == 2
            spawner.gateway.push_worktree_branch.assert_not_called()

    def test_skips_when_branch_caught_up_with_base(self):
        """Behind count == 0 — already up to date, no rebase needed."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _result(returncode=0),  # rev-parse origin/<branch>
                _result(returncode=0),  # rev-parse origin/<base>
                _result(stdout="0\n"),  # rev-list --count == 0
            ]
            _call(spawner)
            assert mock_run.call_count == 3
            spawner.gateway.push_worktree_branch.assert_not_called()

    def test_skips_when_worktree_has_local_commits(self):
        """Mid-pipeline state — refuse to clobber HEAD ahead of base."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _result(returncode=0),  # rev-parse origin/<branch>
                _result(returncode=0),  # rev-parse origin/<base>
                _result(stdout="70\n"),  # 70 commits behind base
                _result(stdout="3\n"),  # HEAD is 3 ahead of base
            ]
            _call(spawner)
            assert mock_run.call_count == 4
            spawner.gateway.push_worktree_branch.assert_not_called()

    def test_clean_rebase_force_pushes(self):
        """Branch is behind base, rebase succeeds, force-push fires."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _result(returncode=0),  # rev-parse origin/<branch>
                _result(returncode=0),  # rev-parse origin/<base>
                _result(stdout="70\n"),  # 70 commits behind base
                _result(stdout="0\n"),  # HEAD not ahead of base
                _result(returncode=0),  # reset --hard origin/<branch>
                _result(returncode=0),  # rebase succeeds
            ]
            _call(spawner)
            assert mock_run.call_count == 6
            spawner.gateway.push_worktree_branch.assert_called_once()
            push_kwargs = spawner.gateway.push_worktree_branch.call_args.kwargs
            assert push_kwargs["force"] is True
            assert push_kwargs["branch"] == "egg/issue-42"
            # Re-fetch happens after successful push (initial fetch + re-fetch)
            assert spawner.gateway.fetch_worktree_branch.call_count == 2

    def test_rebase_conflict_raises_stale_branch_error(self):
        """Rebase conflicts → abort + reset + raise."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _result(returncode=0),  # rev-parse origin/<branch>
                _result(returncode=0),  # rev-parse origin/<base>
                _result(stdout="70\n"),  # behind by 70
                _result(stdout="0\n"),  # HEAD not ahead
                _result(returncode=0),  # reset --hard origin/<branch>
                _result(returncode=1, stderr="CONFLICT (content): foo.py"),  # rebase fails
                _result(returncode=0),  # rebase --abort
                _result(returncode=0),  # reset --hard origin/<base>
            ]
            with pytest.raises(StalePipelineBranchError) as excinfo:
                _call(spawner)
            assert "70 commits behind" in str(excinfo.value)
            assert "main" in str(excinfo.value)
            spawner.gateway.push_worktree_branch.assert_not_called()
            # Verify abort was called
            abort_call = mock_run.call_args_list[6]
            assert "--abort" in abort_call[0][0]

    def test_force_push_failure_raises_stale_branch_error(self):
        """Rebase succeeds but force-push fails → reset + raise."""
        spawner = _make_spawner(push_ok=False)
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _result(returncode=0),  # rev-parse origin/<branch>
                _result(returncode=0),  # rev-parse origin/<base>
                _result(stdout="70\n"),  # behind
                _result(stdout="0\n"),  # not ahead
                _result(returncode=0),  # reset to branch
                _result(returncode=0),  # rebase succeeds
                _result(returncode=0),  # reset to base after push fails
            ]
            with pytest.raises(StalePipelineBranchError) as excinfo:
                _call(spawner)
            assert "force-push" in str(excinfo.value).lower()
            spawner.gateway.push_worktree_branch.assert_called_once()

    def test_rev_list_failure_skips(self):
        """rev-list --count failure → log + skip (best-effort)."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _result(returncode=0),  # rev-parse origin/<branch>
                _result(returncode=0),  # rev-parse origin/<base>
                _result(returncode=128, stderr="ambiguous ref"),  # rev-list fails
            ]
            _call(spawner)
            assert mock_run.call_count == 3
            spawner.gateway.push_worktree_branch.assert_not_called()

    def test_reset_to_branch_failure_skips(self):
        """If we can't reset to the stale tip, skip rather than risk corruption."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _result(returncode=0),  # rev-parse origin/<branch>
                _result(returncode=0),  # rev-parse origin/<base>
                _result(stdout="70\n"),  # behind
                _result(stdout="0\n"),  # not ahead
                _result(returncode=128, stderr="reset failed"),  # reset fails
            ]
            _call(spawner)
            assert mock_run.call_count == 5
            spawner.gateway.push_worktree_branch.assert_not_called()
