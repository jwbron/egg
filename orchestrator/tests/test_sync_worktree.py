"""Tests for _sync_worktree_with_remote."""

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

from gateway_client import PushResult
from routes.pipelines import _sync_worktree_with_remote

_PUSH_OK = PushResult(ok=True, category="", detail="")
_PUSH_FAIL = PushResult(ok=False, category="test", detail="mock failure")


def _make_spawner(fetch_ok: bool = True, push_ok: bool = True) -> MagicMock:
    """Create a mock spawner with gateway.fetch_worktree_branch."""
    spawner = MagicMock()
    spawner.gateway.fetch_worktree_branch.return_value = fetch_ok
    spawner.gateway.push_worktree_branch.return_value = _PUSH_OK if push_ok else _PUSH_FAIL
    return spawner


def _make_subprocess_result(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestSyncWorktreeWithRemote:
    """Tests for _sync_worktree_with_remote."""

    def test_returns_early_when_fetch_fails(self):
        """If gateway fetch fails, function returns without running git commands."""
        spawner = _make_spawner(fetch_ok=False)
        with patch("routes.pipelines.subprocess.run") as mock_run:
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            mock_run.assert_not_called()

    def test_returns_early_on_detached_head(self):
        """If branch --show-current returns empty, function returns."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            # Step 2: empty branch (detached HEAD)
            mock_run.return_value = _make_subprocess_result(stdout="")
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            # Only step 2 should have been called
            assert mock_run.call_count == 1

    def test_returns_early_when_remote_branch_missing(self):
        """If origin/{branch} does not exist, function returns."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                # Step 2: branch name
                _make_subprocess_result(stdout="egg/issue-42\n"),
                # Step 3: rev-parse fails (remote branch missing)
                _make_subprocess_result(returncode=128),
            ]
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            assert mock_run.call_count == 2

    def test_local_ahead_prior_succeeded_pushes_and_returns(self):
        """(a) local ahead + prior phase succeeded → pushes, re-fetches, returns."""
        spawner = _make_spawner(push_ok=True)
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                # Step 2: branch name
                _make_subprocess_result(stdout="egg/issue-42\n"),
                # Step 3: rev-parse succeeds
                _make_subprocess_result(returncode=0),
                # Step 3b: local is 2 ahead, 0 behind
                _make_subprocess_result(stdout="2\t0\n"),
            ]
            _sync_worktree_with_remote(
                spawner,
                "pipe-1",
                Path("/tmp/repo"),
                prior_phase_succeeded=True,
            )
            # Should have pushed to remote
            spawner.gateway.push_worktree_branch.assert_called_once()
            # Should have re-fetched to update tracking ref
            assert spawner.gateway.fetch_worktree_branch.call_count == 2
            # Should NOT have proceeded to reset (early return after push)
            assert mock_run.call_count == 3

    def test_local_ahead_prior_failed_discards_and_resets(self):
        """(b) local ahead + prior phase failed → resets without pushing."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                # Step 2: branch name
                _make_subprocess_result(stdout="egg/issue-42\n"),
                # Step 3: rev-parse succeeds
                _make_subprocess_result(returncode=0),
                # Step 3b: local is 2 ahead, 0 behind
                _make_subprocess_result(stdout="2\t0\n"),
                # Step 4: reset succeeds
                _make_subprocess_result(returncode=0),
            ]
            _sync_worktree_with_remote(
                spawner,
                "pipe-1",
                Path("/tmp/repo"),
                prior_phase_succeeded=False,
            )
            # Should NOT have pushed
            spawner.gateway.push_worktree_branch.assert_not_called()
            # Should have proceeded to reset (step 4)
            assert mock_run.call_count == 4
            reset_call = mock_run.call_args_list[3]
            assert "reset" in reset_call[0][0]

    def test_local_behind_remote_fetches_then_resets(self):
        """(c) local behind remote → fetches then resets (existing behavior)."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                # Step 2: branch name
                _make_subprocess_result(stdout="egg/issue-42\n"),
                # Step 3: rev-parse succeeds
                _make_subprocess_result(returncode=0),
                # Step 3b: local is 0 ahead, 3 behind
                _make_subprocess_result(stdout="0\t3\n"),
                # Step 4: reset succeeds
                _make_subprocess_result(returncode=0),
            ]
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            assert mock_run.call_count == 4
            # Verify step 4 was git reset --hard
            reset_call = mock_run.call_args_list[3]
            assert "reset" in reset_call[0][0]
            assert "--hard" in reset_call[0][0]

    def test_local_in_sync_no_push_needed(self):
        """(d) local in sync → no push, reset is a no-op."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                # Step 2: branch name
                _make_subprocess_result(stdout="egg/issue-42\n"),
                # Step 3: rev-parse succeeds
                _make_subprocess_result(returncode=0),
                # Step 3b: local is 0 ahead, 0 behind
                _make_subprocess_result(stdout="0\t0\n"),
                # Step 4: reset succeeds (no-op when in sync)
                _make_subprocess_result(returncode=0),
            ]
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            spawner.gateway.push_worktree_branch.assert_not_called()
            assert mock_run.call_count == 4

    def test_push_fails_continues_with_reset(self):
        """(e) push fails → logs warning, continues with reset."""
        spawner = _make_spawner(push_ok=False)
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
        ):
            mock_run.side_effect = [
                # Step 2: branch name
                _make_subprocess_result(stdout="egg/issue-42\n"),
                # Step 3: rev-parse succeeds
                _make_subprocess_result(returncode=0),
                # Step 3b: local is 2 ahead, 0 behind
                _make_subprocess_result(stdout="2\t0\n"),
                # Step 4: reset succeeds (after push failure)
                _make_subprocess_result(returncode=0),
            ]
            _sync_worktree_with_remote(
                spawner,
                "pipe-1",
                Path("/tmp/repo"),
                prior_phase_succeeded=True,
            )
            # Push was attempted but failed
            spawner.gateway.push_worktree_branch.assert_called_once()
            # Warning logged about push failure
            mock_logger.warning.assert_called()
            # Reset still happened
            assert mock_run.call_count == 4

    def test_diverged_attempts_rebase(self):
        """(f) diverged → calls the rebase helper (#2337)."""
        spawner = _make_spawner()
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines._rebase_with_agent_output_autoresolve") as mock_rebase,
        ):
            mock_run.side_effect = [
                # Step 2: branch name
                _make_subprocess_result(stdout="egg/issue-42\n"),
                # Step 3: rev-parse succeeds
                _make_subprocess_result(returncode=0),
                # Step 3b: local is 2 ahead, 3 behind (diverged)
                _make_subprocess_result(stdout="2\t3\n"),
            ]
            mock_rebase.return_value = PushResult(ok=True, category="", detail="")
            _sync_worktree_with_remote(
                spawner,
                "pipe-1",
                Path("/tmp/repo"),
                base_branch="main",
            )
            # Rebase helper called with the divergence-resolving form
            mock_rebase.assert_called_once()
            kwargs = mock_rebase.call_args.kwargs
            assert kwargs["branch"] == "egg/issue-42"
            assert kwargs["base_branch"] == "main"
            # No reset was attempted (rebase succeeded → early return)
            assert mock_run.call_count == 3

    def test_diverged_rebase_fails_signals_error(self):
        """(g) diverged + rebase fails → ERROR log with worktree_sync_outcome."""
        spawner = _make_spawner()
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
            patch("routes.pipelines._rebase_with_agent_output_autoresolve") as mock_rebase,
        ):
            mock_run.side_effect = [
                _make_subprocess_result(stdout="egg/issue-42\n"),
                _make_subprocess_result(returncode=0),
                _make_subprocess_result(stdout="2\t3\n"),
            ]
            mock_rebase.return_value = PushResult(
                ok=False,
                category="reconcile_rebase_conflict",
                detail="conflicts outside agent-outputs",
            )
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            mock_logger.error.assert_called()
            err_kwargs = mock_logger.error.call_args.kwargs
            assert err_kwargs.get("case") == "divergence_rebase_failed"
            assert err_kwargs.get("category") == "reconcile_rebase_conflict"
            # No reset was attempted (rebase failure short-circuits)
            assert mock_run.call_count == 3

    def test_successful_reset(self):
        """Happy path: fetch, detect branch, verify remote, reset."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                # Step 2: branch name
                _make_subprocess_result(stdout="egg/issue-42\n"),
                # Step 3: rev-parse succeeds
                _make_subprocess_result(returncode=0),
                # Step 3b: local is 0 ahead, 3 behind
                _make_subprocess_result(stdout="0\t3\n"),
                # Step 4: reset succeeds
                _make_subprocess_result(returncode=0),
            ]
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            assert mock_run.call_count == 4
            # Verify step 4 was git reset --hard
            reset_call = mock_run.call_args_list[3]
            assert "reset" in reset_call[0][0]
            assert "--hard" in reset_call[0][0]

    def test_logs_warning_on_failed_reset(self):
        """If git reset --hard fails, a warning is logged."""
        spawner = _make_spawner()
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
        ):
            mock_run.side_effect = [
                # Step 2: branch name
                _make_subprocess_result(stdout="egg/issue-42\n"),
                # Step 3: rev-parse succeeds
                _make_subprocess_result(returncode=0),
                # Step 3b: local is 0 ahead, 1 behind
                _make_subprocess_result(stdout="0\t1\n"),
                # Step 4: reset fails
                _make_subprocess_result(returncode=1, stderr="permission denied"),
            ]
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            mock_logger.warning.assert_called()
            warning_msgs = [c[0][0] for c in mock_logger.warning.call_args_list]
            assert any("Failed to reset" in m for m in warning_msgs)

    def test_handles_subprocess_timeout(self):
        """If subprocess raises TimeoutExpired, function handles gracefully."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=10)
            # Should not raise
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))

    def test_diverged_rebase_returns_failure_result(self):
        """Diverged + rebase helper returns failure → no reset, ERROR logged."""
        spawner = _make_spawner()
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
            patch("routes.pipelines._rebase_with_agent_output_autoresolve") as mock_rebase,
        ):
            mock_run.side_effect = [
                _make_subprocess_result(stdout="egg/issue-42\n"),
                _make_subprocess_result(returncode=0),
                _make_subprocess_result(stdout="2\t3\n"),
            ]
            mock_rebase.return_value = PushResult(
                ok=False,
                category="reconcile_rebase_timeout",
                detail="timed out",
            )
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            mock_logger.error.assert_called()
            # No reset attempt — failed rebase short-circuits the function
            assert mock_run.call_count == 3

    def test_rev_list_non_numeric_output_defaults_to_reset(self):
        """Non-numeric rev-list output falls through to reset."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                # Step 2: branch name
                _make_subprocess_result(stdout="egg/issue-42\n"),
                # Step 3: rev-parse succeeds
                _make_subprocess_result(returncode=0),
                # Step 3b: unexpected output from rev-list
                _make_subprocess_result(stdout="not-a-number\n"),
                # Step 4: reset succeeds (falls through due to ValueError)
                _make_subprocess_result(returncode=0),
            ]
            # Should not raise, falls through to reset
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            # Should reach step 4 (reset)
            assert mock_run.call_count == 4
            reset_call = mock_run.call_args_list[3]
            assert "reset" in reset_call[0][0]

    def test_prior_phase_succeeded_defaults_to_true(self):
        """Default prior_phase_succeeded=True means local-ahead commits get pushed."""
        spawner = _make_spawner(push_ok=True)
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                # Step 2: branch name
                _make_subprocess_result(stdout="egg/issue-42\n"),
                # Step 3: rev-parse succeeds
                _make_subprocess_result(returncode=0),
                # Step 3b: local is 1 ahead, 0 behind
                _make_subprocess_result(stdout="1\t0\n"),
            ]
            # Call without prior_phase_succeeded param (defaults to True)
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            # Should have attempted push (default True)
            spawner.gateway.push_worktree_branch.assert_called_once()
            # Push succeeded → re-fetch + early return (no reset)
            assert spawner.gateway.fetch_worktree_branch.call_count == 2
            assert mock_run.call_count == 3

    def test_rev_list_check_fails_proceeds_to_reset(self):
        """Rev-list check subprocess failure proceeds to reset."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                # Step 2: branch name
                _make_subprocess_result(stdout="egg/issue-42\n"),
                # Step 3: rev-parse succeeds
                _make_subprocess_result(returncode=0),
                # Step 3b: rev-list fails
                _make_subprocess_result(returncode=1, stderr="error"),
                # Step 4: reset succeeds
                _make_subprocess_result(returncode=0),
            ]
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            # Should still reach reset (step 4)
            assert mock_run.call_count == 4
            reset_call = mock_run.call_args_list[3]
            assert "reset" in reset_call[0][0]


