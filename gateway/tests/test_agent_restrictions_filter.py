"""Tests for agent-role auto-filter behavior on push.

When an agent pushes commits containing files outside their role's allowed set,
the gateway auto-filters: it rewrites the push to include only allowed files
and returns a 200 with details about excluded files.  Blocked files remain as
uncommitted changes in the agent's worktree.
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


def _make_coder_session():
    """Create a mock session with 'coder' agent role."""
    mock_session = MagicMock()
    mock_session.mode = "public"
    mock_session.container_id = "test-container"
    mock_session.expires_at = None
    mock_session.agent_role = "coder"
    mock_session.phase = None
    return mock_session


def _push_context(
    mock_session,
    changed_files=None,
    agent_blocked=True,
    filter_result=None,
):
    """Return a context manager tuple that sets up all mocking for a push request.

    Args:
        mock_session: Mock session object.
        changed_files: List of changed file paths.  Defaults to mixed coder+tester files.
        agent_blocked: If True, check_agent_restrictions returns a blocked result.
        filter_result: Optional (allowed, blocked) tuple for filter_agent_files.
    """
    import auth

    if changed_files is None:
        changed_files = ["gateway/gateway.py", "tests/test_foo.py"]

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
        if "remote" in cmd and "get-url" in cmd:
            result.stdout = "https://github.com/owner/repo.git\n"
        elif "branch" in cmd and "--show-current" in cmd:
            result.stdout = "egg-feature\n"
        elif "push" in cmd:
            result.stdout = "Everything up-to-date\n"
        elif "log" in cmd and "--format=%B" in cmd:
            result.stdout = "Original commit message\n"
        elif "ls-remote" in cmd:
            result.stdout = "abc1234\trefs/heads/egg-feature\n"
        elif "rev-parse" in cmd and "HEAD" in cmd:
            result.stdout = "deadbeef1234567890\n"
        elif "reset" in cmd:
            result.stdout = ""
        elif "commit" in cmd:
            result.stdout = "[egg-feature abc1235] test commit\n"
        elif "merge-base" in cmd:
            result.stdout = "mergebase1234\n"
        else:
            result.stdout = ""
        return result

    if agent_blocked:
        agent_result = FileRestrictionResult.block(
            message="Coder cannot modify test files",
            role="coder",
            blocked_files=["tests/test_foo.py"],
            blocked_reason="Test files belong to tester role",
        )
    else:
        agent_result = FileRestrictionResult.allow("All files allowed for role")

    # Default filter result: first file allowed, second blocked
    if filter_result is None:
        filter_result = (["gateway/gateway.py"], ["tests/test_foo.py"])

    patches = (
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
        patch.object(gateway, "filter_agent_files", return_value=filter_result),
    )
    return patches


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


class TestMixedPushAutoFilter:
    """When a push has both allowed and blocked files, it auto-filters."""

    def test_mixed_push_returns_200_with_filtered_true(self, client):
        """Mixed push succeeds with 200 and filtered=true."""
        session = _make_coder_session()
        patches = _push_context(session, agent_blocked=True)

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
                assert data["data"]["filtered"] is True

    def test_mixed_push_reports_excluded_files(self, client):
        """Mixed push response lists excluded files."""
        session = _make_coder_session()
        patches = _push_context(session, agent_blocked=True)

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
                assert "tests/test_foo.py" in data["data"]["excluded_files"]

    def test_mixed_push_reports_pushed_files(self, client):
        """Mixed push response lists pushed files."""
        session = _make_coder_session()
        patches = _push_context(session, agent_blocked=True)

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
                assert "gateway/gateway.py" in data["data"]["pushed_files"]


class TestAllBlockedPush:
    """When all files are blocked, return soft 200 with nothing-to-push."""

    def test_all_blocked_returns_200(self, client):
        """All-blocked push returns 200 (soft success)."""
        session = _make_coder_session()
        patches = _push_context(
            session,
            changed_files=["tests/test_foo.py", "tests/test_bar.py"],
            agent_blocked=True,
            filter_result=([], ["tests/test_foo.py", "tests/test_bar.py"]),
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
                assert data["data"]["nothing_to_push"] is True
                assert data["data"]["filtered"] is True
                assert len(data["data"]["excluded_files"]) == 2


class TestPushFailureRestoresHead:
    """When the filtered push fails, original HEAD is restored."""

    def test_push_failure_returns_500(self, client):
        """Push failure after rewrite returns 500."""
        session = _make_coder_session()

        def run_side_effect_with_push_failure(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if "remote" in cmd and "get-url" in cmd:
                result.stdout = "https://github.com/owner/repo.git\n"
            elif "branch" in cmd and "--show-current" in cmd:
                result.stdout = "egg-feature\n"
            elif "push" in cmd:
                # Push fails
                result.returncode = 1
                result.stdout = ""
                result.stderr = "remote: push rejected\n"
            elif "log" in cmd and "--format=%B" in cmd:
                result.stdout = "Original commit message\n"
            elif "ls-remote" in cmd:
                result.stdout = "abc1234\trefs/heads/egg-feature\n"
            elif "rev-parse" in cmd and "HEAD" in cmd:
                result.stdout = "deadbeef1234567890\n"
            elif "reset" in cmd:
                result.stdout = ""
            elif "commit" in cmd:
                result.stdout = "[egg-feature abc1235] test commit\n"
            else:
                result.stdout = ""
            return result

        import auth

        mock_result = SessionValidationResult(valid=True, session=session)
        mock_policy_result = PrivateRepoPolicyResult(
            allowed=True, reason="Test mode", visibility="public"
        )
        auth._session_manager = None
        auth._rate_limiter = None
        if "gateway.auth" in sys.modules:
            sys.modules["gateway.auth"]._session_manager = None
            sys.modules["gateway.auth"]._rate_limiter = None
        current_sm = sys.modules.get("session_manager", session_manager)

        agent_result = FileRestrictionResult.block(
            message="Coder cannot modify test files",
            role="coder",
            blocked_files=["tests/test_foo.py"],
            blocked_reason="Test files belong to tester role",
        )

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy_result),
            patch("subprocess.run", side_effect=run_side_effect_with_push_failure),
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
            patch.object(
                gateway,
                "get_changed_files_in_push",
                return_value=(["gateway/gateway.py", "tests/test_foo.py"], None),
            ),
            patch.object(
                gateway, "check_file_restrictions", return_value=FileRestrictionResult.allow()
            ),
            patch.object(gateway, "check_agent_restrictions", return_value=agent_result),
            patch.object(
                gateway,
                "filter_agent_files",
                return_value=(["gateway/gateway.py"], ["tests/test_foo.py"]),
            ),
            patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}),
        ):
            response = _do_push(client)
            assert response.status_code == 500
            data = json.loads(response.data)
            assert "Filtered push failed" in data["message"]


class TestNewBranchFiltering:
    """Auto-filtering works for new branch pushes (no remote tip)."""

    def test_new_branch_push_filtered(self, client):
        """New-branch push with mixed files succeeds with filtering."""
        session = _make_coder_session()

        def run_side_effect_new_branch(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if "remote" in cmd and "get-url" in cmd:
                result.stdout = "https://github.com/owner/repo.git\n"
            elif "branch" in cmd and "--show-current" in cmd:
                result.stdout = "egg-new-feature\n"
            elif "push" in cmd:
                result.stdout = "Everything up-to-date\n"
            elif "log" in cmd and "--format=%B" in cmd:
                result.stdout = "Original commit message\n"
            elif "ls-remote" in cmd:
                # No remote branch exists
                result.stdout = ""
            elif "rev-parse" in cmd and "HEAD" in cmd:
                result.stdout = "deadbeef1234567890\n"
            elif "reset" in cmd:
                result.stdout = ""
            elif "commit" in cmd:
                result.stdout = "[egg-new-feature abc1235] test commit\n"
            elif "merge-base" in cmd:
                result.stdout = "mergebase1234\n"
            else:
                result.stdout = ""
            return result

        import auth

        mock_result = SessionValidationResult(valid=True, session=session)
        mock_policy_result = PrivateRepoPolicyResult(
            allowed=True, reason="Test mode", visibility="public"
        )
        auth._session_manager = None
        auth._rate_limiter = None
        if "gateway.auth" in sys.modules:
            sys.modules["gateway.auth"]._session_manager = None
            sys.modules["gateway.auth"]._rate_limiter = None
        current_sm = sys.modules.get("session_manager", session_manager)

        agent_result = FileRestrictionResult.block(
            message="Coder cannot modify test files",
            role="coder",
            blocked_files=["tests/test_foo.py"],
            blocked_reason="Test files belong to tester role",
        )

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy_result),
            patch("subprocess.run", side_effect=run_side_effect_new_branch),
            patch.object(
                gateway,
                "get_policy_engine",
                return_value=MagicMock(
                    check_branch_ownership=MagicMock(
                        return_value=PolicyResult(
                            allowed=True,
                            reason="OK",
                            details={"branch": "egg-new-feature"},
                        )
                    ),
                ),
            ),
            patch.object(gateway, "get_token_for_repo", return_value=("test-token", "bot", "")),
            patch.object(
                gateway,
                "get_changed_files_in_push",
                return_value=(["gateway/gateway.py", "tests/test_foo.py"], None),
            ),
            patch.object(
                gateway, "check_file_restrictions", return_value=FileRestrictionResult.allow()
            ),
            patch.object(gateway, "check_agent_restrictions", return_value=agent_result),
            patch.object(
                gateway,
                "filter_agent_files",
                return_value=(["gateway/gateway.py"], ["tests/test_foo.py"]),
            ),
            patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}),
        ):
            response = _do_push(client)
            assert response.status_code == 200
            data = json.loads(response.data)
            assert data["data"]["filtered"] is True


class TestWarnOnlyModeUnchanged:
    """Warn-only mode skips filtering — push proceeds as-is."""

    def test_warn_mode_no_filtering(self, client):
        """In warn-only mode, no filtering is applied."""
        session = _make_coder_session()
        patches = _push_context(session, agent_blocked=True)

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
                data = json.loads(response.data)
                # In warn-only mode, filtered should NOT be set
                assert data.get("data", {}).get("filtered") is None


class TestFilterAllowedFilesUnit:
    """Unit tests for the filter_allowed_files function in agent_restrictions."""

    def test_coder_mixed_files(self):
        """Coder role: source files allowed, test files blocked."""
        from agent_restrictions import filter_allowed_files

        files = [
            "gateway/gateway.py",
            "gateway/auth.py",
            "tests/test_foo.py",
            "docs/README.md",
        ]
        allowed, blocked = filter_allowed_files("coder", files)
        assert "gateway/gateway.py" in allowed
        assert "gateway/auth.py" in allowed
        assert "tests/test_foo.py" in blocked
        assert "docs/README.md" in blocked

    def test_unknown_role_allows_all(self):
        """Unknown roles allow all files (backwards compatibility)."""
        from agent_restrictions import filter_allowed_files

        files = ["anything.py", "tests/foo.py", "docs/bar.md"]
        allowed, blocked = filter_allowed_files("unknown_role_xyz", files)
        assert allowed == files
        assert blocked == []

    def test_empty_files(self):
        """Empty file list returns empty results."""
        from agent_restrictions import filter_allowed_files

        allowed, blocked = filter_allowed_files("coder", [])
        assert allowed == []
        assert blocked == []

    def test_all_allowed(self):
        """When all files are allowed, blocked is empty."""
        from agent_restrictions import filter_allowed_files

        files = ["gateway/gateway.py", "shared/utils.py"]
        allowed, blocked = filter_allowed_files("coder", files)
        assert set(allowed) == set(files)
        assert blocked == []

    def test_all_blocked(self):
        """When all files are blocked, allowed is empty."""
        from agent_restrictions import filter_allowed_files

        files = ["tests/test_foo.py", "docs/guide.md"]
        allowed, blocked = filter_allowed_files("coder", files)
        assert allowed == []
        assert set(blocked) == set(files)
