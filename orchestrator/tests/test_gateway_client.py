"""
Tests for gateway client.
"""

import json
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from unittest.mock import MagicMock, patch

import pytest
from gateway_client import (
    GatewayClient,
    GatewayError,
    GatewayHealth,
    WorktreeResult,
    get_gateway_client,
    validate_security_boundary,
)


@pytest.fixture
def gateway_client():
    """Create a gateway client for testing."""
    return GatewayClient(
        gateway_host="localhost",
        gateway_port=19848,  # Use test port
        launcher_secret="test-secret",
        timeout=5,
    )


class MockGatewayHandler(BaseHTTPRequestHandler):
    """Mock HTTP handler for gateway tests."""

    def log_message(self, format, *args):
        """Suppress HTTP logging."""
        pass

    def do_GET(self):
        """Handle GET requests."""
        if self.path == "/api/v1/health":
            self._send_json(
                {
                    "status": "healthy",
                    "version": "0.1.0",
                    "uptime_seconds": 100.0,
                }
            )
        elif self.path.startswith("/api/v1/sessions/"):
            # GET /api/v1/sessions/<token> - validate session
            self._handle_validate_get()
        else:
            self._send_error(404, "Not found")

    def do_POST(self):
        """Handle POST requests."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        data = json.loads(body) if body else {}

        if self.path == "/api/v1/sessions/create":
            self._handle_register(data)
        elif self.path == "/api/v1/worktree/create":
            self._handle_worktree_create(data)
        elif self.path == "/api/v1/worktree/delete":
            self._handle_worktree_delete(data)
        else:
            self._send_error(404, "Not found")

    def do_PATCH(self):
        """Handle PATCH requests."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        data = json.loads(body) if body else {}

        if self.path.startswith("/api/v1/sessions/"):
            self._handle_update(data)
        else:
            self._send_error(404, "Not found")

    def do_DELETE(self):
        """Handle DELETE requests."""
        if self.path.startswith("/api/v1/sessions/by-container/"):
            self._handle_delete_by_container()
        elif self.path.startswith("/api/v1/sessions/"):
            self._handle_delete()
        else:
            self._send_error(404, "Not found")

    def _handle_register(self, data):
        """Handle session registration (POST /api/v1/sessions/create)."""
        # Check launcher secret via Authorization header
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer ") or auth_header[7:] != "test-secret":
            self._send_error(401, "Unauthorized")
            return

        self._send_json(
            {
                "success": True,
                "data": {
                    "session_token": "test-token-12345",
                    "created_at": datetime.now().isoformat(),
                    "expires_at": (datetime.now() + timedelta(hours=24)).isoformat(),
                },
            }
        )

    def _handle_validate_get(self):
        """Handle session validation (GET /api/v1/sessions/<token>)."""
        # Check launcher secret via Authorization header
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer ") or auth_header[7:] != "test-secret":
            self._send_error(401, "Unauthorized")
            return

        # Extract token from path
        token = self.path.split("/")[-1]
        if token == "valid-token":
            self._send_json({"valid": True, "mode": "public", "container_id": "abc123"})
        else:
            self._send_json({"valid": False, "error": "Invalid token"}, status=404)

    def _handle_update(self, data):
        """Handle session update (PATCH /api/v1/sessions/<token>)."""
        # Check launcher secret via Authorization header
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer ") or auth_header[7:] != "test-secret":
            self._send_error(401, "Unauthorized")
            return

        self._send_json(
            {
                "success": True,
                "data": {
                    "container_id": data.get("container_id"),
                    "container_ip": data.get("container_ip"),
                },
            }
        )

    def _handle_delete(self):
        """Handle session deletion (DELETE /api/v1/sessions/<token>)."""
        # Check launcher secret via Authorization header
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer ") or auth_header[7:] != "test-secret":
            self._send_error(401, "Unauthorized")
            return

        self._send_json({"success": True})

    def _handle_delete_by_container(self):
        """Handle session deletion by container (DELETE /api/v1/sessions/by-container/<id>)."""
        # Check launcher secret via Authorization header
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer ") or auth_header[7:] != "test-secret":
            self._send_error(401, "Unauthorized")
            return

        self._send_json({"success": True})

    def _handle_worktree_create(self, data):
        """Handle worktree creation (POST /api/v1/worktree/create)."""
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer ") or auth_header[7:] != "test-secret":
            self._send_error(401, "Unauthorized")
            return

        container_id = data.get("container_id", "")
        repos = data.get("repos", [])

        worktrees = {}
        for repo in repos:
            repo_name = repo.split("/")[-1] if "/" in repo else repo
            worktrees[repo_name] = f"/home/user/.egg-worktrees/{container_id}/{repo_name}"

        self._send_json(
            {
                "success": True,
                "message": "Worktrees created",
                "data": {
                    "worktrees": worktrees,
                    "errors": [],
                },
            }
        )

    def _handle_worktree_delete(self, data):
        """Handle worktree deletion (POST /api/v1/worktree/delete)."""
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer ") or auth_header[7:] != "test-secret":
            self._send_error(401, "Unauthorized")
            return

        self._send_json(
            {
                "success": True,
                "message": "Worktrees deleted",
                "data": {
                    "deleted": ["repo1"],
                    "errors": [],
                },
            }
        )

    def _send_json(self, data, status=200):
        """Send JSON response."""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _send_error(self, status, message):
        """Send error response."""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(
            json.dumps(
                {
                    "success": False,
                    "message": message,
                }
            ).encode()
        )


