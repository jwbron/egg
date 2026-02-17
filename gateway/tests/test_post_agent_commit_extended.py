"""Extended tests for post_agent_commit.py covering review feedback gaps.

Covers:
- checkout failure for blocked files (warns and continues)
- Push via gateway integration (branch detection, URL construction)
- Commit message full format validation
- Multiple files with mixed allowed/blocked
- _push_via_gateway() HTTP request construction
"""

import types
from unittest.mock import MagicMock, patch

from post_agent_commit import _parse_changed_files, _push_via_gateway, auto_commit_worktree


class TestCheckoutFailureHandling:
    """git checkout failure for individual blocked files should warn and continue."""

    @patch("post_agent_commit.subprocess.run")
    def test_checkout_failure_still_commits_allowed(self, mock_run, tmp_path):
        """If git checkout fails for a blocked file, allowed files still commit."""
        mock_run.side_effect = [
            # git status --porcelain
            MagicMock(
                returncode=0,
                stdout=" M src/app.py\n M .egg-state/contracts/c.json\n",
                stderr="",
            ),
            # git checkout -- .egg-state/contracts/c.json (FAILS)
            MagicMock(returncode=1, stdout="", stderr="checkout failed"),
            # git add -- src/app.py
            MagicMock(returncode=0, stdout="", stderr=""),
            # git commit
            MagicMock(returncode=0, stdout="", stderr=""),
            # git rev-parse HEAD
            MagicMock(returncode=0, stdout="sha_after_failed_checkout\n", stderr=""),
        ]

        import sys

        mock_result = MagicMock()
        mock_result.allowed = False
        mock_result.blocked_files = [".egg-state/contracts/c.json"]

        mock_pf = types.ModuleType("phase_filter")
        mock_pf.check_phase_file_restrictions = MagicMock(return_value=mock_result)
        old = sys.modules.get("phase_filter")
        sys.modules["phase_filter"] = mock_pf
        try:
            result = auto_commit_worktree(
                str(tmp_path),
                container_id="c1",
                phase="implement",
            )
            # Commit still succeeds despite checkout failure
            assert result == "sha_after_failed_checkout"

            # Verify blocked file is NOT in add command
            add_call = mock_run.call_args_list[2]
            add_cmd = add_call[0][0]
            assert ".egg-state/contracts/c.json" not in add_cmd
            assert "src/app.py" in add_cmd
        finally:
            if old is not None:
                sys.modules["phase_filter"] = old
            else:
                sys.modules.pop("phase_filter", None)


class TestMultipleBlockedFiles:
    """Tests with multiple blocked and allowed files."""

    @patch("post_agent_commit.subprocess.run")
    def test_multiple_blocked_files_all_restored(self, mock_run, tmp_path):
        """Multiple blocked files all get git checkout called."""
        mock_run.side_effect = [
            # git status --porcelain
            MagicMock(
                returncode=0,
                stdout=(
                    " M src/app.py\n"
                    " M .egg-state/contracts/c.json\n"
                    " M .egg-state/drafts/plan.md\n"
                    " M .egg-state/reviews/r.json\n"
                ),
                stderr="",
            ),
            # git checkout -- .egg-state/contracts/c.json
            MagicMock(returncode=0, stdout="", stderr=""),
            # git checkout -- .egg-state/drafts/plan.md
            MagicMock(returncode=0, stdout="", stderr=""),
            # git checkout -- .egg-state/reviews/r.json
            MagicMock(returncode=0, stdout="", stderr=""),
            # git add -- src/app.py
            MagicMock(returncode=0, stdout="", stderr=""),
            # git commit
            MagicMock(returncode=0, stdout="", stderr=""),
            # git rev-parse HEAD
            MagicMock(returncode=0, stdout="sha_multi\n", stderr=""),
        ]

        import sys

        mock_result = MagicMock()
        mock_result.allowed = False
        mock_result.blocked_files = [
            ".egg-state/contracts/c.json",
            ".egg-state/drafts/plan.md",
            ".egg-state/reviews/r.json",
        ]

        mock_pf = types.ModuleType("phase_filter")
        mock_pf.check_phase_file_restrictions = MagicMock(return_value=mock_result)
        old = sys.modules.get("phase_filter")
        sys.modules["phase_filter"] = mock_pf
        try:
            result = auto_commit_worktree(
                str(tmp_path),
                container_id="c1",
                phase="implement",
            )
            assert result == "sha_multi"

            # Verify 3 checkout calls (one per blocked file)
            checkout_calls = [
                c for c in mock_run.call_args_list if "checkout" in c[0][0]
            ]
            assert len(checkout_calls) == 3

            # Verify add only includes allowed file
            add_call = [c for c in mock_run.call_args_list if "add" in c[0][0]][0]
            add_cmd = add_call[0][0]
            assert "src/app.py" in add_cmd
            assert ".egg-state/contracts/c.json" not in add_cmd
        finally:
            if old is not None:
                sys.modules["phase_filter"] = old
            else:
                sys.modules.pop("phase_filter", None)


