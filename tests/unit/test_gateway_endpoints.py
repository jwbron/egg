"""Unit tests for Phase 2a gateway endpoints.

Tests the 6 new endpoints added in PR #19:
- POST /api/v1/gh/pr/create
- POST /api/v1/gh/pr/comment
- POST /api/v1/gh/pr/edit
- POST /api/v1/gh/pr/close
- POST /api/v1/sessions/<token>/heartbeat
- GET /api/v1/repos/visibility
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

# We need to patch modules before importing gateway
# Use autouse fixture to set up test environment


@pytest.fixture
def mock_gateway_dependencies():
    """Mock all external dependencies for gateway tests."""
    with (
        patch("gateway.gateway.get_github_client") as mock_gh_client,
        patch("gateway.gateway.get_policy_engine") as mock_policy,
        patch("gateway.gateway.get_session_manager") as mock_session_mgr,
        patch("gateway.gateway.get_auth_mode") as mock_auth_mode,
        patch("gateway.gateway.get_repo_visibility") as mock_visibility,
        patch("gateway.gateway.check_private_repo_access") as mock_priv_access,
        patch("gateway.gateway.check_heartbeat_rate_limit") as mock_rate_limit,
        patch("gateway.gateway.validate_session_for_request") as mock_validate_session,
        patch("gateway.gateway.record_failed_lookup") as mock_failed_lookup,
        patch("gateway.gateway.parse_owner_repo") as mock_parse_repo,
        patch("gateway.gateway.get_launcher_secret") as mock_launcher_secret,
    ):
        # Configure mock_auth_mode default
        mock_auth_mode.return_value = "bot"

        # Configure mock_parse_repo to return a simple result
        mock_repo_info = MagicMock()
        mock_repo_info.owner = "test-owner"
        mock_repo_info.repo = "test-repo"
        mock_parse_repo.return_value = mock_repo_info

        # Configure mock_priv_access to allow by default
        mock_priv_result = MagicMock()
        mock_priv_result.allowed = True
        mock_priv_access.return_value = mock_priv_result

        # Configure mock_rate_limit to allow by default
        mock_rate_result = MagicMock()
        mock_rate_result.allowed = True
        mock_rate_limit.return_value = mock_rate_result

        # Configure launcher secret
        mock_launcher_secret.return_value = "test-launcher-secret"

        yield {
            "github_client": mock_gh_client,
            "policy_engine": mock_policy,
            "session_manager": mock_session_mgr,
            "auth_mode": mock_auth_mode,
            "visibility": mock_visibility,
            "private_access": mock_priv_access,
            "rate_limit": mock_rate_limit,
            "validate_session": mock_validate_session,
            "failed_lookup": mock_failed_lookup,
            "parse_repo": mock_parse_repo,
            "launcher_secret": mock_launcher_secret,
        }


@pytest.fixture
def client(mock_gateway_dependencies):
    """Create Flask test client with mocked dependencies."""
    from gateway.gateway import app

    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@pytest.fixture
def valid_session_token():
    """A valid session token for testing."""
    return "test-session-token-12345"


@pytest.fixture
def mock_valid_session(mock_gateway_dependencies):
    """Configure mocks for a valid session."""
    mock_session = MagicMock()
    mock_session.mode = "public"
    mock_session.container_id = "container-123"
    mock_session.expires_at = datetime.now(UTC) + timedelta(hours=24)

    mock_result = MagicMock()
    mock_result.valid = True
    mock_result.session = mock_session
    mock_result.error = None

    mock_gateway_dependencies["validate_session"].return_value = mock_result
    return mock_session


class TestGhPrCreate:
    """Tests for POST /api/v1/gh/pr/create endpoint."""

    def test_creates_pr_successfully(self, client, mock_gateway_dependencies, mock_valid_session):
        """Test successful PR creation."""
        # Configure policy to allow
        mock_policy = MagicMock()
        mock_policy_result = MagicMock()
        mock_policy_result.allowed = True
        mock_policy.check_pr_create_allowed.return_value = mock_policy_result
        mock_gateway_dependencies["policy_engine"].return_value = mock_policy

        # Configure GitHub client to succeed
        mock_gh = MagicMock()
        mock_exec_result = MagicMock()
        mock_exec_result.success = True
        mock_exec_result.stdout = "https://github.com/test-owner/test-repo/pull/42"
        mock_exec_result.stderr = ""
        mock_gh.execute.return_value = mock_exec_result
        mock_gateway_dependencies["github_client"].return_value = mock_gh

        response = client.post(
            "/api/v1/gh/pr/create",
            json={
                "repo": "test-owner/test-repo",
                "title": "Test PR",
                "body": "Test body",
                "base": "main",
                "head": "feature-branch",
            },
            headers={"Authorization": "Bearer test-session-token"},
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        assert "PR created" in data["message"]

    def test_missing_repo_returns_400(self, client, mock_gateway_dependencies, mock_valid_session):
        """Test that missing repo returns 400."""
        response = client.post(
            "/api/v1/gh/pr/create",
            json={
                "title": "Test PR",
                "head": "feature-branch",
            },
            headers={"Authorization": "Bearer test-session-token"},
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data["success"] is False
        assert "Missing repo" in data["message"]

    def test_missing_title_returns_400(self, client, mock_gateway_dependencies, mock_valid_session):
        """Test that missing title returns 400."""
        response = client.post(
            "/api/v1/gh/pr/create",
            json={
                "repo": "test-owner/test-repo",
                "head": "feature-branch",
            },
            headers={"Authorization": "Bearer test-session-token"},
        )

        assert response.status_code == 400
        data = response.get_json()
        assert "Missing title" in data["message"]

    def test_missing_head_returns_400(self, client, mock_gateway_dependencies, mock_valid_session):
        """Test that missing head branch returns 400."""
        response = client.post(
            "/api/v1/gh/pr/create",
            json={
                "repo": "test-owner/test-repo",
                "title": "Test PR",
            },
            headers={"Authorization": "Bearer test-session-token"},
        )

        assert response.status_code == 400
        assert "Missing head" in response.get_json()["message"]

    def test_blocked_in_user_mode(self, client, mock_gateway_dependencies, mock_valid_session):
        """Test that PR creation is blocked in user mode."""
        mock_gateway_dependencies["auth_mode"].return_value = "user"

        # Configure policy to deny
        mock_policy = MagicMock()
        mock_policy_result = MagicMock()
        mock_policy_result.allowed = False
        mock_policy_result.reason = "PR creation is not allowed in user mode"
        mock_policy_result.details = {"auth_mode": "user"}
        mock_policy.check_pr_create_allowed.return_value = mock_policy_result
        mock_gateway_dependencies["policy_engine"].return_value = mock_policy

        response = client.post(
            "/api/v1/gh/pr/create",
            json={
                "repo": "test-owner/test-repo",
                "title": "Test PR",
                "body": "Test body",
                "head": "feature-branch",
            },
            headers={"Authorization": "Bearer test-session-token"},
        )

        assert response.status_code == 403
        data = response.get_json()
        assert data["success"] is False
        assert "user mode" in data["message"].lower()

    def test_requires_auth(self, client, mock_gateway_dependencies):
        """Test that endpoint requires authentication."""
        mock_result = MagicMock()
        mock_result.valid = False
        mock_result.error = "Invalid token"
        mock_gateway_dependencies["validate_session"].return_value = mock_result

        response = client.post(
            "/api/v1/gh/pr/create",
            json={
                "repo": "test-owner/test-repo",
                "title": "Test PR",
                "head": "feature-branch",
            },
            headers={"Authorization": "Bearer invalid-token"},
        )

        assert response.status_code == 401


class TestGhPrComment:
    """Tests for POST /api/v1/gh/pr/comment endpoint."""

    def test_adds_comment_successfully(self, client, mock_gateway_dependencies, mock_valid_session):
        """Test successful comment addition."""
        # Configure policy to allow
        mock_policy = MagicMock()
        mock_policy_result = MagicMock()
        mock_policy_result.allowed = True
        mock_policy.check_pr_comment_allowed.return_value = mock_policy_result
        mock_gateway_dependencies["policy_engine"].return_value = mock_policy

        # Configure GitHub client to succeed
        mock_gh = MagicMock()
        mock_exec_result = MagicMock()
        mock_exec_result.success = True
        mock_exec_result.stdout = "Comment added"
        mock_gh.execute.return_value = mock_exec_result
        mock_gateway_dependencies["github_client"].return_value = mock_gh

        response = client.post(
            "/api/v1/gh/pr/comment",
            json={
                "repo": "test-owner/test-repo",
                "pr_number": 42,
                "body": "LGTM!",
            },
            headers={"Authorization": "Bearer test-session-token"},
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        assert "Comment added" in data["message"]

    def test_missing_repo_returns_400(self, client, mock_gateway_dependencies, mock_valid_session):
        """Test that missing repo returns 400."""
        response = client.post(
            "/api/v1/gh/pr/comment",
            json={
                "pr_number": 42,
                "body": "Comment",
            },
            headers={"Authorization": "Bearer test-session-token"},
        )

        assert response.status_code == 400
        assert "Missing repo" in response.get_json()["message"]

    def test_missing_pr_number_returns_400(
        self, client, mock_gateway_dependencies, mock_valid_session
    ):
        """Test that missing pr_number returns 400."""
        response = client.post(
            "/api/v1/gh/pr/comment",
            json={
                "repo": "test-owner/test-repo",
                "body": "Comment",
            },
            headers={"Authorization": "Bearer test-session-token"},
        )

        assert response.status_code == 400
        assert "Missing pr_number" in response.get_json()["message"]

    def test_missing_body_returns_400(self, client, mock_gateway_dependencies, mock_valid_session):
        """Test that missing body returns 400."""
        response = client.post(
            "/api/v1/gh/pr/comment",
            json={
                "repo": "test-owner/test-repo",
                "pr_number": 42,
            },
            headers={"Authorization": "Bearer test-session-token"},
        )

        assert response.status_code == 400
        assert "Missing body" in response.get_json()["message"]

    def test_allowed_on_any_pr(self, client, mock_gateway_dependencies, mock_valid_session):
        """Test that comments are allowed on any PR (not just owned)."""
        # Configure policy to allow (comments should always be allowed on any PR)
        mock_policy = MagicMock()
        mock_policy_result = MagicMock()
        mock_policy_result.allowed = True
        mock_policy_result.reason = "Comments are allowed on any PR"
        mock_policy.check_pr_comment_allowed.return_value = mock_policy_result
        mock_gateway_dependencies["policy_engine"].return_value = mock_policy

        # Configure GitHub client
        mock_gh = MagicMock()
        mock_exec_result = MagicMock()
        mock_exec_result.success = True
        mock_exec_result.stdout = ""
        mock_gh.execute.return_value = mock_exec_result
        mock_gateway_dependencies["github_client"].return_value = mock_gh

        response = client.post(
            "/api/v1/gh/pr/comment",
            json={
                "repo": "someone-else/their-repo",
                "pr_number": 999,
                "body": "Nice work!",
            },
            headers={"Authorization": "Bearer test-session-token"},
        )

        assert response.status_code == 200


class TestGhPrEdit:
    """Tests for POST /api/v1/gh/pr/edit endpoint."""

    def test_edits_pr_successfully(self, client, mock_gateway_dependencies, mock_valid_session):
        """Test successful PR edit."""
        # Configure policy to allow (ownership check)
        mock_policy = MagicMock()
        mock_policy_result = MagicMock()
        mock_policy_result.allowed = True
        mock_policy.check_pr_ownership.return_value = mock_policy_result
        mock_gateway_dependencies["policy_engine"].return_value = mock_policy

        # Configure GitHub client
        mock_gh = MagicMock()
        mock_exec_result = MagicMock()
        mock_exec_result.success = True
        mock_exec_result.stdout = "PR updated"
        mock_gh.execute.return_value = mock_exec_result
        mock_gateway_dependencies["github_client"].return_value = mock_gh

        response = client.post(
            "/api/v1/gh/pr/edit",
            json={
                "repo": "test-owner/test-repo",
                "pr_number": 42,
                "title": "Updated title",
                "body": "Updated description",
            },
            headers={"Authorization": "Bearer test-session-token"},
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        assert "PR edited" in data["message"]

    def test_edit_title_only(self, client, mock_gateway_dependencies, mock_valid_session):
        """Test editing only the title."""
        mock_policy = MagicMock()
        mock_policy_result = MagicMock()
        mock_policy_result.allowed = True
        mock_policy.check_pr_ownership.return_value = mock_policy_result
        mock_gateway_dependencies["policy_engine"].return_value = mock_policy

        mock_gh = MagicMock()
        mock_exec_result = MagicMock()
        mock_exec_result.success = True
        mock_exec_result.stdout = ""
        mock_gh.execute.return_value = mock_exec_result
        mock_gateway_dependencies["github_client"].return_value = mock_gh

        response = client.post(
            "/api/v1/gh/pr/edit",
            json={
                "repo": "test-owner/test-repo",
                "pr_number": 42,
                "title": "Only updating title",
            },
            headers={"Authorization": "Bearer test-session-token"},
        )

        assert response.status_code == 200

    def test_missing_both_title_and_body_returns_400(
        self, client, mock_gateway_dependencies, mock_valid_session
    ):
        """Test that request without title or body returns 400."""
        response = client.post(
            "/api/v1/gh/pr/edit",
            json={
                "repo": "test-owner/test-repo",
                "pr_number": 42,
            },
            headers={"Authorization": "Bearer test-session-token"},
        )

        assert response.status_code == 400
        assert "title or body" in response.get_json()["message"]

    def test_denied_for_non_owned_pr(self, client, mock_gateway_dependencies, mock_valid_session):
        """Test that editing non-owned PR is denied."""
        mock_policy = MagicMock()
        mock_policy_result = MagicMock()
        mock_policy_result.allowed = False
        mock_policy_result.reason = "PR is not owned by egg"
        mock_policy_result.details = {"author": "someone-else"}
        mock_policy.check_pr_ownership.return_value = mock_policy_result
        mock_gateway_dependencies["policy_engine"].return_value = mock_policy

        response = client.post(
            "/api/v1/gh/pr/edit",
            json={
                "repo": "test-owner/test-repo",
                "pr_number": 42,
                "title": "Trying to edit",
            },
            headers={"Authorization": "Bearer test-session-token"},
        )

        assert response.status_code == 403
        data = response.get_json()
        assert data["success"] is False
        assert "denied" in data["message"].lower()


class TestGhPrClose:
    """Tests for POST /api/v1/gh/pr/close endpoint."""

    def test_closes_pr_successfully(self, client, mock_gateway_dependencies, mock_valid_session):
        """Test successful PR close."""
        mock_policy = MagicMock()
        mock_policy_result = MagicMock()
        mock_policy_result.allowed = True
        mock_policy.check_pr_ownership.return_value = mock_policy_result
        mock_gateway_dependencies["policy_engine"].return_value = mock_policy

        mock_gh = MagicMock()
        mock_exec_result = MagicMock()
        mock_exec_result.success = True
        mock_exec_result.stdout = "Closed pull request"
        mock_gh.execute.return_value = mock_exec_result
        mock_gateway_dependencies["github_client"].return_value = mock_gh

        response = client.post(
            "/api/v1/gh/pr/close",
            json={
                "repo": "test-owner/test-repo",
                "pr_number": 42,
            },
            headers={"Authorization": "Bearer test-session-token"},
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        assert "closed" in data["message"].lower()

    def test_missing_repo_returns_400(self, client, mock_gateway_dependencies, mock_valid_session):
        """Test that missing repo returns 400."""
        response = client.post(
            "/api/v1/gh/pr/close",
            json={"pr_number": 42},
            headers={"Authorization": "Bearer test-session-token"},
        )

        assert response.status_code == 400
        assert "Missing repo" in response.get_json()["message"]

    def test_missing_pr_number_returns_400(
        self, client, mock_gateway_dependencies, mock_valid_session
    ):
        """Test that missing pr_number returns 400."""
        response = client.post(
            "/api/v1/gh/pr/close",
            json={"repo": "test-owner/test-repo"},
            headers={"Authorization": "Bearer test-session-token"},
        )

        assert response.status_code == 400
        assert "Missing pr_number" in response.get_json()["message"]

    def test_denied_for_non_owned_pr(self, client, mock_gateway_dependencies, mock_valid_session):
        """Test that closing non-owned PR is denied."""
        mock_policy = MagicMock()
        mock_policy_result = MagicMock()
        mock_policy_result.allowed = False
        mock_policy_result.reason = "PR is not owned by egg"
        mock_policy_result.details = {}
        mock_policy.check_pr_ownership.return_value = mock_policy_result
        mock_gateway_dependencies["policy_engine"].return_value = mock_policy

        response = client.post(
            "/api/v1/gh/pr/close",
            json={
                "repo": "test-owner/test-repo",
                "pr_number": 42,
            },
            headers={"Authorization": "Bearer test-session-token"},
        )

        assert response.status_code == 403


class TestSessionHeartbeat:
    """Tests for POST /api/v1/sessions/<token>/heartbeat endpoint."""

    def test_heartbeat_success(self, client, mock_gateway_dependencies):
        """Test successful heartbeat."""
        mock_session = MagicMock()
        mock_session.container_id = "container-123"
        mock_session.expires_at = datetime.now(UTC) + timedelta(hours=24)

        mock_result = MagicMock()
        mock_result.valid = True
        mock_result.session = mock_session
        mock_result.error = None

        mock_gateway_dependencies["validate_session"].return_value = mock_result

        response = client.post(
            "/api/v1/sessions/test-session-token/heartbeat",
            headers={"Authorization": "Bearer test-launcher-secret"},
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        assert "Heartbeat recorded" in data["message"]
        assert "expires_at" in data["data"]

    def test_heartbeat_invalid_session(self, client, mock_gateway_dependencies):
        """Test heartbeat with invalid session returns 401."""
        mock_result = MagicMock()
        mock_result.valid = False
        mock_result.session = None
        mock_result.error = "Invalid session"

        mock_gateway_dependencies["validate_session"].return_value = mock_result

        response = client.post(
            "/api/v1/sessions/invalid-token/heartbeat",
            headers={"Authorization": "Bearer test-launcher-secret"},
        )

        assert response.status_code == 401

    def test_heartbeat_rate_limited(self, client, mock_gateway_dependencies):
        """Test heartbeat respects rate limiting."""
        mock_session = MagicMock()
        mock_session.container_id = "container-123"
        mock_session.expires_at = datetime.now(UTC) + timedelta(hours=24)

        mock_result = MagicMock()
        mock_result.valid = True
        mock_result.session = mock_session

        mock_gateway_dependencies["validate_session"].return_value = mock_result

        # Configure rate limiter to deny
        mock_rate_result = MagicMock()
        mock_rate_result.allowed = False
        mock_rate_result.retry_after_seconds = 60
        mock_gateway_dependencies["rate_limit"].return_value = mock_rate_result

        response = client.post(
            "/api/v1/sessions/test-token/heartbeat",
            headers={"Authorization": "Bearer test-launcher-secret"},
        )

        assert response.status_code == 429
        assert "rate limit" in response.get_json()["message"].lower()

    def test_heartbeat_requires_launcher_auth(self, client, mock_gateway_dependencies):
        """Test heartbeat requires launcher authentication."""
        response = client.post(
            "/api/v1/sessions/test-token/heartbeat",
            headers={"Authorization": "Bearer wrong-secret"},
        )

        assert response.status_code == 401


class TestReposVisibility:
    """Tests for GET /api/v1/repos/visibility endpoint."""

    def test_queries_visibility_successfully(self, client, mock_gateway_dependencies):
        """Test successful visibility query."""
        mock_gateway_dependencies["visibility"].return_value = "public"

        response = client.get(
            "/api/v1/repos/visibility?repos=owner/repo1,owner/repo2",
            headers={"Authorization": "Bearer test-launcher-secret"},
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        assert "visibilities" in data["data"]

    def test_handles_multiple_repos(self, client, mock_gateway_dependencies):
        """Test querying visibility for multiple repos."""
        # Return different values for different calls
        mock_gateway_dependencies["visibility"].side_effect = [
            "public",
            "private",
            "internal",
        ]

        # Configure parse_repo to return different repos
        mock_repo_infos = []
        for name in ["public-repo", "private-repo", "internal-repo"]:
            mock_info = MagicMock()
            mock_info.owner = "owner"
            mock_info.repo = name
            mock_repo_infos.append(mock_info)
        mock_gateway_dependencies["parse_repo"].side_effect = mock_repo_infos

        response = client.get(
            "/api/v1/repos/visibility?repos=owner/public-repo,owner/private-repo,owner/internal-repo",
            headers={"Authorization": "Bearer test-launcher-secret"},
        )

        assert response.status_code == 200
        data = response.get_json()
        visibilities = data["data"]["visibilities"]
        assert visibilities["owner/public-repo"] == "public"
        assert visibilities["owner/private-repo"] == "private"
        assert visibilities["owner/internal-repo"] == "internal"

    def test_missing_repos_param_returns_400(self, client, mock_gateway_dependencies):
        """Test that missing repos param returns 400."""
        response = client.get(
            "/api/v1/repos/visibility",
            headers={"Authorization": "Bearer test-launcher-secret"},
        )

        assert response.status_code == 400
        assert "Missing repos" in response.get_json()["message"]

    def test_empty_repos_param_returns_400(self, client, mock_gateway_dependencies):
        """Test that empty repos param returns 400."""
        response = client.get(
            "/api/v1/repos/visibility?repos=",
            headers={"Authorization": "Bearer test-launcher-secret"},
        )

        assert response.status_code == 400

    def test_handles_invalid_repo_format(self, client, mock_gateway_dependencies):
        """Test handling of invalid repo format (no owner/repo)."""
        # When parse_owner_repo returns None, visibility should be None
        mock_gateway_dependencies["parse_repo"].return_value = None

        response = client.get(
            "/api/v1/repos/visibility?repos=invalid-format",
            headers={"Authorization": "Bearer test-launcher-secret"},
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["data"]["visibilities"]["invalid-format"] is None

    def test_requires_launcher_auth(self, client, mock_gateway_dependencies):
        """Test that endpoint requires launcher authentication."""
        response = client.get(
            "/api/v1/repos/visibility?repos=owner/repo",
            headers={"Authorization": "Bearer wrong-secret"},
        )

        assert response.status_code == 401


class TestPrivateRepoModeIntegration:
    """Tests for Private Repo Mode policy enforcement across endpoints."""

    def test_pr_create_denied_in_private_mode_for_public_repo(
        self, client, mock_gateway_dependencies, mock_valid_session
    ):
        """Test that PR creation for public repo is denied in private mode."""
        # Configure private mode session
        mock_valid_session.mode = "private"

        # Configure private repo access to deny
        mock_priv_result = MagicMock()
        mock_priv_result.allowed = False
        mock_priv_result.reason = "Public repos not allowed in private mode"
        mock_priv_result.visibility = "public"
        mock_priv_result.to_dict.return_value = {
            "visibility": "public",
            "reason": "Public repos not allowed in private mode",
        }
        mock_gateway_dependencies["private_access"].return_value = mock_priv_result

        response = client.post(
            "/api/v1/gh/pr/create",
            json={
                "repo": "test-owner/public-repo",
                "title": "Test PR",
                "head": "feature",
            },
            headers={"Authorization": "Bearer test-session-token"},
        )

        assert response.status_code == 403


class TestMissingRequestBody:
    """Tests for handling missing request bodies."""

    def test_pr_create_empty_json_body(self, client, mock_gateway_dependencies, mock_valid_session):
        """Test PR create with empty JSON body returns validation error."""
        response = client.post(
            "/api/v1/gh/pr/create",
            json={},  # Empty body, not missing
            headers={"Authorization": "Bearer test-session-token"},
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data["success"] is False
        # Should fail on first required field
        assert "Missing" in data["message"]

    def test_pr_comment_empty_json_body(
        self, client, mock_gateway_dependencies, mock_valid_session
    ):
        """Test PR comment with empty JSON body returns validation error."""
        response = client.post(
            "/api/v1/gh/pr/comment",
            json={},  # Empty body, not missing
            headers={"Authorization": "Bearer test-session-token"},
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data["success"] is False
        assert "Missing" in data["message"]