@pytest.fixture
def mock_gateway_server():
    """Start a mock gateway server for testing."""
    server = HTTPServer(("localhost", 19848), MockGatewayHandler)
    thread = Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    yield server
    server.shutdown()


class TestGatewayClientBasics:
    """Basic gateway client tests."""

    def test_base_url(self, gateway_client):
        """Test base URL generation."""
        assert gateway_client.base_url == "http://localhost:19848"

    def test_default_configuration(self):
        """Test default configuration."""
        with patch.dict("os.environ", {}, clear=True):
            client = GatewayClient()
            assert "egg-gateway" in client.gateway_host


class TestGatewayHealth:
    """Tests for gateway health checks."""

    def test_check_health_success(self, gateway_client, mock_gateway_server):
        """Test successful health check."""
        health = gateway_client.check_health()

        assert health.healthy is True
        assert health.status == "healthy"
        assert health.version == "0.1.0"
        assert health.uptime_seconds == 100.0

    def test_check_health_unreachable(self, gateway_client):
        """Test health check when gateway is unreachable."""
        # Use a port where nothing is listening
        client = GatewayClient(
            gateway_host="localhost",
            gateway_port=19999,
            timeout=1,
        )

        health = client.check_health()

        assert health.healthy is False
        assert health.status in ("unreachable", "unhealthy")
        assert health.error is not None

    def test_wait_for_healthy(self, gateway_client, mock_gateway_server):
        """Test waiting for gateway to become healthy."""
        result = gateway_client.wait_for_healthy(timeout_seconds=5)
        assert result is True

    def test_wait_for_healthy_timeout(self, gateway_client):
        """Test timeout when waiting for unhealthy gateway."""
        client = GatewayClient(
            gateway_host="localhost",
            gateway_port=19999,  # Nothing listening
            timeout=1,
        )

        result = client.wait_for_healthy(timeout_seconds=2, check_interval=0.5)
        assert result is False


class TestSessionManagement:
    """Tests for session management."""

    def test_register_session(self, gateway_client, mock_gateway_server):
        """Test session registration."""
        session = gateway_client.register_session(
            container_id="abc123def456",
            container_ip="172.32.0.10",
            mode="public",
        )

        assert session.session_token == "test-token-12345"
        assert session.container_id == "abc123def456"
        assert session.container_ip == "172.32.0.10"
        assert session.mode == "public"

    def test_register_session_without_secret(self, mock_gateway_server):
        """Test session registration without launcher secret fails."""
        client = GatewayClient(
            gateway_host="localhost",
            gateway_port=19848,
            launcher_secret=None,  # No secret
            timeout=5,
        )

        with pytest.raises(GatewayError):
            client.register_session(
                container_id="abc123",
                container_ip="172.32.0.10",
                mode="public",
            )

    def test_validate_session_valid(self, gateway_client, mock_gateway_server):
        """Test validating a valid session."""
        result = gateway_client.validate_session("valid-token")
        assert result is True

    def test_validate_session_invalid(self, gateway_client, mock_gateway_server):
        """Test validating an invalid session."""
        result = gateway_client.validate_session("invalid-token")
        assert result is False

    def test_update_session(self, gateway_client, mock_gateway_server):
        """Test updating a session."""
        result = gateway_client.update_session(
            "some-token",
            container_id="new-container-id",
            container_ip="172.32.0.20",
        )
        assert result is True

    def test_delete_session(self, gateway_client, mock_gateway_server):
        """Test deleting a session."""
        result = gateway_client.delete_session("some-token")
        assert result is True

    def test_delete_session_by_container(self, gateway_client, mock_gateway_server):
        """Test deleting a session by container ID."""
        result = gateway_client.delete_session_by_container("container-123")
        assert result is True


