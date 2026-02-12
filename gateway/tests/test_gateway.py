"""
Tests for Gateway Sidecar REST API.

Tests cover:
- Health check endpoint
- Authentication (valid/invalid tokens)
- Git push endpoint with policy enforcement
- gh PR endpoints (create, comment, edit, close)
- Blocked commands (merge)
- Generic gh execute endpoint
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

# conftest.py sets up the module loading and TEST_LAUNCHER_SECRET
# Modules are loaded via importlib in conftest.py

# Import the test secrets and modules (loaded by conftest.py)
TEST_LAUNCHER_SECRET = os.environ.get("EGG_LAUNCHER_SECRET", "test-launcher-secret-12345")

import session_manager
from policy import PolicyResult
from session_manager import SessionValidationResult

import gateway


@pytest.fixture
def client():
    """Create test client for Flask app."""
    gateway.app.config["TESTING"] = True
    with gateway.app.test_client() as client:
        yield client


@pytest.fixture
def launcher_auth_headers():
    """Return valid launcher authentication headers."""
    return {"Authorization": f"Bearer {TEST_LAUNCHER_SECRET}"}


@pytest.fixture
def auth_headers():
    """Return valid session authentication headers with mocked session validation.

    Session-protected endpoints require valid session tokens. This fixture
    mocks session validation and private repo policy to allow tests to proceed.

    Note: We patch sys.modules entries directly to handle cases where other tests
    may have loaded different module instances into sys.modules.
    """
    import sys

    import auth

    mock_session = MagicMock()
    mock_session.mode = "public"
    mock_session.container_id = "test-container"
    mock_session.expires_at = None

    mock_result = SessionValidationResult(valid=True, session=mock_session)

    # Mock private repo policy to allow access (default public mode)
    from private_repo_policy import PrivateRepoPolicyResult

    mock_policy_result = PrivateRepoPolicyResult(
        allowed=True,
        reason="Test mode - access allowed",
        visibility="public",
    )

    # Clear auth module's cached references so it picks up our patched module
    auth._session_manager = None
    auth._rate_limiter = None

    # Also clear any package-style cached references
    if "gateway.auth" in sys.modules:
        sys.modules["gateway.auth"]._session_manager = None
        sys.modules["gateway.auth"]._rate_limiter = None

    # Patch the module that's currently in sys.modules, not the one we imported at module load time.
    # This handles cases where other tests may have loaded different instances.
    current_session_manager = sys.modules.get("session_manager", session_manager)

    with (
        patch.object(
            current_session_manager, "validate_session_for_request", return_value=mock_result
        ),
        patch.object(gateway, "check_private_repo_access", return_value=mock_policy_result),
    ):
        yield {"Authorization": "Bearer test-session-token"}


class TestHealthCheck:
    """Tests for /api/v1/health endpoint."""

    def test_health_check_returns_status(self, client):
        """Health check returns status info without auth."""
        with patch.object(gateway, "get_github_client") as mock_gh:
            mock_gh.return_value.is_token_valid.return_value = True

            response = client.get("/api/v1/health")

            assert response.status_code == 200
            data = json.loads(response.data)
            assert "status" in data
            assert data["service"] == "gateway"

    def test_health_check_no_auth_required(self, client):
        """Health check does not require authentication."""
        with patch.object(gateway, "get_github_client") as mock_gh:
            mock_gh.return_value.is_token_valid.return_value = True

            response = client.get("/api/v1/health")

            # Should succeed without auth headers
            assert response.status_code == 200

    def test_health_check_degraded_when_token_invalid(self, client):
        """Health check shows degraded when GitHub token invalid."""
        with (
            patch.object(gateway, "get_github_client") as mock_gh,
            patch.object(gateway, "get_launcher_secret", return_value="test-secret"),
        ):
            mock_gh.return_value.is_token_valid.return_value = False

            response = client.get("/api/v1/health")

            data = json.loads(response.data)
            assert data["status"] == "degraded"
            assert data["github_token_valid"] is False

    def test_health_check_excludes_orchestrator_when_not_configured(self, client):
        """Health check excludes orchestrator status when not configured."""
        with (
            patch.object(gateway, "get_github_client") as mock_gh,
            patch.object(
                gateway, "_check_orchestrator_connectivity", return_value={"configured": False}
            ),
        ):
            mock_gh.return_value.is_token_valid.return_value = True

            response = client.get("/api/v1/health")

            assert response.status_code == 200
            data = json.loads(response.data)
            # orchestrator key is only present when configured
            assert "orchestrator" not in data

    def test_health_check_orchestrator_reachable(self, client, monkeypatch):
        """Health check shows orchestrator reachability when configured."""
        monkeypatch.setenv("EGG_ORCHESTRATOR_URL", "http://orchestrator:8080")

        with (
            patch.object(gateway, "get_github_client") as mock_gh,
            patch.object(
                gateway,
                "_check_orchestrator_connectivity",
                return_value={"configured": True, "reachable": True, "status": "healthy"},
            ),
        ):
            mock_gh.return_value.is_token_valid.return_value = True

            response = client.get("/api/v1/health")

            assert response.status_code == 200
            data = json.loads(response.data)
            assert data["orchestrator"]["configured"] is True
            assert data["orchestrator"]["reachable"] is True

    def test_health_check_orchestrator_unreachable(self, client, monkeypatch):
        """Health check shows orchestrator unreachable when connection fails."""
        monkeypatch.setenv("EGG_ORCHESTRATOR_URL", "http://orchestrator:8080")

        with (
            patch.object(gateway, "get_github_client") as mock_gh,
            patch.object(
                gateway,
                "_check_orchestrator_connectivity",
                return_value={
                    "configured": True,
                    "reachable": False,
                    "error": "Connection refused",
                },
            ),
        ):
            mock_gh.return_value.is_token_valid.return_value = True

            response = client.get("/api/v1/health")

            assert response.status_code == 200
            data = json.loads(response.data)
            assert data["orchestrator"]["configured"] is True
            assert data["orchestrator"]["reachable"] is False
            assert "error" in data["orchestrator"]


class TestAuthentication:
    """Tests for authentication."""

    def test_missing_auth_header_returns_401(self, client):
        """Requests without auth header return 401."""
        response = client.post(
            "/api/v1/gh/pr/create",
            data=json.dumps({"repo": "test/repo", "title": "Test", "head": "branch"}),
            content_type="application/json",
        )

        assert response.status_code == 401
        data = json.loads(response.data)
        assert data["success"] is False
        assert "Authorization" in data["message"]

    def test_invalid_auth_header_format_returns_401(self, client):
        """Requests with malformed auth header return 401."""
        response = client.post(
            "/api/v1/gh/pr/create",
            headers={"Authorization": "Basic dXNlcjpwYXNz"},
            data=json.dumps({"repo": "test/repo", "title": "Test", "head": "branch"}),
            content_type="application/json",
        )

        assert response.status_code == 401

    def test_wrong_token_returns_401(self, client):
        """Requests with wrong token return 401."""
        response = client.post(
            "/api/v1/gh/pr/create",
            headers={"Authorization": "Bearer wrong-token"},
            data=json.dumps({"repo": "test/repo", "title": "Test", "head": "branch"}),
            content_type="application/json",
        )

        assert response.status_code == 401
        data = json.loads(response.data)
        assert "Invalid" in data["message"]

    def test_valid_token_succeeds(self, client, auth_headers):
        """Requests with valid token pass authentication."""
        with patch.object(gateway, "get_github_client") as mock_gh:
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.stdout = "https://github.com/test/repo/pull/1"
            mock_result.stderr = ""
            mock_gh.return_value.execute.return_value = mock_result

            response = client.post(
                "/api/v1/gh/pr/create",
                headers=auth_headers,
                data=json.dumps({"repo": "test/repo", "title": "Test PR", "head": "feature"}),
                content_type="application/json",
            )

            # Should not be 401 (may fail for other reasons)
            assert response.status_code != 401


class TestGitPush:
    """Tests for /api/v1/git/push endpoint."""

    def test_push_requires_repo_path(self, client, auth_headers):
        """Push requires repo_path parameter."""
        response = client.post(
            "/api/v1/git/push",
            headers=auth_headers,
            data=json.dumps({"remote": "origin"}),
            content_type="application/json",
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert "repo_path" in data["message"]

    def test_push_denied_by_policy(self, client, auth_headers):
        """Push denied when policy check fails."""
        with (
            patch("subprocess.run") as mock_run,
            patch.object(gateway, "get_policy_engine") as mock_policy,
        ):
            # Mock git remote get-url
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="https://github.com/owner/repo.git\n",
                stderr="",
            )

            # Mock policy denial
            mock_engine = MagicMock()
            mock_engine.check_branch_ownership.return_value = PolicyResult(
                allowed=False,
                reason="Branch 'main' is not owned by james-in-a-box",
                details={"branch": "main"},
            )
            mock_policy.return_value = mock_engine

            response = client.post(
                "/api/v1/git/push",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "repo_path": "/home/egg/repos/test-repo",
                        "remote": "origin",
                        "refspec": "main",
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 403
            data = json.loads(response.data)
            assert "denied" in data["message"].lower()
            assert data["success"] is False

    def test_push_allowed_for_bot_prefixed_branch(self, client, auth_headers):
        """Push allowed for bot-prefixed branch."""
        with (
            patch("subprocess.run") as mock_run,
            patch.object(gateway, "get_policy_engine") as mock_policy,
            patch.object(gateway, "get_token_for_repo") as mock_get_token,
        ):
            # Configure subprocess.run to return different values based on args
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
                else:
                    result.stdout = ""
                return result

            mock_run.side_effect = run_side_effect

            # Mock policy approval
            mock_engine = MagicMock()
            mock_engine.check_branch_ownership.return_value = PolicyResult(
                allowed=True,
                reason="Branch is owned by james-in-a-box",
                details={"branch": "egg-feature"},
            )
            mock_policy.return_value = mock_engine

            # Mock get_token_for_repo to return valid token
            mock_get_token.return_value = ("test-token", "bot", "")

            response = client.post(
                "/api/v1/git/push",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "repo_path": "/home/egg/repos/test-repo",
                        "remote": "origin",
                        "refspec": "egg-feature",
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 200
            data = json.loads(response.data)
            assert data["success"] is True

    def test_push_denied_for_implementer_modifying_contract_files(self, client):
        """Push denied when implementer tries to modify contract files."""
        import sys

        import auth

        # Create a session with implementer role
        mock_session = MagicMock()
        mock_session.mode = "public"
        mock_session.container_id = "test-container"
        mock_session.expires_at = None
        mock_session.agent_role = "implementer"

        mock_result = SessionValidationResult(valid=True, session=mock_session)

        from private_repo_policy import PrivateRepoPolicyResult

        mock_policy_result = PrivateRepoPolicyResult(
            allowed=True,
            reason="Test mode - access allowed",
            visibility="public",
        )

        auth._session_manager = None
        auth._rate_limiter = None
        if "gateway.auth" in sys.modules:
            sys.modules["gateway.auth"]._session_manager = None
            sys.modules["gateway.auth"]._rate_limiter = None

        current_session_manager = sys.modules.get("session_manager", session_manager)

        with (
            patch.object(
                current_session_manager, "validate_session_for_request", return_value=mock_result
            ),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy_result),
            patch("subprocess.run") as mock_run,
            patch.object(gateway, "get_policy_engine") as mock_policy,
            patch.object(gateway, "get_changed_files_in_push") as mock_get_changed_files,
            patch.object(gateway, "check_file_restrictions") as mock_check_restrictions,
        ):
            # Mock git remote get-url
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="https://github.com/owner/repo.git\n",
                stderr="",
            )

            # Mock policy approval (branch ownership OK)
            mock_engine = MagicMock()
            mock_engine.check_branch_ownership.return_value = PolicyResult(
                allowed=True,
                reason="Branch is owned by james-in-a-box",
                details={"branch": "egg-feature"},
            )
            mock_policy.return_value = mock_engine

            # Mock get_changed_files_in_push - returns files being modified
            mock_get_changed_files.return_value = (
                ["src/main.py", ".egg-state/contracts/123.json"],
                None,
            )

            # Mock check_file_restrictions - contract file is blocked
            from phase_filter import FileRestrictionResult

            mock_check_restrictions.return_value = FileRestrictionResult.block(
                message="Role 'implementer' cannot modify: .egg-state/contracts/123.json",
                role="implementer",
                blocked_files=[".egg-state/contracts/123.json"],
                blocked_reason="Contract files can only be modified through the contract API",
            )

            response = client.post(
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

            assert response.status_code == 403
            data = json.loads(response.data)
            assert "cannot modify" in data["message"].lower()
            assert ".egg-state/contracts/123.json" in data["data"]["blocked_files"]
            assert data["data"]["role"] == "implementer"

    def test_push_allowed_for_reviewer_modifying_contract_files(self, client):
        """Push allowed when reviewer modifies contract files.

        The protected files check should NOT be invoked for reviewer role.
        """
        import sys

        import auth

        # Create a session with reviewer role
        mock_session = MagicMock()
        mock_session.mode = "public"
        mock_session.container_id = "test-container"
        mock_session.expires_at = None
        mock_session.agent_role = "reviewer"

        mock_result = SessionValidationResult(valid=True, session=mock_session)

        from private_repo_policy import PrivateRepoPolicyResult

        mock_policy_result = PrivateRepoPolicyResult(
            allowed=True,
            reason="Test mode - access allowed",
            visibility="public",
        )

        auth._session_manager = None
        auth._rate_limiter = None
        if "gateway.auth" in sys.modules:
            sys.modules["gateway.auth"]._session_manager = None
            sys.modules["gateway.auth"]._rate_limiter = None

        current_session_manager = sys.modules.get("session_manager", session_manager)

        with (
            patch.object(
                current_session_manager, "validate_session_for_request", return_value=mock_result
            ),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy_result),
            patch("subprocess.run") as mock_run,
            patch.object(gateway, "get_policy_engine") as mock_policy,
            patch.object(gateway, "get_token_for_repo") as mock_get_token,
            patch.object(gateway, "get_changed_files_in_push") as mock_get_changed_files,
            patch.object(gateway, "check_file_restrictions") as mock_check_restrictions,
        ):

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
                else:
                    result.stdout = ""
                return result

            mock_run.side_effect = run_side_effect

            # Mock policy approval (branch ownership OK)
            mock_engine = MagicMock()
            mock_engine.check_branch_ownership.return_value = PolicyResult(
                allowed=True,
                reason="Branch is owned by james-in-a-box",
                details={"branch": "egg-feature"},
            )
            mock_policy.return_value = mock_engine

            # Mock get_token_for_repo to return valid token
            mock_get_token.return_value = ("test-token", "bot", "")

            # Mock get_changed_files_in_push
            mock_get_changed_files.return_value = ([".egg-state/contracts/123.json"], None)

            # Mock check_file_restrictions - reviewer is allowed (no restrictions for reviewer)
            from phase_filter import FileRestrictionResult

            mock_check_restrictions.return_value = FileRestrictionResult.allow(
                "No file restrictions for role: reviewer"
            )

            response = client.post(
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

            # Reviewer should be allowed to push
            assert response.status_code == 200
            data = json.loads(response.data)
            assert data["success"] is True

    def test_push_allowed_for_implementer_without_contract_files(self, client):
        """Push allowed when implementer is not modifying contract files."""
        import sys

        import auth

        # Create a session with implementer role
        mock_session = MagicMock()
        mock_session.mode = "public"
        mock_session.container_id = "test-container"
        mock_session.expires_at = None
        mock_session.agent_role = "implementer"

        mock_result = SessionValidationResult(valid=True, session=mock_session)

        from private_repo_policy import PrivateRepoPolicyResult

        mock_policy_result = PrivateRepoPolicyResult(
            allowed=True,
            reason="Test mode - access allowed",
            visibility="public",
        )

        auth._session_manager = None
        auth._rate_limiter = None
        if "gateway.auth" in sys.modules:
            sys.modules["gateway.auth"]._session_manager = None
            sys.modules["gateway.auth"]._rate_limiter = None

        current_session_manager = sys.modules.get("session_manager", session_manager)

        with (
            patch.object(
                current_session_manager, "validate_session_for_request", return_value=mock_result
            ),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy_result),
            patch("subprocess.run") as mock_run,
            patch.object(gateway, "get_policy_engine") as mock_policy,
            patch.object(gateway, "get_changed_files_in_push") as mock_get_changed_files,
            patch.object(gateway, "check_file_restrictions") as mock_check_restrictions,
            patch.object(gateway, "get_token_for_repo") as mock_get_token,
        ):

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
                else:
                    result.stdout = ""
                return result

            mock_run.side_effect = run_side_effect

            # Mock policy approval (branch ownership OK)
            mock_engine = MagicMock()
            mock_engine.check_branch_ownership.return_value = PolicyResult(
                allowed=True,
                reason="Branch is owned by james-in-a-box",
                details={"branch": "egg-feature"},
            )
            mock_policy.return_value = mock_engine

            # Mock get_changed_files_in_push - NO contract files being modified
            mock_get_changed_files.return_value = (["src/main.py", "README.md"], None)

            # Mock check_file_restrictions - all files allowed
            from phase_filter import FileRestrictionResult

            mock_check_restrictions.return_value = FileRestrictionResult.allow("All files allowed")

            # Mock get_token_for_repo to return valid token
            mock_get_token.return_value = ("test-token", "bot", "")

            response = client.post(
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

            # Implementer should be allowed when not modifying protected files
            assert response.status_code == 200
            data = json.loads(response.data)
            assert data["success"] is True

    def test_push_denied_when_file_check_fails(self, client):
        """Push denied when protected files check fails (fail closed)."""
        import sys

        import auth

        # Create a session with implementer role
        mock_session = MagicMock()
        mock_session.mode = "public"
        mock_session.container_id = "test-container"
        mock_session.expires_at = None
        mock_session.agent_role = "implementer"

        mock_result = SessionValidationResult(valid=True, session=mock_session)

        from private_repo_policy import PrivateRepoPolicyResult

        mock_policy_result = PrivateRepoPolicyResult(
            allowed=True,
            reason="Test mode - access allowed",
            visibility="public",
        )

        auth._session_manager = None
        auth._rate_limiter = None
        if "gateway.auth" in sys.modules:
            sys.modules["gateway.auth"]._session_manager = None
            sys.modules["gateway.auth"]._rate_limiter = None

        current_session_manager = sys.modules.get("session_manager", session_manager)

        with (
            patch.object(
                current_session_manager, "validate_session_for_request", return_value=mock_result
            ),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy_result),
            patch("subprocess.run") as mock_run,
            patch.object(gateway, "get_policy_engine") as mock_policy,
            patch.object(gateway, "get_changed_files_in_push") as mock_get_changed_files,
        ):
            # Mock git remote get-url
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="https://github.com/owner/repo.git\n",
                stderr="",
            )

            # Mock policy approval (branch ownership OK)
            mock_engine = MagicMock()
            mock_engine.check_branch_ownership.return_value = PolicyResult(
                allowed=True,
                reason="Branch is owned by james-in-a-box",
                details={"branch": "egg-feature"},
            )
            mock_policy.return_value = mock_engine

            # Mock get_changed_files_in_push - returns error (fail closed)
            mock_get_changed_files.return_value = ([], "Timeout determining changed files")

            response = client.post(
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

            # SECURITY: Should fail closed with 500 error
            assert response.status_code == 500
            data = json.loads(response.data)
            assert "could not verify" in data["message"].lower()
            assert data["data"]["role"] == "implementer"

    def test_force_push_with_protected_files_blocked(self, client):
        """Force push containing protected files should also be blocked."""
        import sys

        import auth

        # Create a session with implementer role
        mock_session = MagicMock()
        mock_session.mode = "public"
        mock_session.container_id = "test-container"
        mock_session.expires_at = None
        mock_session.agent_role = "implementer"

        mock_result = SessionValidationResult(valid=True, session=mock_session)

        from private_repo_policy import PrivateRepoPolicyResult

        mock_policy_result = PrivateRepoPolicyResult(
            allowed=True,
            reason="Test mode - access allowed",
            visibility="public",
        )

        auth._session_manager = None
        auth._rate_limiter = None
        if "gateway.auth" in sys.modules:
            sys.modules["gateway.auth"]._session_manager = None
            sys.modules["gateway.auth"]._rate_limiter = None

        current_session_manager = sys.modules.get("session_manager", session_manager)

        with (
            patch.object(
                current_session_manager, "validate_session_for_request", return_value=mock_result
            ),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy_result),
            patch("subprocess.run") as mock_run,
            patch.object(gateway, "get_policy_engine") as mock_policy,
            patch.object(gateway, "get_changed_files_in_push") as mock_get_changed_files,
            patch.object(gateway, "check_file_restrictions") as mock_check_restrictions,
        ):
            # Mock git remote get-url
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="https://github.com/owner/repo.git\n",
                stderr="",
            )

            # Mock policy approval (branch ownership OK)
            mock_engine = MagicMock()
            mock_engine.check_branch_ownership.return_value = PolicyResult(
                allowed=True,
                reason="Branch is owned by james-in-a-box",
                details={"branch": "egg-feature"},
            )
            mock_policy.return_value = mock_engine

            # Mock get_changed_files_in_push - contract file is being modified
            mock_get_changed_files.return_value = ([".egg-state/contracts/123.json"], None)

            # Mock check_file_restrictions - contract file is blocked
            from phase_filter import FileRestrictionResult

            mock_check_restrictions.return_value = FileRestrictionResult.block(
                message="Role 'implementer' cannot modify: .egg-state/contracts/123.json",
                role="implementer",
                blocked_files=[".egg-state/contracts/123.json"],
                blocked_reason="Contract files can only be modified through the contract API",
            )

            # Request with force=true
            response = client.post(
                "/api/v1/git/push",
                headers={"Authorization": "Bearer test-session-token"},
                data=json.dumps(
                    {
                        "repo_path": "/home/egg/repos/test-repo",
                        "remote": "origin",
                        "refspec": "egg-feature",
                        "force": True,
                    }
                ),
                content_type="application/json",
            )

            # Force push with blocked files should still be blocked
            assert response.status_code == 403
            data = json.loads(response.data)
            assert "cannot modify" in data["message"].lower()

    def test_push_allowed_when_role_unavailable(self, client):
        """Push allowed when session role is unavailable (backwards compatibility).

        This ensures pushes work for legacy sessions without role information,
        as documented in ac-14 of the contract.
        """
        import sys

        import auth

        # Create a session WITHOUT agent_role (backwards compatibility)
        mock_session = MagicMock()
        mock_session.mode = "public"
        mock_session.container_id = "test-container"
        mock_session.expires_at = None
        # Explicitly NOT setting agent_role to simulate legacy session
        mock_session.agent_role = None
        # Explicitly set phase to None to prevent MagicMock auto-creation
        mock_session.phase = None

        mock_result = SessionValidationResult(valid=True, session=mock_session)

        from private_repo_policy import PrivateRepoPolicyResult

        mock_policy_result = PrivateRepoPolicyResult(
            allowed=True,
            reason="Test mode - access allowed",
            visibility="public",
        )

        auth._session_manager = None
        auth._rate_limiter = None
        if "gateway.auth" in sys.modules:
            sys.modules["gateway.auth"]._session_manager = None
            sys.modules["gateway.auth"]._rate_limiter = None

        current_session_manager = sys.modules.get("session_manager", session_manager)

        with (
            patch.object(
                current_session_manager, "validate_session_for_request", return_value=mock_result
            ),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy_result),
            patch("subprocess.run") as mock_run,
            patch.object(gateway, "get_policy_engine") as mock_policy,
            patch.object(gateway, "get_token_for_repo") as mock_get_token,
            patch.object(gateway, "get_changed_files_in_push") as mock_get_changed_files,
            patch.object(gateway, "check_file_restrictions") as mock_check_restrictions,
        ):

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
                else:
                    result.stdout = ""
                return result

            mock_run.side_effect = run_side_effect

            # Mock policy approval (branch ownership OK)
            mock_engine = MagicMock()
            mock_engine.check_branch_ownership.return_value = PolicyResult(
                allowed=True,
                reason="Branch is owned by james-in-a-box",
                details={"branch": "egg-feature"},
            )
            mock_policy.return_value = mock_engine

            # Mock get_token_for_repo to return valid token
            mock_get_token.return_value = ("test-token", "bot", "")

            # Even though we're pushing contract files, the check should NOT run
            # when role is unavailable (backwards compatibility)
            mock_get_changed_files.return_value = ([".egg-state/contracts/123.json"], None)

            response = client.post(
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

            # BACKWARDS COMPATIBILITY: Push should succeed when role is unavailable
            assert response.status_code == 200
            data = json.loads(response.data)
            assert data["success"] is True

            # Verify file restriction check was NOT invoked
            mock_check_restrictions.assert_not_called()

            # Verify get_changed_files_in_push was NOT invoked (check skipped entirely)
            mock_get_changed_files.assert_not_called()


class TestGhPrCreate:
    """Tests for /api/v1/gh/pr/create endpoint."""

    def test_pr_create_requires_repo(self, client, auth_headers):
        """PR create requires repo parameter."""
        response = client.post(
            "/api/v1/gh/pr/create",
            headers=auth_headers,
            data=json.dumps({"title": "Test", "head": "branch"}),
            content_type="application/json",
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert "repo" in data["message"]

    def test_pr_create_requires_title(self, client, auth_headers):
        """PR create requires title parameter."""
        response = client.post(
            "/api/v1/gh/pr/create",
            headers=auth_headers,
            data=json.dumps({"repo": "test/repo", "head": "branch"}),
            content_type="application/json",
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert "title" in data["message"]

    def test_pr_create_requires_head(self, client, auth_headers):
        """PR create requires head parameter."""
        response = client.post(
            "/api/v1/gh/pr/create",
            headers=auth_headers,
            data=json.dumps({"repo": "test/repo", "title": "Test"}),
            content_type="application/json",
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert "head" in data["message"]

    def test_pr_create_success(self, client, auth_headers):
        """PR create succeeds with valid parameters."""
        with patch.object(gateway, "get_github_client") as mock_gh:
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.stdout = "https://github.com/test/repo/pull/42"
            mock_result.stderr = ""
            mock_gh.return_value.execute.return_value = mock_result

            response = client.post(
                "/api/v1/gh/pr/create",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "repo": "test/repo",
                        "title": "Add feature",
                        "body": "Description",
                        "base": "main",
                        "head": "feature-branch",
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 200
            data = json.loads(response.data)
            assert data["success"] is True
            assert "pull/42" in data["data"]["stdout"]


class TestGhPrCreatePhaseRestrictions:
    """Tests for phase-based PR creation restrictions."""

    @pytest.fixture
    def auth_headers_with_phase(self):
        """Return session headers with specific phase for testing."""
        import sys

        import auth

        def _make_headers(phase: str | None):
            mock_session = MagicMock()
            mock_session.mode = "public"
            mock_session.container_id = "test-container"
            mock_session.expires_at = None
            mock_session.phase = phase

            mock_result = SessionValidationResult(valid=True, session=mock_session)

            # Mock private repo policy to allow access
            from private_repo_policy import PrivateRepoPolicyResult

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

            current_session_manager = sys.modules.get("session_manager", session_manager)

            return (
                {"Authorization": "Bearer test-session-token"},
                mock_result,
                mock_policy_result,
                current_session_manager,
            )

        return _make_headers

    def test_pr_create_blocked_during_implement_phase(self, client, auth_headers_with_phase):
        """PR create is blocked when session phase is 'implement'."""
        headers, mock_result, mock_policy_result, current_session_manager = auth_headers_with_phase(
            "implement"
        )

        with (
            patch.object(
                current_session_manager, "validate_session_for_request", return_value=mock_result
            ),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy_result),
            patch.object(gateway, "get_github_client") as mock_gh,
        ):
            mock_gh_result = MagicMock()
            mock_gh_result.success = True
            mock_gh_result.stdout = "https://github.com/test/repo/pull/1"
            mock_gh_result.stderr = ""
            mock_gh.return_value.execute.return_value = mock_gh_result

            response = client.post(
                "/api/v1/gh/pr/create",
                headers=headers,
                data=json.dumps(
                    {
                        "repo": "test/repo",
                        "title": "Test PR",
                        "head": "feature-branch",
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 403
            data = json.loads(response.data)
            assert data["success"] is False
            assert "phase" in data["message"].lower() or "blocked" in data["message"].lower()

    def test_pr_create_allowed_during_pr_phase(self, client, auth_headers_with_phase):
        """PR create is allowed when session phase is 'pr'."""
        headers, mock_result, mock_policy_result, current_session_manager = auth_headers_with_phase(
            "pr"
        )

        with (
            patch.object(
                current_session_manager, "validate_session_for_request", return_value=mock_result
            ),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy_result),
            patch.object(gateway, "get_github_client") as mock_gh,
        ):
            mock_gh_result = MagicMock()
            mock_gh_result.success = True
            mock_gh_result.stdout = "https://github.com/test/repo/pull/1"
            mock_gh_result.stderr = ""
            mock_gh.return_value.execute.return_value = mock_gh_result

            response = client.post(
                "/api/v1/gh/pr/create",
                headers=headers,
                data=json.dumps(
                    {
                        "repo": "test/repo",
                        "title": "Test PR",
                        "head": "feature-branch",
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 200
            data = json.loads(response.data)
            assert data["success"] is True

    def test_pr_create_allowed_without_phase_backward_compat(self, client, auth_headers_with_phase):
        """PR create is allowed when session has no phase (backward compatibility)."""
        headers, mock_result, mock_policy_result, current_session_manager = auth_headers_with_phase(
            None  # No phase set
        )

        with (
            patch.object(
                current_session_manager, "validate_session_for_request", return_value=mock_result
            ),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy_result),
            patch.object(gateway, "get_github_client") as mock_gh,
        ):
            mock_gh_result = MagicMock()
            mock_gh_result.success = True
            mock_gh_result.stdout = "https://github.com/test/repo/pull/1"
            mock_gh_result.stderr = ""
            mock_gh.return_value.execute.return_value = mock_gh_result

            response = client.post(
                "/api/v1/gh/pr/create",
                headers=headers,
                data=json.dumps(
                    {
                        "repo": "test/repo",
                        "title": "Test PR",
                        "head": "feature-branch",
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 200
            data = json.loads(response.data)
            assert data["success"] is True

    def test_pr_create_blocked_during_refine_phase(self, client, auth_headers_with_phase):
        """PR create is blocked when session phase is 'refine'."""
        headers, mock_result, mock_policy_result, current_session_manager = auth_headers_with_phase(
            "refine"
        )

        with (
            patch.object(
                current_session_manager, "validate_session_for_request", return_value=mock_result
            ),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy_result),
            patch.object(gateway, "get_github_client") as mock_gh,
        ):
            mock_gh_result = MagicMock()
            mock_gh_result.success = True
            mock_gh.return_value.execute.return_value = mock_gh_result

            response = client.post(
                "/api/v1/gh/pr/create",
                headers=headers,
                data=json.dumps(
                    {
                        "repo": "test/repo",
                        "title": "Test PR",
                        "head": "feature-branch",
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 403

    def test_pr_create_blocked_during_plan_phase(self, client, auth_headers_with_phase):
        """PR create is blocked when session phase is 'plan'."""
        headers, mock_result, mock_policy_result, current_session_manager = auth_headers_with_phase(
            "plan"
        )

        with (
            patch.object(
                current_session_manager, "validate_session_for_request", return_value=mock_result
            ),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy_result),
            patch.object(gateway, "get_github_client") as mock_gh,
        ):
            mock_gh_result = MagicMock()
            mock_gh_result.success = True
            mock_gh.return_value.execute.return_value = mock_gh_result

            response = client.post(
                "/api/v1/gh/pr/create",
                headers=headers,
                data=json.dumps(
                    {
                        "repo": "test/repo",
                        "title": "Test PR",
                        "head": "feature-branch",
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 403


class TestGhPrComment:
    """Tests for /api/v1/gh/pr/comment endpoint."""

    def test_pr_comment_requires_pr_number(self, client, auth_headers):
        """PR comment requires pr_number parameter."""
        response = client.post(
            "/api/v1/gh/pr/comment",
            headers=auth_headers,
            data=json.dumps({"repo": "test/repo", "body": "Comment"}),
            content_type="application/json",
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert "pr_number" in data["message"]

    def test_pr_comment_denied_when_pr_not_found(self, client, auth_headers):
        """PR comment denied when PR doesn't exist."""
        with patch.object(gateway, "get_policy_engine") as mock_policy:
            mock_engine = MagicMock()
            # Comments are allowed on any PR, but denied if PR doesn't exist
            mock_engine.check_pr_comment_allowed.return_value = PolicyResult(
                allowed=False,
                reason="PR #999 not found or inaccessible",
                details={"pr_number": 999},
            )
            mock_policy.return_value = mock_engine

            response = client.post(
                "/api/v1/gh/pr/comment",
                headers=auth_headers,
                data=json.dumps({"repo": "test/repo", "pr_number": 999, "body": "Comment"}),
                content_type="application/json",
            )

            assert response.status_code == 403
            data = json.loads(response.data)
            assert data["success"] is False
            assert "denied" in data["message"].lower()

    def test_pr_comment_allowed_when_owner(self, client, auth_headers):
        """PR comment allowed when james-in-a-box owns the PR."""
        with (
            patch.object(gateway, "get_policy_engine") as mock_policy,
            patch.object(gateway, "get_github_client") as mock_gh,
        ):
            mock_engine = MagicMock()
            mock_engine.check_pr_ownership.return_value = PolicyResult(
                allowed=True,
                reason="PR is owned by james-in-a-box",
                details={"author": "james-in-a-box"},
            )
            mock_policy.return_value = mock_engine

            mock_result = MagicMock()
            mock_result.success = True
            mock_result.stdout = "Comment added"
            mock_result.stderr = ""
            mock_gh.return_value.execute.return_value = mock_result

            response = client.post(
                "/api/v1/gh/pr/comment",
                headers=auth_headers,
                data=json.dumps({"repo": "test/repo", "pr_number": 123, "body": "Thanks!"}),
                content_type="application/json",
            )

            assert response.status_code == 200
            data = json.loads(response.data)
            assert data["success"] is True


