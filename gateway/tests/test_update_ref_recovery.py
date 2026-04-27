"""Tests for the detached-HEAD recovery primitive (issue #2162).

Covers:
- ``git update-ref`` is allowed only when scoped to ``refs/heads/<assigned_branch>``.
- ``update-ref`` is denied for non-pipeline sessions (no assigned_branch).
- ``update-ref`` is denied when the target ref does not match the assigned branch.
- ``update-ref`` is denied for malformed positional args (too few / too many).
- Disallowed flags (``--stdin``, ``-d``, ``-z``) are rejected by the allowlist.
- A successful ``git commit`` on detached HEAD in a pipeline session
  surfaces a recovery hint pointing at the ``update-ref`` command.
"""

import json
import sys
from unittest.mock import MagicMock, patch

import auth
import pytest
import session_manager as session_manager_module
from private_repo_policy import PrivateRepoPolicyResult
from session_manager import SessionValidationResult

import gateway

TEST_LAUNCHER_SECRET = "test-launcher-secret-12345"


@pytest.fixture
def client():
    gateway.app.config["TESTING"] = True
    with gateway.app.test_client() as client:
        yield client


def _make_session(assigned_branch, phase="implement", agent_role="coder"):
    mock_session = MagicMock()
    mock_session.mode = "private"
    mock_session.container_id = "test-container"
    mock_session.expires_at = None
    mock_session.phase = phase
    mock_session.agent_role = agent_role
    mock_session.assigned_branch = assigned_branch
    mock_session.pipeline_id = "issue-42" if assigned_branch else None
    mock_session.last_branch = assigned_branch
    mock_session.checkpoint_repo = None
    mock_session.last_repo_path = None
    return mock_session


def _setup_auth(session):
    mock_result = SessionValidationResult(valid=True, session=session)
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
    current_sm = sys.modules.get("session_manager", session_manager_module)
    return (
        {"Authorization": "Bearer test-session-token"},
        mock_result,
        mock_policy_result,
        current_sm,
    )


def _execute(client, headers, args):
    return client.post(
        "/api/v1/git/execute",
        json={
            "repo_path": "/home/egg/repos/myrepo",
            "operation": "update-ref",
            "args": args,
        },
        headers=headers,
    )


