"""Tests for auto-filter push behavior in the gateway.

When agents push commits containing files outside their role's allowed set,
the gateway should auto-filter disallowed files from the push instead of
blocking the entire push. This test module validates the auto-filter logic
in both the gateway endpoint and the _execute_filtered_push helper.

Covers:
- Auto-filter mode (enforce=true with mixed allowed/blocked files)
- All-blocked edge case (soft success, nothing to push)
- Filtered push success response format
- Filtered push failure handling (rollback)
- _execute_filtered_push helper: commit rewriting, unstaging, push
- _execute_filtered_push: new branch vs existing branch handling
- _execute_filtered_push: rollback on failure scenarios
- Backward compatibility: warn-only mode unchanged
- Backward compatibility: no-role sessions unaffected

Related: issue #1470 — Gateway auto-filter disallowed files on push
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
import session_manager
from phase_filter import FileRestrictionResult
from policy import PolicyResult
from private_repo_policy import PrivateRepoPolicyResult
from session_manager import SessionValidationResult

import gateway


@pytest.fixture
def client():
    """Create test client for Flask app."""
    gateway.app.config["TESTING"] = True
    with gateway.app.test_client() as client:
        yield client


def _make_session(role="coder"):
    """Create a mock session with the given agent role."""
    mock_session = MagicMock()
    mock_session.mode = "public"
    mock_session.container_id = "test-container"
    mock_session.expires_at = None
    mock_session.agent_role = role
    mock_session.phase = None
    return mock_session


def _auto_filter_push_context(
    mock_session,
    changed_files=None,
    agent_blocked=True,
    allowed_files=None,
    blocked_files=None,
):
    """Return context managers for auto-filter push tests.

    Args:
        mock_session: Mock session object.
        changed_files: Files reported as changed. Defaults to mixed set.
        agent_blocked: If True, check_agent_restrictions returns blocked.
        allowed_files: Files the role can push (for filter_agent_files).
        blocked_files: Files the role cannot push (for filter_agent_files).
    """
    import auth

    if changed_files is None:
        changed_files = ["src/main.py", "tests/test_foo.py"]
    if allowed_files is None:
        allowed_files = ["src/main.py"]
    if blocked_files is None:
        blocked_files = ["tests/test_foo.py"]

    mock_result = SessionValidationResult(valid=True, session=mock_session)
    mock_policy_result = PrivateRepoPolicyResult(
        allowed=True,
        reason="Test mode",
        visibility="public",
    )

    auth._session_manager = None
    auth._rate_limiter = None
    if "gateway.auth" in sys.modules:
        sys.modules["gateway.auth"]._session_manager = None
        sys.modules["gateway.auth"]._rate_limiter = None

    current_sm = sys.modules.get("session_manager", session_manager)

    def run_side_effect(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        if isinstance(cmd, (list, tuple)):
            cmd_str = " ".join(str(c) for c in cmd)
        else:
            cmd_str = str(cmd)

        if "remote" in cmd_str and "get-url" in cmd_str:
            result.stdout = "https://github.com/owner/repo.git\n"
        elif "branch" in cmd_str and "--show-current" in cmd_str:
            result.stdout = "egg-feature\n"
        elif "ls-remote" in cmd_str:
            result.stdout = "abc1234\trefs/heads/egg-feature\n"
        elif "rev-parse" in cmd_str and "HEAD" in cmd_str:
            result.stdout = "deadbeef1234567890abcdef\n"
        elif "merge-base" in cmd_str:
            result.stdout = "basecommit1234567890abcdef\n"
        elif "log" in cmd_str and "--format=%B" in cmd_str:
            result.stdout = "Original commit message\n"
        elif "reset" in cmd_str:
            result.stdout = ""
        elif "commit" in cmd_str:
            result.stdout = "1 file changed\n"
        elif "push" in cmd_str:
            result.stdout = "Everything up-to-date\n"
        else:
            result.stdout = ""
        return result

    if agent_blocked:
        agent_result = FileRestrictionResult.block(
            message="Coder cannot modify test files",
            role="coder",
            blocked_files=blocked_files,
            blocked_reason="Test files belong to tester role",
        )
    else:
        agent_result = FileRestrictionResult.allow("All files allowed for role")

    return (
        patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
        patch.object(gateway, "check_private_repo_access", return_value=mock_policy_result),
        patch("subprocess.run", side_effect=run_side_effect),
        patch.object(
            gateway,
            "get_policy_engine",
            return_value=MagicMock(
                check_branch_ownership=MagicMock(
                    return_value=PolicyResult(
                        allowed=True,
                        reason="OK",
                        details={"branch": "egg-feature"},
                    )
                ),
            ),
        ),
        patch.object(gateway, "get_token_for_repo", return_value=("test-token", "bot", "")),
        patch.object(gateway, "get_changed_files_in_push", return_value=(changed_files, None)),
        patch.object(
            gateway, "check_file_restrictions", return_value=FileRestrictionResult.allow()
        ),
        patch.object(gateway, "check_agent_restrictions", return_value=agent_result),
        patch.object(gateway, "filter_agent_files", return_value=(allowed_files, blocked_files)),
    )


def _do_push(client):
    """Send a push request and return the response."""
    return client.post(
        "/api/v1/git/push",
        headers={"Authorization": "Bearer test-session-token"},
        data=json.dumps(
            {
                "repo_path": "/home/egg/repos/test-repo",
                "remote": "origin",
                "refspec": "egg-feature",
            }
        ),
        content_type="application/json",
    )


def _enter_patches(patches):
    """Enter all context managers in the patches tuple."""
    contexts = []
    for p in patches:
        ctx = p.__enter__()
        contexts.append(ctx)
    return contexts


def _exit_patches(patches):
    """Exit all context managers in the patches tuple."""
    for p in reversed(patches):
        p.__exit__(None, None, None)


class TestAutoFilterPushEnforceMode:
    """Auto-filter mode replaces the old blocking behavior in enforce mode."""

    def test_mixed_files_returns_success(self, client):
        """Push with mixed allowed/blocked files succeeds with filtering."""
        session = _make_session("coder")
        patches = _auto_filter_push_context(session)

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                # Should succeed with filtered push, not 403
                assert response.status_code == 200
                data = json.loads(response.data)
                assert data.get("filtered") is True or "filtered" in str(data)

    def test_all_blocked_returns_soft_success(self, client):
        """When ALL files are blocked, return soft success (nothing to push)."""
        session = _make_session("coder")
        patches = _auto_filter_push_context(
            session,
            changed_files=["tests/test_foo.py", "docs/guide.md"],
            agent_blocked=True,
            allowed_files=[],
            blocked_files=["tests/test_foo.py", "docs/guide.md"],
        )

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                assert response.status_code == 200
                data = json.loads(response.data)
                assert (
                    data.get("nothing_to_push") is True
                    or "nothing to push" in data.get("message", "").lower()
                )

    def test_no_violations_pushes_normally(self, client):
        """When no files are blocked, push proceeds normally (no filtering)."""
        session = _make_session("coder")
        patches = _auto_filter_push_context(
            session,
            changed_files=["src/main.py", "gateway/handler.py"],
            agent_blocked=False,
        )

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                assert response.status_code == 200


class TestAutoFilterPushResponseFormat:
    """Verify the response format for auto-filtered pushes."""

    def test_all_blocked_response_includes_excluded_files(self, client):
        """All-blocked response includes excluded file list."""
        session = _make_session("coder")
        blocked = ["tests/test_foo.py", "docs/guide.md"]
        patches = _auto_filter_push_context(
            session,
            changed_files=blocked,
            agent_blocked=True,
            allowed_files=[],
            blocked_files=blocked,
        )

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                data = json.loads(response.data)
                # Response wraps details under "data" key
                inner = data.get("data", data)
                assert "excluded_files" in inner

    def test_all_blocked_response_shows_empty_pushed_files(self, client):
        """All-blocked response shows empty pushed_files list."""
        session = _make_session("coder")
        blocked = ["tests/test_foo.py"]
        patches = _auto_filter_push_context(
            session,
            changed_files=blocked,
            agent_blocked=True,
            allowed_files=[],
            blocked_files=blocked,
        )

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                data = json.loads(response.data)
                inner = data.get("data", data)
                assert inner.get("pushed_files") == [] or inner.get("nothing_to_push") is True


class TestAutoFilterWarnModeUnchanged:
    """Warn-only mode should still allow pushes without filtering."""

    def test_warn_mode_still_allows_push(self, client):
        """With EGG_AGENT_RESTRICTIONS_ENFORCE=false, push proceeds (no filter)."""
        session = _make_session("coder")
        patches = _auto_filter_push_context(session)

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "false"}):
                response = _do_push(client)
                assert response.status_code == 200


class TestAutoFilterNoRoleUnchanged:
    """Sessions without agent_role should push normally."""

    def test_no_role_pushes_normally(self, client):
        """Sessions without agent_role bypass agent restriction checks."""
        session = _make_session(role=None)
        patches = _auto_filter_push_context(session, agent_blocked=False)

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
        ):
            response = _do_push(client)
            assert response.status_code == 200


# ---------------------------------------------------------------------------
# _execute_filtered_push unit tests
# ---------------------------------------------------------------------------


class TestExecuteFilteredPush:
    """Unit tests for _execute_filtered_push helper function."""

    def _make_subprocess_results(
        self,
        head_sha="deadbeef1234567890abcdef",
        merge_base_sha="basecommit1234567890abcdef",
        reset_ok=True,
        unstage_ok=True,
        commit_ok=True,
        push_ok=True,
    ):
        """Create a subprocess.run side_effect for _execute_filtered_push."""

        def side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            cmd_str = " ".join(str(c) for c in cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
            result = MagicMock()
            result.stderr = ""

            if "rev-parse" in cmd_str and "HEAD" in cmd_str:
                result.returncode = 0
                result.stdout = head_sha + "\n"
            elif "merge-base" in cmd_str:
                result.returncode = 0
                result.stdout = merge_base_sha + "\n"
            elif "reset" in cmd_str and "--soft" in cmd_str:
                result.returncode = 0 if reset_ok else 1
                result.stdout = ""
                result.stderr = "" if reset_ok else "reset failed"
            elif "reset" in cmd_str and "HEAD" in cmd_str and "--" in cmd_str:
                # Unstage
                result.returncode = 0 if unstage_ok else 1
                result.stdout = ""
                result.stderr = "" if unstage_ok else "unstage warning"
            elif "reset" in cmd_str and "--hard" in cmd_str:
                # Restore
                result.returncode = 0
                result.stdout = ""
            elif "commit" in cmd_str:
                result.returncode = 0 if commit_ok else 1
                result.stdout = "1 file changed\n" if commit_ok else ""
                result.stderr = "" if commit_ok else "nothing to commit"
            elif "push" in cmd_str:
                result.returncode = 0 if push_ok else 1
                result.stdout = "ok\n" if push_ok else ""
                result.stderr = "" if push_ok else "push rejected"
            else:
                result.returncode = 0
                result.stdout = ""

            return result

        return side_effect

    def test_successful_filtered_push_existing_branch(self):
        """Filtered push to existing branch uses old_ref_sha as reset target."""
        side_effect = self._make_subprocess_results()
        with patch("subprocess.run", side_effect=side_effect):
            success, stdout, stderr = gateway._execute_filtered_push(
                exec_path="/tmp/repo",
                blocked_files=["tests/test_foo.py"],
                cmd=["git", "push", "origin", "egg-feature"],
                branch="egg-feature",
                old_ref_sha="abc123",
                env={"PATH": "/usr/bin"},
                original_msg="Add feature",
            )
            assert success is True
            assert stderr == ""

    def test_successful_filtered_push_new_branch(self):
        """Filtered push to new branch uses merge-base with origin/main."""
        side_effect = self._make_subprocess_results()
        with patch("subprocess.run", side_effect=side_effect):
            success, stdout, stderr = gateway._execute_filtered_push(
                exec_path="/tmp/repo",
                blocked_files=["tests/test_foo.py"],
                cmd=["git", "push", "origin", "egg-feature"],
                branch="egg-feature",
                old_ref_sha="0" * 40,
                env={"PATH": "/usr/bin"},
                original_msg="Add feature",
            )
            assert success is True

    def test_new_branch_with_none_ref(self):
        """old_ref_sha=None is treated as new branch."""
        side_effect = self._make_subprocess_results()
        with patch("subprocess.run", side_effect=side_effect):
            success, _, _ = gateway._execute_filtered_push(
                exec_path="/tmp/repo",
                blocked_files=["tests/test_foo.py"],
                cmd=["git", "push", "origin", "egg-feature"],
                branch="egg-feature",
                old_ref_sha=None,
                env={"PATH": "/usr/bin"},
                original_msg="Add feature",
            )
            assert success is True

    def test_head_parse_failure(self):
        """If rev-parse HEAD fails, return failure immediately."""

        def side_effect(*args, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stdout = ""
            result.stderr = "fatal: not a git repo"
            return result

        with patch("subprocess.run", side_effect=side_effect):
            success, stdout, stderr = gateway._execute_filtered_push(
                exec_path="/tmp/repo",
                blocked_files=["tests/test_foo.py"],
                cmd=["git", "push"],
                branch="main",
                old_ref_sha="abc123",
                env={},
                original_msg="msg",
            )
            assert success is False
            assert "HEAD" in stderr

    def test_soft_reset_failure_restores_head(self):
        """If soft reset fails, original HEAD is restored."""
        side_effect = self._make_subprocess_results(reset_ok=False)
        with patch("subprocess.run", side_effect=side_effect):
            success, _, stderr = gateway._execute_filtered_push(
                exec_path="/tmp/repo",
                blocked_files=["tests/test_foo.py"],
                cmd=["git", "push"],
                branch="egg-feature",
                old_ref_sha="abc123",
                env={},
                original_msg="msg",
            )
            assert success is False
            assert "reset failed" in stderr.lower() or "Soft reset failed" in stderr

    def test_commit_failure_restores_head(self):
        """If filtered commit fails, original HEAD is restored."""
        side_effect = self._make_subprocess_results(commit_ok=False)
        with patch("subprocess.run", side_effect=side_effect):
            success, _, stderr = gateway._execute_filtered_push(
                exec_path="/tmp/repo",
                blocked_files=["tests/test_foo.py"],
                cmd=["git", "push"],
                branch="egg-feature",
                old_ref_sha="abc123",
                env={},
                original_msg="msg",
            )
            assert success is False
            assert "commit" in stderr.lower() or "Filtered commit failed" in stderr

    def test_push_failure_restores_head(self):
        """If push fails, original HEAD is restored."""
        side_effect = self._make_subprocess_results(push_ok=False)
        with patch("subprocess.run", side_effect=side_effect):
            success, _, stderr = gateway._execute_filtered_push(
                exec_path="/tmp/repo",
                blocked_files=["tests/test_foo.py"],
                cmd=["git", "push"],
                branch="egg-feature",
                old_ref_sha="abc123",
                env={},
                original_msg="msg",
            )
            assert success is False
            assert "push rejected" in stderr

    def test_unstage_warning_does_not_fail(self):
        """Unstage warnings don't block the push."""
        side_effect = self._make_subprocess_results(unstage_ok=False)
        with patch("subprocess.run", side_effect=side_effect):
            success, _, _ = gateway._execute_filtered_push(
                exec_path="/tmp/repo",
                blocked_files=["tests/test_foo.py"],
                cmd=["git", "push", "origin", "egg-feature"],
                branch="egg-feature",
                old_ref_sha="abc123",
                env={},
                original_msg="msg",
            )
            # Unstage failure is only a warning, push should still proceed
            assert success is True

    def test_commit_message_includes_auto_filtered_suffix(self):
        """The filtered commit message should include [auto-filtered] suffix."""
        calls = []

        def side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            cmd_str = " ".join(str(c) for c in cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
            calls.append(cmd_str)
            result = MagicMock()
            result.returncode = 0
            result.stdout = "deadbeef\n" if "rev-parse" in cmd_str else ""
            result.stderr = ""
            return result

        with patch("subprocess.run", side_effect=side_effect):
            gateway._execute_filtered_push(
                exec_path="/tmp/repo",
                blocked_files=["tests/test_foo.py"],
                cmd=["git", "push", "origin", "egg-feature"],
                branch="egg-feature",
                old_ref_sha="abc123",
                env={},
                original_msg="Add feature",
            )

        commit_calls = [c for c in calls if "commit" in c and "auto-filtered" in c]
        assert len(commit_calls) == 1
        assert "Add feature [auto-filtered]" in commit_calls[0]

    def test_exception_restores_head(self):
        """On unexpected exception, original HEAD is restored and exception re-raised."""
        call_count = {"n": 0}

        def side_effect(*args, **kwargs):
            call_count["n"] += 1
            cmd = args[0] if args else kwargs.get("args", [])
            cmd_str = " ".join(str(c) for c in cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""

            if "rev-parse" in cmd_str:
                result.stdout = "deadbeef\n"
            elif "reset" in cmd_str and "--soft" in cmd_str:
                raise OSError("disk error")
            elif "reset" in cmd_str and "--hard" in cmd_str:
                result.stdout = ""
            else:
                result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=side_effect):
            with pytest.raises(OSError, match="disk error"):
                gateway._execute_filtered_push(
                    exec_path="/tmp/repo",
                    blocked_files=["tests/test_foo.py"],
                    cmd=["git", "push"],
                    branch="egg-feature",
                    old_ref_sha="abc123",
                    env={},
                    original_msg="msg",
                )

    def test_merge_base_failure_for_new_branch(self):
        """If merge-base fails for new branch, return failure."""

        def side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            cmd_str = " ".join(str(c) for c in cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
            result = MagicMock()
            result.stderr = ""
            result.returncode = 0

            if "rev-parse" in cmd_str:
                result.stdout = "deadbeef\n"
            elif "merge-base" in cmd_str:
                result.returncode = 1
                result.stderr = "fatal: not a valid object"
            else:
                result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=side_effect):
            success, _, stderr = gateway._execute_filtered_push(
                exec_path="/tmp/repo",
                blocked_files=["tests/test_foo.py"],
                cmd=["git", "push"],
                branch="egg-feature",
                old_ref_sha="0" * 40,
                env={},
                original_msg="msg",
            )
            assert success is False
            assert "merge-base" in stderr.lower()

    def test_push_success_restores_blocked_files_to_worktree(self):
        """After successful push, blocked files are restored to the working tree."""
        calls = []

        def side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            cmd_str = " ".join(str(c) for c in cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
            calls.append(cmd_str)
            result = MagicMock()
            result.returncode = 0
            result.stdout = "deadbeef1234\n" if "rev-parse" in cmd_str else ""
            result.stderr = ""
            return result

        with patch("subprocess.run", side_effect=side_effect):
            success, _, _ = gateway._execute_filtered_push(
                exec_path="/tmp/repo",
                blocked_files=["tests/test_foo.py"],
                cmd=["git", "push", "origin", "egg-feature"],
                branch="egg-feature",
                old_ref_sha="abc123",
                env={},
                original_msg="msg",
            )

        assert success is True
        # After push succeeds, blocked files should be checked out from the
        # original commit (restored to the working tree) and then unstaged.
        checkout_calls = [c for c in calls if "checkout" in c and "deadbeef1234" in c]
        assert len(checkout_calls) >= 1
        # Blocked files must be unstaged via `git reset HEAD` so they appear
        # as working-tree changes and aren't included in the next commit.
        # There are two reset HEAD calls: one before commit (to unstage
        # blocked files from the index) and one after push (to unstage
        # the restored blocked files from checkout).
        unstage_calls = [
            c for c in calls if "reset" in c and "HEAD" in c and "tests/test_foo.py" in c
        ]
        assert len(unstage_calls) >= 1, f"Expected unstage call(s), got: {unstage_calls}"
        # Verify the post-push unstage happens after the checkout restoration
        last_checkout_idx = max(
            i for i, c in enumerate(calls) if "checkout" in c and "deadbeef1234" in c
        )
        post_push_unstage = [
            i
            for i, c in enumerate(calls)
            if "reset" in c and "HEAD" in c and "tests/test_foo.py" in c and i > last_checkout_idx
        ]
        assert len(post_push_unstage) == 1, (
            "Expected exactly one unstage after checkout restoration"
        )
        # Should NOT do a hard reset to original HEAD (which would diverge
        # local and remote branches)
        reset_hard_calls = [c for c in calls if "reset" in c and "--hard" in c]
        assert len(reset_hard_calls) == 0

    def test_push_success_removes_blocked_deleted_files(self, tmp_path):
        """After successful push, blocked file deletions are restored by removing the file."""
        # Create a file that exists in the filtered commit (not deleted) but
        # was deleted in the original commit.  The checkout from original_head
        # will fail because the file doesn't exist there, so _execute_filtered_push
        # should remove it from the working tree.
        blocked_file = "docs/blocked.md"
        blocked_path = tmp_path / blocked_file
        blocked_path.parent.mkdir(parents=True, exist_ok=True)
        blocked_path.write_text("content")

        calls = []

        def side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            cmd_str = " ".join(str(c) for c in cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
            calls.append(cmd_str)
            result = MagicMock()
            result.returncode = 0
            result.stdout = "deadbeef1234\n" if "rev-parse" in cmd_str else ""
            result.stderr = ""
            # Simulate checkout failure for the blocked file (it was deleted
            # in the original commit, so git checkout original -- file fails).
            if "checkout" in cmd_str and blocked_file in cmd_str:
                result.returncode = 1
                result.stderr = "error: pathspec 'docs/blocked.md' did not match"
            return result

        with patch("subprocess.run", side_effect=side_effect):
            success, _, _ = gateway._execute_filtered_push(
                exec_path=str(tmp_path),
                blocked_files=[blocked_file],
                cmd=["git", "push", "origin", "egg-feature"],
                branch="egg-feature",
                old_ref_sha="abc123",
                env={},
                original_msg="msg",
            )

        assert success is True
        # The file should have been removed from the working tree
        assert not blocked_path.exists(), "Blocked deleted file should be removed from worktree"


class TestAutoFilterPushFailureHandling:
    """Test that filtered push failures return appropriate errors."""

    def test_filtered_push_failure_returns_500(self, client):
        """When filtered push fails, return 500 with error details."""
        session = _make_session("coder")

        # Set up context where auto-filter is triggered
        patches = _auto_filter_push_context(
            session,
            changed_files=["src/main.py", "tests/test_foo.py"],
            agent_blocked=True,
            allowed_files=["src/main.py"],
            blocked_files=["tests/test_foo.py"],
        )

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
        ):
            # Make _execute_filtered_push return failure
            with patch.object(
                gateway,
                "_execute_filtered_push",
                return_value=(False, "", "push rejected by remote"),
            ):
                with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                    response = _do_push(client)
                    assert response.status_code == 500
                    data = json.loads(response.data)
                    assert (
                        "failed" in data.get("message", "").lower()
                        or "error" in data.get("message", "").lower()
                    )