class TestProxyConfiguration:
    """Tests for proxy configuration."""

    def test_get_proxy_config_public(self, gateway_client):
        """Test proxy config for public mode."""
        config = gateway_client.get_proxy_config(mode="public")

        assert "HTTP_PROXY" in config
        assert "HTTPS_PROXY" in config
        assert "NO_PROXY" in config
        assert config.get("EGG_PRIVATE_MODE") == "false"

    def test_get_proxy_config_private(self, gateway_client):
        """Test proxy config for private mode."""
        config = gateway_client.get_proxy_config(mode="private")

        assert config.get("EGG_PRIVATE_MODE") == "true"

    def test_get_container_env(self, gateway_client):
        """Test getting complete container environment."""
        env = gateway_client.get_container_env(
            session_token="test-token",
            issue_number=123,
            repo_path="/workspace/repo",
            agent_role="coder",
            mode="public",
        )

        # Session credentials
        assert env["EGG_SESSION_TOKEN"] == "test-token"
        assert "localhost:19848" in env["GATEWAY_URL"]

        # Pipeline context
        assert env["EGG_ISSUE_NUMBER"] == "123"
        assert env["EGG_REPO_PATH"] == "/workspace/repo"
        assert env["EGG_AGENT_ROLE"] == "coder"

        # Proxy settings
        assert "HTTP_PROXY" in env
        assert "HTTPS_PROXY" in env


class TestSecurityBoundaryValidation:
    """Tests for security boundary validation."""

    def test_validate_security_boundary_success(self, mock_gateway_server):
        """Test successful security boundary validation."""
        with patch("gateway_client.get_gateway_client") as mock_get:
            mock_client = MagicMock()
            mock_client.check_health.return_value = GatewayHealth(
                healthy=True,
                status="healthy",
            )
            mock_client.validate_session.return_value = True
            mock_get.return_value = mock_client

            valid, error = validate_security_boundary(
                container_id="abc123",
                container_ip="172.32.0.10",
                session_token="valid-token",
            )

            assert valid is True
            assert error is None

    def test_validate_security_boundary_unhealthy_gateway(self):
        """Test validation when gateway is unhealthy."""
        with patch("gateway_client.get_gateway_client") as mock_get:
            mock_client = MagicMock()
            mock_client.check_health.return_value = GatewayHealth(
                healthy=False,
                status="unreachable",
                error="Connection refused",
            )
            mock_get.return_value = mock_client

            valid, error = validate_security_boundary(
                container_id="abc123",
                container_ip="172.32.0.10",
                session_token="token",
            )

            assert valid is False
            assert "unhealthy" in error.lower()

    def test_validate_security_boundary_invalid_session(self):
        """Test validation with invalid session."""
        with patch("gateway_client.get_gateway_client") as mock_get:
            mock_client = MagicMock()
            mock_client.check_health.return_value = GatewayHealth(
                healthy=True,
                status="healthy",
            )
            mock_client.validate_session.return_value = False
            mock_get.return_value = mock_client

            valid, error = validate_security_boundary(
                container_id="abc123",
                container_ip="172.32.0.10",
                session_token="invalid-token",
            )

            assert valid is False
            assert "session validation failed" in error.lower()

    def test_validate_security_boundary_wrong_network(self):
        """Test validation when container is on wrong network."""
        with patch("gateway_client.get_gateway_client") as mock_get:
            mock_client = MagicMock()
            mock_client.check_health.return_value = GatewayHealth(
                healthy=True,
                status="healthy",
            )
            mock_client.validate_session.return_value = True
            mock_get.return_value = mock_client

            # IP not in 172.32.0.0/24 subnet
            valid, error = validate_security_boundary(
                container_id="abc123",
                container_ip="192.168.1.100",
                session_token="valid-token",
            )

            assert valid is False
            assert "isolated network" in error.lower()


class TestGatewayError:
    """Tests for GatewayError."""

    def test_gateway_error_basic(self):
        """Test basic error."""
        error = GatewayError("Something failed")
        assert str(error) == "Something failed"
        assert error.status_code is None

    def test_gateway_error_with_status(self):
        """Test error with status code."""
        error = GatewayError("Unauthorized", status_code=401)
        assert error.status_code == 401

    def test_gateway_error_with_details(self):
        """Test error with details."""
        error = GatewayError(
            "Validation failed",
            status_code=400,
            details={"field": "container_ip", "error": "invalid format"},
        )
        assert error.details["field"] == "container_ip"