class TestGhPrEdit:
    """Tests for /api/v1/gh/pr/edit endpoint."""

    def test_pr_edit_requires_title_or_body(self, client, auth_headers):
        """PR edit requires either title or body."""
        response = client.post(
            "/api/v1/gh/pr/edit",
            headers=auth_headers,
            data=json.dumps({"repo": "test/repo", "pr_number": 123}),
            content_type="application/json",
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert "title or body" in data["message"]

    def test_pr_edit_denied_when_not_owner(self, client, auth_headers):
        """PR edit denied when bot doesn't own the PR."""
        with patch.object(gateway, "get_policy_engine") as mock_policy:
            mock_engine = MagicMock()
            mock_engine.check_pr_ownership.return_value = PolicyResult(
                allowed=False,
                reason="PR #123 is not owned by james-in-a-box",
                details={"author": "someone-else"},
            )
            mock_policy.return_value = mock_engine

            response = client.post(
                "/api/v1/gh/pr/edit",
                headers=auth_headers,
                data=json.dumps({"repo": "test/repo", "pr_number": 123, "title": "New title"}),
                content_type="application/json",
            )

            assert response.status_code == 403


class TestGhPrClose:
    """Tests for /api/v1/gh/pr/close endpoint."""

    def test_pr_close_requires_pr_number(self, client, auth_headers):
        """PR close requires pr_number parameter."""
        response = client.post(
            "/api/v1/gh/pr/close",
            headers=auth_headers,
            data=json.dumps({"repo": "test/repo"}),
            content_type="application/json",
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert "pr_number" in data["message"]

    def test_pr_close_denied_when_not_owner(self, client, auth_headers):
        """PR close denied when bot doesn't own the PR."""
        with patch.object(gateway, "get_policy_engine") as mock_policy:
            mock_engine = MagicMock()
            mock_engine.check_pr_ownership.return_value = PolicyResult(
                allowed=False,
                reason="PR #123 is not owned by james-in-a-box",
                details={"author": "someone-else"},
            )
            mock_policy.return_value = mock_engine

            response = client.post(
                "/api/v1/gh/pr/close",
                headers=auth_headers,
                data=json.dumps({"repo": "test/repo", "pr_number": 123}),
                content_type="application/json",
            )

            assert response.status_code == 403


class TestGhExecute:
    """Tests for /api/v1/gh/execute endpoint."""

    def test_execute_requires_args(self, client, auth_headers):
        """Execute requires args parameter."""
        response = client.post(
            "/api/v1/gh/execute",
            headers=auth_headers,
            data=json.dumps({}),
            content_type="application/json",
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        # Empty dict means missing request body or missing args
        assert "Missing" in data["message"]

    def test_execute_blocks_merge(self, client, auth_headers):
        """Execute blocks pr merge command."""
        response = client.post(
            "/api/v1/gh/execute",
            headers=auth_headers,
            data=json.dumps({"args": ["pr", "merge", "123"]}),
            content_type="application/json",
        )

        assert response.status_code == 403
        data = json.loads(response.data)
        assert "not allowed" in data["message"].lower()

    def test_execute_blocks_repo_delete(self, client, auth_headers):
        """Execute blocks repo delete command."""
        response = client.post(
            "/api/v1/gh/execute",
            headers=auth_headers,
            data=json.dumps({"args": ["repo", "delete", "test/repo"]}),
            content_type="application/json",
        )

        assert response.status_code == 403

    def test_execute_allows_read_operations(self, client, auth_headers):
        """Execute allows read-only operations."""
        with patch.object(gateway, "get_github_client") as mock_gh:
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.stdout = "PR #1: Feature"
            mock_result.stderr = ""
            mock_result.to_dict.return_value = {
                "success": True,
                "stdout": "PR #1: Feature",
                "stderr": "",
            }
            mock_gh.return_value.execute.return_value = mock_result

            response = client.post(
                "/api/v1/gh/execute",
                headers=auth_headers,
                data=json.dumps({"args": ["pr", "list"]}),
                content_type="application/json",
            )

            assert response.status_code == 200
            data = json.loads(response.data)
            assert data["success"] is True

    def test_execute_repo_command_no_repo_flag_injection(self, client, auth_headers):
        """Execute does not inject --repo for 'gh repo' commands.

        gh repo view/list/clone take repository as positional argument,
        not via --repo flag. Injecting --repo would cause command failure.
        """
        with (
            patch.object(gateway, "get_github_client") as mock_gh,
            patch.object(gateway, "get_auth_mode", return_value="bot"),
        ):
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.stdout = "repo info"
            mock_result.stderr = ""
            mock_result.to_dict.return_value = {
                "success": True,
                "stdout": "repo info",
                "stderr": "",
            }
            mock_gh.return_value.execute.return_value = mock_result

            response = client.post(
                "/api/v1/gh/execute",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "args": ["repo", "view", "owner/repo", "--json", "name"],
                        "repo": "owner/repo",  # repo in payload should NOT cause --repo injection
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 200
            # Verify the args passed to execute don't have --repo injected
            call_args = mock_gh.return_value.execute.call_args
            executed_args = call_args[0][0]  # First positional arg is args list
            assert executed_args[0] == "repo"  # First arg should be 'repo', not '--repo'
            assert "--repo" not in executed_args

    def test_execute_non_repo_command_gets_repo_flag_injection(self, client, auth_headers):
        """Execute injects --repo for non-repo commands when repo is in payload."""
        with (
            patch.object(gateway, "get_github_client") as mock_gh,
            patch.object(gateway, "get_auth_mode", return_value="bot"),
        ):
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.stdout = "PR list"
            mock_result.stderr = ""
            mock_result.to_dict.return_value = {
                "success": True,
                "stdout": "PR list",
                "stderr": "",
            }
            mock_gh.return_value.execute.return_value = mock_result

            response = client.post(
                "/api/v1/gh/execute",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "args": ["pr", "list"],
                        "repo": "owner/repo",  # repo in payload SHOULD cause --repo injection
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 200
            # Verify the args passed to execute have --repo injected
            call_args = mock_gh.return_value.execute.call_args
            executed_args = call_args[0][0]  # First positional arg is args list
            assert executed_args[0] == "--repo"  # --repo should be first
            assert executed_args[1] == "owner/repo"
            assert executed_args[2] == "pr"

    def test_execute_api_with_template_variables_resolved(self, client, auth_headers):
        """Execute resolves {owner}/{repo} template variables from cwd.

        This tests the fix for issue #321: gateway should resolve gh CLI
        template variables before checking visibility.
        """
        with (
            patch.object(gateway, "get_github_client") as mock_gh,
            patch.object(gateway, "get_auth_mode", return_value="bot"),
            patch.object(gateway, "resolve_gh_api_template_variables") as mock_resolve,
        ):
            # Mock successful template resolution
            mock_resolve.return_value = "repos/myowner/myrepo/pulls/123/commits"

            mock_result = MagicMock()
            mock_result.success = True
            mock_result.stdout = '[{"sha": "abc123"}]'
            mock_result.stderr = ""
            mock_result.to_dict.return_value = {
                "success": True,
                "stdout": '[{"sha": "abc123"}]',
                "stderr": "",
            }
            mock_gh.return_value.execute.return_value = mock_result

            response = client.post(
                "/api/v1/gh/execute",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "args": [
                            "api",
                            "repos/{owner}/{repo}/pulls/123/commits",
                            "--jq",
                            ".[-1].sha",
                        ],
                        "cwd": "/home/egg/repos/myrepo",
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 200
            data = json.loads(response.data)
            assert data["success"] is True

            # Verify template resolution was called with correct args
            mock_resolve.assert_called_once_with(
                "repos/{owner}/{repo}/pulls/123/commits", "/home/egg/repos/myrepo"
            )

            # Verify the resolved path was passed to execute
            call_args = mock_gh.return_value.execute.call_args
            executed_args = call_args[0][0]
            assert "repos/myowner/myrepo/pulls/123/commits" in executed_args

    def test_execute_api_template_resolution_fails_returns_error(self, client, auth_headers):
        """Execute returns 400 when template variable resolution fails."""
        with patch.object(gateway, "resolve_gh_api_template_variables") as mock_resolve:
            # Mock failed template resolution (no cwd or no remote)
            mock_resolve.return_value = None

            response = client.post(
                "/api/v1/gh/execute",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "args": ["api", "repos/{owner}/{repo}/pulls"],
                        # No cwd provided, resolution should fail
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 400
            data = json.loads(response.data)
            assert data["success"] is False
            assert "template variables" in data["message"].lower()


class TestGhExecuteReviewerToken:
    """Tests for reviewer token selection logic in gh_execute."""

    def test_pr_review_uses_reviewer_token_when_available_bot_mode(self, client, auth_headers):
        """PR review command switches to reviewer mode when token available (bot mode)."""
        with (
            patch.object(gateway, "get_github_client") as mock_gh,
            patch.object(gateway, "get_auth_mode", return_value="bot"),
            patch("token_refresher.is_reviewer_token_available", return_value=True),
        ):
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.stdout = "Reviewed"
            mock_result.stderr = ""
            mock_result.to_dict.return_value = {
                "success": True,
                "stdout": "Reviewed",
                "stderr": "",
            }
            mock_gh.return_value.execute.return_value = mock_result

            response = client.post(
                "/api/v1/gh/execute",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "args": ["pr", "review", "123", "--approve"],
                        "repo": "owner/repo",
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 200
            # Verify get_github_client was called with reviewer mode
            mock_gh.assert_called_with(mode="reviewer")

    def test_pr_review_uses_reviewer_token_when_available_user_mode(self, client, auth_headers):
        """PR review command switches to reviewer mode when token available (user mode)."""
        with (
            patch.object(gateway, "get_github_client") as mock_gh,
            patch.object(gateway, "get_auth_mode", return_value="user"),
            patch("token_refresher.is_reviewer_token_available", return_value=True),
        ):
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.stdout = "Reviewed"
            mock_result.stderr = ""
            mock_result.to_dict.return_value = {
                "success": True,
                "stdout": "Reviewed",
                "stderr": "",
            }
            mock_gh.return_value.execute.return_value = mock_result

            response = client.post(
                "/api/v1/gh/execute",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "args": ["pr", "review", "123", "--approve"],
                        "repo": "owner/repo",
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 200
            # Verify get_github_client was called with reviewer mode
            mock_gh.assert_called_with(mode="reviewer")

    def test_pr_review_stays_in_bot_mode_when_token_unavailable(self, client, auth_headers):
        """PR review command stays in bot mode when reviewer token unavailable."""
        with (
            patch.object(gateway, "get_github_client") as mock_gh,
            patch.object(gateway, "get_auth_mode", return_value="bot"),
            patch("token_refresher.is_reviewer_token_available", return_value=False),
        ):
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.stdout = "Reviewed"
            mock_result.stderr = ""
            mock_result.to_dict.return_value = {
                "success": True,
                "stdout": "Reviewed",
                "stderr": "",
            }
            mock_gh.return_value.execute.return_value = mock_result

            response = client.post(
                "/api/v1/gh/execute",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "args": ["pr", "review", "123", "--approve"],
                        "repo": "owner/repo",
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 200
            # Verify get_github_client was called with bot mode (not reviewer)
            mock_gh.assert_called_with(mode="bot")

    def test_non_review_pr_commands_dont_switch_to_reviewer_mode(self, client, auth_headers):
        """Non-review PR commands (like pr create) don't switch to reviewer mode."""
        with (
            patch.object(gateway, "get_github_client") as mock_gh,
            patch.object(gateway, "get_auth_mode", return_value="bot"),
            patch("token_refresher.is_reviewer_token_available", return_value=True),
        ):
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.stdout = "PR created"
            mock_result.stderr = ""
            mock_result.to_dict.return_value = {
                "success": True,
                "stdout": "PR created",
                "stderr": "",
            }
            mock_gh.return_value.execute.return_value = mock_result

            response = client.post(
                "/api/v1/gh/execute",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "args": ["pr", "create", "--title", "Test"],
                        "repo": "owner/repo",
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 200
            # Verify get_github_client was called with bot mode (not reviewer)
            mock_gh.assert_called_with(mode="bot")

    def test_pr_review_handles_import_error_gracefully(self, client, auth_headers):
        """PR review command handles ImportError gracefully."""
        import sys

        # Remove token_refresher from sys.modules temporarily to simulate ImportError
        original_module = sys.modules.get("token_refresher")
        sys.modules["token_refresher"] = None  # type: ignore[assignment]
        try:
            with (
                patch.object(gateway, "get_github_client") as mock_gh,
                patch.object(gateway, "get_auth_mode", return_value="bot"),
            ):
                mock_result = MagicMock()
                mock_result.success = True
                mock_result.stdout = "Reviewed"
                mock_result.stderr = ""
                mock_result.to_dict.return_value = {
                    "success": True,
                    "stdout": "Reviewed",
                    "stderr": "",
                }
                mock_gh.return_value.execute.return_value = mock_result

                response = client.post(
                    "/api/v1/gh/execute",
                    headers=auth_headers,
                    data=json.dumps(
                        {
                            "args": ["pr", "review", "123", "--approve"],
                            "repo": "owner/repo",
                        }
                    ),
                    content_type="application/json",
                )

                assert response.status_code == 200
                # Verify get_github_client was called with bot mode (fallback)
                mock_gh.assert_called_with(mode="bot")
        finally:
            # Restore original module
            if original_module is not None:
                sys.modules["token_refresher"] = original_module
            elif "token_refresher" in sys.modules:
                del sys.modules["token_refresher"]

    def test_pr_review_stays_in_user_mode_when_token_unavailable(self, client, auth_headers):
        """PR review command stays in user mode when reviewer token unavailable."""
        with (
            patch.object(gateway, "get_github_client") as mock_gh,
            patch.object(gateway, "get_auth_mode", return_value="user"),
            patch("token_refresher.is_reviewer_token_available", return_value=False),
        ):
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.stdout = "Reviewed"
            mock_result.stderr = ""
            mock_result.to_dict.return_value = {
                "success": True,
                "stdout": "Reviewed",
                "stderr": "",
            }
            mock_gh.return_value.execute.return_value = mock_result

            response = client.post(
                "/api/v1/gh/execute",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "args": ["pr", "review", "123", "--approve"],
                        "repo": "owner/repo",
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 200
            # Verify get_github_client was called with user mode (not reviewer)
            mock_gh.assert_called_with(mode="user")

    def test_pr_review_with_repo_flag_prepended_uses_reviewer_token(self, client, auth_headers):
        """PR review command with --repo flag prepended still switches to reviewer mode."""
        with (
            patch.object(gateway, "get_github_client") as mock_gh,
            patch.object(gateway, "get_auth_mode", return_value="bot"),
            patch("token_refresher.is_reviewer_token_available", return_value=True),
        ):
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.stdout = "Reviewed"
            mock_result.stderr = ""
            mock_result.to_dict.return_value = {
                "success": True,
                "stdout": "Reviewed",
                "stderr": "",
            }
            mock_gh.return_value.execute.return_value = mock_result

            # Args with --repo flag prepended (as the gateway does for some commands)
            response = client.post(
                "/api/v1/gh/execute",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "args": ["--repo", "owner/repo", "pr", "review", "123", "--approve"],
                        "repo": "owner/repo",
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 200
            # Verify get_github_client was called with reviewer mode
            mock_gh.assert_called_with(mode="reviewer")


class TestGitFetch:
    """Tests for /api/v1/git/fetch endpoint."""

    def test_fetch_requires_repo_path(self, client, auth_headers):
        """Fetch requires repo_path parameter."""
        response = client.post(
            "/api/v1/git/fetch",
            headers=auth_headers,
            data=json.dumps({"remote": "origin"}),
            content_type="application/json",
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert "repo_path" in data["message"]

    def test_fetch_path_traversal_blocked(self, client, auth_headers):
        """Fetch blocked for path traversal attempts."""
        response = client.post(
            "/api/v1/git/fetch",
            headers=auth_headers,
            data=json.dumps(
                {
                    "repo_path": "/home/egg/repos/../../../etc/passwd",
                    "remote": "origin",
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 403
        data = json.loads(response.data)
        assert data["success"] is False

    def test_fetch_invalid_args_rejected(self, client, auth_headers):
        """Fetch rejects invalid arguments."""
        response = client.post(
            "/api/v1/git/fetch",
            headers=auth_headers,
            data=json.dumps(
                {
                    "repo_path": "/home/egg/repos/test",
                    "remote": "origin",
                    "args": ["--upload-pack=/bin/evil"],
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert "not allowed" in data["message"]

    def test_fetch_success(self, client, auth_headers):
        """Fetch succeeds with valid parameters."""
        with (
            patch("subprocess.run") as mock_run,
            patch.object(gateway, "get_token_for_repo") as mock_get_token,
        ):
            # Configure subprocess.run to return different values based on args
            def run_side_effect(*args, **kwargs):
                cmd = args[0] if args else kwargs.get("args", [])
                result = MagicMock()
                result.returncode = 0
                result.stderr = ""

                if "remote" in cmd and "get-url" in cmd:
                    result.stdout = "https://github.com/owner/repo.git\n"
                elif "fetch" in cmd:
                    result.stdout = ""
                else:
                    result.stdout = ""
                return result

            mock_run.side_effect = run_side_effect

            # Mock get_token_for_repo to return valid token
            mock_get_token.return_value = ("test-token", "bot", "")

            response = client.post(
                "/api/v1/git/fetch",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "repo_path": "/home/egg/repos/test",
                        "remote": "origin",
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 200
            data = json.loads(response.data)
            assert data["success"] is True

    def test_ls_remote_success(self, client, auth_headers):
        """ls-remote succeeds with valid parameters."""
        with (
            patch("subprocess.run") as mock_run,
            patch.object(gateway, "get_token_for_repo") as mock_get_token,
        ):
            # Configure subprocess.run
            def run_side_effect(*args, **kwargs):
                cmd = args[0] if args else kwargs.get("args", [])
                result = MagicMock()
                result.returncode = 0
                result.stderr = ""

                if "remote" in cmd and "get-url" in cmd:
                    result.stdout = "https://github.com/owner/repo.git\n"
                elif "ls-remote" in cmd:
                    result.stdout = "abc123\trefs/heads/main\n"
                else:
                    result.stdout = ""
                return result

            mock_run.side_effect = run_side_effect

            # Mock get_token_for_repo to return valid token
            mock_get_token.return_value = ("test-token", "bot", "")

            response = client.post(
                "/api/v1/git/fetch",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "repo_path": "/home/egg/repos/test",
                        "operation": "ls-remote",
                        "remote": "origin",
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 200
            data = json.loads(response.data)
            assert data["success"] is True
            assert "refs/heads/main" in data["data"]["stdout"]

    def test_fetch_unsupported_operation_rejected(self, client, auth_headers):
        """Unsupported operations are rejected."""
        response = client.post(
            "/api/v1/git/fetch",
            headers=auth_headers,
            data=json.dumps(
                {
                    "repo_path": "/home/egg/repos/test",
                    "operation": "clone",
                    "remote": "origin",
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert "Unsupported" in data["message"]


class TestBlockedCommands:
    """Tests for blocked commands."""

    @pytest.mark.parametrize(
        "args",
        [
            ["pr", "merge", "123"],
            ["repo", "delete", "test/repo"],
            ["repo", "archive", "test/repo"],
            ["release", "delete", "v1.0"],
            ["auth", "logout"],
            ["auth", "login"],
            ["config", "set", "key", "value"],
        ],
    )
    def test_blocked_commands_return_403(self, client, auth_headers, args):
        """Blocked commands return 403 Forbidden."""
        response = client.post(
            "/api/v1/gh/execute",
            headers=auth_headers,
            data=json.dumps({"args": args}),
            content_type="application/json",
        )

        assert response.status_code == 403
        data = json.loads(response.data)
        assert data["success"] is False
        assert "not allowed" in data["message"].lower()


class TestGhExecutePrivateMode:
    """Tests for private mode enforcement in gh execute."""

    @pytest.fixture
    def private_mode_auth_headers(self):
        """Auth headers with private mode session."""
        import sys

        import auth

        mock_session = MagicMock()
        mock_session.mode = "private"  # Private mode session
        mock_session.container_id = "test-container"
        mock_session.expires_at = None

        mock_result = SessionValidationResult(valid=True, session=mock_session)

        # Clear auth module's cached references so it picks up our patched module
        auth._session_manager = None
        auth._rate_limiter = None

        # Also clear any package-style cached references
        if "gateway.auth" in sys.modules:
            sys.modules["gateway.auth"]._session_manager = None
            sys.modules["gateway.auth"]._rate_limiter = None

        # Patch the module that's currently in sys.modules, not the one we imported at module load time.
        current_session_manager = sys.modules.get("session_manager", session_manager)

        with patch.object(
            current_session_manager, "validate_session_for_request", return_value=mock_result
        ):
            yield {"Authorization": "Bearer test-session-token"}

    def test_search_blocked_in_private_mode(self, client, private_mode_auth_headers):
        """gh search is blocked entirely in private mode (too broad)."""
        response = client.post(
            "/api/v1/gh/execute",
            headers=private_mode_auth_headers,
            data=json.dumps({"args": ["search", "repos", "query"]}),
            content_type="application/json",
        )

        assert response.status_code == 403
        data = json.loads(response.data)
        assert data["success"] is False
        assert "private mode" in data["message"].lower()

    def test_search_allowed_in_public_mode(self, client, auth_headers):
        """gh search is allowed in public mode."""
        with patch.object(gateway, "get_github_client") as mock_gh:
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.stdout = "search results"
            mock_result.stderr = ""
            mock_result.to_dict.return_value = {
                "success": True,
                "stdout": "search results",
                "stderr": "",
            }
            mock_gh.return_value.execute.return_value = mock_result

            response = client.post(
                "/api/v1/gh/execute",
                headers=auth_headers,
                data=json.dumps({"args": ["search", "repos", "query"]}),
                content_type="application/json",
            )

            # Should succeed (not blocked)
            assert response.status_code == 200

    def test_gh_repo_view_public_blocked_in_private_mode(self, client, private_mode_auth_headers):
        """gh repo view of public repo blocked in private mode (full integration)."""
        with patch("private_repo_policy.get_repo_visibility", return_value="public"):
            response = client.post(
                "/api/v1/gh/execute",
                headers=private_mode_auth_headers,
                data=json.dumps({"args": ["repo", "view", "torvalds/linux"]}),
                content_type="application/json",
            )

            assert response.status_code == 403
            data = json.loads(response.data)
            assert data["success"] is False

    def test_gh_api_repos_path_blocked_in_private_mode(self, client, private_mode_auth_headers):
        """gh api /repos/owner/repo/... blocked for public repos in private mode."""
        with patch("private_repo_policy.get_repo_visibility", return_value="public"):
            response = client.post(
                "/api/v1/gh/execute",
                headers=private_mode_auth_headers,
                data=json.dumps({"args": ["api", "repos/torvalds/linux/issues"]}),
                content_type="application/json",
            )

            assert response.status_code == 403
            data = json.loads(response.data)
            assert data["success"] is False


class TestRepoExtraction:
    """Tests for extract_repo_from_gh_command and extract_repo_from_gh_api_path."""

    def test_extract_repo_from_gh_api_path_standard(self):
        """Extract repo from standard repos/ API path."""
        from github_client import extract_repo_from_gh_api_path

        assert extract_repo_from_gh_api_path("repos/owner/repo/pulls") == "owner/repo"
        assert extract_repo_from_gh_api_path("repos/owner/repo") == "owner/repo"
        assert extract_repo_from_gh_api_path("/repos/owner/repo/issues/123") == "owner/repo"

    def test_extract_repo_from_gh_api_path_non_repo(self):
        """Non-repo paths return None."""
        from github_client import extract_repo_from_gh_api_path

        assert extract_repo_from_gh_api_path("user") is None
        assert extract_repo_from_gh_api_path("orgs/myorg/repos") is None
        assert extract_repo_from_gh_api_path("rate_limit") is None

    def test_extract_repo_from_gh_command_repo_flag(self):
        """Extract repo from --repo/-R flag."""
        from github_client import extract_repo_from_gh_command

        assert (
            extract_repo_from_gh_command(["pr", "view", "123", "-R", "owner/repo"]) == "owner/repo"
        )
        assert extract_repo_from_gh_command(["pr", "list", "--repo", "owner/repo"]) == "owner/repo"

    def test_extract_repo_from_gh_command_positional(self):
        """Extract repo from positional args in gh repo commands."""
        from github_client import extract_repo_from_gh_command

        assert extract_repo_from_gh_command(["repo", "view", "owner/repo"]) == "owner/repo"
        assert extract_repo_from_gh_command(["repo", "clone", "owner/repo"]) == "owner/repo"
        assert extract_repo_from_gh_command(["repo", "fork", "owner/repo"]) == "owner/repo"

    def test_extract_repo_from_gh_command_api_path(self):
        """Extract repo from gh api path."""
        from github_client import extract_repo_from_gh_command

        assert extract_repo_from_gh_command(["api", "/repos/owner/repo/issues"]) == "owner/repo"
        assert extract_repo_from_gh_command(["api", "repos/owner/repo/pulls/123"]) == "owner/repo"

    def test_extract_repo_from_gh_command_api_with_flags(self):
        """Extract repo from gh api with flags before path."""
        from github_client import extract_repo_from_gh_command

        # Flags before the API path should be skipped correctly
        assert (
            extract_repo_from_gh_command(
                ["api", "-X", "GET", "-H", "Accept: application/json", "repos/owner/repo/issues"]
            )
            == "owner/repo"
        )
        assert (
            extract_repo_from_gh_command(
                ["api", "--method", "POST", "-f", "title=test", "repos/owner/repo/pulls"]
            )
            == "owner/repo"
        )

    def test_extract_repo_from_gh_command_none(self):
        """Return None when repo cannot be determined."""
        from github_client import extract_repo_from_gh_command

        assert extract_repo_from_gh_command(["auth", "status"]) is None
        assert extract_repo_from_gh_command(["api", "/rate_limit"]) is None
        assert extract_repo_from_gh_command([]) is None

    def test_extract_repo_repo_flag_takes_priority(self):
        """--repo flag takes priority over positional args."""
        from github_client import extract_repo_from_gh_command

        # Even if positional looks like a repo, --repo flag wins
        assert (
            extract_repo_from_gh_command(["repo", "view", "other/repo", "-R", "owner/repo"])
            == "owner/repo"
        )

    def test_extract_repo_from_gh_api_path_with_template_variables(self):
        """Template variables in API path return None (need resolution first)."""
        from github_client import extract_repo_from_gh_api_path

        # Paths with {owner} and {repo} template variables should return None
        # because they need to be resolved from the current repo's git remote
        assert extract_repo_from_gh_api_path("repos/{owner}/{repo}/pulls") is None
        assert extract_repo_from_gh_api_path("repos/{owner}/{repo}/pulls/123/commits") is None
        assert extract_repo_from_gh_api_path("/repos/{owner}/{repo}/issues") is None

    def test_has_gh_template_variables(self):
        """Test detection of template variables in API paths."""
        from github_client import has_gh_template_variables

        # Paths with template variables
        assert has_gh_template_variables("repos/{owner}/{repo}/pulls") is True
        assert has_gh_template_variables("repos/{owner}/myrepo/pulls") is True
        assert has_gh_template_variables("repos/myowner/{repo}/pulls") is True

        # Paths without template variables
        assert has_gh_template_variables("repos/owner/repo/pulls") is False
        assert has_gh_template_variables("user") is False
        assert has_gh_template_variables("rate_limit") is False

    def test_resolve_gh_api_template_variables_no_variables(self):
        """Paths without template variables are returned unchanged."""
        from github_client import resolve_gh_api_template_variables

        # No resolution needed, return as-is
        assert (
            resolve_gh_api_template_variables("repos/owner/repo/pulls", None)
            == "repos/owner/repo/pulls"
        )
        assert resolve_gh_api_template_variables("user", None) == "user"

    def test_resolve_gh_api_template_variables_no_cwd(self):
        """Template variables without cwd return None."""
        from github_client import resolve_gh_api_template_variables

        # Cannot resolve without cwd
        assert resolve_gh_api_template_variables("repos/{owner}/{repo}/pulls", None) is None

    def test_resolve_gh_api_template_variables_with_cwd(self):
        """Template variables are resolved from cwd's git remote."""
        import repo_parser
        from github_client import resolve_gh_api_template_variables
        from repo_parser import RepoInfo

        # Patch repo_parser module - the import happens at runtime inside the function
        with patch.object(repo_parser, "get_remote_url") as mock_get_remote:
            with patch.object(repo_parser, "parse_github_url") as mock_parse:
                mock_get_remote.return_value = "https://github.com/myowner/myrepo.git"
                mock_parse.return_value = RepoInfo(owner="myowner", repo="myrepo")

                result = resolve_gh_api_template_variables(
                    "repos/{owner}/{repo}/pulls/123/commits", "/path/to/repo"
                )
                assert result == "repos/myowner/myrepo/pulls/123/commits"

                mock_get_remote.assert_called_once_with("/path/to/repo", "origin")

    def test_resolve_gh_api_template_variables_remote_failure(self):
        """Template variable resolution fails gracefully when remote unavailable."""
        import repo_parser
        from github_client import resolve_gh_api_template_variables

        # Patch repo_parser module - the import happens at runtime inside the function
        with patch.object(repo_parser, "get_remote_url") as mock_get_remote:
            mock_get_remote.return_value = None

            result = resolve_gh_api_template_variables(
                "repos/{owner}/{repo}/pulls", "/path/to/repo"
            )
            assert result is None

    def test_resolve_gh_api_template_variables_non_github_remote(self):
        """Template resolution fails when remote is not a GitHub URL."""
        import repo_parser
        from github_client import resolve_gh_api_template_variables

        # Patch repo_parser module - the import happens at runtime inside the function
        with patch.object(repo_parser, "get_remote_url") as mock_get_remote:
            with patch.object(repo_parser, "parse_github_url") as mock_parse:
                # Remote exists but is not a GitHub URL
                mock_get_remote.return_value = "https://gitlab.com/myowner/myrepo.git"
                mock_parse.return_value = None

                result = resolve_gh_api_template_variables(
                    "repos/{owner}/{repo}/pulls", "/path/to/repo"
                )
                assert result is None

    def test_resolve_gh_api_template_variables_invalid_owner(self):
        """Template resolution fails when owner contains invalid characters."""
        import repo_parser
        from github_client import resolve_gh_api_template_variables
        from repo_parser import RepoInfo

        with patch.object(repo_parser, "get_remote_url") as mock_get_remote:
            with patch.object(repo_parser, "parse_github_url") as mock_parse:
                mock_get_remote.return_value = "https://github.com/bad/../owner/repo.git"
                # Simulate a malicious remote with path traversal in owner
                mock_parse.return_value = RepoInfo(owner="../admin", repo="myrepo")

                result = resolve_gh_api_template_variables(
                    "repos/{owner}/{repo}/pulls", "/path/to/repo"
                )
                assert result is None

    def test_resolve_gh_api_template_variables_invalid_repo(self):
        """Template resolution fails when repo contains invalid characters."""
        import repo_parser
        from github_client import resolve_gh_api_template_variables
        from repo_parser import RepoInfo

        with patch.object(repo_parser, "get_remote_url") as mock_get_remote:
            with patch.object(repo_parser, "parse_github_url") as mock_parse:
                mock_get_remote.return_value = "https://github.com/owner/bad.git"
                # Simulate a malicious remote with path traversal in repo
                mock_parse.return_value = RepoInfo(owner="myowner", repo="../../admin/users")

                result = resolve_gh_api_template_variables(
                    "repos/{owner}/{repo}/pulls", "/path/to/repo"
                )
                assert result is None

    def test_extract_repo_from_gh_command_with_template_variables(self):
        """gh api commands with template variables return None for repo."""
        from github_client import extract_repo_from_gh_command

        # Template variables in API path mean repo can't be extracted
        assert extract_repo_from_gh_command(["api", "repos/{owner}/{repo}/pulls"]) is None
        assert (
            extract_repo_from_gh_command(["api", "repos/{owner}/{repo}/pulls/123/commits"]) is None
        )


class TestGitApplyAndFormatPatch:
    """Tests for git apply and format-patch operations (issue #118)."""

    def test_apply_succeeds(self, client, auth_headers):
        """git apply executes successfully through the gateway."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="",
                stderr="",
            )

            response = client.post(
                "/api/v1/git/execute",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "repo_path": "/home/egg/repos/test",
                        "operation": "apply",
                        "args": ["--check", "patch.diff"],
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 200
            call_args = mock_run.call_args[0][0]
            assert "apply" in call_args
            assert "--check" in call_args
            assert "patch.diff" in call_args

    def test_apply_rejects_unknown_flags(self, client, auth_headers):
        """git apply rejects unknown flags."""
        response = client.post(
            "/api/v1/git/execute",
            headers=auth_headers,
            data=json.dumps(
                {
                    "repo_path": "/home/egg/repos/test",
                    "operation": "apply",
                    "args": ["--exec=evil"],
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert "not allowed" in data["message"]

    def test_format_patch_succeeds(self, client, auth_headers):
        """git format-patch executes successfully through the gateway."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="0001-test.patch\n",
                stderr="",
            )

            response = client.post(
                "/api/v1/git/execute",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "repo_path": "/home/egg/repos/test",
                        "operation": "format-patch",
                        "args": ["--stdout", "-1", "HEAD"],
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 200
            call_args = mock_run.call_args[0][0]
            assert "format-patch" in call_args
            assert "--stdout" in call_args

    def test_format_patch_rejects_unknown_flags(self, client, auth_headers):
        """git format-patch rejects unknown flags."""
        response = client.post(
            "/api/v1/git/execute",
            headers=auth_headers,
            data=json.dumps(
                {
                    "repo_path": "/home/egg/repos/test",
                    "operation": "format-patch",
                    "args": ["--exec=evil"],
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert "not allowed" in data["message"]

    def test_apply_does_not_inject_no_verify(self, client, auth_headers):
        """git apply should not get --no-verify (it doesn't support it)."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="",
                stderr="",
            )

            response = client.post(
                "/api/v1/git/execute",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "repo_path": "/home/egg/repos/test",
                        "operation": "apply",
                        "args": ["patch.diff"],
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 200
            call_args = mock_run.call_args[0][0]
            assert "--no-verify" not in call_args
            assert "core.hooksPath=/dev/null" in call_args


class TestGitHookDisabling:
    """Tests for git hook disabling (issue #58 security fix).

    Git hooks execute in the gateway (trusted environment), not in the sandbox.
    A malicious repository could include pre-commit hooks that execute arbitrary
    code. We use a defense-in-depth approach:

    1. Primary: core.hooksPath=/dev/null disables ALL hooks for all git operations
    2. Belt-and-suspenders: --no-verify for operations that support it (commit,
       merge, am, push)

    Note: cherry-pick is NOT included in --no-verify injection because git <2.36
    does not support it (see issue #118).
    """

    def test_commit_injects_no_verify(self, client, auth_headers):
        """Commit operations get --no-verify to disable hooks."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="[main abc1234] Test commit",
                stderr="",
            )

            response = client.post(
                "/api/v1/git/execute",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "repo_path": "/home/egg/repos/test",
                        "operation": "commit",
                        "args": ["-m", "Test commit"],
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 200
            # Verify --no-verify was injected into the command
            call_args = mock_run.call_args[0][0]
            assert "--no-verify" in call_args
            # --no-verify should come before user args
            no_verify_idx = call_args.index("--no-verify")
            commit_idx = call_args.index("commit")
            assert no_verify_idx == commit_idx + 1

    def test_merge_injects_no_verify(self, client, auth_headers):
        """Merge operations get --no-verify to disable hooks."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="Merge made",
                stderr="",
            )

            response = client.post(
                "/api/v1/git/execute",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "repo_path": "/home/egg/repos/test",
                        "operation": "merge",
                        "args": ["feature-branch"],
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 200
            call_args = mock_run.call_args[0][0]
            assert "--no-verify" in call_args

    def test_cherry_pick_does_not_inject_no_verify(self, client, auth_headers):
        """Cherry-pick does NOT get --no-verify (unsupported on git <2.36).

        The --no-verify flag was added to cherry-pick in git 2.36. Older versions
        reject it with a usage error. The primary protection (core.hooksPath=/dev/null)
        already covers cherry-pick. See issue #118.
        """
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="[main abc1234] Cherry-picked",
                stderr="",
            )

            response = client.post(
                "/api/v1/git/execute",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "repo_path": "/home/egg/repos/test",
                        "operation": "cherry-pick",
                        "args": ["abc1234"],
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 200
            call_args = mock_run.call_args[0][0]
            # cherry-pick should NOT have --no-verify (not supported on git <2.36)
            assert "--no-verify" not in call_args
            # But core.hooksPath=/dev/null should still be present
            assert "core.hooksPath=/dev/null" in call_args

    def test_push_includes_no_verify(self, client, auth_headers):
        """Push operations include --no-verify to disable pre-push hooks."""
        with (
            patch("subprocess.run") as mock_run,
            patch.object(gateway, "get_policy_engine") as mock_policy,
            patch.object(gateway, "get_token_for_repo") as mock_get_token,
        ):
            # Configure subprocess.run to return different values based on args
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
                    # Verify --no-verify is in the push command
                    assert "--no-verify" in cmd, "Push command must include --no-verify"
                else:
                    result.stdout = ""
                return result

            mock_run.side_effect = run_side_effect

            mock_engine = MagicMock()
            mock_engine.check_branch_ownership.return_value = PolicyResult(
                allowed=True,
                reason="Branch is owned by james-in-a-box",
                details={"branch": "egg-feature"},
            )
            mock_policy.return_value = mock_engine
            mock_get_token.return_value = ("test-token", "bot", "")

            response = client.post(
                "/api/v1/git/push",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "repo_path": "/home/egg/repos/test-repo",
                        "remote": "origin",
                        "refspec": "egg-feature",
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 200

    def test_am_injects_no_verify(self, client, auth_headers):
        """Am (apply mailbox) operations get --no-verify to disable hooks."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="Applying: patch",
                stderr="",
            )

            response = client.post(
                "/api/v1/git/execute",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "repo_path": "/home/egg/repos/test",
                        "operation": "am",
                        "args": ["--abort"],
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 200
            call_args = mock_run.call_args[0][0]
            assert "--no-verify" in call_args

    def test_status_does_not_inject_no_verify(self, client, auth_headers):
        """Status and other safe operations do not get --no-verify."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="",
                stderr="",
            )

            response = client.post(
                "/api/v1/git/execute",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "repo_path": "/home/egg/repos/test",
                        "operation": "status",
                        "args": ["--porcelain"],
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 200
            call_args = mock_run.call_args[0][0]
            # status should NOT have --no-verify (it doesn't support it)
            assert "--no-verify" not in call_args

    def test_all_operations_have_hooks_disabled_via_config(self, client, auth_headers):
        """All git operations have core.hooksPath=/dev/null to disable hooks globally."""
        # Test multiple operations to ensure the config is always present
        operations_to_test = [
            ("status", ["--porcelain"]),
            ("checkout", ["-b", "test-branch"]),
            ("rebase", ["--abort"]),
            ("log", ["--oneline"]),
        ]

        for operation, args in operations_to_test:
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0,
                    stdout="",
                    stderr="",
                )

                response = client.post(
                    "/api/v1/git/execute",
                    headers=auth_headers,
                    data=json.dumps(
                        {
                            "repo_path": "/home/egg/repos/test",
                            "operation": operation,
                            "args": args,
                        }
                    ),
                    content_type="application/json",
                )

                assert response.status_code == 200, f"Failed for {operation}"
                call_args = mock_run.call_args[0][0]
                # Verify core.hooksPath=/dev/null is in the command
                assert "core.hooksPath=/dev/null" in call_args, (
                    f"core.hooksPath=/dev/null missing for {operation}: {call_args}"
                )


class TestGitEditorEnv:
    """Tests for GIT_EDITOR=true in git subprocess environment (issue #235).

    Operations like `rebase --continue` after conflict resolution need an
    editor to confirm the commit message. In the gateway's headless
    environment, GIT_EDITOR=true makes git accept the default message.
    """

    def test_git_execute_sets_git_editor(self, client, auth_headers):
        """Git execute subprocess gets GIT_EDITOR=true in its environment."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="",
                stderr="",
            )

            response = client.post(
                "/api/v1/git/execute",
                headers=auth_headers,
                data=json.dumps(
                    {
                        "repo_path": "/home/egg/repos/test",
                        "operation": "rebase",
                        "args": ["--continue"],
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 200
            call_kwargs = mock_run.call_args[1]
            assert "env" in call_kwargs
            assert call_kwargs["env"].get("GIT_EDITOR") == "true"


class TestSessionPhaseUpdate:
    """Tests for PATCH /api/v1/sessions/<token>/phase endpoint."""

    def test_session_phase_update_requires_launcher_auth(self, client):
        """Session phase update requires launcher authentication."""
        response = client.patch(
            "/api/v1/sessions/test-token/phase",
            data=json.dumps({"phase": "pr"}),
            content_type="application/json",
        )

        assert response.status_code == 401

    def test_session_phase_update_requires_phase(self, client, launcher_auth_headers):
        """Session phase update requires phase parameter."""
        response = client.patch(
            "/api/v1/sessions/test-token/phase",
            headers=launcher_auth_headers,
            data=json.dumps({"foo": "bar"}),  # Non-empty body without phase
            content_type="application/json",
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert "phase" in data["message"].lower()

    def test_session_phase_update_validates_phase(self, client, launcher_auth_headers):
        """Session phase update validates phase value."""
        response = client.patch(
            "/api/v1/sessions/test-token/phase",
            headers=launcher_auth_headers,
            data=json.dumps({"phase": "invalid-phase"}),
            content_type="application/json",
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert "invalid" in data["message"].lower()

    def test_session_phase_update_success(self, client, launcher_auth_headers, tmp_path):
        """Session phase update succeeds with valid parameters."""
        from session_manager import SessionManager

        # Create a real session manager with temp file
        manager = SessionManager(persistence_file=tmp_path / "sessions.json")
        token, _ = manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            phase="implement",
        )

        with patch.object(gateway, "get_session_manager", return_value=manager):
            response = client.patch(
                f"/api/v1/sessions/{token}/phase",
                headers=launcher_auth_headers,
                data=json.dumps({"phase": "pr"}),
                content_type="application/json",
            )

            assert response.status_code == 200
            data = json.loads(response.data)
            assert data["success"] is True
            assert data["data"]["phase"] == "pr"

            # Verify session was updated
            session = manager.get_session(token)
            assert session.phase == "pr"

    def test_session_phase_update_session_not_found(self, client, launcher_auth_headers):
        """Session phase update returns 404 for unknown session."""
        with patch.object(gateway, "get_session_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.update_phase.return_value = False
            mock_get_manager.return_value = mock_manager

            response = client.patch(
                "/api/v1/sessions/unknown-token/phase",
                headers=launcher_auth_headers,
                data=json.dumps({"phase": "pr"}),
                content_type="application/json",
            )

            assert response.status_code == 404


class TestSessionCreateWithPhase:
    """Tests for session creation with phase parameter."""

    def test_session_create_accepts_phase(self, client, launcher_auth_headers, tmp_path):
        """Session create accepts optional phase parameter."""
        from session_manager import SessionManager

        manager = SessionManager(persistence_file=tmp_path / "sessions.json")

        with (
            patch.object(gateway, "get_session_manager", return_value=manager),
            patch.object(gateway, "get_repo_visibility", return_value="private"),
            patch.object(gateway, "get_worktree_manager") as mock_worktree,
        ):
            # Mock worktree creation
            mock_worktree_info = MagicMock()
            mock_worktree_info.worktree_path = "/path/to/worktree"
            mock_worktree.return_value.create_worktree.return_value = mock_worktree_info

            response = client.post(
                "/api/v1/sessions/create",
                headers=launcher_auth_headers,
                data=json.dumps(
                    {
                        "container_id": "test-container",
                        "container_ip": "172.18.0.5",
                        "mode": "private",
                        "repos": ["owner/repo"],
                        "phase": "implement",
                    }
                ),
                content_type="application/json",
            )

            assert response.status_code == 200
            data = json.loads(response.data)
            assert data["success"] is True
            assert "session_token" in data["data"]

            # Verify session was created with phase
            token = data["data"]["session_token"]
            session = manager.get_session(token)
            assert session is not None
            assert session.phase == "implement"

    def test_session_create_validates_phase(self, client, launcher_auth_headers):
        """Session create validates phase parameter."""
        response = client.post(
            "/api/v1/sessions/create",
            headers=launcher_auth_headers,
            data=json.dumps(
                {
                    "container_id": "test-container",
                    "container_ip": "172.18.0.5",
                    "mode": "private",
                    "repos": ["owner/repo"],
                    "phase": "invalid-phase",
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert "invalid" in data["message"].lower()
