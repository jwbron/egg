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
    """
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

    with (
        patch.object(gateway, "validate_session_for_request", return_value=mock_result),
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
                reason="Branch 'main' is not owned by egg",
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
        """Push allowed for bot-prefixed branch (egg-)."""
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
                reason="Branch is owned by egg",
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
        """PR comment allowed when egg owns the PR."""
        with (
            patch.object(gateway, "get_policy_engine") as mock_policy,
            patch.object(gateway, "get_github_client") as mock_gh,
        ):
            mock_engine = MagicMock()
            mock_engine.check_pr_ownership.return_value = PolicyResult(
                allowed=True,
                reason="PR is owned by egg",
                details={"author": "egg"},
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
        """PR edit denied when egg doesn't own the PR."""
        with patch.object(gateway, "get_policy_engine") as mock_policy:
            mock_engine = MagicMock()
            mock_engine.check_pr_ownership.return_value = PolicyResult(
                allowed=False,
                reason="PR #123 is not owned by egg",
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
        """PR close denied when egg doesn't own the PR."""
        with patch.object(gateway, "get_policy_engine") as mock_policy:
            mock_engine = MagicMock()
            mock_engine.check_pr_ownership.return_value = PolicyResult(
                allowed=False,
                reason="PR #123 is not owned by egg",
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
        mock_session = MagicMock()
        mock_session.mode = "private"  # Private mode session
        mock_session.container_id = "test-container"
        mock_session.expires_at = None

        mock_result = SessionValidationResult(valid=True, session=mock_session)

        with patch.object(gateway, "validate_session_for_request", return_value=mock_result):
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
        with patch.object(gateway, "get_repo_visibility", return_value="public"):
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
        with patch.object(gateway, "get_repo_visibility", return_value="public"):
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


class TestGitHookDisabling:
    """Tests for git hook disabling (issue #58 security fix).

    Git hooks execute in the gateway (trusted environment), not in the sandbox.
    A malicious repository could include pre-commit hooks that execute arbitrary
    code. We disable all hooks by injecting --no-verify for operations that
    support it.
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

    def test_cherry_pick_injects_no_verify(self, client, auth_headers):
        """Cherry-pick operations get --no-verify to disable hooks."""
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
            assert "--no-verify" in call_args

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
                reason="Branch is owned by egg",
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
