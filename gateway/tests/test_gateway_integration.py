"""Integration tests for gateway endpoints.

These tests require a running gateway server or mock the Flask app.
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from session_manager import Session, SessionValidationResult

from gateway import app

# Get launcher secret from environment (set by conftest.py)
TEST_LAUNCHER_SECRET = os.environ.get("EGG_LAUNCHER_SECRET", "test-launcher-secret-12345")


def _create_mock_session(mode="private"):
    """Create a mock session for testing."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    return Session(
        session_token="test-session-token",
        session_token_hash="test-hash",
        container_id="test-container",
        container_ip="127.0.0.1",
        mode=mode,
        created_at=now,
        last_seen=now,
        expires_at=now + timedelta(hours=24),
    )


def _mock_session_validation(*args, **kwargs):
    """Return a successful session validation result with private mode."""
    return SessionValidationResult(valid=True, session=_create_mock_session("private"))


def _mock_session_validation_public(*args, **kwargs):
    """Return a successful session validation result with public mode."""
    return SessionValidationResult(valid=True, session=_create_mock_session("public"))


@pytest.fixture
def client():
    """Create a test client for the Flask app."""
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@pytest.fixture
def auth_headers():
    """Create authorization headers with a mock session token."""
    with patch("gateway.validate_session_for_request", side_effect=_mock_session_validation):
        yield {"Authorization": "Bearer test-session-token"}


@pytest.fixture
def launcher_auth_headers():
    """Create authorization headers with the launcher secret."""
    return {"Authorization": f"Bearer {TEST_LAUNCHER_SECRET}"}


class TestHealthEndpoint:
    """Tests for /api/v1/health endpoint."""

    def test_health_no_auth_required(self, client):
        """Health endpoint should not require authentication."""
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        data = json.loads(response.data)
        assert "status" in data


class TestGitExecuteEndpoint:
    """Tests for /api/v1/git/execute endpoint."""

    def test_requires_auth(self, client):
        """Endpoint should require authentication."""
        response = client.post(
            "/api/v1/git/execute",
            json={"repo_path": "/home/egg/repos/test", "operation": "status"},
        )
        assert response.status_code == 401

    @patch("gateway.validate_session_for_request", side_effect=_mock_session_validation)
    def test_missing_body(self, mock_session, client):
        """Missing request body should return error."""
        response = client.post(
            "/api/v1/git/execute",
            headers={"Authorization": "Bearer test-session-token"},
            content_type="application/json",
        )
        assert response.status_code == 400

    @patch("gateway.validate_session_for_request", side_effect=_mock_session_validation)
    def test_missing_repo_path(self, mock_session, client):
        """Missing repo_path should return error."""
        response = client.post(
            "/api/v1/git/execute",
            headers={"Authorization": "Bearer test-session-token"},
            json={"operation": "status"},
        )
        assert response.status_code == 400
        data = json.loads(response.data)
        assert "repo_path" in data["message"].lower()

    @patch("gateway.validate_session_for_request", side_effect=_mock_session_validation)
    def test_missing_operation(self, mock_session, client):
        """Missing operation should return error."""
        response = client.post(
            "/api/v1/git/execute",
            headers={"Authorization": "Bearer test-session-token"},
            json={"repo_path": "/home/egg/repos/test"},
        )
        assert response.status_code == 400
        data = json.loads(response.data)
        assert "operation" in data["message"].lower()

    @patch("gateway.validate_session_for_request", side_effect=_mock_session_validation)
    @patch("gateway.validate_repo_path", return_value=(True, ""))
    @patch("subprocess.run")
    def test_status_command_executed(self, mock_run, mock_validate, mock_session, client):
        """Status command should be executed."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="## main",
            stderr="",
        )

        response = client.post(
            "/api/v1/git/execute",
            headers={"Authorization": "Bearer test-session-token"},
            json={
                "repo_path": "/home/egg/repos/test",
                "operation": "status",
                "args": ["--porcelain"],
            },
        )

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["success"] is True

    @patch("gateway.validate_session_for_request", side_effect=_mock_session_validation_public)
    @patch("gateway.validate_repo_path", return_value=(True, ""))
    @patch("subprocess.run")
    def test_status_command_with_public_mode(self, mock_run, mock_validate, mock_session, client):
        """Status command should work with public mode session."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="## main",
            stderr="",
        )

        response = client.post(
            "/api/v1/git/execute",
            headers={"Authorization": "Bearer test-session-token"},
            json={
                "repo_path": "/home/egg/repos/test",
                "operation": "status",
            },
        )

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["success"] is True

    @patch("gateway.validate_session_for_request", side_effect=_mock_session_validation)
    def test_disallowed_operation_rejected(self, mock_session, client):
        """Operations not in allowlist should be rejected."""
        response = client.post(
            "/api/v1/git/execute",
            headers={"Authorization": "Bearer test-session-token"},
            json={
                "repo_path": "/home/egg/repos/test",
                "operation": "clone",  # Not in allowlist
            },
        )

        assert response.status_code == 403
        data = json.loads(response.data)
        assert "not allowed" in data["message"].lower()

    @patch("gateway.validate_session_for_request", side_effect=_mock_session_validation)
    def test_network_ops_redirected(self, mock_session, client):
        """Network operations should be redirected to dedicated endpoints."""
        for op in ["push", "fetch", "ls-remote"]:
            response = client.post(
                "/api/v1/git/execute",
                headers={"Authorization": "Bearer test-session-token"},
                json={
                    "repo_path": "/home/egg/repos/test",
                    "operation": op,
                },
            )

            assert response.status_code == 400
            data = json.loads(response.data)
            assert "dedicated endpoint" in data["message"].lower()


