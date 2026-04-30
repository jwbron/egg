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

    def test_local_in_sync_returns_early_without_reset(self):
        """(d) local in sync → no push, no reset (early return on already_in_sync)."""
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                # Step 2: branch name
                _make_subprocess_result(stdout="egg/issue-42\n"),
                # Step 3: rev-parse succeeds
                _make_subprocess_result(returncode=0),
                # Step 3b: local is 0 ahead, 0 behind
                _make_subprocess_result(stdout="0\t0\n"),
            ]
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            spawner.gateway.push_worktree_branch.assert_not_called()
            # Already-in-sync skips the step-4 reset entirely.
            assert mock_run.call_count == 3

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
            warning_kwargs = mock_logger.warning.call_args.kwargs
            assert warning_kwargs.get("case") == "reset_failed"

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
    """Return the `case=` values from every worktree_sync_outcome log call, in order."""
    cases: list[str] = []
    # method_calls preserves call order across info/warning/error.
    for call in mock_logger.method_calls:
        if call[0] not in ("info", "warning", "error"):
            continue
        args = call.args
        kwargs = call.kwargs
        if args and args[0] == "worktree_sync_outcome" and "case" in kwargs:
            cases.append(kwargs["case"])
    return cases


class TestSyncWorktreeOutcomeTaxonomy:
    """Each return path emits worktree_sync_outcome with the expected case label."""

    def test_case_fetch_failed(self):
        spawner = _make_spawner(fetch_ok=False)
        with patch("routes.pipelines.logger") as mock_logger:
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
        assert _outcome_cases(mock_logger) == ["fetch_failed"]

    def test_case_detached_head(self):
        spawner = _make_spawner()
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
        ):
            mock_run.return_value = _make_subprocess_result(stdout="")
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
        assert _outcome_cases(mock_logger) == ["detached_head"]

    def test_case_branch_detect_failed(self):
        spawner = _make_spawner()
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
        ):
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=10)
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
        assert _outcome_cases(mock_logger) == ["branch_detect_failed"]

    def test_case_no_remote_tracking(self):
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
        assert _outcome_cases(mock_logger) == ["no_remote_tracking"]

    def test_case_rev_parse_failed(self):
        spawner = _make_spawner()
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
        ):
            mock_run.side_effect = [
                _make_subprocess_result(stdout="egg/issue-42\n"),
                subprocess.TimeoutExpired(cmd="git rev-parse", timeout=10),
            ]
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
        assert _outcome_cases(mock_logger) == ["rev_parse_failed"]

    def test_case_rev_list_failed_falls_through_to_reset(self):
        """rev-list exception emits rev_list_failed, then step 4 emits reset_succeeded."""
        spawner = _make_spawner()
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
        ):
            mock_run.side_effect = [
                _make_subprocess_result(stdout="egg/issue-42\n"),
                _make_subprocess_result(returncode=0),
                # rev-list returns two non-numeric tokens → int() raises ValueError
                _make_subprocess_result(stdout="foo\tbar\n"),
                # Step 4: reset succeeds
                _make_subprocess_result(returncode=0),
            ]
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
        assert _outcome_cases(mock_logger) == ["rev_list_failed", "reset_succeeded"]

    def test_case_rev_list_failed_returncode_falls_through_to_reset(self):
        """rev-list non-zero returncode emits rev_list_failed (no exception path)."""
        spawner = _make_spawner()
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
        ):
            mock_run.side_effect = [
                _make_subprocess_result(stdout="egg/issue-42\n"),
                _make_subprocess_result(returncode=0),
                # rev-list exits non-zero — must still emit rev_list_failed
                _make_subprocess_result(returncode=128, stderr="bad ref"),
                _make_subprocess_result(returncode=0),
            ]
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
        assert _outcome_cases(mock_logger) == ["rev_list_failed", "reset_succeeded"]
        # The rev_list_failed log carries the rc field for non-exception failures.
        rev_list_call = next(
            c
            for c in mock_logger.info.call_args_list
            if c.args
            and c.args[0] == "worktree_sync_outcome"
            and c.kwargs.get("case") == "rev_list_failed"
        )
        assert rev_list_call.kwargs.get("rc") == 128

    def test_case_rev_list_failed_unparseable_output(self):
        """rev-list returncode=0 but malformed output emits rev_list_failed."""
        spawner = _make_spawner()
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
        ):
            mock_run.side_effect = [
                _make_subprocess_result(stdout="egg/issue-42\n"),
                _make_subprocess_result(returncode=0),
                # rev-list rc=0 but only one token — len(parts) != 2
                _make_subprocess_result(stdout="42\n"),
                _make_subprocess_result(returncode=0),
            ]
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
        assert _outcome_cases(mock_logger) == ["rev_list_failed", "reset_succeeded"]

    def test_case_already_in_sync(self):
        spawner = _make_spawner()
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
        ):
            mock_run.side_effect = [
                _make_subprocess_result(stdout="egg/issue-42\n"),
                _make_subprocess_result(returncode=0),
                _make_subprocess_result(stdout="0\t0\n"),
            ]
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
        assert _outcome_cases(mock_logger) == ["already_in_sync"]

    def test_case_local_ahead_pushed(self):
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
            _sync_worktree_with_remote(
                spawner, "pipe-1", Path("/tmp/repo"), prior_phase_succeeded=True
            )
        assert _outcome_cases(mock_logger) == ["local_ahead_pushed"]

    def test_case_local_ahead_push_failed_falls_through_to_reset(self):
        """Push failure emits local_ahead_push_failed, then step 4 emits reset_succeeded."""
        spawner = _make_spawner(push_ok=False)
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
        ):
            mock_run.side_effect = [
                _make_subprocess_result(stdout="egg/issue-42\n"),
                _make_subprocess_result(returncode=0),
                _make_subprocess_result(stdout="2\t0\n"),
                _make_subprocess_result(returncode=0),
            ]
            _sync_worktree_with_remote(
                spawner, "pipe-1", Path("/tmp/repo"), prior_phase_succeeded=True
            )
        assert _outcome_cases(mock_logger) == [
            "local_ahead_push_failed",
            "reset_succeeded",
        ]
        # PushResult diagnostics are propagated into the structured log so
        # operators don't need to pull gateway-side logs for the failure mode.
        push_failed_call = next(
            c
            for c in mock_logger.warning.call_args_list
            if c.args
            and c.args[0] == "worktree_sync_outcome"
            and c.kwargs.get("case") == "local_ahead_push_failed"
        )
        assert push_failed_call.kwargs.get("category") == "test"
        assert push_failed_call.kwargs.get("error") == "mock failure"

    def test_case_local_ahead_discarded(self):
        """Prior phase failed + local-ahead → emit local_ahead_discarded then reset."""
        spawner = _make_spawner()
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
        ):
            mock_run.side_effect = [
                _make_subprocess_result(stdout="egg/issue-42\n"),
                _make_subprocess_result(returncode=0),
                _make_subprocess_result(stdout="2\t0\n"),
                _make_subprocess_result(returncode=0),
            ]
            _sync_worktree_with_remote(
                spawner,
                "pipe-1",
                Path("/tmp/repo"),
                prior_phase_succeeded=False,
            )
            spawner.gateway.push_worktree_branch.assert_not_called()
        assert _outcome_cases(mock_logger) == [
            "local_ahead_discarded",
            "reset_succeeded",
        ]

    def test_case_diverged_rebased(self):
        """Real divergence triggers _rebase_with_agent_output_autoresolve; success emits divergence_rebased."""
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
        assert _outcome_cases(mock_logger) == ["divergence_rebased"]

    def test_case_diverged_rebase_failed(self):
        """Rebase failure emits divergence_rebase_failed; the function returns without falling through to reset."""
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
        assert _outcome_cases(mock_logger) == ["divergence_rebase_failed"]

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

    def test_case_reset_succeeded(self):
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
        assert _outcome_cases(mock_logger) == ["reset_succeeded"]

    def test_case_reset_failed_returncode(self):
        spawner = _make_spawner()
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
        ):
            mock_run.side_effect = [
                _make_subprocess_result(stdout="egg/issue-42\n"),
                _make_subprocess_result(returncode=0),
                _make_subprocess_result(stdout="0\t3\n"),
                _make_subprocess_result(returncode=1, stderr="permission denied"),
            ]
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
        assert _outcome_cases(mock_logger) == ["reset_failed"]

    def test_case_reset_failed_exception(self):
        """Subprocess crash during reset collapses into reset_failed."""
        spawner = _make_spawner()
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
        ):
            mock_run.side_effect = [
                _make_subprocess_result(stdout="egg/issue-42\n"),
                _make_subprocess_result(returncode=0),
                _make_subprocess_result(stdout="0\t3\n"),
                subprocess.TimeoutExpired(cmd="git reset", timeout=30),
            ]
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
        assert _outcome_cases(mock_logger) == ["reset_failed"]

    def test_counters_present_when_known(self):
        """local_ahead/remote_ahead are included in the structured log when known."""
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
        # Find the reset_succeeded call
        reset_call = next(
            c
            for c in mock_logger.info.call_args_list
            if c.args
            and c.args[0] == "worktree_sync_outcome"
            and c.kwargs.get("case") == "reset_succeeded"
        )
        assert reset_call.kwargs["local_ahead"] == 0
        assert reset_call.kwargs["remote_ahead"] == 3


