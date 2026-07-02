"""Slice-7 (#3393): ``_materialize_work_branch_on_remote`` behavior (task-7-1).

Executable-seam coverage for the gateway-side best-effort push that
materializes a participating repo's pipeline work branch on its OWN remote
right after the worktree exists. Multi-repo pipelines rely on this so a
secondary repo's context / slice PR opens against a head branch that actually
exists instead of soft-failing on a missing head (the slice-4 known limit).

These pin the behavior the coder's ``push_branch`` threading must preserve:

* the non-forced ``HEAD:refs/heads/<target>`` refspec, pushed to ``origin``;
* target resolution — the assigned branch when set, else the per-worktree
  ``branch_name``, with any ``origin/`` prefix stripped; and
* the idempotent-push contract the contract-verification review flagged as
  uncovered — an already-materialized branch (up-to-date / non-fast-forward
  rejection) AND any push failure (timeout, auth, exception) are both
  swallowed so worktree creation never fails on the best-effort push, and a
  force push (which could clobber the primary repo's contract-init commit) is
  never issued.
"""

import contextlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from worktree_manager import WorktreeManager


@pytest.fixture
def manager(tmp_path):
    """A WorktreeManager whose bases live under tmp_path (no real git needed)."""
    return WorktreeManager(worktree_base=tmp_path / "wt", repos_base=tmp_path / "repos")


@contextlib.contextmanager
def _fake_credential_env(*args, **kwargs):
    """Stand in for ``_git_credential_env`` — yields a dummy push env."""
    yield {"GIT_ASKPASS": "/tmp/askpass"}


def _push_result(returncode=0, stderr=""):
    """A subprocess.run-shaped result for the push call."""
    result = MagicMock()
    result.returncode = returncode
    result.stderr = stderr
    result.stdout = ""
    return result


def _materialize(manager, **overrides):
    """Invoke the seam with sensible defaults, overridable per-test."""
    kwargs = {
        "worktree_path": Path("/wt/egg-x/repo"),
        "branch_name": "egg/egg-x/work",
        "assigned_branch": "egg/pipe-1/work",
        "repo_slug": "owner/repo",
        "container_id": "egg-x",
    }
    kwargs.update(overrides)
    return manager._materialize_work_branch_on_remote(**kwargs)


class TestRefspecConstruction:
    """The push targets a non-forced ``HEAD:refs/heads/<target>`` refspec."""

    def test_pushes_head_to_assigned_branch_non_forced(self, manager):
        """assigned_branch set → HEAD:refs/heads/<assigned>, pushed to origin, no force."""
        with (
            patch.object(manager, "_git_credential_env", _fake_credential_env),
            patch("subprocess.run", return_value=_push_result(0)) as mock_run,
        ):
            _materialize(manager, assigned_branch="egg/pipe-1/work")

        cmd = mock_run.call_args.args[0]
        assert "push" in cmd
        assert "origin" in cmd
        assert "HEAD:refs/heads/egg/pipe-1/work" in cmd
        # Non-forced: no force flag, and the refspec carries no leading '+'
        # (which would clobber the primary repo's contract-init commit).
        assert "--force" not in cmd and "-f" not in cmd
        assert not any(str(c).startswith("+") for c in cmd)

    def test_falls_back_to_branch_name_when_no_assigned_branch(self, manager):
        """assigned_branch None → target is the per-worktree branch_name."""
        with (
            patch.object(manager, "_git_credential_env", _fake_credential_env),
            patch("subprocess.run", return_value=_push_result(0)) as mock_run,
        ):
            _materialize(manager, assigned_branch=None, branch_name="egg/egg-x/work")

        cmd = mock_run.call_args.args[0]
        assert "HEAD:refs/heads/egg/egg-x/work" in cmd

    def test_strips_origin_prefix_from_target(self, manager):
        """An ``origin/``-prefixed assigned branch is normalized to the bare ref."""
        with (
            patch.object(manager, "_git_credential_env", _fake_credential_env),
            patch("subprocess.run", return_value=_push_result(0)) as mock_run,
        ):
            _materialize(manager, assigned_branch="origin/egg/pipe-1/work")

        cmd = mock_run.call_args.args[0]
        assert "HEAD:refs/heads/egg/pipe-1/work" in cmd
        assert "HEAD:refs/heads/origin/egg/pipe-1/work" not in cmd


class TestIdempotentPushIsSwallowed:
    """An already-materialized branch is a no-op success, never fatal."""

    @pytest.mark.parametrize(
        "stderr",
        [
            "! [rejected]        HEAD -> egg/pipe-1/work (non-fast-forward)",
            "error: failed to push some refs; hint: Updates were rejected; fetch first",
            "Everything up-to-date",
        ],
    )
    def test_already_present_rejection_is_swallowed(self, manager, stderr):
        """Non-fast-forward / up-to-date rejection → treated as materialized, no raise."""
        with (
            patch.object(manager, "_git_credential_env", _fake_credential_env),
            patch("subprocess.run", return_value=_push_result(1, stderr)),
        ):
            # Must not raise; a best-effort push returns None regardless of outcome.
            assert _materialize(manager) is None


class TestPushFailureIsSwallowed:
    """A real push failure never fails worktree creation."""

    def test_generic_push_failure_is_swallowed(self, manager):
        """Auth/network failure (non-idempotent stderr) is logged and swallowed."""
        stderr = "fatal: could not read Username for 'https://github.com'"
        with (
            patch.object(manager, "_git_credential_env", _fake_credential_env),
            patch("subprocess.run", return_value=_push_result(128, stderr)),
        ):
            assert _materialize(manager) is None

    def test_subprocess_exception_is_swallowed(self, manager):
        """subprocess.run raising (e.g. timeout) is caught, not propagated."""
        with (
            patch.object(manager, "_git_credential_env", _fake_credential_env),
            patch(
                "subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="git push", timeout=30),
            ) as mock_run,
        ):
            assert _materialize(manager) is None
        assert mock_run.called
