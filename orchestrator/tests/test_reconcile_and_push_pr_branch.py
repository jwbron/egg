"""
Tests for ``_reconcile_and_push_pr_branch``.

Covers the PR-phase push reconciliation path (originally added for #1706,
rewritten for #1731 to prefer rebase over merge and to auto-resolve
conflicts under ``.egg-state/agent-outputs/`` by taking the remote side):

- First push attempt succeeds → return True, no fetch/rebase attempted.
- First push fails → fetch+rebase+retry path engaged.
- Fetch failure → hard fail, no rebase attempted, return False.
- Rebase conflict in a non-ephemeral path → rebase --abort, return False.
- Rebase conflict only under .egg-state/agent-outputs/ → auto-resolve and continue.
- Rebase timeout → rebase --abort, return False.
- Rebase succeeds but retry push still fails → return False.
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
    def test_first_push_success_skips_fetch_and_rebase(self, tmp_path):
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

    def test_push_fail_then_fetch_rebase_retry_succeeds(self, tmp_path):
        """On initial failure: fetch, rebase, retry; all succeed → True."""
        from routes.pipelines import _reconcile_and_push_pr_branch

        spawner = _make_spawner([False, True])
        # Two subprocess.run calls expected: fetch, rebase (clean — rc=0).
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
        fetch_cmd = mock_run.call_args_list[0].args[0]
        rebase_cmd = mock_run.call_args_list[1].args[0]
        assert "fetch" in fetch_cmd and "origin" in fetch_cmd and "egg/feature" in fetch_cmd
        assert "rebase" in rebase_cmd and "origin/egg/feature" in rebase_cmd

    def test_push_fail_then_fetch_fails_gives_up(self, tmp_path):
        """If fetch itself fails, rebase is not attempted and result is False."""
        from routes.pipelines import _reconcile_and_push_pr_branch

        spawner = _make_spawner([False])

        def _run_side_effect(cmd, *args, **kwargs):
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
        assert spawner.gateway.push_worktree_branch.call_count == 1

    def test_rebase_conflict_outside_agent_outputs_aborts(self, tmp_path):
        """Conflict in a non-ephemeral path triggers ``git rebase --abort`` and returns False."""
        from routes.pipelines import _reconcile_and_push_pr_branch

        spawner = _make_spawner([False])
        # fetch ok, rebase returns non-zero, diff --name-only returns a code path,
        # rebase --abort ok
        with patch(
            "routes.pipelines.subprocess.run",
            side_effect=[
                _run_result(),  # fetch
                _run_result(returncode=1, stdout="CONFLICT"),  # rebase (conflict)
                _run_result(stdout="src/app.py\n"),  # diff --name-only --diff-filter=U
                _run_result(),  # rebase --abort
            ],
        ) as mock_run:
            ok = _reconcile_and_push_pr_branch(
                spawner, tmp_path, "issue-42", "egg/feature", "public"
            )

        assert ok is False
        # No retry push after a failed rebase
        assert spawner.gateway.push_worktree_branch.call_count == 1
        # Last subprocess call must be `git rebase --abort`
        abort_cmd = mock_run.call_args_list[-1].args[0]
        assert "rebase" in abort_cmd and "--abort" in abort_cmd

    def test_rebase_conflict_only_in_agent_outputs_auto_resolves(self, tmp_path):
        """Conflicts confined to .egg-state/agent-outputs/ auto-resolve to remote side."""
        from routes.pipelines import _reconcile_and_push_pr_branch

        spawner = _make_spawner([False, True])  # first push fails, retry push ok
        # Sequence:
        #   fetch ok → rebase (conflict) → diff --name-only (agent-outputs only)
        #   → checkout --theirs → add → diff --cached --quiet (index has changes: rc=1)
        #   → rebase --continue (clean — rc=0) → retry push ok
        with patch(
            "routes.pipelines.subprocess.run",
            side_effect=[
                _run_result(),  # fetch
                _run_result(returncode=1, stdout="CONFLICT"),  # rebase
                _run_result(
                    stdout=".egg-state/agent-outputs/coder-test-changes.patch\n"
                ),  # unmerged paths
                _run_result(),  # checkout --theirs
                _run_result(),  # add
                _run_result(returncode=1),  # diff --cached --quiet → has staged changes
                _run_result(),  # rebase --continue (success)
            ],
        ) as mock_run:
            ok = _reconcile_and_push_pr_branch(
                spawner, tmp_path, "issue-42", "egg/feature", "public"
            )

        assert ok is True
        assert spawner.gateway.push_worktree_branch.call_count == 2
        # Assert we called checkout --theirs and rebase --continue
        all_cmds = [c.args[0] for c in mock_run.call_args_list]
        assert any("checkout" in c and "--theirs" in c for c in all_cmds)
        assert any("rebase" in c and "--continue" in c for c in all_cmds)

    def test_rebase_auto_resolve_uses_skip_when_index_empty(self, tmp_path):
        """When ``--theirs`` deletes the only conflicting file and leaves the index
        empty, ``git rebase --skip`` is used instead of ``--continue`` to avoid
        the 'No changes - did you forget to use git add?' error.
        """
        from routes.pipelines import _reconcile_and_push_pr_branch

        spawner = _make_spawner([False, True])
        with patch(
            "routes.pipelines.subprocess.run",
            side_effect=[
                _run_result(),  # fetch
                _run_result(returncode=1, stdout="CONFLICT"),  # rebase
                _run_result(stdout=".egg-state/agent-outputs/x.patch\n"),  # unmerged
                _run_result(),  # checkout --theirs
                _run_result(),  # add
                _run_result(returncode=0),  # diff --cached --quiet → empty index
                _run_result(),  # rebase --skip (success)
            ],
        ) as mock_run:
            ok = _reconcile_and_push_pr_branch(
                spawner, tmp_path, "issue-42", "egg/feature", "public"
            )

        assert ok is True
        # Assert we chose --skip over --continue
        all_cmds = [c.args[0] for c in mock_run.call_args_list]
        assert any("rebase" in c and "--skip" in c for c in all_cmds)
        assert not any("rebase" in c and "--continue" in c for c in all_cmds)

    def test_rebase_mixed_conflict_aborts(self, tmp_path):
        """A conflict list that includes any non-agent-outputs path aborts the rebase."""
        from routes.pipelines import _reconcile_and_push_pr_branch

        spawner = _make_spawner([False])
        with patch(
            "routes.pipelines.subprocess.run",
            side_effect=[
                _run_result(),  # fetch
                _run_result(returncode=1, stdout="CONFLICT"),  # rebase
                _run_result(
                    stdout=(".egg-state/agent-outputs/x.patch\norchestrator/routes/pipelines.py\n")
                ),  # mixed conflicts
                _run_result(),  # rebase --abort
            ],
        ) as mock_run:
            ok = _reconcile_and_push_pr_branch(
                spawner, tmp_path, "issue-42", "egg/feature", "public"
            )

        assert ok is False
        # We should NOT have invoked checkout --theirs — that path is reserved
        # for the agent-outputs-only case.
        all_cmds = [c.args[0] for c in mock_run.call_args_list]
        assert not any("checkout" in c and "--theirs" in c for c in all_cmds)
        abort_cmd = mock_run.call_args_list[-1].args[0]
        assert "rebase" in abort_cmd and "--abort" in abort_cmd

    def test_rebase_timeout_aborts(self, tmp_path):
        """Rebase TimeoutExpired triggers ``git rebase --abort`` and returns False."""
        from routes.pipelines import _reconcile_and_push_pr_branch

        spawner = _make_spawner([False])

        call_count = 0

        def _run_side_effect(cmd, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _run_result()  # fetch ok
            if call_count == 2:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=120)  # rebase times out
            return _run_result()  # rebase --abort

        with patch("routes.pipelines.subprocess.run", side_effect=_run_side_effect) as mock_run:
            ok = _reconcile_and_push_pr_branch(
                spawner, tmp_path, "issue-42", "egg/feature", "public"
            )

        assert ok is False
        assert spawner.gateway.push_worktree_branch.call_count == 1
        abort_cmd = mock_run.call_args_list[-1].args[0]
        assert "rebase" in abort_cmd and "--abort" in abort_cmd

    def test_rebase_succeeds_retry_fails(self, tmp_path):
        """Successful reconcile but still-failing retry push returns False."""
        from routes.pipelines import _reconcile_and_push_pr_branch

        spawner = _make_spawner([False, False])
        with patch(
            "routes.pipelines.subprocess.run",
            side_effect=[_run_result(), _run_result()],  # fetch, rebase
        ):
            ok = _reconcile_and_push_pr_branch(
                spawner, tmp_path, "issue-42", "egg/feature", "public"
            )

        assert ok is False
        assert spawner.gateway.push_worktree_branch.call_count == 2

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
