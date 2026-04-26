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

    def test_skips_when_head_not_ancestor_of_branch(self):
        """HEAD has commits not on origin/<branch> — defer to push reconcile.

        This is the only case where ``merge-base --is-ancestor`` returns
        non-zero with the worktree in a sane state: someone (or some other
        path) staged commits on HEAD that haven't been published yet.
        Resetting would discard them, so we skip.
        """
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _result(returncode=0),  # rev-parse origin/<branch>
                _result(returncode=0),  # rev-parse origin/<base>
                _result(stdout="70\n"),  # 70 commits behind base
                _result(returncode=1),  # is-ancestor: HEAD NOT on origin/<branch>
            ]
            _call(spawner)
            assert mock_run.call_count == 4
            spawner.gateway.push_worktree_branch.assert_not_called()

    def test_proceeds_when_head_is_ancestor_of_branch(self):
        """Canonical preserved-worktree resume case (#2098).

        After a cancelled run was preserved with ``preserve_worktrees=True``,
        the orchestrator-side worktree's HEAD carries state-file commits
        that were *already pushed* to ``origin/<branch>`` — i.e. HEAD is
        a strict ancestor of ``origin/<branch>`` (commit-and-pushed, plus
        old-base ancestors that diverged from new main).  The previous
        ``rev-list base..HEAD > 0`` guard wrongly skipped this case; the
        ancestry check must let it proceed so the rebase actually runs.
        """
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _result(returncode=0),  # rev-parse origin/<branch>
                _result(returncode=0),  # rev-parse origin/<base>
                _result(stdout="70\n"),  # 70 commits behind base
                _result(returncode=0),  # is-ancestor: HEAD IS on origin/<branch>
                _result(returncode=0),  # reset --hard origin/<branch>
                _result(returncode=0),  # rebase succeeds
            ]
            _call(spawner)
            assert mock_run.call_count == 6
            spawner.gateway.push_worktree_branch.assert_called_once()
            push_kwargs = spawner.gateway.push_worktree_branch.call_args.kwargs
            assert push_kwargs["force"] is True

    def test_clean_rebase_force_pushes(self):
        """Branch is behind base, rebase succeeds, force-push fires."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _result(returncode=0),  # rev-parse origin/<branch>
                _result(returncode=0),  # rev-parse origin/<base>
                _result(stdout="70\n"),  # 70 commits behind base
                _result(returncode=0),  # is-ancestor: HEAD on origin/<branch>
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

    def test_clean_rebase_logs_skipped_commit_count(self):
        """``warning: skipped previously applied commit`` lines on rebase
        stderr should be counted and surfaced in the success log so
        operators can confirm the helper actually dropped stale commits.
        """
        spawner = _make_spawner()
        rebase_stderr = (
            "warning: skipped previously applied commit aaa111\n"
            "warning: skipped previously applied commit bbb222\n"
            "warning: skipped previously applied commit ccc333\n"
            "Successfully rebased and updated detached HEAD.\n"
        )
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
        ):
            mock_run.side_effect = [
                _result(returncode=0),  # rev-parse origin/<branch>
                _result(returncode=0),  # rev-parse origin/<base>
                _result(stdout="70\n"),  # behind by 70
                _result(returncode=0),  # is-ancestor ok
                _result(returncode=0),  # reset
                _result(returncode=0, stderr=rebase_stderr),  # rebase
            ]
            _call(spawner)
            success_calls = [
                c
                for c in mock_logger.info.call_args_list
                if c.args and "rebased and force-pushed" in c.args[0]
            ]
            assert success_calls, "expected success log line"
            assert success_calls[0].kwargs["skipped_via_rebase"] == 3
            assert success_calls[0].kwargs["dropped_stale_commits"] == 70

    def test_rebase_conflict_raises_stale_branch_error(self):
        """Rebase conflicts → abort + reset + raise."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _result(returncode=0),  # rev-parse origin/<branch>
                _result(returncode=0),  # rev-parse origin/<base>
                _result(stdout="70\n"),  # behind by 70
                _result(returncode=0),  # is-ancestor ok
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

    def test_rebase_timeout_aborts_and_raises(self):
        """``subprocess.TimeoutExpired`` mid-rebase must not propagate;
        abort + reset + raise as a ``StalePipelineBranchError`` instead.

        Without this, the timeout would bubble past the
        ``except StalePipelineBranchError`` handler in ``_run_pipeline``
        and leave the worktree mid-rebase.
        """
        spawner = _make_spawner()

        def _run_side_effect(args, **kwargs):
            cmd = args
            if "rebase" in cmd and "--abort" not in cmd:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=120)
            # rev-parse / rev-list / is-ancestor / reset / abort / reset
            if "rev-list" in cmd:
                return _result(stdout="70\n")
            if "merge-base" in cmd:
                return _result(returncode=0)
            return _result(returncode=0)

        with patch("routes.pipelines.subprocess.run", side_effect=_run_side_effect):
            with pytest.raises(StalePipelineBranchError) as excinfo:
                _call(spawner)
        assert "rebasing it failed" in str(excinfo.value).lower()
        assert "timed out" in str(excinfo.value).lower()
        spawner.gateway.push_worktree_branch.assert_not_called()

    def test_force_push_failure_raises_stale_branch_error(self):
        """Rebase succeeds but force-push fails → reset + raise."""
        spawner = _make_spawner(push_ok=False)
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _result(returncode=0),  # rev-parse origin/<branch>
                _result(returncode=0),  # rev-parse origin/<base>
                _result(stdout="70\n"),  # behind
                _result(returncode=0),  # is-ancestor ok
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

    def test_rev_parse_oserror_skips(self):
        """If git itself fails to spawn (``OSError``), the helper must
        treat that as a best-effort skip rather than crashing the pipeline.
        """
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run", side_effect=OSError("git not found")):
            _call(spawner)
            spawner.gateway.push_worktree_branch.assert_not_called()

    def test_reset_to_branch_failure_skips(self):
        """If we can't reset to the stale tip, skip rather than risk corruption."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _result(returncode=0),  # rev-parse origin/<branch>
                _result(returncode=0),  # rev-parse origin/<base>
                _result(stdout="70\n"),  # behind
                _result(returncode=0),  # is-ancestor ok
                _result(returncode=128, stderr="reset failed"),  # reset fails
            ]
            _call(spawner)
            assert mock_run.call_count == 5
            spawner.gateway.push_worktree_branch.assert_not_called()