class TestWorktreeCreateEndpoint:
    """Tests for /api/v1/worktree/create endpoint.

    Note: Worktree endpoints use launcher auth (not session auth) since they
    are called by the launcher during container setup, not from within containers.
    """

    def test_requires_auth(self, client):
        """Endpoint should require authentication."""
        response = client.post(
            "/api/v1/worktree/create",
            json={"container_id": "egg-123", "repos": ["myrepo"]},
        )
        assert response.status_code == 401

    def test_missing_container_id(self, client, launcher_auth_headers):
        """Missing container_id should return error."""
        response = client.post(
            "/api/v1/worktree/create",
            headers=launcher_auth_headers,
            json={"repos": ["myrepo"]},
        )
        assert response.status_code == 400
        data = json.loads(response.data)
        assert "container_id" in data["message"].lower()

    def test_missing_repos(self, client, launcher_auth_headers):
        """Missing repos should return error."""
        response = client.post(
            "/api/v1/worktree/create",
            headers=launcher_auth_headers,
            json={"container_id": "egg-123"},
        )
        assert response.status_code == 400
        data = json.loads(response.data)
        assert "repos" in data["message"].lower()


class TestWorktreeDeleteEndpoint:
    """Tests for /api/v1/worktree/delete endpoint.

    Note: Worktree endpoints use launcher auth (not session auth).
    """

    def test_requires_auth(self, client):
        """Endpoint should require authentication."""
        response = client.post(
            "/api/v1/worktree/delete",
            json={"container_id": "egg-123"},
        )
        assert response.status_code == 401

    def test_missing_container_id(self, client, launcher_auth_headers):
        """Missing container_id should return error."""
        response = client.post(
            "/api/v1/worktree/delete",
            headers=launcher_auth_headers,
            json={"force": True},  # Valid body but missing required container_id
        )
        assert response.status_code == 400
        data = json.loads(response.data)
        assert "container_id" in data["message"].lower()


class TestWorktreeListEndpoint:
    """Tests for /api/v1/worktree/list endpoint.

    Note: Worktree endpoints use launcher auth (not session auth).
    """

    def test_requires_auth(self, client):
        """Endpoint should require authentication."""
        response = client.get("/api/v1/worktree/list")
        assert response.status_code == 401

    @patch("gateway.get_worktree_manager")
    def test_returns_worktrees(self, mock_manager, client, launcher_auth_headers):
        """Should return list of worktrees."""
        mock_manager.return_value.list_worktrees.return_value = [
            {"container_id": "egg-123", "repos": [{"name": "myrepo", "path": "/path"}]}
        ]

        response = client.get(
            "/api/v1/worktree/list",
            headers=launcher_auth_headers,
        )

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["success"] is True
        assert "worktrees" in data["data"]


class TestPathValidation:
    """Tests for path validation in endpoints."""

    @patch("gateway.validate_session_for_request", side_effect=_mock_session_validation)
    def test_path_traversal_blocked(self, mock_session, client):
        """Path traversal attempts should be blocked."""
        response = client.post(
            "/api/v1/git/execute",
            headers={"Authorization": "Bearer test-session-token"},
            json={
                "repo_path": "/home/egg/repos/../../../etc/passwd",
                "operation": "status",
            },
        )

        assert response.status_code == 403
        data = json.loads(response.data)
        assert "allowed directories" in data["message"].lower()

    @patch("gateway.validate_session_for_request", side_effect=_mock_session_validation)
    def test_repos_parent_directory_rejected(self, mock_session, client):
        """Git operations from repos parent directory should fail with clear error.

        This tests the common case where Claude Code or similar tools run
        'git rev-parse' to detect if they're in a repo. When run from /home/egg/repos,
        the gateway should return a clear error without logging a noisy warning.
        """
        response = client.post(
            "/api/v1/git/execute",
            headers={"Authorization": "Bearer test-session-token"},
            json={
                "repo_path": "/home/egg/repos",
                "operation": "rev-parse",
                "args": ["--abbrev-ref", "HEAD"],
            },
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        # Should get a clear error message
        assert (
            "not a git repository" in data["message"].lower()
            or "not itself a git repository" in data["message"].lower()
        )
        # Should include a hint
        assert data.get("data", {}).get("hint") is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
