"""Tests for ``_refresh_pipeline_branch_against_current_base`` (#2224 PR 2).

Closes the gap where ``base_branch`` advances *during* the PR phase:
before this helper, ``_auto_create_pr`` opened the PR against
whatever tip ``origin/<pipeline_branch>`` had — which could be N
commits behind current ``origin/<base_branch>``.  The helper is
best-effort: any failure falls back to opening the PR against the
un-rebased tip.  ``base_branch`` is never written to.
"""

from __future__ import annotations

import subprocess
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

from gateway_client import PushResult  # noqa: E402
from routes.pipelines import (  # noqa: E402
    _refresh_pipeline_branch_against_current_base,
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


def _call(spawner: MagicMock, **overrides) -> bool:
    kwargs = {
        "spawner": spawner,
        "pipeline_id": "pipe-1",
        "worktree_repo_path": Path("/tmp/repo"),
        "pipeline_branch": "egg/issue-42",
        "base_branch": "main",
        "gateway_mode": "public",
    }
    kwargs.update(overrides)
    return _refresh_pipeline_branch_against_current_base(**kwargs)


class TestRefreshPipelineBranchAgainstCurrentBase:
    """Coverage for the success and failure paths."""

    def test_skips_when_pipeline_branch_empty(self):
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            assert _call(spawner, pipeline_branch="") is False
            spawner.gateway.fetch_worktree_branch.assert_not_called()
            mock_run.assert_not_called()

    def test_skips_when_base_branch_empty(self):
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            assert _call(spawner, base_branch="") is False
            spawner.gateway.fetch_worktree_branch.assert_not_called()
            mock_run.assert_not_called()

    def test_skips_when_branch_equals_base(self):
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            assert _call(spawner, pipeline_branch="main", base_branch="main") is False
            spawner.gateway.fetch_worktree_branch.assert_not_called()
            mock_run.assert_not_called()

    def test_returns_false_when_fetch_fails(self):
        """Best-effort: fetch failure → no rebase, no exception."""
        spawner = _make_spawner(fetch_ok=False)
        with patch("routes.pipelines.subprocess.run") as mock_run:
            assert _call(spawner) is False
            mock_run.assert_not_called()
            spawner.gateway.push_worktree_branch.assert_not_called()

    def test_returns_false_when_origin_branch_missing(self):
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _result(returncode=128),  # rev-parse origin/<branch> fails
            ]
            assert _call(spawner) is False
            spawner.gateway.push_worktree_branch.assert_not_called()

    def test_returns_false_when_origin_base_missing(self):
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _result(returncode=0),  # rev-parse origin/<branch>
                _result(returncode=128),  # rev-parse origin/<base> fails
            ]
            assert _call(spawner) is False
            spawner.gateway.push_worktree_branch.assert_not_called()

    def test_returns_false_when_branch_already_caught_up(self):
        """0 commits behind — already current, no force-push needed."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _result(returncode=0),  # rev-parse origin/<branch>
                _result(returncode=0),  # rev-parse origin/<base>
                _result(stdout="0\n"),  # rev-list --count == 0
            ]
            assert _call(spawner) is False
            spawner.gateway.push_worktree_branch.assert_not_called()

    def test_returns_false_when_merge_base_resolution_fails(self):
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _result(returncode=0),  # rev-parse origin/<branch>
                _result(returncode=0),  # rev-parse origin/<base>
                _result(stdout="5\n"),  # behind by 5
                _result(returncode=128, stderr="bad"),  # merge-base fails
            ]
            assert _call(spawner) is False
            spawner.gateway.push_worktree_branch.assert_not_called()

    def test_returns_false_when_reset_fails(self):
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _result(returncode=0),  # rev-parse origin/<branch>
                _result(returncode=0),  # rev-parse origin/<base>
                _result(stdout="5\n"),  # behind by 5
                _result(stdout="abcdef0\n"),  # merge-base
                _result(returncode=1, stderr="reset boom"),  # reset --hard
            ]
            assert _call(spawner) is False
            spawner.gateway.push_worktree_branch.assert_not_called()

    def test_returns_false_and_aborts_on_rebase_conflict(self):
        """Rebase conflict → abort + restore + return False; PR still opens."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _result(returncode=0),  # rev-parse origin/<branch>
                _result(returncode=0),  # rev-parse origin/<base>
                _result(stdout="5\n"),  # behind
                _result(stdout="abcdef0\n"),  # merge-base
                _result(returncode=0),  # reset
                _result(returncode=1, stderr="CONFLICT"),  # rebase fails
                _result(returncode=0),  # rebase --abort
                _result(returncode=0),  # restore reset
            ]
            assert _call(spawner) is False
            spawner.gateway.push_worktree_branch.assert_not_called()

    def test_returns_false_and_restores_on_push_failure(self):
        """Push reject after successful rebase → restore + return False.

        The PR can still open against the (un-rebased) remote tip,
        and the caller never sees an exception.
        """
        spawner = _make_spawner(push_ok=False)
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _result(returncode=0),  # rev-parse origin/<branch>
                _result(returncode=0),  # rev-parse origin/<base>
                _result(stdout="5\n"),
                _result(stdout="abcdef0\n"),
                _result(returncode=0),  # reset
                _result(returncode=0, stderr=""),  # rebase ok
                _result(returncode=0),  # restore reset (after push fail)
            ]
            assert _call(spawner) is False
            spawner.gateway.push_worktree_branch.assert_called_once()

    def test_returns_true_on_full_success(self):
        """Happy path: rebase + push succeed, returns True."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _result(returncode=0),  # rev-parse origin/<branch>
                _result(returncode=0),  # rev-parse origin/<base>
                _result(stdout="5\n"),
                _result(stdout="abcdef0\n"),
                _result(returncode=0),  # reset
                _result(returncode=0, stderr=""),  # rebase ok
            ]
            assert _call(spawner) is True
            push_kwargs = spawner.gateway.push_worktree_branch.call_args.kwargs
            assert push_kwargs["force"] is True
            assert push_kwargs["branch"] == "egg/issue-42"
            # Re-fetch after successful push.
            assert spawner.gateway.fetch_worktree_branch.call_count == 2

    def test_uses_safe_onto_form(self):
        """The rebase call uses ``--onto <new_base> <upstream>`` (2-arg form).

        HEAD is the implicit branch being rebased — the step-5 reset to
        ``origin/<pipeline_branch>`` puts it there.  Verifying argv
        shape so a refactor that drops ``--onto`` (reintroducing the
        bare-form contamination shape from #2222) is caught at
        unit-test time.
        """
        spawner = _make_spawner()
        merge_base_sha = "abcdef0123456789"
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _result(returncode=0),
                _result(returncode=0),
                _result(stdout="5\n"),
                _result(stdout=f"{merge_base_sha}\n"),
                _result(returncode=0),
                _result(returncode=0, stderr=""),
            ]
            _call(spawner)

            # Find the rebase invocation.
            rebase_calls = [
                call.args[0]
                for call in mock_run.call_args_list
                if any("rebase" == arg for arg in call.args[0])
            ]
            assert rebase_calls, "expected at least one rebase invocation"
            assert len(rebase_calls) == 1, (
                f"expected exactly one rebase invocation, got {len(rebase_calls)}"
            )
            rebase_argv = rebase_calls[0]
            # Strip leading ``git -c ... -C ... rebase``.
            assert "rebase" in rebase_argv
            rebase_idx = rebase_argv.index("rebase")
            tail = rebase_argv[rebase_idx + 1 :]
            assert tail[0] == "--onto"
            assert tail[1] == "origin/main"
            assert tail[2] == merge_base_sha
            # No fourth positional — the worktree HEAD is the branch
            # being rebased (we reset to origin/<branch> in step 5).
            assert len(tail) == 3
