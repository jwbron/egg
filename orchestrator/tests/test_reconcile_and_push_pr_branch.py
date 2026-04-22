"""
Tests for the reconcile-on-failure path inside ``push_worktree_branch``.

Reconcile was originally a wrapper (``_reconcile_and_push_pr_branch``)
around the gateway client push; it was folded into
``GatewayClient.push_worktree_branch`` itself in #1808 so every push
call site gets the same fetch+rebase+retry behavior without a wrapper.

Cases covered (originally added for #1706, rewritten for #1731 to
prefer rebase over merge and to auto-resolve conflicts under
``.egg-state/agent-outputs/`` by taking the remote side):

- First push attempt succeeds → return True, no fetch/rebase attempted.
- First push fails → fetch+rebase+retry path engaged.
- Fetch failure → hard fail, no rebase attempted, return False.
- Rebase conflict in a non-ephemeral path → rebase --abort, return False.
- Rebase conflict only under .egg-state/agent-outputs/ → auto-resolve and continue.
- Rebase timeout → rebase --abort, return False.
- Rebase succeeds but retry push still fails → return False.
- ``ref`` set (state-sync style, #1808): no reconcile — rebase is only
  meaningful when ``repo_path`` is a worktree checked out to the branch.
"""

import subprocess
from unittest.mock import MagicMock, patch

from gateway_client import GatewayClient, PushResult


def _run_result(returncode=0, stdout="", stderr=""):
    """Build a CompletedProcess stand-in for subprocess.run mocks."""
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def _make_client(push_results):
    """Return a GatewayClient whose ``_do_push`` yields ``push_results`` in order.

    Each element of ``push_results`` is a bool expressing success/failure;
    bools are wrapped into ``PushResult`` so the test doesn't need to
    care about the failure category (the reconcile helpers branch only
    on ``.ok``).
    """
    client = GatewayClient(
        gateway_host="test-gateway",
        gateway_port=9848,  # noqa: EGG002
        launcher_secret="test-secret",
    )
    results = [
        r if isinstance(r, PushResult) else PushResult(ok=bool(r), category=None if r else "test")
        for r in push_results
    ]
    client._do_push = MagicMock(side_effect=results)
    return client


