"""
Tests for gateway client.
"""

import json
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from unittest.mock import MagicMock, patch

import pytest
from egg_config.constants import TEST_GATEWAY_PORT
from gateway_client import (
    GatewayClient,
    GatewayError,
    GatewayHealth,
    PushResult,
    WorktreeResult,
    _classify_push_stderr,
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
        elif self.path == "/api/v1/git/push":
            self._handle_git_push(data)
        elif self.path == "/api/v1/git/fetch":
            self._handle_git_fetch(data)
        elif self.path == "/api/v1/gh/pr/create":
            self._handle_pr_create(data)
        elif self.path.startswith("/api/v1/sessions/by-container/") and self.path.endswith(
            "/heartbeat"
        ):
            self._handle_heartbeat_by_container()
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

    def _handle_heartbeat_by_container(self):
        """Handle session heartbeat by container ID (POST .../by-container/<id>/heartbeat)."""
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer ") or auth_header[7:] != "test-secret":
            self._send_error(401, "Unauthorized")
            return

        # Path: /api/v1/sessions/by-container/<id>/heartbeat
        container_id = self.path.split("/")[-2]
        if container_id == "missing":
            self._send_json({"success": False, "message": "Session not found"}, status=404)
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

    def _handle_git_push(self, data):
        """Handle git push (POST /api/v1/git/push)."""
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            self._send_error(401, "Unauthorized")
            return

        # Accept any session token (registered via /sessions/create)
        token = auth_header[7:]
        if not token:
            self._send_error(401, "Unauthorized")
            return

        self._send_json(
            {
                "success": True,
                "message": "Push successful",
                "data": {
                    "refspec": data.get("refspec", ""),
                },
            }
        )

    def _handle_git_fetch(self, data):
        """Handle git fetch (POST /api/v1/git/fetch)."""
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            self._send_error(401, "Unauthorized")
            return

        token = auth_header[7:]
        if not token:
            self._send_error(401, "Unauthorized")
            return

        self._send_json(
            {
                "success": True,
                "message": "Fetch successful",
            }
        )

    def _handle_pr_create(self, data):
        """Handle PR creation (POST /api/v1/gh/pr/create)."""
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            self._send_error(401, "Unauthorized")
            return

        token = auth_header[7:]
        if not token:
            self._send_error(401, "Unauthorized")
            return

        repo = data.get("repo", "")

        self._send_json(
            {
                "success": True,
                "message": "PR created",
                "data": {
                    "stdout": f"https://github.com/{repo}/pull/1",
                    "stderr": "",
                    "auth_mode": "bot",
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

    def test_register_session_forwards_worktree_container_id(self, gateway_client):
        """worktree_container_id reaches the wire payload so the gateway can
        reuse an existing per-agent worktree instead of creating a second
        one (#1857)."""
        captured: dict = {}

        def fake_make_request(endpoint, method, data, use_launcher_auth):
            captured["endpoint"] = endpoint
            captured["data"] = data
            return {
                "success": True,
                "data": {
                    "session_token": "tok-1",
                    "created_at": datetime.now().isoformat(),
                    "expires_at": datetime.now().isoformat(),
                },
            }

        with patch.object(gateway_client, "_make_request", side_effect=fake_make_request):
            gateway_client.register_session(
                container_id="egg-agent-pipe-1-coder",
                mode="private",
                pipeline_id="pipe-1",
                worktree_container_id="pipe-1-coder",
            )

        assert captured["endpoint"] == "/api/v1/sessions/create"
        assert captured["data"]["worktree_container_id"] == "pipe-1-coder"

    def test_register_session_omits_worktree_container_id_when_none(self, gateway_client):
        """When the caller doesn't supply worktree_container_id, the field is
        absent from the payload — keeps the wire contract minimal for callers
        that still rely on the gateway creating a worktree for them."""
        captured: dict = {}

        def fake_make_request(endpoint, method, data, use_launcher_auth):
            captured["data"] = data
            return {
                "success": True,
                "data": {
                    "session_token": "tok-1",
                    "created_at": datetime.now().isoformat(),
                    "expires_at": datetime.now().isoformat(),
                },
            }

        with patch.object(gateway_client, "_make_request", side_effect=fake_make_request):
            gateway_client.register_session(
                container_id="abc",
                mode="public",
            )

        assert "worktree_container_id" not in captured["data"]

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

    def test_heartbeat_session_by_container(self, gateway_client, mock_gateway_server):
        """Heartbeat-by-container returns True for an active session."""
        result = gateway_client.heartbeat_session_by_container("egg-agent-pipe-1-coder")
        assert result is True

    def test_heartbeat_session_by_container_missing_returns_false(
        self, gateway_client, mock_gateway_server
    ):
        """Heartbeat-by-container swallows 404 and returns False (best-effort path)."""
        result = gateway_client.heartbeat_session_by_container("missing")
        assert result is False


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

    def test_create_worktrees_forwards_assigned_branch(self, gateway_client):
        """assigned_branch is forwarded to the gateway create-worktree API.

        Regression guard for #1809: the orchestrator must tell the gateway
        which remote branch a per-agent worktree should push to, otherwise
        the default ``git push`` is denied by push_denied_wrong_branch.
        """
        with patch.object(gateway_client, "_make_request") as mock_request:
            mock_request.return_value = {
                "success": True,
                "data": {"worktrees": {"repo1": "/tmp/wt"}, "errors": []},
            }
            gateway_client.create_worktrees(
                container_id="issue-1759-v3-task_planner",
                repos=["owner/repo1"],
                base_branch="main",
                assigned_branch="egg/issue-1759-v3",
            )

        assert mock_request.call_count == 1
        sent = mock_request.call_args.kwargs["data"]
        assert sent["assigned_branch"] == "egg/issue-1759-v3"
        assert sent["base_branch"] == "main"

    def test_create_worktrees_omits_assigned_branch_when_none(self, gateway_client):
        """When assigned_branch is None, the key is omitted from the request."""
        with patch.object(gateway_client, "_make_request") as mock_request:
            mock_request.return_value = {
                "success": True,
                "data": {"worktrees": {"repo1": "/tmp/wt"}, "errors": []},
            }
            gateway_client.create_worktrees(
                container_id="test",
                repos=["owner/repo1"],
            )

        sent = mock_request.call_args.kwargs["data"]
        assert "assigned_branch" not in sent

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

    def test_create_worktrees_inlines_per_repo_errors_on_total_failure(self, gateway_client):
        """Per-repo error reasons from details.errors surface in str(e).

        Regression guard for #1838: when every worktree fails, the gateway
        returns a 500 with ``details.errors`` listing each repo's reason.
        Downstream callers (kubernetes_spawner, concurrent_executor) only
        stringify the exception, so the per-repo detail must be inlined
        into the message — otherwise spawn failures are undiagnosable
        without ``kubectl logs``.
        """
        per_repo_errors = [
            "coder: Timed out fetching base branch 'main' from remote",
            "reviewer_contract: Timed out fetching base branch 'main' from remote",
        ]
        with patch.object(gateway_client, "_make_request") as mock_request:
            mock_request.side_effect = GatewayError(
                "Failed to create any worktrees",
                status_code=500,
                details={"errors": per_repo_errors},
            )
            with pytest.raises(GatewayError) as excinfo:
                gateway_client.create_worktrees(
                    container_id="issue-1758-coder",
                    repos=["owner/repo1", "owner/repo2"],
                )

        raised = excinfo.value
        assert raised.status_code == 500
        assert raised.details == {"errors": per_repo_errors}
        for reason in per_repo_errors:
            assert reason in str(raised)
        assert "Failed to create any worktrees" in str(raised)

    def test_create_worktrees_reraises_without_details_errors(self, gateway_client):
        """GatewayError without ``details.errors`` is re-raised unchanged."""
        with patch.object(gateway_client, "_make_request") as mock_request:
            original = GatewayError("Unauthorized", status_code=401)
            mock_request.side_effect = original
            with pytest.raises(GatewayError) as excinfo:
                gateway_client.create_worktrees(
                    container_id="test",
                    repos=["owner/repo1"],
                )

        # No rewrap — message stays identical, details stays None.
        assert excinfo.value is original
        assert str(excinfo.value) == "Unauthorized"
        assert excinfo.value.details is None

    def test_create_worktrees_partial_success_path_unchanged(self, gateway_client):
        """When gateway returns 200 with partial errors, WorktreeResult is untouched.

        The inlining only fires on the 500-all-failed path; the partial-
        success path (worktrees present, some per-repo errors) must keep
        returning a normal WorktreeResult without raising.
        """
        with patch.object(gateway_client, "_make_request") as mock_request:
            mock_request.return_value = {
                "success": True,
                "data": {
                    "worktrees": {"repo1": "/tmp/wt/repo1"},
                    "errors": ["repo2: some transient error"],
                },
            }
            result = gateway_client.create_worktrees(
                container_id="test-pipeline",
                repos=["owner/repo1", "owner/repo2"],
            )

        assert result.success is True
        assert result.worktrees == {"repo1": "/tmp/wt/repo1"}
        assert result.errors == ["repo2: some transient error"]

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


class TestClassifyPushStderr:
    """Tests for the git-push stderr classifier used by PushResult."""

    @pytest.mark.parametrize(
        "stderr,expected",
        [
            (
                "! [rejected] egg/issue-42 -> egg/issue-42 (non-fast-forward)",
                "non_fast_forward",
            ),
            (
                "! [rejected] egg/issue-42 -> egg/issue-42 (fetch first)",
                "non_fast_forward",
            ),
            (
                "remote: HTTP 403: Authentication failed\nfatal: unable to access",
                "auth_failed",
            ),
            (
                "fatal: could not resolve host: github.com",
                "network",
            ),
            (
                "fatal: unable to access 'https://github.com/x.git/': "
                "Could not read from remote repository",
                "network",
            ),
            ("error: some other weird push error", "push_rejected"),
        ],
    )
    def test_classifies_common_stderr_shapes(self, stderr, expected):
        assert _classify_push_stderr(stderr) == expected


class TestPushResult:
    """Tests for the PushResult dataclass."""

    def test_bool_is_ok(self):
        assert bool(PushResult(ok=True)) is True
        assert bool(PushResult(ok=False, category="non_fast_forward", detail="x")) is False

    def test_describe_on_success(self):
        assert PushResult(ok=True).describe() == "ok"

    def test_describe_on_failure_with_detail(self):
        r = PushResult(ok=False, category="auth_failed", detail="403 Forbidden")
        assert r.describe() == "auth_failed: 403 Forbidden"

    def test_describe_on_failure_without_detail(self):
        r = PushResult(ok=False, category="auth_failed")
        assert r.describe() == "auth_failed"

    def test_describe_on_unclassified_failure(self):
        # A failure that somehow lost its category still produces something
        # readable rather than the literal string ``"None"``.
        r = PushResult(ok=False)
        assert r.describe() == "unknown"


class TestPushWorktreeBranch:
    """Tests for push_worktree_branch method."""

    def test_push_worktree_branch_success(self, gateway_client, mock_gateway_server):
        """Test successful push of worktree branch."""
        result = gateway_client.push_worktree_branch(
            pipeline_id="issue-42",
            repo_path="/home/egg/.egg-worktrees/issue-42/repo",
            branch="egg/issue-42",
        )
        assert result.ok is True
        assert bool(result) is True
        assert result.category is None
        assert result.detail is None

    def test_push_worktree_branch_gateway_unreachable(self):
        """Test push fails gracefully when gateway is unreachable.

        Uses ``ref=`` so reconcile is skipped — otherwise the fetch step
        would supersede the initial ``gateway_unreachable`` classification
        with ``reconcile_fetch_failed``. The ref=None path exercises both
        classifications (see test_reconcile_and_push_pr_branch.py).
        """
        client = GatewayClient(
            gateway_host="localhost",
            gateway_port=19999,
            launcher_secret="test-secret",
            timeout=1,
        )

        result = client.push_worktree_branch(
            pipeline_id="issue-42",
            repo_path="/some/path",
            branch="egg/issue-42",
            ref="egg/issue-42",
        )
        assert result.ok is False
        assert bool(result) is False
        # No stderr available from a transport-level failure — category
        # must still carry actionable detail so callers can distinguish
        # "gateway down" from "push rejected".
        assert result.category == "gateway_unreachable"
        assert result.detail

    def test_push_worktree_branch_uses_launcher_auth(self, gateway_client, mock_gateway_server):
        """Push authenticates with the launcher secret, not a session token.

        The orchestrator's failsafe push is on the privileged side of the
        trust boundary — same secret used by ``/api/v1/sessions/create``.
        It should NOT register a temp session and should NOT pass a
        per-session bearer token (#2051).
        """
        with (
            patch.object(
                gateway_client, "register_session", wraps=gateway_client.register_session
            ) as mock_reg,
            patch.object(gateway_client, "delete_session") as mock_delete,
            patch.object(
                gateway_client, "_make_request", wraps=gateway_client._make_request
            ) as mock_req,
        ):
            gateway_client.push_worktree_branch(
                pipeline_id="issue-42",
                repo_path="/some/path",
                branch="egg/issue-42",
            )
            mock_reg.assert_not_called()
            mock_delete.assert_not_called()
            push_calls = [c for c in mock_req.call_args_list if c.args[0] == "/api/v1/git/push"]
            assert len(push_calls) == 1
            assert push_calls[0].kwargs.get("use_launcher_auth") is True
            assert push_calls[0].kwargs.get("bearer_token") is None

    def test_push_worktree_branch_uses_head_refspec(self, gateway_client, mock_gateway_server):
        """Test that push uses HEAD:refs/heads/<branch> refspec format.

        This ensures the worktree HEAD is pushed to the correct remote branch,
        regardless of the local branch name (which in worktrees is often
        egg/{container_id}/work rather than the desired egg/issue-{N}).
        """
        with patch.object(
            gateway_client, "_make_request", wraps=gateway_client._make_request
        ) as mock_req:
            gateway_client.push_worktree_branch(
                pipeline_id="issue-42",
                repo_path="/some/path",
                branch="egg/issue-42",
            )
            # Find the push call
            push_calls = [c for c in mock_req.call_args_list if c.args[0] == "/api/v1/git/push"]
            assert len(push_calls) == 1
            push_data = push_calls[0].kwargs["data"]
            assert push_data["refspec"] == "HEAD:refs/heads/egg/issue-42"
            # Mode is forwarded so the gateway can apply private-repo policy
            # without a session record to read it from.
            assert push_data.get("mode") == "public"

    def test_push_worktree_branch_classifies_gateway_push_rejection(self, gateway_client):
        """When the gateway push endpoint returns 500 with git stderr in
        details, the returned PushResult should classify the failure and
        carry the stderr text — no more opaque ``returned False``.

        Regression for #1852.
        """
        # Fake a gateway 500 response whose body mirrors the real gateway
        # push endpoint's error shape: message + details.stderr.
        rejected_stderr = (
            "To github.com:owner/repo.git\n"
            " ! [rejected]        egg/issue-42 -> egg/issue-42 (fetch first)\n"
            "error: failed to push some refs"
        )

        def _raise_rejection(endpoint, **kwargs):
            if endpoint == "/api/v1/git/push":
                raise GatewayError(
                    f"Push failed: {rejected_stderr}",
                    status_code=500,
                    details={"stdout": "", "stderr": rejected_stderr},
                )
            # Let session register/delete succeed via mock_gateway_server
            # below is bypassed — we stub _make_request wholesale.
            return {
                "success": True,
                "data": {
                    "session_token": "test-token-12345",
                    "created_at": "2026-01-01T00:00:00",
                    "expires_at": "2026-01-02T00:00:00",
                },
            }

        with patch.object(gateway_client, "_make_request", side_effect=_raise_rejection):
            result = gateway_client.push_worktree_branch(
                pipeline_id="issue-42",
                repo_path="/home/egg/.egg-worktrees/issue-42/repo",
                branch="egg/issue-42",
                ref="egg/issue-42",  # Skip reconcile so we see the raw classification.
            )

        assert result.ok is False
        assert result.category == "non_fast_forward"
        assert "fetch first" in (result.detail or "")
        # describe() is what callers surface to operators.
        described = result.describe()
        assert "non_fast_forward" in described
        assert "fetch first" in described

    def test_push_worktree_branch_ref_param_uses_branch_refspec(
        self, gateway_client, mock_gateway_server
    ):
        """Test that ``ref=`` produces refs/heads/<ref>:refs/heads/<branch>.

        Used for state-sync pushes (#1808): the gateway ``cd``s into the
        main repo (shared hostPath) and pushes the state branch ref from
        the shared ``.git/`` object DB, since the state worktree itself
        lives in a pod-local unshared volume.
        """
        with patch.object(
            gateway_client, "_make_request", wraps=gateway_client._make_request
        ) as mock_req:
            gateway_client.push_worktree_branch(
                pipeline_id="state-sync",
                repo_path="/home/egg/repos/myrepo",
                branch="egg/pipeline-state",
                ref="egg/pipeline-state",
            )
            push_calls = [c for c in mock_req.call_args_list if c.args[0] == "/api/v1/git/push"]
            assert len(push_calls) == 1
            push_data = push_calls[0].kwargs["data"]
            assert (
                push_data["refspec"]
                == "refs/heads/egg/pipeline-state:refs/heads/egg/pipeline-state"
            )

    def test_push_worktree_branch_force_flag_propagates(self, gateway_client, mock_gateway_server):
        """``force=True`` must reach the ``/api/v1/git/push`` request body.

        The rebase-on-resume helper (#2098) relies on this — without it,
        the gateway never sees ``--force`` and the post-rebase push hits
        the same non-fast-forward error the helper just rebased to fix.
        """
        with patch.object(
            gateway_client, "_make_request", wraps=gateway_client._make_request
        ) as mock_req:
            gateway_client.push_worktree_branch(
                pipeline_id="issue-42",
                repo_path="/some/path",
                branch="egg/issue-42",
                force=True,
            )
            push_calls = [c for c in mock_req.call_args_list if c.args[0] == "/api/v1/git/push"]
            assert len(push_calls) == 1
            push_data = push_calls[0].kwargs["data"]
            assert push_data["force"] is True

    def test_push_worktree_branch_default_force_false(self, gateway_client, mock_gateway_server):
        """Default callers must not silently force-push.  The body's
        ``force`` field must be ``False`` unless explicitly opted in.
        """
        with patch.object(
            gateway_client, "_make_request", wraps=gateway_client._make_request
        ) as mock_req:
            gateway_client.push_worktree_branch(
                pipeline_id="issue-42",
                repo_path="/some/path",
                branch="egg/issue-42",
            )
            push_calls = [c for c in mock_req.call_args_list if c.args[0] == "/api/v1/git/push"]
            assert len(push_calls) == 1
            push_data = push_calls[0].kwargs["data"]
            assert push_data["force"] is False


class TestDeleteRemoteBranch:
    """Tests for delete_remote_branch method.

    Post-#2055: deletion goes through ``_do_push`` with launcher auth
    (no temp-session ceremony).  The pipeline-push enforcement that
    blocked the prior shape (#2028) exempts launcher-auth requests.
    """

    def test_delete_remote_branch_success(self, gateway_client, mock_gateway_server):
        """Successful deletion returns a truthy ``PushResult``."""
        result = gateway_client.delete_remote_branch(
            pipeline_id="issue-42",
            repo_path="/home/egg/.egg-worktrees/issue-42/repo",
            branch="egg/container-abc123/work",
        )
        assert isinstance(result, PushResult)
        assert result.ok is True
        assert bool(result) is True

    def test_delete_remote_branch_gateway_unreachable(self):
        """Transport failure surfaces as a falsy ``PushResult`` with category."""
        client = GatewayClient(
            gateway_host="localhost",
            gateway_port=19999,
            launcher_secret="test-secret",
            timeout=1,
        )

        result = client.delete_remote_branch(
            pipeline_id="issue-42",
            repo_path="/some/path",
            branch="egg/container-abc123/work",
        )
        assert isinstance(result, PushResult)
        assert result.ok is False
        assert result.category in ("gateway_unreachable", "gateway_error", "unknown")
        assert bool(result) is False

    def test_delete_remote_branch_uses_launcher_auth(self, gateway_client, mock_gateway_server):
        """Pin the trust-boundary fix: deletion authenticates with the
        launcher secret (not a session token), so it bypasses the
        gateway's pipeline-push enforcement (#2028, #2055)."""
        captured: dict = {}

        original = gateway_client._make_request

        def spy(endpoint, *args, **kwargs):
            if endpoint == "/api/v1/git/push":
                captured["use_launcher_auth"] = kwargs.get("use_launcher_auth")
                captured["bearer_token"] = kwargs.get("bearer_token")
                captured["data"] = kwargs.get("data")
            return original(endpoint, *args, **kwargs)

        with patch.object(gateway_client, "_make_request", side_effect=spy):
            with patch.object(gateway_client, "register_session") as mock_reg:
                gateway_client.delete_remote_branch(
                    pipeline_id="issue-42",
                    repo_path="/some/path",
                    branch="egg/container-abc123/work",
                )
                # No temp session — the orchestrator authenticates directly.
                mock_reg.assert_not_called()

        assert captured["use_launcher_auth"] is True
        assert captured["bearer_token"] is None
        assert captured["data"]["refspec"] == ":egg/container-abc123/work"

    def test_delete_remote_branch_already_deleted_classifier(self):
        """``remote ref does not exist`` stderr classifies as ``already_deleted``."""
        # Direct classifier check — keeps the test independent of HTTP
        # plumbing while pinning the category callers depend on to
        # distinguish desired-state-already from real failures.
        category = _classify_push_stderr(
            "error: unable to delete 'egg/container-abc/work': remote ref does not exist\n"
            "error: failed to push some refs to 'origin'"
        )
        assert category == "already_deleted"


class TestFetchWorktreeBranch:
    """Tests for fetch_worktree_branch method."""

    def test_fetch_worktree_branch_success(self, gateway_client, mock_gateway_server):
        """Test successful fetch of worktree branch."""
        result = gateway_client.fetch_worktree_branch(
            pipeline_id="issue-42",
            repo_path="/home/egg/.egg-worktrees/issue-42/repo",
        )
        assert result is True

    def test_fetch_worktree_branch_gateway_unreachable(self):
        """Test fetch fails gracefully when gateway is unreachable."""
        client = GatewayClient(
            gateway_host="localhost",
            gateway_port=19999,
            launcher_secret="test-secret",
            timeout=1,
        )

        result = client.fetch_worktree_branch(
            pipeline_id="issue-42",
            repo_path="/some/path",
        )
        assert result is False

    def test_fetch_worktree_branch_cleans_up_session(self, gateway_client, mock_gateway_server):
        """Test that temp session is cleaned up after fetch."""
        with patch.object(gateway_client, "delete_session") as mock_delete:
            gateway_client.fetch_worktree_branch(
                pipeline_id="issue-42",
                repo_path="/some/path",
            )
            # Session should be cleaned up
            mock_delete.assert_called_once_with("test-token-12345")


class TestCreatePR:
    """Tests for create_pr method."""

    def test_create_pr_success(self, gateway_client, mock_gateway_server):
        """Test successful PR creation returns URL."""
        result = gateway_client.create_pr(
            pipeline_id="issue-42",
            repo="owner/repo",
            title="Fix the bug",
            body="This fixes the bug.\n\nCloses #42",
            head="egg/issue-42",
        )
        assert result == "https://github.com/owner/repo/pull/1"

    def test_create_pr_gateway_unreachable(self):
        """Test PR creation raises when gateway is unreachable."""
        client = GatewayClient(
            gateway_host="localhost",
            gateway_port=19999,
            launcher_secret="test-secret",
            timeout=1,
        )

        with pytest.raises(GatewayError):
            client.create_pr(
                pipeline_id="issue-42",
                repo="owner/repo",
                title="Fix",
                body="Body",
                head="egg/issue-42",
            )

    def test_create_pr_cleans_up_session(self, gateway_client, mock_gateway_server):
        """Test that temp session is cleaned up after PR creation."""
        with patch.object(gateway_client, "delete_session") as mock_delete:
            gateway_client.create_pr(
                pipeline_id="issue-42",
                repo="owner/repo",
                title="Fix",
                body="Body",
                head="egg/issue-42",
            )
            mock_delete.assert_called_once_with("test-token-12345")

    def test_create_pr_registers_session_with_pr_phase(self, gateway_client, mock_gateway_server):
        """Test that session is registered with phase='pr'."""
        with patch.object(
            gateway_client, "register_session", wraps=gateway_client.register_session
        ) as mock_reg:
            gateway_client.create_pr(
                pipeline_id="issue-42",
                repo="owner/repo",
                title="Fix",
                body="Body",
                head="egg/issue-42",
            )
            mock_reg.assert_called_once()
            call_kwargs = mock_reg.call_args
            assert call_kwargs.kwargs.get("phase") == "pr" or call_kwargs[1].get("phase") == "pr"


class TestCreateSlicePR:
    """Body / title composition for create_slice_pr (#2340, #2538, #2745)."""

    def _capture(self, gateway_client):
        """Patch create_pr to capture (title, body) and return a dummy URL."""
        captured: dict[str, str] = {}

        def _fake_create_pr(*, title: str, body: str, **_kwargs: object) -> str:
            captured["title"] = title
            captured["body"] = body
            return "https://example/pr/1"

        return captured, patch.object(gateway_client, "create_pr", side_effect=_fake_create_pr)

    def test_no_contract_pr_falls_back_to_auto_generated_title_and_body(self, gateway_client):
        """When ``contract.pr`` is missing (no ``program_title``), every slice
        — terminal or not — falls back to the deterministic ``<slice_id>:
        <name>`` title and a bulleted task body. No narrative to render."""
        captured, ctx = self._capture(gateway_client)
        with ctx:
            gateway_client.create_slice_pr(
                pipeline_id="issue-42",
                repo="owner/repo",
                slice_id="slice-1",
                slice_name="Pattern adoption",
                slice_tasks=[{"id": "task-1-1", "description": "Add the barrel re-export"}],
                head="egg/issue-42/slice-1",
                base="egg/issue-42",
            )
        assert captured["title"] == "slice-1: Pattern adoption"
        assert "Pattern adoption" in captured["body"]
        assert "Tasks in this slice:" in captured["body"]
        assert "- task-1-1: Add the barrel re-export" in captured["body"]
        assert "Slice slice-1 of pipeline issue-42" in captured["body"]
        # No program-umbrella banner / per-slice section when neither field is set.
        assert "Program-level umbrella PR" not in captured["body"]
        assert "## This slice" not in captured["body"]

    def test_non_terminal_slice_with_base_pr_renders_lean_body(self, gateway_client):
        """#2745: when the base/context PR (#2548) exists, non-terminal slice
        bodies are lean — a 1-line program blurb + ``Base PR:`` link + the
        per-slice ``## This slice`` scope. The duplicated program test plan
        / manual steps that pre-#2745 every slice carried is gone."""
        captured, ctx = self._capture(gateway_client)
        with ctx:
            gateway_client.create_slice_pr(
                pipeline_id="issue-42",
                repo="owner/repo",
                slice_id="slice-1",
                slice_name="Pattern adoption",
                slice_tasks=[
                    {
                        "id": "task-1-1",
                        "description": "Add the barrel re-export so callers route through it.",
                        "acceptance_criteria": "Imports through the barrel succeed; mypy green.",
                    },
                ],
                head="egg/issue-42/slice-1",
                base="egg/issue-42/work",
                program_title="Decompose oversize files; ratchet allowlist",
                program_description=(
                    "The lint added in #2250 caps Python files at 1500 lines. "
                    "Multiple files cross that cap; this program decomposes them."
                ),
                program_test_plan="- Automated: make lint and make test-all green.",
                program_manual_steps="Pre-merge (terminal slice only): verify seam tables.",
                terminal_slice_id="slice-3",
                slice_index=1,
                slice_count=3,
                slice_files_affected=["orchestrator/foo.py", "shared/bar.py"],
                context_pr_number=99,
            )
        # Title: ``[<program-slug>][slice-N/M] <slice subject>``.
        assert captured["title"] == "[issue-42][slice-1/3] Pattern adoption"
        body = captured["body"]
        # Program-wide rollups are GONE on lean non-terminal slices —
        # they live on the base PR / terminal slice now.
        assert "Program-level umbrella PR" not in body
        assert "## Test Plan" not in body
        assert "## Manual Steps" not in body
        # First-sentence blurb is present; rest of the description is not.
        assert "The lint added in #2250 caps Python files at 1500 lines." in body
        assert "Multiple files cross that cap" not in body
        # Base PR link is present and points to the context PR number.
        assert "**Base PR:** #99" in body
        # Per-slice scope: subject, files affected, full task + AC.
        assert "## This slice" in body
        assert "Pattern adoption" in body
        assert "Files affected:" in body
        assert "`orchestrator/foo.py`" in body
        assert "`shared/bar.py`" in body
        assert "- task-1-1: Add the barrel re-export so callers route through it." in body
        assert "Acceptance criteria: Imports through the barrel succeed; mypy green." in body
        # ``## Stack`` block names parent / base PR position.
        assert "## Stack" in body
        assert "- Base PR: #99" in body
        assert "- Stacked on top of `egg/issue-42/work`" in body
        assert "- Position: slice 1 of 3 in pipeline `issue-42`" in body
        # Legacy footer string kept for tooling/scrapers.
        assert "Slice slice-1 of pipeline issue-42" in body

    def test_non_terminal_slice_without_base_pr_falls_back_to_inline_narrative(
        self, gateway_client
    ):
        """#2745 / #2744: UX backstop. When ``context_pr_number`` is None
        (the base/context PR was not opened — #2744 regression), the slice
        PR body falls back to the pre-#2745 inline-narrative shape so the
        slice PR is still reviewable as a standalone diff against
        ``/work``. NOTE: the stack is still structurally unmergeable in
        this state; this is a body-rendering backstop, not a fix."""
        captured, ctx = self._capture(gateway_client)
        with ctx:
            gateway_client.create_slice_pr(
                pipeline_id="issue-42",
                repo="owner/repo",
                slice_id="slice-1",
                slice_name="Pattern adoption",
                slice_tasks=[{"id": "task-1-1", "description": "Add the barrel re-export"}],
                head="egg/issue-42/slice-1",
                base="egg/issue-42/work",
                program_title="Decompose oversize files; ratchet allowlist",
                program_description="The lint added in #2250 caps Python files at 1500 lines.",
                program_test_plan="- Automated: make lint and make test-all green.",
                program_manual_steps="Pre-merge (terminal slice only): verify seam tables.",
                terminal_slice_id="slice-3",
                slice_index=1,
                slice_count=3,
                context_pr_number=None,
            )
        body = captured["body"]
        # Title still uses the new shape.
        assert captured["title"] == "[issue-42][slice-1/3] Pattern adoption"
        # Inline narrative is present because there's no base PR to defer to.
        assert "The lint added in #2250 caps Python files at 1500 lines." in body
        assert "## Test Plan" in body
        assert "make lint" in body
        assert "## Manual Steps" in body
        assert "seam tables" in body
        # No Base PR pointer — there is no base PR in this state.
        assert "**Base PR:**" not in body
        assert "- Base PR:" not in body
        # Per-slice scope is still rendered.
        assert "## This slice" in body
        assert "Pattern adoption" in body
        # Section ordering: description → This slice → Test Plan → Manual Steps.
        assert body.index("The lint added in #2250") < body.index("## This slice")
        assert body.index("## This slice") < body.index("## Test Plan")
        assert body.index("## Test Plan") < body.index("## Manual Steps")

    def test_terminal_slice_keeps_umbrella_rollup_and_uses_merge_gate_marker(self, gateway_client):
        """#2745: terminal slice keeps the umbrella treatment — program
        description + ``## Test Plan`` + ``## Manual Steps`` + pre-merge
        obligations — because the base/context PR (#2548) is a strategic-
        direction surface (analysis + plan + BRC history), not a merge-the-
        whole-stack rollup. Execution-time concerns live on the merge gate.

        Title now uses the ``[<slug>][merge-gate] <program_title>`` shape so
        the terminal PR is still distinguishable from the program-level
        base PR by title alone (the original #2745 complaint)."""
        captured, ctx = self._capture(gateway_client)
        with ctx:
            gateway_client.create_slice_pr(
                pipeline_id="issue-42",
                repo="owner/repo",
                slice_id="slice-3",
                slice_name="Apply the ratchet",
                slice_tasks=[{"id": "task-3-1", "description": "Bump the allowlist"}],
                head="egg/issue-42/slice-3",
                base="egg/issue-42/slice-2",
                program_title="Decompose oversize files; ratchet allowlist",
                program_description="The lint added in #2250 caps Python files at 1500 lines.",
                program_test_plan="- Automated: make lint and make test-all green.",
                program_manual_steps="Pre-merge (terminal slice only): verify seam tables.",
                slice_index=3,
                slice_count=3,
                context_pr_number=99,
            )
        assert captured["title"] == (
            "[issue-42][merge-gate] Decompose oversize files; ratchet allowlist"
        )
        body = captured["body"]
        assert "Program-level umbrella PR" in body
        assert "issue-42" in body
        assert "The lint added in #2250" in body
        # Per-slice scope section + slice name/tasks present on terminal too.
        assert "## This slice" in body
        assert "Apply the ratchet" in body
        assert "- task-3-1: Bump the allowlist" in body
        # Program test plan / manual steps still rendered on terminal —
        # this is the merge gate; execution-time concerns live here.
        assert "## Test Plan" in body
        assert "make lint" in body
        assert "## Manual Steps" in body
        assert "seam tables" in body
        # ``## Stack`` block names the merge-gate position.
        assert "## Stack" in body
        assert "- Base PR: #99" in body
        assert "- Position: merge-gate (slice 3 of 3) in pipeline `issue-42`" in body
        # Legacy footer string survives.
        assert "Slice slice-3 of pipeline issue-42" in body

    def test_terminal_slice_truncates_long_title_to_70_chars(self, gateway_client):
        """Title-length cap (70 chars) is symmetric for the new
        ``[<slug>][merge-gate] <program_title>`` shape. The slug + marker
        prefix survives the truncation — only the subject is cut — so
        reviewers can still tell it's the merge gate by title alone."""
        captured, ctx = self._capture(gateway_client)
        long_title = "A" * 90
        with ctx:
            gateway_client.create_slice_pr(
                pipeline_id="issue-42",
                repo="owner/repo",
                slice_id="slice-3",
                slice_name="Tip",
                slice_tasks=None,
                head="egg/issue-42/slice-3",
                base="egg/issue-42/slice-2",
                program_title=long_title,
                slice_index=3,
                slice_count=3,
            )
        assert len(captured["title"]) == 70
        assert captured["title"].endswith("...")
        # Pin the new shape: slug + merge-gate marker must survive the
        # 70-char truncation so reviewers can still tell it's the merge
        # gate by title alone (#2746 review item 7).
        assert captured["title"].startswith("[issue-42][merge-gate] ")

    def test_terminal_slice_renders_program_deferred_actions(self, gateway_client):
        """#2354: when the terminal slice receives ``program_deferred_actions``
        (already-normalized list of ``{reviewer, condition, resolved_in_diff}``
        dicts produced by ``_collect_pre_merge_obligations``), the umbrella
        PR body carries the same Pre-merge Obligations section the legacy
        ``_auto_create_pr`` path emits — so reviewers don't have to discover
        obligations out-of-band. Asserts the section sits *before*
        ``## Test Plan`` (#2354 review item 1)."""
        captured, ctx = self._capture(gateway_client)
        with ctx:
            gateway_client.create_slice_pr(
                pipeline_id="issue-42",
                repo="owner/repo",
                slice_id="slice-3",
                slice_name="Apply the ratchet",
                slice_tasks=None,
                head="egg/issue-42/slice-3",
                base="egg/issue-42/slice-2",
                program_title="Decompose oversize files",
                program_description="Description.",
                program_test_plan="- Automated: make test-all green.",
                program_manual_steps="Verify seam tables.",
                program_deferred_actions=[
                    {
                        "reviewer": "coder",
                        "condition": "git mv legacy/x new/x before merge",
                        "resolved_in_diff": "",
                    },
                    {
                        "reviewer": "reviewer_contract",
                        "condition": "verify tests green against merged state",
                        "resolved_in_diff": "2c319626a",
                    },
                ],
                slice_index=3,
                slice_count=3,
            )
        body = captured["body"]
        assert "## ⚠️ Pre-merge Obligations" in body
        assert "- **coder** — git mv legacy/x new/x before merge" in body
        assert "## ✅ Resolved within this PR" in body
        assert "Resolved in 2c319626a" in body
        # Obligations sit *before* ``## Test Plan`` so the merge-blocking
        # banner is visible without scrolling past plan/steps. The original
        # placement (after Test Plan / Manual Steps) defeated the warning's
        # visibility intent — see PR #2382 review item 1.
        assert body.index("## ⚠️ Pre-merge Obligations") < body.index("## Test Plan")
        assert body.index("## ⚠️ Pre-merge Obligations") < body.index("## Manual Steps")
        # And before the slice-context footer (so reviewers see it without
        # scrolling past pipeline metadata).
        assert body.index("## ⚠️ Pre-merge Obligations") < body.index(
            "Slice slice-3 of pipeline issue-42"
        )

    def test_terminal_slice_with_no_deferred_actions_omits_section(self, gateway_client):
        """When the contract carries no obligations the body must not emit
        an empty Pre-merge Obligations heading."""
        captured, ctx = self._capture(gateway_client)
        with ctx:
            gateway_client.create_slice_pr(
                pipeline_id="issue-42",
                repo="owner/repo",
                slice_id="slice-3",
                slice_name="Apply the ratchet",
                slice_tasks=None,
                head="egg/issue-42/slice-3",
                base="egg/issue-42/slice-2",
                program_title="Decompose oversize files",
                program_deferred_actions=None,
            )
        assert "Pre-merge Obligations" not in captured["body"]
        assert "Resolved within this PR" not in captured["body"]

    def test_terminal_slice_with_empty_deferred_actions_list_omits_section(self, gateway_client):
        """The realistic production case is ``contract.pr`` populated with
        ``deferred_actions=[]`` (no obligations), not ``None``. The
        ``_collect_pre_merge_obligations`` snapshot in
        ``_run_one_slice_inner`` returns ``None`` only when there's nothing
        to render, but a defensive ``[]`` should also short-circuit cleanly
        — same merge-block visibility behaviour as the ``None`` case
        (#2354 review item 4)."""
        captured, ctx = self._capture(gateway_client)
        with ctx:
            gateway_client.create_slice_pr(
                pipeline_id="issue-42",
                repo="owner/repo",
                slice_id="slice-3",
                slice_name="Apply the ratchet",
                slice_tasks=None,
                head="egg/issue-42/slice-3",
                base="egg/issue-42/slice-2",
                program_title="Decompose oversize files",
                program_test_plan="- Automated: make test-all green.",
                program_deferred_actions=[],
            )
        body = captured["body"]
        assert "Pre-merge Obligations" not in body
        assert "Resolved within this PR" not in body
        # Test plan is unaffected by the empty obligations list.
        assert "## Test Plan" in body

    def test_non_terminal_slice_with_program_deferred_actions_raises(self, gateway_client):
        """Wiring ``program_deferred_actions`` to a non-terminal slice (no
        ``program_title``) is a caller mistake — the umbrella is the only
        place obligations belong. Failing fast catches the regression
        instead of silently dropping the obligations
        (#2354 review nit)."""
        captured, ctx = self._capture(gateway_client)
        with ctx, pytest.raises(AssertionError, match="program_deferred_actions must be None"):
            gateway_client.create_slice_pr(
                pipeline_id="issue-42",
                repo="owner/repo",
                slice_id="slice-1",
                slice_name="Pattern adoption",
                slice_tasks=[{"id": "task-1-1", "description": "do X"}],
                head="egg/issue-42/slice-1",
                base="egg/issue-42",
                program_deferred_actions=[
                    {"reviewer": "r1", "condition": "do X", "resolved_in_diff": ""},
                ],
            )

    def test_non_terminal_slice_lean_branch_with_obligations_raises(self, gateway_client):
        """#2746 review item 1: the lean non-terminal branch (program_title
        set, base PR opened) must also reject mis-routed
        ``program_deferred_actions``. The pre-fix code only asserted in
        the no-program-title branch, so a non-terminal slice with a
        program_title silently dropped obligations."""
        captured, ctx = self._capture(gateway_client)
        with ctx, pytest.raises(AssertionError, match="program_deferred_actions must be None"):
            gateway_client.create_slice_pr(
                pipeline_id="issue-42",
                repo="owner/repo",
                slice_id="slice-1",
                slice_name="Pattern adoption",
                slice_tasks=[{"id": "task-1-1", "description": "do X"}],
                head="egg/issue-42/slice-1",
                base="egg/issue-42/work",
                program_title="Decompose oversize files",
                terminal_slice_id="slice-3",
                slice_index=1,
                slice_count=3,
                context_pr_number=99,  # lean branch
                program_deferred_actions=[
                    {"reviewer": "r1", "condition": "do X", "resolved_in_diff": ""},
                ],
            )

    def test_non_terminal_slice_inline_fallback_branch_with_obligations_raises(
        self, gateway_client
    ):
        """#2746 review item 1: the inline-fallback non-terminal branch
        (program_title set, no base PR) must also reject mis-routed
        ``program_deferred_actions`` — same reason as the lean branch."""
        captured, ctx = self._capture(gateway_client)
        with ctx, pytest.raises(AssertionError, match="program_deferred_actions must be None"):
            gateway_client.create_slice_pr(
                pipeline_id="issue-42",
                repo="owner/repo",
                slice_id="slice-1",
                slice_name="Pattern adoption",
                slice_tasks=[{"id": "task-1-1", "description": "do X"}],
                head="egg/issue-42/slice-1",
                base="egg/issue-42/work",
                program_title="Decompose oversize files",
                terminal_slice_id="slice-3",
                slice_index=1,
                slice_count=3,
                context_pr_number=None,  # inline-fallback branch
                program_deferred_actions=[
                    {"reviewer": "r1", "condition": "do X", "resolved_in_diff": ""},
                ],
            )

    def test_whitespace_program_title_does_not_trigger_assertion(self, gateway_client):
        """``PRMetadata.title`` validates with ``min_length=1`` but does not
        ``.strip()`` — so a whitespace-only title (e.g. ``" "``) currently
        passes contract validation. The non-terminal-slice assertion must
        guard on ``program_title is None`` rather than on
        ``has_program_block`` (truthy after strip), so a whitespace-only
        title doesn't spuriously masquerade as a slice routing error
        (#2354 review observation B). The whitespace-title bug, if any, is
        for ``PRMetadata`` to catch — not the slice PR builder."""
        captured, ctx = self._capture(gateway_client)
        # Should NOT raise AssertionError — program_title is not None,
        # so the routing-error guard does not fire.
        with ctx:
            gateway_client.create_slice_pr(
                pipeline_id="issue-42",
                repo="owner/repo",
                slice_id="slice-1",
                slice_name="Pattern adoption",
                slice_tasks=[{"id": "task-1-1", "description": "do X"}],
                head="egg/issue-42/slice-1",
                base="egg/issue-42",
                program_title=" ",
                program_deferred_actions=[
                    {"reviewer": "r1", "condition": "do X", "resolved_in_diff": ""},
                ],
            )

    def test_task_descriptions_not_truncated_and_acceptance_criteria_rendered(self, gateway_client):
        """#2745: drop the 300-char task description truncation introduced
        in pre-#2745 ``create_slice_pr`` (cuts task descriptions
        mid-sentence with ``...``). Slice PR bodies surface full task
        descriptions and per-task acceptance criteria when present."""
        long_desc = "x" * 500
        long_ac = "y" * 400
        captured, ctx = self._capture(gateway_client)
        with ctx:
            gateway_client.create_slice_pr(
                pipeline_id="issue-42",
                repo="owner/repo",
                slice_id="slice-1",
                slice_name="Pattern adoption",
                slice_tasks=[
                    {
                        "id": "task-1-1",
                        "description": long_desc,
                        "acceptance_criteria": long_ac,
                    },
                ],
                head="egg/issue-42/slice-1",
                base="egg/issue-42/work",
                program_title="Decompose oversize files",
                program_description="A long-form description.",
                terminal_slice_id="slice-3",
                slice_index=1,
                slice_count=3,
                context_pr_number=99,
            )
        body = captured["body"]
        assert long_desc in body  # full description, no ``...`` truncation
        assert long_ac in body  # full acceptance criteria

    def test_pipeline_hash_id_truncates_slug_in_title(self, gateway_client):
        """Pipelines opened via ``submit_task`` without an issue number get
        identifiers like ``pipeline-f4c7d780``. The program slug truncates
        long identifiers so the title stays scannable."""
        captured, ctx = self._capture(gateway_client)
        with ctx:
            gateway_client.create_slice_pr(
                pipeline_id="pipeline-f4c7d780abc123",
                repo="owner/repo",
                slice_id="slice-2",
                slice_name="Bring up the orchestrator wire",
                slice_tasks=None,
                head="egg/pipeline-f4c7d780abc123/slice-2",
                base="egg/pipeline-f4c7d780abc123/slice-1",
                program_title="Actionable Plan Framework MVP",
                terminal_slice_id="slice-15",
                slice_index=2,
                slice_count=15,
                context_pr_number=5,
            )
        # Slug should be truncated to the configured max (18 chars).
        assert captured["title"].startswith("[pipeline-f4c7d780")
        assert "[slice-2/15]" in captured["title"]
        assert "Bring up the orchestrator wire" in captured["title"]


class TestSelfIpResolution:
    """Tests for self_ip property used in temporary session registration."""

    def test_self_ip_resolves_to_local_address(self, gateway_client, mock_gateway_server):
        """Test that self_ip resolves to a routable local address."""
        ip = gateway_client.self_ip
        # Should be a valid IPv4 address, not empty
        assert ip
        parts = ip.split(".")
        assert len(parts) == 4
        assert all(p.isdigit() for p in parts)

    def test_self_ip_is_cached(self, gateway_client, mock_gateway_server):
        """Test that self_ip result is cached across accesses."""
        ip1 = gateway_client.self_ip
        ip2 = gateway_client.self_ip
        assert ip1 == ip2

    def test_self_ip_fallback_on_resolution_failure(self):
        """Test fallback to 127.0.0.1 when gateway host is unresolvable."""
        client = GatewayClient(
            gateway_host="nonexistent-host-that-will-never-resolve.invalid",
            gateway_port=TEST_GATEWAY_PORT,
            launcher_secret="test-secret",
        )
        assert client.self_ip == "127.0.0.1"

    def test_temp_sessions_use_self_ip(self, gateway_client, mock_gateway_server):
        """Test that temporary sessions register with self_ip, not 127.0.0.1.

        ``push_worktree_branch`` no longer registers a session (#2051 — it
        uses launcher auth directly), so this exercises a temp-session
        path that still does: ``fetch_worktree_branch``.
        """
        with patch.object(
            gateway_client, "register_session", wraps=gateway_client.register_session
        ) as mock_reg:
            gateway_client.fetch_worktree_branch(
                pipeline_id="issue-42",
                repo_path="/some/path",
            )
            mock_reg.assert_called_once()
            call_kwargs = mock_reg.call_args
            registered_ip = call_kwargs.kwargs.get("container_ip") or call_kwargs[1].get(
                "container_ip"
            )
            assert registered_ip == gateway_client.self_ip
            assert registered_ip != "127.0.0.1" or gateway_client.self_ip == "127.0.0.1"


class TestGetRepoVisibility:
    """Tests for get_repo_visibility method."""

    def test_get_repo_visibility_public(self, gateway_client):
        """Test detecting a public repo."""
        with patch.object(gateway_client, "_make_request") as mock_request:
            mock_request.return_value = {
                "data": {"visibilities": {"owner/repo": "public"}},
            }
            result = gateway_client.get_repo_visibility("owner/repo")
            assert result == "public"
            mock_request.assert_called_once_with(
                "/api/v1/repos/visibility?repos=owner%2Frepo",
                use_launcher_auth=True,
            )

    def test_get_repo_visibility_private(self, gateway_client):
        """Test detecting a private repo."""
        with patch.object(gateway_client, "_make_request") as mock_request:
            mock_request.return_value = {
                "data": {"visibilities": {"owner/repo": "private"}},
            }
            result = gateway_client.get_repo_visibility("owner/repo")
            assert result == "private"

    def test_get_repo_visibility_internal(self, gateway_client):
        """Test detecting an internal repo."""
        with patch.object(gateway_client, "_make_request") as mock_request:
            mock_request.return_value = {
                "data": {"visibilities": {"owner/repo": "internal"}},
            }
            result = gateway_client.get_repo_visibility("owner/repo")
            assert result == "internal"

    def test_get_repo_visibility_gateway_error(self, gateway_client):
        """Test graceful fallback when gateway returns an error."""
        with patch.object(gateway_client, "_make_request") as mock_request:
            mock_request.side_effect = GatewayError("Connection refused")
            result = gateway_client.get_repo_visibility("owner/repo")
            assert result is None

    def test_get_repo_visibility_missing_repo(self, gateway_client):
        """Test when repo is not in the response."""
        with patch.object(gateway_client, "_make_request") as mock_request:
            mock_request.return_value = {
                "data": {"visibilities": {}},
            }
            result = gateway_client.get_repo_visibility("owner/repo")
            assert result is None

    def test_get_repo_visibility_empty_data(self, gateway_client):
        """Test when response has no data field."""
        with patch.object(gateway_client, "_make_request") as mock_request:
            mock_request.return_value = {}
            result = gateway_client.get_repo_visibility("owner/repo")
            assert result is None

    def test_get_repo_visibility_null_data(self, gateway_client):
        """Test graceful handling when data field is None (would cause AttributeError)."""
        with patch.object(gateway_client, "_make_request") as mock_request:
            mock_request.return_value = {"data": None}
            result = gateway_client.get_repo_visibility("owner/repo")
            assert result is None


class TestListRemoteBranchesWithShasOperationTag:
    """Tests for the operation_tag kwarg on list_remote_branches_with_shas.

    The kwarg controls the audit-log identifier the synthetic gateway
    session registers under (``f"{pipeline_id}-{operation_tag}"``).
    Empty / non-alphanumeric values would silently break the log-filter
    rules the kwarg was added to preserve, so callers that pass garbage
    must fail loudly rather than register a malformed session.
    """

    def test_empty_operation_tag_raises(self, gateway_client):
        with pytest.raises(ValueError, match="operation_tag"):
            gateway_client.list_remote_branches_with_shas(
                "pipeline-1",
                "/repo",
                operation_tag="",
            )

    def test_operation_tag_with_slash_raises(self, gateway_client):
        with pytest.raises(ValueError, match="operation_tag"):
            gateway_client.list_remote_branches_with_shas(
                "pipeline-1",
                "/repo",
                operation_tag="ls/remote",
            )

    def test_operation_tag_with_whitespace_raises(self, gateway_client):
        with pytest.raises(ValueError, match="operation_tag"):
            gateway_client.list_remote_branches_with_shas(
                "pipeline-1",
                "/repo",
                operation_tag="ls remote",
            )

    def test_unicode_operation_tag_raises(self, gateway_client):
        # ``str.isalnum()`` accepts non-ASCII letters (e.g. "café"),
        # which would silently produce a mixed-encoding audit-log
        # identifier and break the log-filter rules the kwarg was
        # added to preserve. The validator must reject these.
        with pytest.raises(ValueError, match="operation_tag"):
            gateway_client.list_remote_branches_with_shas(
                "pipeline-1",
                "/repo",
                operation_tag="café",
            )

    def test_hyphenated_operation_tag_accepted(self, gateway_client, mock_gateway_server):
        # The canonical caller passes a hyphen-separated tag — must pass
        # validation AND propagate to the synthetic-session container_id
        # as ``f"{pipeline_id}-{operation_tag}"``. Pinning the container_id
        # is the actual contract the kwarg was added to preserve, so we
        # capture the ``register_session`` call-site rather than relying
        # on the bare return-type assertion.
        with patch.object(
            gateway_client, "register_session", wraps=gateway_client.register_session
        ) as mock_reg:
            result = gateway_client.list_remote_branches_with_shas(
                "pipeline-1",
                "/repo",
                operation_tag="stacked-pr-ls-remote",
            )
        assert isinstance(result, dict)
        mock_reg.assert_called_once()
        registered_id = mock_reg.call_args.kwargs.get("container_id") or mock_reg.call_args[1].get(
            "container_id"
        )
        assert registered_id == "pipeline-1-stacked-pr-ls-remote"


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
