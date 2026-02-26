"""Gap tests for post_agent_commit.py auto-commit functionality.

Covers edge cases not in the coder's initial tests:
- Combined phase + task filtering (both layers AND together)
- PhaseFileRestriction import failure during task filtering (fallback path)
- Empty allowed_files=[] (empty list, not None)
- allowed_files + phase both blocking different files
- Restore failure for blocked files
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from post_agent_commit import auto_commit_worktree


class TestAutoCommitCombinedPhaseAndTaskFiltering:
    """Tests for phase + task filtering applied together (AND semantics)."""

    @patch("post_agent_commit.subprocess.run")
    def test_phase_blocks_then_task_filters_remaining(self, mock_run, tmp_path):
        """Phase blocks .egg-state files, then task filter blocks out-of-scope files.

        Changed files:
        - src/auth/login.py      -> allowed by phase, allowed by task
        - .egg-state/c.json      -> blocked by phase
        - src/payments/pay.py    -> allowed by phase, blocked by task

        Only src/auth/login.py should be committed.
        """
        mock_run.side_effect = [
            # git status --porcelain
            MagicMock(
                returncode=0,
                stdout=(
                    " M src/auth/login.py\n"
                    " M .egg-state/contracts/c.json\n"
                    " M src/payments/pay.py\n"
                ),
                stderr="",
            ),
            # git checkout -- .egg-state/contracts/c.json (phase-blocked restore)
            MagicMock(returncode=0, stdout="", stderr=""),
            # git checkout -- src/payments/pay.py (task-blocked restore)
            MagicMock(returncode=0, stdout="", stderr=""),
            # git add -- src/auth/login.py
            MagicMock(returncode=0, stdout="", stderr=""),
            # git commit
            MagicMock(returncode=0, stdout="", stderr=""),
            # git rev-parse HEAD
            MagicMock(returncode=0, stdout="sha_combined\n", stderr=""),
        ]

        # Mock phase_filter to block .egg-state files
        mock_phase_result = MagicMock()
        mock_phase_result.allowed = False
        mock_phase_result.blocked_files = [".egg-state/contracts/c.json"]

        # Mock PhaseFileRestriction for task filtering
        mock_task_restriction = MagicMock()

        def mock_is_file_allowed(f):
            if f.startswith("src/auth/"):
                return (True, "allowed")
            return (False, "not in scope")

        mock_task_restriction.is_file_allowed = mock_is_file_allowed

        import sys
        import types

        mock_pf = types.ModuleType("phase_filter")
        mock_pf.check_phase_file_restrictions = MagicMock(return_value=mock_phase_result)
        mock_pf.PhaseFileRestriction = MagicMock(return_value=mock_task_restriction)

        old = sys.modules.get("phase_filter")
        sys.modules["phase_filter"] = mock_pf
        try:
            result = auto_commit_worktree(
                str(tmp_path),
                container_id="c1",
                phase="implement",
                allowed_files=["src/auth/*"],
            )
            assert result == "sha_combined"

            # Verify two restores happened (one for phase, one for task)
            checkout_calls = [
                c for c in mock_run.call_args_list
                if "checkout" in c[0][0]
            ]
            assert len(checkout_calls) == 2

            # Verify only src/auth/login.py was staged
            add_call = [c for c in mock_run.call_args_list if "add" in c[0][0]]
            assert len(add_call) == 1
            add_cmd = add_call[0][0][0]
            assert "src/auth/login.py" in add_cmd
            assert "src/payments/pay.py" not in add_cmd
            assert ".egg-state/contracts/c.json" not in add_cmd
        finally:
            if old is not None:
                sys.modules["phase_filter"] = old
            else:
                sys.modules.pop("phase_filter", None)

    @patch("post_agent_commit.subprocess.run")
    def test_all_blocked_by_both_layers_returns_none(self, mock_run, tmp_path):
        """When phase blocks some and task blocks the rest, no commit happens."""
        mock_run.side_effect = [
            # git status --porcelain
            MagicMock(
                returncode=0,
                stdout=(
                    " M .egg-state/contracts/c.json\n"
                    " M src/payments/pay.py\n"
                ),
                stderr="",
            ),
            # git checkout -- .egg-state/contracts/c.json (phase restore)
            MagicMock(returncode=0, stdout="", stderr=""),
            # git checkout -- src/payments/pay.py (task restore)
            MagicMock(returncode=0, stdout="", stderr=""),
        ]

        mock_phase_result = MagicMock()
        mock_phase_result.allowed = False
        mock_phase_result.blocked_files = [".egg-state/contracts/c.json"]

        mock_task_restriction = MagicMock()
        mock_task_restriction.is_file_allowed = MagicMock(
            return_value=(False, "not in scope")
        )

        import sys
        import types

        mock_pf = types.ModuleType("phase_filter")
        mock_pf.check_phase_file_restrictions = MagicMock(return_value=mock_phase_result)
        mock_pf.PhaseFileRestriction = MagicMock(return_value=mock_task_restriction)

        old = sys.modules.get("phase_filter")
        sys.modules["phase_filter"] = mock_pf
        try:
            result = auto_commit_worktree(
                str(tmp_path),
                container_id="c1",
                phase="implement",
                allowed_files=["src/auth/*"],
            )
            assert result is None
        finally:
            if old is not None:
                sys.modules["phase_filter"] = old
            else:
                sys.modules.pop("phase_filter", None)


class TestAutoCommitTaskFilterImportFallback:
    """Tests for PhaseFileRestriction import failure during task filtering."""

    @patch("post_agent_commit.subprocess.run")
    def test_task_filter_import_fails_commits_all(self, mock_run, tmp_path):
        """If PhaseFileRestriction can't be imported, all files are committed (fail-open)."""
        mock_run.side_effect = [
            # git status --porcelain
            MagicMock(
                returncode=0,
                stdout=" M src/auth/login.py\n M src/payments/pay.py\n",
                stderr="",
            ),
            # git add (all files since filter can't import)
            MagicMock(returncode=0, stdout="", stderr=""),
            # git commit
            MagicMock(returncode=0, stdout="", stderr=""),
            # git rev-parse HEAD
            MagicMock(returncode=0, stdout="sha_fallback\n", stderr=""),
        ]

        import sys

        old_pf = sys.modules.get("phase_filter")
        old_gw_pf = sys.modules.get("gateway.phase_filter")
        # Block import of phase_filter entirely
        sys.modules["phase_filter"] = None  # type: ignore[assignment]
        sys.modules["gateway.phase_filter"] = None  # type: ignore[assignment]
        try:
            result = auto_commit_worktree(
                str(tmp_path),
                container_id="c1",
                allowed_files=["src/auth/*"],
                # No phase so phase filtering is skipped, only task filtering
            )
            assert result == "sha_fallback"

            # Both files should be staged since import failed
            add_cmd = mock_run.call_args_list[1][0][0]
            assert "src/auth/login.py" in add_cmd
            assert "src/payments/pay.py" in add_cmd
        finally:
            if old_pf is not None:
                sys.modules["phase_filter"] = old_pf
            else:
                sys.modules.pop("phase_filter", None)
            if old_gw_pf is not None:
                sys.modules["gateway.phase_filter"] = old_gw_pf
            else:
                sys.modules.pop("gateway.phase_filter", None)