class TestPushWorktreeBranchReconcile:
    def test_first_push_success_skips_fetch_and_rebase(self, tmp_path):
        """When the initial push succeeds, no git subprocess calls happen."""
        client = _make_client([True])
        with patch("gateway_client.subprocess.run") as mock_run:
            ok = client.push_worktree_branch(
                pipeline_id="issue-42",
                repo_path=str(tmp_path),
                branch="egg/feature",
            )

        assert ok.ok is True
        assert client._do_push.call_count == 1
        mock_run.assert_not_called()

    def test_push_fail_then_fetch_rebase_retry_succeeds(self, tmp_path):
        """On initial failure: fetch, rebase, retry; all succeed → True."""
        client = _make_client([False, True])
        with patch(
            "gateway_client.subprocess.run",
            side_effect=[_run_result(), _run_result()],
        ) as mock_run:
            ok = client.push_worktree_branch(
                pipeline_id="issue-42",
                repo_path=str(tmp_path),
                branch="egg/feature",
            )

        assert ok.ok is True
        assert client._do_push.call_count == 2
        assert mock_run.call_count == 2
        fetch_cmd = mock_run.call_args_list[0].args[0]
        rebase_cmd = mock_run.call_args_list[1].args[0]
        assert "fetch" in fetch_cmd and "origin" in fetch_cmd and "egg/feature" in fetch_cmd
        assert "rebase" in rebase_cmd and "origin/egg/feature" in rebase_cmd

    def test_push_fail_then_fetch_fails_gives_up(self, tmp_path):
        """If fetch itself fails, rebase is not attempted and result is False."""
        client = _make_client([False])

        def _run_side_effect(cmd, *args, **kwargs):
            if "fetch" in cmd:
                raise subprocess.CalledProcessError(
                    returncode=128, cmd=cmd, stderr="fatal: remote hung up"
                )
            raise AssertionError(f"Unexpected subprocess invocation: {cmd}")

        with patch("gateway_client.subprocess.run", side_effect=_run_side_effect):
            ok = client.push_worktree_branch(
                pipeline_id="issue-42",
                repo_path=str(tmp_path),
                branch="egg/feature",
            )

        assert ok.ok is False
        assert client._do_push.call_count == 1

    def test_rebase_conflict_outside_agent_outputs_aborts(self, tmp_path):
        """Conflict in a non-ephemeral path triggers ``git rebase --abort`` and returns False."""
        client = _make_client([False])
        with patch(
            "gateway_client.subprocess.run",
            side_effect=[
                _run_result(),  # fetch
                _run_result(returncode=1, stdout="CONFLICT"),  # rebase (conflict)
                _run_result(stdout="src/app.py\n"),  # diff --name-only --diff-filter=U
                _run_result(),  # rebase --abort
            ],
        ) as mock_run:
            ok = client.push_worktree_branch(
                pipeline_id="issue-42",
                repo_path=str(tmp_path),
                branch="egg/feature",
            )

        assert ok.ok is False
        assert client._do_push.call_count == 1
        abort_cmd = mock_run.call_args_list[-1].args[0]
        assert "rebase" in abort_cmd and "--abort" in abort_cmd

    def test_rebase_conflict_only_in_agent_outputs_auto_resolves(self, tmp_path):
        """Conflicts confined to .egg-state/agent-outputs/ auto-resolve to remote side."""
        client = _make_client([False, True])
        with patch(
            "gateway_client.subprocess.run",
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
            ok = client.push_worktree_branch(
                pipeline_id="issue-42",
                repo_path=str(tmp_path),
                branch="egg/feature",
            )

        assert ok.ok is True
        assert client._do_push.call_count == 2
        all_cmds = [c.args[0] for c in mock_run.call_args_list]
        assert any("checkout" in c and "--theirs" in c for c in all_cmds)
        assert any("rebase" in c and "--continue" in c for c in all_cmds)

    def test_rebase_auto_resolve_uses_skip_when_index_empty(self, tmp_path):
        """When ``--theirs`` deletes the only conflicting file and leaves the index
        empty, ``git rebase --skip`` is used instead of ``--continue`` to avoid
        the 'No changes - did you forget to use git add?' error.
        """
        client = _make_client([False, True])
        with patch(
            "gateway_client.subprocess.run",
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
            ok = client.push_worktree_branch(
                pipeline_id="issue-42",
                repo_path=str(tmp_path),
                branch="egg/feature",
            )

        assert ok.ok is True
        all_cmds = [c.args[0] for c in mock_run.call_args_list]
        assert any("rebase" in c and "--skip" in c for c in all_cmds)
        assert not any("rebase" in c and "--continue" in c for c in all_cmds)

    def test_rebase_mixed_conflict_aborts(self, tmp_path):
        """A conflict list that includes any non-agent-outputs path aborts the rebase."""
        client = _make_client([False])
        with patch(
            "gateway_client.subprocess.run",
            side_effect=[
                _run_result(),  # fetch
                _run_result(returncode=1, stdout="CONFLICT"),  # rebase
                _run_result(
                    stdout=(".egg-state/agent-outputs/x.patch\norchestrator/routes/pipelines.py\n")
                ),  # mixed conflicts
                _run_result(),  # rebase --abort
            ],
        ) as mock_run:
            ok = client.push_worktree_branch(
                pipeline_id="issue-42",
                repo_path=str(tmp_path),
                branch="egg/feature",
            )

        assert ok.ok is False
        all_cmds = [c.args[0] for c in mock_run.call_args_list]
        assert not any("checkout" in c and "--theirs" in c for c in all_cmds)
        abort_cmd = mock_run.call_args_list[-1].args[0]
        assert "rebase" in abort_cmd and "--abort" in abort_cmd

    def test_rebase_timeout_aborts(self, tmp_path):
        """Rebase TimeoutExpired triggers ``git rebase --abort`` and returns False."""
        client = _make_client([False])

        call_count = 0

        def _run_side_effect(cmd, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _run_result()  # fetch ok
            if call_count == 2:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=120)  # rebase times out
            return _run_result()  # rebase --abort

        with patch("gateway_client.subprocess.run", side_effect=_run_side_effect) as mock_run:
            ok = client.push_worktree_branch(
                pipeline_id="issue-42",
                repo_path=str(tmp_path),
                branch="egg/feature",
            )

        assert ok.ok is False
        assert client._do_push.call_count == 1
        abort_cmd = mock_run.call_args_list[-1].args[0]
        assert "rebase" in abort_cmd and "--abort" in abort_cmd

    def test_rebase_succeeds_retry_fails(self, tmp_path):
        """Successful reconcile but still-failing retry push returns False."""
        client = _make_client([False, False])
        with patch(
            "gateway_client.subprocess.run",
            side_effect=[_run_result(), _run_result()],  # fetch, rebase
        ):
            ok = client.push_worktree_branch(
                pipeline_id="issue-42",
                repo_path=str(tmp_path),
                branch="egg/feature",
            )

        assert ok.ok is False
        assert client._do_push.call_count == 2

    def test_ref_push_skips_reconcile(self, tmp_path):
        """When ``ref`` is set (state-sync style), reconcile is skipped.

        The rebase would mutate the checkout at repo_path, which for a
        ``ref``-based push is not a dedicated pipeline worktree but a
        shared repo whose checkout we must not disturb (see #1808).
        """
        client = _make_client([False])
        with patch("gateway_client.subprocess.run") as mock_run:
            ok = client.push_worktree_branch(
                pipeline_id="state-sync",
                repo_path=str(tmp_path),
                branch="egg/pipeline-state",
                ref="egg/pipeline-state",
            )

        assert ok.ok is False
        assert client._do_push.call_count == 1
        mock_run.assert_not_called()