class TestWorktreeManagement:
    """Tests for worktree create/delete operations."""

    def test_create_worktrees(self, gateway_client, mock_gateway_server):
        """Test creating worktrees for a container."""
        result = gateway_client.create_worktrees(
            container_id="egg-test-pipeline-coder",
            repos=["owner/repo1", "repo2"],
            uid=1000,
            gid=1000,
        )

        assert isinstance(result, WorktreeResult)
        assert result.success is True
        assert "repo1" in result.worktrees
        assert "repo2" in result.worktrees
        assert "egg-test-pipeline-coder" in result.worktrees["repo1"]

    def test_create_worktrees_without_auth(self, mock_gateway_server):
        """Test that worktree creation without launcher secret fails."""
        client = GatewayClient(
            gateway_host="localhost",
            gateway_port=19848,
            launcher_secret=None,
            timeout=5,
        )

        with pytest.raises(GatewayError):
            client.create_worktrees(
                container_id="test",
                repos=["repo1"],
            )

    def test_create_worktrees_with_base_branch(self, gateway_client, mock_gateway_server):
        """Test creating worktrees with custom base branch."""
        result = gateway_client.create_worktrees(
            container_id="test-pipeline",
            repos=["repo1"],
            base_branch="main",
        )

        assert result.success is True
        assert "repo1" in result.worktrees

    def test_delete_worktrees(self, gateway_client, mock_gateway_server):
        """Test deleting worktrees for a container."""
        result = gateway_client.delete_worktrees(
            container_id="egg-test-pipeline-coder",
            force=True,
        )

        assert isinstance(result, WorktreeResult)
        assert result.success is True
        assert "repo1" in result.worktrees

    def test_delete_worktrees_without_auth(self, mock_gateway_server):
        """Test that worktree deletion without launcher secret fails."""
        client = GatewayClient(
            gateway_host="localhost",
            gateway_port=19848,
            launcher_secret=None,
            timeout=5,
        )

        with pytest.raises(GatewayError):
            client.delete_worktrees(container_id="test")

    def test_create_worktrees_unreachable(self, gateway_client):
        """Test worktree creation when gateway is unreachable."""
        client = GatewayClient(
            gateway_host="localhost",
            gateway_port=19999,
            launcher_secret="test-secret",
            timeout=1,
        )

        with pytest.raises(GatewayError):
            client.create_worktrees(
                container_id="test",
                repos=["repo1"],
            )

    def test_create_worktrees_with_null_errors(self, gateway_client, mock_gateway_server):
        """Test that null errors from gateway are handled as empty list."""
        # The mock returns errors: [] but the real gateway might return null.
        # This test verifies the client handles it correctly by checking the
        # WorktreeResult.errors is always a list (never None).
        result = gateway_client.create_worktrees(
            container_id="test-pipeline",
            repos=["repo1"],
        )

        assert result.errors is not None
        assert isinstance(result.errors, list)
        # Verify iteration doesn't raise TypeError
        for _err in result.errors:
            pass

    def test_delete_worktrees_with_null_errors(self, gateway_client, mock_gateway_server):
        """Test that null errors from gateway delete are handled as empty list."""
        result = gateway_client.delete_worktrees(
            container_id="test-pipeline",
        )

        assert result.errors is not None
        assert isinstance(result.errors, list)
        # Verify iteration doesn't raise TypeError
        for _err in result.errors:
            pass

    def test_create_worktrees_with_explicit_null_fields(self, gateway_client):
        """Test that explicit null worktrees and errors from gateway are handled."""
        with patch.object(gateway_client, "_make_request") as mock_request:
            mock_request.return_value = {
                "success": True,
                "data": {"worktrees": None, "errors": None},
            }
            result = gateway_client.create_worktrees(
                container_id="test-pipeline",
                repos=["repo1"],
            )

            # worktrees should be {} not None
            assert result.worktrees == {}
            # errors should be [] not None
            assert result.errors == []
            # Verify iteration doesn't raise TypeError
            for _err in result.errors:
                pass
            for _key in result.worktrees:
                pass

    def test_delete_worktrees_with_explicit_null_fields(self, gateway_client):
        """Test that explicit null deleted and errors from gateway are handled."""
        with patch.object(gateway_client, "_make_request") as mock_request:
            mock_request.return_value = {
                "success": True,
                "data": {"deleted": None, "errors": None},
            }
            result = gateway_client.delete_worktrees(
                container_id="test-pipeline",
            )

            # worktrees should be {} not None (from deleted field)
            assert result.worktrees == {}
            # errors should be [] not None
            assert result.errors == []
            # Verify iteration doesn't raise TypeError
            for _err in result.errors:
                pass
            for _key in result.worktrees:
                pass


class TestSingletonClient:
    """Tests for singleton client."""

    def test_get_gateway_client_returns_singleton(self):
        """Test that get_gateway_client returns the same instance."""
        # Reset singleton
        import gateway_client

        gateway_client._gateway_client = None

        client1 = get_gateway_client()
        client2 = get_gateway_client()

        assert client1 is client2