def _outcome_cases(mock_logger: MagicMock) -> list[str]:
    """Collect every ``case=...`` kwarg from worktree_sync_outcome log calls."""
    cases: list[str] = []
    for level in ("info", "warning", "error"):
        method = getattr(mock_logger, level)
        for call in method.call_args_list:
            args = call[0]
            kwargs = call[1]
            if args and args[0] == "worktree_sync_outcome" and "case" in kwargs:
                cases.append(kwargs["case"])
    return cases


class TestSyncWorktreeOutcomeLogging:
    """#2337: every return path emits a single worktree_sync_outcome log line."""

    def test_fetch_failed_emits_outcome(self):
        spawner = _make_spawner(fetch_ok=False)
        with patch("routes.pipelines.logger") as mock_logger:
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            assert "fetch_failed" in _outcome_cases(mock_logger)

    def test_detached_head_emits_outcome(self):
        spawner = _make_spawner()
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
        ):
            mock_run.return_value = _make_subprocess_result(stdout="")
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            assert "detached_head" in _outcome_cases(mock_logger)

    def test_no_remote_tracking_emits_outcome(self):
        spawner = _make_spawner()
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
        ):
            mock_run.side_effect = [
                _make_subprocess_result(stdout="egg/issue-42\n"),
                _make_subprocess_result(returncode=128),
            ]
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            assert "no_remote_tracking" in _outcome_cases(mock_logger)

    def test_reset_succeeded_emits_outcome(self):
        spawner = _make_spawner()
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
        ):
            mock_run.side_effect = [
                _make_subprocess_result(stdout="egg/issue-42\n"),
                _make_subprocess_result(returncode=0),
                _make_subprocess_result(stdout="0\t3\n"),
                _make_subprocess_result(returncode=0),
            ]
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            assert "reset_succeeded" in _outcome_cases(mock_logger)

    def test_reset_failed_emits_outcome(self):
        spawner = _make_spawner()
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
        ):
            mock_run.side_effect = [
                _make_subprocess_result(stdout="egg/issue-42\n"),
                _make_subprocess_result(returncode=0),
                _make_subprocess_result(stdout="0\t1\n"),
                _make_subprocess_result(returncode=1, stderr="permission denied"),
            ]
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            assert "reset_failed" in _outcome_cases(mock_logger)

    def test_local_ahead_pushed_emits_outcome(self):
        spawner = _make_spawner(push_ok=True)
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
        ):
            mock_run.side_effect = [
                _make_subprocess_result(stdout="egg/issue-42\n"),
                _make_subprocess_result(returncode=0),
                _make_subprocess_result(stdout="2\t0\n"),
            ]
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            assert "local_ahead_pushed" in _outcome_cases(mock_logger)

    def test_divergence_rebased_emits_outcome(self):
        spawner = _make_spawner()
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
            patch("routes.pipelines._rebase_with_agent_output_autoresolve") as mock_rebase,
        ):
            mock_run.side_effect = [
                _make_subprocess_result(stdout="egg/issue-42\n"),
                _make_subprocess_result(returncode=0),
                _make_subprocess_result(stdout="2\t3\n"),
            ]
            mock_rebase.return_value = PushResult(ok=True, category="", detail="")
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            assert "divergence_rebased" in _outcome_cases(mock_logger)

    def test_divergence_rebase_failed_emits_outcome(self):
        spawner = _make_spawner()
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
            patch("routes.pipelines._rebase_with_agent_output_autoresolve") as mock_rebase,
        ):
            mock_run.side_effect = [
                _make_subprocess_result(stdout="egg/issue-42\n"),
                _make_subprocess_result(returncode=0),
                _make_subprocess_result(stdout="2\t3\n"),
            ]
            mock_rebase.return_value = PushResult(
                ok=False,
                category="reconcile_rebase_conflict",
                detail="conflict",
            )
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            assert "divergence_rebase_failed" in _outcome_cases(mock_logger)

    def test_local_ahead_discarded_emits_outcome(self):
        """#2337 review S2: prior-phase-failed-discard branch must emit a
        case-discriminator outcome before falling through to reset, so
        log-based dashboards see a uniform exit-path event.
        """
        spawner = _make_spawner()
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
        ):
            mock_run.side_effect = [
                _make_subprocess_result(stdout="egg/issue-42\n"),
                _make_subprocess_result(returncode=0),
                # Local 2 ahead, 0 behind — prior phase failed → discard.
                _make_subprocess_result(stdout="2\t0\n"),
                # Reset succeeds.
                _make_subprocess_result(returncode=0),
            ]
            _sync_worktree_with_remote(
                spawner,
                "pipe-1",
                Path("/tmp/repo"),
                prior_phase_succeeded=False,
            )
            cases = _outcome_cases(mock_logger)
            # The fall-through emits BOTH the discard discriminator and
            # the reset_succeeded outcome — fall-through paths emit a
            # sequence so dashboards see every exit-path event.
            assert "local_ahead_discarded_falling_through_to_reset" in cases
            assert "reset_succeeded" in cases

    def test_divergence_with_base_branch_none_logs_contamination_warning(self):
        """#2337 review S7: divergence rebase with ``base_branch=None``
        falls back to the bare-rebase form (the #2222 contamination
        vector); log a warning so the next person debugging
        contamination has a breadcrumb at the call site.
        """
        spawner = _make_spawner()
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
            patch("routes.pipelines._rebase_with_agent_output_autoresolve") as mock_rebase,
        ):
            mock_run.side_effect = [
                _make_subprocess_result(stdout="egg/issue-42\n"),
                _make_subprocess_result(returncode=0),
                _make_subprocess_result(stdout="2\t3\n"),
            ]
            mock_rebase.return_value = PushResult(ok=True, category="", detail="")
            # base_branch=None — the contamination-prone fallback path.
            _sync_worktree_with_remote(
                spawner,
                "pipe-1",
                Path("/tmp/repo"),
                base_branch=None,
            )
            warning_msgs = [c[0][0] for c in mock_logger.warning.call_args_list]
            assert any("base_branch=None" in m and "#2222" in m for m in warning_msgs)

    def test_divergence_with_base_branch_set_does_not_log_contamination_warning(self):
        """#2337 review S7: with ``base_branch`` threaded the safer
        ``--onto`` form is used; no contamination breadcrumb required.
        """
        spawner = _make_spawner()
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
            patch("routes.pipelines._rebase_with_agent_output_autoresolve") as mock_rebase,
        ):
            mock_run.side_effect = [
                _make_subprocess_result(stdout="egg/issue-42\n"),
                _make_subprocess_result(returncode=0),
                _make_subprocess_result(stdout="2\t3\n"),
            ]
            mock_rebase.return_value = PushResult(ok=True, category="", detail="")
            _sync_worktree_with_remote(
                spawner,
                "pipe-1",
                Path("/tmp/repo"),
                base_branch="main",
            )
            warning_msgs = [c[0][0] for c in mock_logger.warning.call_args_list]
            assert not any("base_branch=None" in m for m in warning_msgs)