class TestCommitMessageFormat:
    """Tests for commit message format details."""

    @patch("post_agent_commit.subprocess.run")
    def test_full_message_format(self, mock_run, tmp_path):
        """Full commit message format with role and pipeline ID."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=" M file.py\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="sha123\n", stderr=""),
        ]
        auto_commit_worktree(
            str(tmp_path),
            container_id="test-container-xyz",
            agent_role="coder",
            pipeline_id="issue-644",
        )
        commit_cmd = mock_run.call_args_list[2][0][0]
        msg_idx = commit_cmd.index("-m") + 1
        message = commit_cmd[msg_idx]

        assert message.startswith("WIP: auto-commit uncommitted work")
        assert "(coder)" in message
        assert "[issue-644]" in message
        assert "test-container-xyz" in message
        assert "Authored-by: egg" in message

    @patch("post_agent_commit.subprocess.run")
    def test_message_without_role_or_pipeline(self, mock_run, tmp_path):
        """Commit message without optional role and pipeline ID."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=" M file.py\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="sha123\n", stderr=""),
        ]
        auto_commit_worktree(str(tmp_path), container_id="c1")
        commit_cmd = mock_run.call_args_list[2][0][0]
        msg_idx = commit_cmd.index("-m") + 1
        message = commit_cmd[msg_idx]

        assert "WIP: auto-commit uncommitted work\n" in message
        assert "()" not in message  # No empty parens
        assert "[]" not in message  # No empty brackets


class TestPushViaGateway:
    """Tests for _push_via_gateway() HTTP integration."""

    @patch("urllib.request.urlopen")
    @patch("urllib.request.Request")
    def test_constructs_correct_request(self, mock_request_cls, mock_urlopen):
        """Push constructs POST to /api/v1/git/push with correct payload."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = _push_via_gateway(
            "/path/to/worktree",
            "tok-123",
            "http://egg-gateway:9848",
            "egg/my-branch",
        )

        assert result is True
        # Verify the Request was constructed with correct URL
        call_args = mock_request_cls.call_args
        assert call_args[0][0] == "http://egg-gateway:9848/api/v1/git/push"

    @patch("urllib.request.urlopen")
    def test_returns_false_on_exception(self, mock_urlopen):
        """Push returns False on network errors."""
        mock_urlopen.side_effect = Exception("Connection refused")

        result = _push_via_gateway(
            "/path/to/worktree",
            "tok-123",
            "http://egg-gateway:9848",
            "egg/my-branch",
        )
        assert result is False


class TestPushBranchDetection:
    """Tests for branch name detection during auto-push."""

    @patch("post_agent_commit._push_via_gateway", return_value=True)
    @patch("post_agent_commit.subprocess.run")
    def test_branch_name_from_rev_parse(self, mock_run, mock_push, tmp_path):
        """Branch name for push comes from rev-parse --abbrev-ref HEAD."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=" M file.py\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="sha123\n", stderr=""),
            # rev-parse --abbrev-ref HEAD
            MagicMock(returncode=0, stdout="egg/issue-644\n", stderr=""),
        ]
        auto_commit_worktree(
            str(tmp_path),
            container_id="c1",
            session_token="tok",
            gateway_url="http://gw:9848",
        )
        mock_push.assert_called_once_with(
            str(tmp_path), "tok", "http://gw:9848", "egg/issue-644"
        )

    @patch("post_agent_commit._push_via_gateway", return_value=True)
    @patch("post_agent_commit.subprocess.run")
    def test_no_push_if_rev_parse_fails(self, mock_run, mock_push, tmp_path):
        """If rev-parse --abbrev-ref fails, push is not attempted."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=" M file.py\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="sha123\n", stderr=""),
            # rev-parse --abbrev-ref HEAD fails
            MagicMock(returncode=128, stdout="", stderr="not a git repo"),
        ]
        result = auto_commit_worktree(
            str(tmp_path),
            container_id="c1",
            session_token="tok",
            gateway_url="http://gw:9848",
        )
        assert result == "sha123"
        mock_push.assert_not_called()

    @patch("post_agent_commit._push_via_gateway", return_value=True)
    @patch("post_agent_commit.subprocess.run")
    def test_no_push_if_branch_empty(self, mock_run, mock_push, tmp_path):
        """If branch name is empty string, push is not attempted."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=" M file.py\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="sha123\n", stderr=""),
            # rev-parse returns empty
            MagicMock(returncode=0, stdout="\n", stderr=""),
        ]
        result = auto_commit_worktree(
            str(tmp_path),
            container_id="c1",
            session_token="tok",
            gateway_url="http://gw:9848",
        )
        assert result == "sha123"
        mock_push.assert_not_called()


class TestParseChangedFilesExtended:
    """Extended tests for _parse_changed_files()."""

    def test_deleted_file(self):
        assert _parse_changed_files(" D deleted.py\n") == ["deleted.py"]

    def test_added_file(self):
        assert _parse_changed_files("A  added.py\n") == ["added.py"]

    def test_mixed_statuses(self):
        output = " M modified.py\n D deleted.py\nA  added.py\n?? untracked.py\n"
        result = _parse_changed_files(output)
        assert len(result) == 4

    def test_file_with_spaces(self):
        result = _parse_changed_files(" M path with spaces/file.py\n")
        assert result == ["path with spaces/file.py"]

    def test_file_in_deep_directory(self):
        result = _parse_changed_files(" M a/b/c/d/e/f.py\n")
        assert result == ["a/b/c/d/e/f.py"]

    def test_rename_with_directories(self):
        result = _parse_changed_files("R  old/path.py -> new/path.py\n")
        assert result == ["new/path.py"]