class TestAutoCommitEmptyAllowedFiles:
    """Tests for allowed_files=[] (empty list)."""

    @patch("post_agent_commit.subprocess.run")
    def test_empty_list_allowed_files_no_filtering(self, mock_run, tmp_path):
        """Empty list [] is falsy, so no task filtering is applied."""
        mock_run.side_effect = [
            MagicMock(
                returncode=0,
                stdout=" M any/file.py\n M other/file.py\n",
                stderr="",
            ),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="sha_empty_list\n", stderr=""),
        ]

        result = auto_commit_worktree(
            str(tmp_path),
            container_id="c1",
            allowed_files=[],
        )
        assert result == "sha_empty_list"

        # Both files should be staged (empty list = no filtering)
        add_cmd = mock_run.call_args_list[1][0][0]
        assert "any/file.py" in add_cmd
        assert "other/file.py" in add_cmd


class TestAutoCommitRestoreFailure:
    """Tests for restore failure on blocked files."""

    @patch("post_agent_commit.subprocess.run")
    def test_restore_failure_logged_but_commit_proceeds(self, mock_run, tmp_path):
        """If git checkout fails for a blocked file, commit still proceeds with allowed files."""
        mock_run.side_effect = [
            # git status --porcelain
            MagicMock(
                returncode=0,
                stdout=" M src/auth/login.py\n M out/of/scope.py\n",
                stderr="",
            ),
            # git checkout -- out/of/scope.py (FAILS)
            MagicMock(returncode=1, stdout="", stderr="pathspec did not match"),
            # git add -- src/auth/login.py
            MagicMock(returncode=0, stdout="", stderr=""),
            # git commit
            MagicMock(returncode=0, stdout="", stderr=""),
            # git rev-parse HEAD
            MagicMock(returncode=0, stdout="sha_partial\n", stderr=""),
        ]

        result = auto_commit_worktree(
            str(tmp_path),
            container_id="c1",
            allowed_files=["src/auth/*"],
        )
        # Commit should still succeed with the allowed file
        assert result == "sha_partial"