class TestUpdateRefScope:
    """update-ref is restricted to refs/heads/<assigned_branch>."""

    @pytest.fixture
    def auth_with_branch(self):
        return _setup_auth(_make_session("egg/issue-42-coder/work"))

    @pytest.fixture
    def auth_without_branch(self):
        return _setup_auth(_make_session(None))

    def test_update_ref_to_assigned_branch_allowed(self, client, auth_with_branch):
        """update-ref refs/heads/<assigned> <sha> reaches subprocess."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/worktree/path"),
            patch("gateway.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            response = _execute(
                client,
                headers,
                ["refs/heads/egg/issue-42-coder/work", "deadbeef"],
            )
            assert response.status_code == 200

    def test_update_ref_no_deref_injected_server_side(self, client, auth_with_branch):
        """The gateway force-prepends ``--no-deref`` so symref-following never applies."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/worktree/path"),
            patch("gateway.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            response = _execute(
                client,
                headers,
                ["refs/heads/egg/issue-42-coder/work", "deadbeef"],
            )
            assert response.status_code == 200
            # Verify the actual subprocess call includes --no-deref before the
            # positional args. ``git_cmd`` prepends additional ``-c`` config
            # flags, so just assert presence + ordering relative to "update-ref".
            assert mock_run.call_count == 1
            called_cmd = list(mock_run.call_args[0][0])
            update_ref_idx = called_cmd.index("update-ref")
            assert "--no-deref" in called_cmd[update_ref_idx:]
            no_deref_idx = called_cmd.index("--no-deref", update_ref_idx)
            assert no_deref_idx == update_ref_idx + 1
            # Positional args still come through after the injected flag.
            assert called_cmd[no_deref_idx + 1] == "refs/heads/egg/issue-42-coder/work"
            assert called_cmd[no_deref_idx + 2] == "deadbeef"

    def test_update_ref_user_passed_no_deref_rejected(self, client, auth_with_branch):
        """User-passed ``--no-deref`` is redundant (gateway injects it) and rejected."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
        ):
            response = _execute(
                client,
                headers,
                ["--no-deref", "refs/heads/egg/issue-42-coder/work", "deadbeef"],
            )
            # 400 from arg validation: --no-deref is no longer in the allowlist
            # because the gateway always injects it.
            assert response.status_code == 400
            # Error message must name the flag so the agent can self-correct
            # without escalating.
            data = json.loads(response.data)
            assert "--no-deref" in data.get("message", "")

    def test_update_ref_with_oldvalue_allowed(self, client, auth_with_branch):
        """The optional <oldvalue> third positional is accepted and reaches subprocess."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/worktree/path"),
            patch("gateway.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            response = _execute(
                client,
                headers,
                ["refs/heads/egg/issue-42-coder/work", "deadbeef", "cafef00d"],
            )
            assert response.status_code == 200
            # All three positional args (ref, newvalue, oldvalue) must reach
            # the subprocess unchanged — `update-ref` uses <oldvalue> as a
            # CAS guard and silently dropping it would change semantics.
            assert mock_run.call_count == 1
            called_cmd = list(mock_run.call_args[0][0])
            update_ref_idx = called_cmd.index("update-ref")
            tail = called_cmd[update_ref_idx:]
            assert "refs/heads/egg/issue-42-coder/work" in tail
            assert "deadbeef" in tail
            assert "cafef00d" in tail
            # Order is preserved: ref, newvalue, oldvalue.
            ref_idx = tail.index("refs/heads/egg/issue-42-coder/work")
            assert tail[ref_idx + 1] == "deadbeef"
            assert tail[ref_idx + 2] == "cafef00d"

    def test_update_ref_to_other_branch_blocked(self, client, auth_with_branch):
        """update-ref against any other ref is rejected with 403."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
        ):
            response = _execute(
                client,
                headers,
                ["refs/heads/main", "deadbeef"],
            )
            assert response.status_code == 403
            data = json.loads(response.data)
            msg = data.get("message", "")
            assert "refs/heads/main" in msg
            assert "egg/issue-42-coder/work" in msg

    def test_update_ref_to_remote_ref_blocked(self, client, auth_with_branch):
        """update-ref against refs/remotes/* is rejected (not your branch)."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
        ):
            response = _execute(
                client,
                headers,
                ["refs/remotes/origin/main", "deadbeef"],
            )
            assert response.status_code == 403

    def test_update_ref_with_no_assigned_branch_blocked(self, client, auth_without_branch):
        """Sessions without assigned_branch cannot use update-ref at all."""
        headers, mock_result, mock_policy, current_sm = auth_without_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
        ):
            response = _execute(
                client,
                headers,
                ["refs/heads/main", "deadbeef"],
            )
            assert response.status_code == 403
            data = json.loads(response.data)
            assert "pipeline session" in data.get("message", "").lower()

    def test_update_ref_with_too_few_args_blocked(self, client, auth_with_branch):
        """update-ref with only one positional arg is rejected."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
        ):
            response = _execute(
                client,
                headers,
                ["refs/heads/egg/issue-42-coder/work"],
            )
            assert response.status_code == 403

    def test_update_ref_with_too_many_args_blocked(self, client, auth_with_branch):
        """update-ref with more than three positional args is rejected."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
        ):
            response = _execute(
                client,
                headers,
                [
                    "refs/heads/egg/issue-42-coder/work",
                    "deadbeef",
                    "cafef00d",
                    "extra",
                ],
            )
            assert response.status_code == 403

    def test_update_ref_stdin_flag_blocked_by_allowlist(self, client, auth_with_branch):
        """--stdin is not in the allowed_flags and is rejected during arg validation."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
        ):
            response = _execute(client, headers, ["--stdin"])
            # 400 from arg validation (flag not allowed).
            assert response.status_code == 400
            # Error message must name the flag so the agent can self-correct
            # without escalating.
            data = json.loads(response.data)
            assert "--stdin" in data.get("message", "")

    def test_update_ref_delete_flag_blocked_by_allowlist(self, client, auth_with_branch):
        """-d (delete ref) is not in allowed_flags and is rejected during arg validation."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
        ):
            response = _execute(
                client,
                headers,
                ["-d", "refs/heads/egg/issue-42-coder/work"],
            )
            assert response.status_code == 400
            data = json.loads(response.data)
            assert "-d" in data.get("message", "")


class TestDetachedHeadCommitHint:
    """git commit on detached HEAD surfaces a recovery hint in stderr."""

    @pytest.fixture
    def auth_pipeline(self):
        return _setup_auth(_make_session("egg/issue-42-coder/work"))

    def _make_commit_subprocess_side_effect(self, head_detached: bool):
        """Return a subprocess.run side_effect that simulates the commit flow.

        Order of subprocess calls inside git_execute for a `commit`:
          1. ``git diff --cached --name-only``     (phase-validation staged files)
          2. ``git rev-parse HEAD``                 (commit observer before-snapshot)
          3. The actual ``git commit ...`` invocation.
          4. ``git rev-parse HEAD``                 (commit observer after-snapshot,
             only if observe_after_git_execute is callable — patched out below)
          5. ``git symbolic-ref --quiet HEAD``      (detached-HEAD detection)
        We mock all of these as success and only flip the return code of the
        symbolic-ref call to simulate detached vs attached HEAD.
        """

        def side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            cmd_str = " ".join(str(c) for c in cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            if "diff" in cmd_str and "--cached" in cmd_str:
                # No staged files to validate against phase restrictions
                result.stdout = ""
            elif "symbolic-ref" in cmd_str:
                result.returncode = 1 if head_detached else 0
                result.stdout = "" if head_detached else "refs/heads/egg/issue-42-coder/work\n"
            else:
                result.stdout = "ok"
            return result

        return side_effect

    def test_commit_on_detached_head_includes_hint(self, client, auth_pipeline):
        """Hint appears in stderr when HEAD is detached after a successful commit."""
        headers, mock_result, mock_policy, current_sm = auth_pipeline

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/worktree/path"),
            patch.object(gateway, "_lookup_commit_observer_fn", return_value=None),
            patch(
                "gateway.subprocess.run",
                side_effect=self._make_commit_subprocess_side_effect(head_detached=True),
            ),
        ):
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "commit",
                    "args": ["-m", "wip"],
                },
                headers=headers,
            )
            assert response.status_code == 200
            data = json.loads(response.data)
            stderr = data.get("data", {}).get("stderr", "") or ""
            assert "HEAD is detached" in stderr
            assert "git update-ref refs/heads/egg/issue-42-coder/work HEAD" in stderr

    def test_commit_on_attached_head_no_hint(self, client, auth_pipeline):
        """Hint is absent when HEAD is on a branch."""
        headers, mock_result, mock_policy, current_sm = auth_pipeline

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/worktree/path"),
            patch.object(gateway, "_lookup_commit_observer_fn", return_value=None),
            patch(
                "gateway.subprocess.run",
                side_effect=self._make_commit_subprocess_side_effect(head_detached=False),
            ),
        ):
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "commit",
                    "args": ["-m", "real"],
                },
                headers=headers,
            )
            assert response.status_code == 200
            data = json.loads(response.data)
            stderr = data.get("data", {}).get("stderr", "") or ""
            assert "HEAD is detached" not in stderr

    def test_commit_on_detached_head_no_assigned_branch_no_hint(self, client):
        """Sessions without assigned_branch never see the hint (non-pipeline)."""
        # Use a session with assigned_branch=None to simulate non-pipeline mode.
        headers, mock_result, mock_policy, current_sm = _setup_auth(_make_session(None))

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/worktree/path"),
            patch.object(gateway, "_lookup_commit_observer_fn", return_value=None),
            patch(
                "gateway.subprocess.run",
                side_effect=self._make_commit_subprocess_side_effect(head_detached=True),
            ),
        ):
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "commit",
                    "args": ["-m", "wip"],
                },
                headers=headers,
            )
            assert response.status_code == 200
            data = json.loads(response.data)
            stderr = data.get("data", {}).get("stderr", "") or ""
            # Without an assigned_branch the gateway has no recovery target to
            # name, so the hint must not fire — even though HEAD is detached.
            assert "HEAD is detached" not in stderr
            assert "update-ref" not in stderr

    def test_commit_on_detached_head_subprocess_error_no_hint(self, client, auth_pipeline):
        """If symbolic-ref times out or raises OSError, no hint is surfaced."""
        headers, mock_result, mock_policy, current_sm = auth_pipeline

        def side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            cmd_str = " ".join(str(c) for c in cmd)
            if "symbolic-ref" in cmd_str:
                # Simulate the kernel killing the subprocess with EAGAIN, etc.
                raise OSError("simulated symbolic-ref failure")
            result = MagicMock()
            result.returncode = 0
            result.stdout = "ok" if "diff" not in cmd_str else ""
            result.stderr = ""
            return result

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/worktree/path"),
            patch.object(gateway, "_lookup_commit_observer_fn", return_value=None),
            patch("gateway.subprocess.run", side_effect=side_effect),
        ):
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "commit",
                    "args": ["-m", "wip"],
                },
                headers=headers,
            )
            # The commit itself succeeded; only the post-hoc check failed.
            # The hint helper swallows the error and returns no hint.
            assert response.status_code == 200
            data = json.loads(response.data)
            stderr = data.get("data", {}).get("stderr", "") or ""
            assert "HEAD is detached" not in stderr

    def test_commit_on_corrupt_repo_no_hint(self, client, auth_pipeline):
        """A corrupt repo (returncode 128, fatal stderr) must not get a misleading hint."""
        headers, mock_result, mock_policy, current_sm = auth_pipeline

        def side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            cmd_str = " ".join(str(c) for c in cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            if "diff" in cmd_str and "--cached" in cmd_str:
                result.stdout = ""
            elif "symbolic-ref" in cmd_str:
                # `git symbolic-ref --quiet HEAD` against a corrupt or missing
                # .git directory exits 128 with a fatal: line on stderr.  This
                # is *not* detached HEAD; the hint must stay silent.
                result.returncode = 128
                result.stdout = ""
                result.stderr = (
                    "fatal: not a git repository (or any of the parent directories): .git\n"
                )
            else:
                result.stdout = "ok"
            return result

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/worktree/path"),
            patch.object(gateway, "_lookup_commit_observer_fn", return_value=None),
            patch("gateway.subprocess.run", side_effect=side_effect),
        ):
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "commit",
                    "args": ["-m", "wip"],
                },
                headers=headers,
            )
            assert response.status_code == 200
            data = json.loads(response.data)
            stderr = data.get("data", {}).get("stderr", "") or ""
            assert "HEAD is detached" not in stderr
            assert "update-ref" not in stderr

    def test_commit_failure_on_detached_head_includes_hint(self, client, auth_pipeline):
        """Hint is surfaced even when the commit *fails* (e.g. mid-rebase conflict)."""
        headers, mock_result, mock_policy, current_sm = auth_pipeline

        def side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            cmd_str = " ".join(str(c) for c in cmd)
            result = MagicMock()
            result.stdout = ""
            result.stderr = ""
            if "diff" in cmd_str and "--cached" in cmd_str:
                result.returncode = 0
                result.stdout = ""
            elif "symbolic-ref" in cmd_str:
                # Detached HEAD: rc=1, no stdout, no stderr.
                result.returncode = 1
                result.stdout = ""
                result.stderr = ""
            elif "commit" in cmd_str:
                # The actual `git commit` call fails (e.g. nothing to commit
                # because conflict markers remain unresolved).
                result.returncode = 1
                result.stderr = "nothing to commit, working tree clean\n"
            else:
                result.returncode = 0
                result.stdout = "ok"
            return result

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/worktree/path"),
            patch.object(gateway, "_lookup_commit_observer_fn", return_value=None),
            patch("gateway.subprocess.run", side_effect=side_effect),
        ):
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "commit",
                    "args": ["-m", "wip"],
                },
                headers=headers,
            )
            # The commit failed, but the hint must still appear so the agent
            # doesn't burn turns guessing why their commit didn't land.
            assert response.status_code == 500
            data = json.loads(response.data)
            stderr = data.get("data", {}).get("stderr", "") or ""
            assert "HEAD is detached" in stderr
            assert "git update-ref refs/heads/egg/issue-42-coder/work HEAD" in stderr

    def test_commit_symbolic_ref_nonempty_stderr_suppresses_hint(self, client, auth_pipeline):
        """rc=1 with empty stdout but non-empty stderr suppresses the hint and logs at debug.

        Locks in the contract added in the third review pass: if a future git
        version (or the host environment) leaks a config-deprecation warning
        onto stderr, the hint must stay silent — telling the agent to run
        ``update-ref`` against an unclear HEAD state would be misleading — and
        the suppression must be observable via a debug-level log so the
        missing hint is debuggable rather than invisible.
        """
        headers, mock_result, mock_policy, current_sm = auth_pipeline

        def side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            cmd_str = " ".join(str(c) for c in cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            if "diff" in cmd_str and "--cached" in cmd_str:
                result.stdout = ""
            elif "symbolic-ref" in cmd_str:
                # rc=1 (would normally trigger the hint) but stderr is non-empty:
                # ambiguous state, hint must stay silent.
                result.returncode = 1
                result.stdout = ""
                result.stderr = "warning: deprecated config option foo.bar\n"
            else:
                result.stdout = "ok"
            return result

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/worktree/path"),
            patch.object(gateway, "_lookup_commit_observer_fn", return_value=None),
            patch("gateway.subprocess.run", side_effect=side_effect),
            patch.object(gateway, "logger") as mock_logger,
        ):
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "commit",
                    "args": ["-m", "wip"],
                },
                headers=headers,
            )
            assert response.status_code == 200
            data = json.loads(response.data)
            stderr = data.get("data", {}).get("stderr", "") or ""
            # No hint surfaced — the stderr-non-empty branch suppresses it.
            assert "HEAD is detached" not in stderr
            assert "update-ref" not in stderr
            # The suppression event was logged at debug so the missing hint
            # is debuggable rather than silent.
            debug_events = [
                call.args[0] if call.args else call.kwargs.get("event", "")
                for call in mock_logger.debug.call_args_list
            ]
            assert "detached_head_hint_suppressed_stderr" in debug_events, (
                f"Expected detached_head_hint_suppressed_stderr debug log, got: {debug_events}"
            )