def _git_args(call) -> list[str]:
    """Extract the git argv list from a ``subprocess.run`` mock call."""
    return list(call.args[0])


class TestSyncWorktreePipelineBranch:
    """#2367 — pipeline_branch overrides local branch for remote-side refs.

    Orchestrator worktrees check out ``egg/<pid>/work``; the agent-facing
    branch on origin is ``egg/<pid>``.  Without an explicit
    ``pipeline_branch``, the function looked up
    ``origin/egg/<pid>/work``, missed, and exited at
    ``no_remote_tracking`` — stranding the pipeline with full BRC plan
    output sitting on origin and no recovery path.
    """

    def test_pipeline_branch_used_for_remote_lookup_not_local(self):
        """rev-parse must target ``origin/<pipeline_branch>``, not
        ``origin/<local_branch>`` — the bug-trigger condition.
        """
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                # Step 2: local branch is /work-suffixed
                _make_subprocess_result(stdout="egg/issue-42/work\n"),
                # Step 3: rev-parse succeeds for the canonical name
                _make_subprocess_result(returncode=0),
                # Step 3b: already in sync — early return
                _make_subprocess_result(stdout="0\t0\n"),
            ]
            _sync_worktree_with_remote(
                spawner,
                "pipe-1",
                Path("/tmp/repo"),
                pipeline_branch="egg/issue-42",
            )
            rev_parse_call = mock_run.call_args_list[1]
            argv = _git_args(rev_parse_call)
            assert "origin/egg/issue-42" in argv
            assert "origin/egg/issue-42/work" not in argv

    def test_pipeline_branch_used_for_rev_list(self):
        """rev-list (divergence detection) must compare against
        ``origin/<pipeline_branch>``.
        """
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _make_subprocess_result(stdout="egg/issue-42/work\n"),
                _make_subprocess_result(returncode=0),
                _make_subprocess_result(stdout="0\t0\n"),
            ]
            _sync_worktree_with_remote(
                spawner,
                "pipe-1",
                Path("/tmp/repo"),
                pipeline_branch="egg/issue-42",
            )
            rev_list_argv = _git_args(mock_run.call_args_list[2])
            assert "HEAD...origin/egg/issue-42" in rev_list_argv

    def test_pipeline_branch_used_for_reset_target(self):
        """``git reset --hard`` must target ``origin/<pipeline_branch>``
        so the local ``/work`` branch is brought up to the agent-facing
        remote tip — the recovery path #2367 was missing.
        """
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _make_subprocess_result(stdout="egg/issue-42/work\n"),
                _make_subprocess_result(returncode=0),
                # Local 0 ahead, 3 behind — fall through to reset
                _make_subprocess_result(stdout="0\t3\n"),
                _make_subprocess_result(returncode=0),
            ]
            _sync_worktree_with_remote(
                spawner,
                "pipe-1",
                Path("/tmp/repo"),
                pipeline_branch="egg/issue-42",
            )
            reset_argv = _git_args(mock_run.call_args_list[3])
            assert "origin/egg/issue-42" in reset_argv
            assert "origin/egg/issue-42/work" not in reset_argv

    def test_pipeline_branch_used_for_push_target(self):
        """Latent companion bug: when local is ahead, the gateway push
        must target ``pipeline_branch`` (the agent-facing remote ref),
        not the ``/work``-suffixed local branch.  The gateway builds
        ``HEAD:refs/heads/{branch}`` from this argument.
        """
        spawner = _make_spawner(push_ok=True)
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _make_subprocess_result(stdout="egg/issue-42/work\n"),
                _make_subprocess_result(returncode=0),
                # Local 2 ahead, 0 behind → push path
                _make_subprocess_result(stdout="2\t0\n"),
            ]
            _sync_worktree_with_remote(
                spawner,
                "pipe-1",
                Path("/tmp/repo"),
                prior_phase_succeeded=True,
                pipeline_branch="egg/issue-42",
            )
            push_kwargs = spawner.gateway.push_worktree_branch.call_args.kwargs
            assert push_kwargs["branch"] == "egg/issue-42"

    def test_pipeline_branch_used_for_divergence_rebase(self):
        """The divergence-rebase helper must rebase onto
        ``origin/<pipeline_branch>``.
        """
        spawner = _make_spawner()
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines._rebase_with_agent_output_autoresolve") as mock_rebase,
        ):
            mock_run.side_effect = [
                _make_subprocess_result(stdout="egg/issue-42/work\n"),
                _make_subprocess_result(returncode=0),
                _make_subprocess_result(stdout="2\t3\n"),
            ]
            mock_rebase.return_value = PushResult(ok=True, category="", detail="")
            _sync_worktree_with_remote(
                spawner,
                "pipe-1",
                Path("/tmp/repo"),
                pipeline_branch="egg/issue-42",
                base_branch="main",
            )
            assert mock_rebase.call_args.kwargs["branch"] == "egg/issue-42"

    def test_no_remote_tracking_does_not_fire_when_pipeline_branch_resolves(self):
        """The bug signature: ``case=no_remote_tracking`` must NOT be
        emitted when ``origin/<pipeline_branch>`` resolves, even though
        ``origin/<local_branch>`` would not.  This is the regression
        guard for #2367.
        """
        spawner = _make_spawner()
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
        ):
            mock_run.side_effect = [
                _make_subprocess_result(stdout="egg/issue-42/work\n"),
                # rev-parse against pipeline_branch succeeds
                _make_subprocess_result(returncode=0),
                _make_subprocess_result(stdout="0\t3\n"),
                _make_subprocess_result(returncode=0),
            ]
            _sync_worktree_with_remote(
                spawner,
                "pipe-1",
                Path("/tmp/repo"),
                pipeline_branch="egg/issue-42",
            )
        cases = [
            c.kwargs.get("case")
            for c in mock_logger.info.call_args_list
            if c.args and c.args[0] == "worktree_sync_outcome"
        ]
        assert "no_remote_tracking" not in cases
        assert "reset_succeeded" in cases

    def test_falls_back_to_local_branch_when_pipeline_branch_omitted(self):
        """Backward compatibility: callers without a pipeline (e.g.
        scripts) keep the pre-#2367 behavior of looking up
        ``origin/<local_branch>``.
        """
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _make_subprocess_result(stdout="some-branch\n"),
                _make_subprocess_result(returncode=0),
                _make_subprocess_result(stdout="0\t0\n"),
            ]
            _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
            rev_parse_argv = _git_args(mock_run.call_args_list[1])
            assert "origin/some-branch" in rev_parse_argv
