"""
Tests for ``_reconcile_and_push_pr_branch``.

Covers the PR-phase push reconciliation path added for #1706:

- First push attempt succeeds → return True, no fetch/merge attempted.
- First push fails → fetch+merge+retry path engaged.
- Fetch failure → hard fail, no merge attempted, return False.
- Merge conflict → merge --abort called, return False.
- Merge succeeds but retry push still fails → return False.
- Successful reconcile path returns True only when retry push succeeds.
"""

import subprocess
import sys
from unittest.mock import MagicMock, patch

# Mock heavy dependencies that pipelines.py imports at module level
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)


def _make_spawner(push_results):
    """Return a spawner whose ``gateway.push_worktree_branch`` yields ``push_results`` in order."""
    spawner = MagicMock()
    spawner.gateway.push_worktree_branch.side_effect = list(push_results)
    return spawner


def _run_result(returncode=0, stdout="", stderr=""):
    """Build a CompletedProcess stand-in for subprocess.run mocks."""
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


class TestReconcileAndPushPrBranch:
    def test_first_push_success_skips_fetch_and_merge(self, tmp_path):
        """When the initial push succeeds, no git subprocess calls happen."""
        from routes.pipelines import _reconcile_and_push_pr_branch

        spawner = _make_spawner([True])
        with patch("routes.pipelines.subprocess.run") as mock_run:
            ok = _reconcile_and_push_pr_branch(
                spawner, tmp_path, "issue-42", "egg/feature", "public"
            )

        assert ok is True
        assert spawner.gateway.push_worktree_branch.call_count == 1
        mock_run.assert_not_called()

    def test_push_fail_then_fetch_merge_retry_succeeds(self, tmp_path):
        """On initial failure: fetch, merge, retry; all succeed → True."""
        from routes.pipelines import _reconcile_and_push_pr_branch

        spawner = _make_spawner([False, True])
        # Two subprocess.run calls expected: fetch, merge.
        with patch(
            "routes.pipelines.subprocess.run",
            side_effect=[_run_result(), _run_result()],
        ) as mock_run:
            ok = _reconcile_and_push_pr_branch(
                spawner, tmp_path, "issue-42", "egg/feature", "public"
            )

        assert ok is True
        assert spawner.gateway.push_worktree_branch.call_count == 2
        assert mock_run.call_count == 2
        # Validate the commands issued
        fetch_cmd = mock_run.call_args_list[0].args[0]
        merge_cmd = mock_run.call_args_list[1].args[0]
        assert "fetch" in fetch_cmd and "origin" in fetch_cmd and "egg/feature" in fetch_cmd
        assert "merge" in merge_cmd and "origin/egg/feature" in merge_cmd
        assert "--no-edit" in merge_cmd

    def test_push_fail_then_fetch_fails_gives_up(self, tmp_path):
        """If fetch itself fails, merge is not attempted and result is False."""
        from routes.pipelines import _reconcile_and_push_pr_branch

        spawner = _make_spawner([False])

        def _run_side_effect(cmd, *args, **kwargs):
            # Fetch is the first git call after a failed push.
            if "fetch" in cmd:
                raise subprocess.CalledProcessError(
                    returncode=128, cmd=cmd, stderr="fatal: remote hung up"
                )
            raise AssertionError(f"Unexpected subprocess invocation: {cmd}")

        with patch("routes.pipelines.subprocess.run", side_effect=_run_side_effect):
            ok = _reconcile_and_push_pr_branch(
                spawner, tmp_path, "issue-42", "egg/feature", "public"
            )

        assert ok is False
        # No retry push should have been attempted since fetch failed.
        assert spawner.gateway.push_worktree_branch.call_count == 1

    def test_push_fail_then_merge_conflict_aborts(self, tmp_path):
        """Non-zero merge exit triggers ``git merge --abort`` and returns False."""
        from routes.pipelines import _reconcile_and_push_pr_branch

        spawner = _make_spawner([False])
        # fetch ok, merge returns non-zero, abort ok
        with patch(
            "routes.pipelines.subprocess.run",
            side_effect=[
                _run_result(),  # fetch
                _run_result(returncode=1, stdout="CONFLICT"),  # merge
                _run_result(),  # merge --abort
            ],
        ) as mock_run:
            ok = _reconcile_and_push_pr_branch(
                spawner, tmp_path, "issue-42", "egg/feature", "public"
            )

        assert ok is False
        # No retry push after a failed merge
        assert spawner.gateway.push_worktree_branch.call_count == 1
        # The third subprocess call must be `git merge --abort`
        abort_cmd = mock_run.call_args_list[2].args[0]
        assert "merge" in abort_cmd and "--abort" in abort_cmd

    def test_push_fail_merge_succeeds_retry_fails(self, tmp_path):
        """Successful reconcile but still-failing retry push returns False."""
        from routes.pipelines import _reconcile_and_push_pr_branch

        spawner = _make_spawner([False, False])
        with patch(
            "routes.pipelines.subprocess.run",
            side_effect=[_run_result(), _run_result()],
        ):
            ok = _reconcile_and_push_pr_branch(
                spawner, tmp_path, "issue-42", "egg/feature", "public"
            )

        assert ok is False
        assert spawner.gateway.push_worktree_branch.call_count == 2

    def test_push_fail_then_merge_timeout_aborts(self, tmp_path):
        """Merge TimeoutExpired triggers ``git merge --abort`` and returns False."""
        from routes.pipelines import _reconcile_and_push_pr_branch

        spawner = _make_spawner([False])

        call_count = 0

        def _run_side_effect(cmd, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # fetch succeeds
                return _run_result()
            if call_count == 2:
                # merge times out
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)
            # merge --abort succeeds
            return _run_result()

        with patch(
            "routes.pipelines.subprocess.run", side_effect=_run_side_effect
        ) as mock_run:
            ok = _reconcile_and_push_pr_branch(
                spawner, tmp_path, "issue-42", "egg/feature", "public"
            )

        assert ok is False
        assert spawner.gateway.push_worktree_branch.call_count == 1
        # The third subprocess call must be `git merge --abort`
        abort_cmd = mock_run.call_args_list[2].args[0]
        assert "merge" in abort_cmd and "--abort" in abort_cmd

    def test_worktree_path_passed_to_gateway_push(self, tmp_path):
        """The worktree path is threaded through to push_worktree_branch."""
        from routes.pipelines import _reconcile_and_push_pr_branch

        worktree = tmp_path / "my-worktree"
        spawner = _make_spawner([True])
        _reconcile_and_push_pr_branch(spawner, worktree, "issue-42", "egg/feature", "private")

        call = spawner.gateway.push_worktree_branch.call_args
        assert call.kwargs["repo_path"] == str(worktree)
        assert call.kwargs["pipeline_id"] == "issue-42"
        assert call.kwargs["branch"] == "egg/feature"
        assert call.kwargs["mode"] == "private"
