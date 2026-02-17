"""Tests for pipeline enforcement features in gateway.py.

Covers:
- Branch switch blocking for pipeline sessions (git_execute)
- Commit-time phase file restriction validation (git_execute)
"""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

import auth
import gateway
import session_manager as session_manager_module
from private_repo_policy import PrivateRepoPolicyResult
from session_manager import Session, SessionValidationResult, _hash_token

# Re-use the test client fixtures from the gateway test module
TEST_LAUNCHER_SECRET = "test-launcher-secret-12345"


@pytest.fixture
def client():
    """Create test client for Flask app."""
    gateway.app.config["TESTING"] = True
    with gateway.app.test_client() as client:
        yield client


def _make_session_with_branch(assigned_branch, phase=None, agent_role=None):
    """Create a mock session with an assigned branch for pipeline lock tests."""
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
    """Set up auth mocking for a session. Returns (headers, context_managers)."""
    mock_result = SessionValidationResult(valid=True, session=session)
    mock_policy_result = PrivateRepoPolicyResult(
        allowed=True,
        reason="Test mode - access allowed",
        visibility="public",
    )

    # Clear cached references
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


class TestBranchSwitchBlocking:
    """Tests for branch switch blocking in git_execute."""

    @pytest.fixture
    def auth_with_branch(self):
        """Create auth setup with a session that has an assigned branch."""
        session = _make_session_with_branch("egg/c1/work", phase="implement")
        return _setup_auth(session)

    @pytest.fixture
    def auth_without_branch(self):
        """Create auth setup with a session that has no assigned branch."""
        session = _make_session_with_branch(None)
        return _setup_auth(session)

    def test_checkout_branch_blocked(self, client, auth_with_branch):
        """git checkout <branch> blocked for pipeline sessions."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
        ):
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "checkout",
                    "args": ["main"],
                },
                headers=headers,
            )
            assert response.status_code == 403
            data = json.loads(response.data)
            assert "Branch switching" in data.get("message", "")

    def test_checkout_b_blocked(self, client, auth_with_branch):
        """git checkout -b <new-branch> blocked for pipeline sessions."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
        ):
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "checkout",
                    "args": ["-b", "new-feature"],
                },
                headers=headers,
            )
            assert response.status_code == 403

    def test_switch_blocked(self, client, auth_with_branch):
        """git switch blocked for pipeline sessions."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
        ):
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "switch",
                    "args": ["main"],
                },
                headers=headers,
            )
            assert response.status_code == 403

    def test_checkout_file_allowed(self, client, auth_with_branch):
        """git checkout -- file.txt allowed for pipeline sessions."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/worktree/path"),
            patch("gateway.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "checkout",
                    "args": ["--", "file.txt"],
                },
                headers=headers,
            )
            # Not blocked by branch switch check (may succeed or fail for other reasons)
            assert response.status_code != 403

    def test_error_message_includes_locked_branch(self, client, auth_with_branch):
        """403 error message includes the locked branch name."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
        ):
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "checkout",
                    "args": ["other-branch"],
                },
                headers=headers,
            )
            data = json.loads(response.data)
            assert "egg/c1/work" in data.get("message", "")

    def test_no_assigned_branch_allows_checkout(self, client, auth_without_branch):
        """Sessions without assigned_branch allow checkout."""
        headers, mock_result, mock_policy, current_sm = auth_without_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/worktree/path"),
            patch("gateway.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "checkout",
                    "args": ["main"],
                },
                headers=headers,
            )
            assert response.status_code != 403


class TestCommitTimePhaseValidation:
    """Tests for commit-time staged file validation in git_execute."""

    @pytest.fixture
    def auth_implement_phase(self):
        """Auth setup with implement phase session."""
        session = _make_session_with_branch(
            "egg/c1/work", phase="implement", agent_role="coder"
        )
        return _setup_auth(session)

    def test_commit_with_blocked_staged_files_returns_403(
        self, client, auth_implement_phase
    ):
        """Commit is blocked if staged files violate phase restrictions."""
        headers, mock_result, mock_policy, current_sm = auth_implement_phase

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/worktree/path"),
            patch("gateway.subprocess.run") as mock_run,
        ):
            # First call is the staged files check (git diff --cached),
            # subsequent calls are the actual commit execution
            mock_staged = MagicMock(
                returncode=0,
                stdout=".egg-state/contracts/644.json\n",
                stderr="",
            )
            mock_run.return_value = mock_staged
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "commit",
                    "args": ["-m", "test commit"],
                },
                headers=headers,
            )
            assert response.status_code == 403
            data = json.loads(response.data)
            assert "blocked" in data.get("message", "").lower()

    def test_commit_with_allowed_files_not_blocked(
        self, client, auth_implement_phase
    ):
        """Commit with only allowed files is not blocked at phase check."""
        headers, mock_result, mock_policy, current_sm = auth_implement_phase

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/worktree/path"),
            patch("gateway.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="src/main.py\nsrc/utils.py\n",
                stderr="",
            )
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "commit",
                    "args": ["-m", "test commit"],
                },
                headers=headers,
            )
            # Not blocked by phase restriction
            assert response.status_code != 403
