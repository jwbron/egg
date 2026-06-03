"""
Tests for gateway client.
"""

import json
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from threading import Thread
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

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


class TestMakeRequestErrorDetailParsing:
    """End-to-end coverage of ``_make_request``'s ``HTTPError`` → parse →
    ``GatewayError(details=...)`` path.

    Locks in the PR #2895 fix that reads error details from the
    gateway's actual envelope shape (``"data"`` key, produced by
    ``make_error → make_response``) with a fallback to ``"details"`` for
    routes that emit the key directly (e.g. ``mode_gate`` private-mode
    403). Without this, ``exc.details`` was always ``None`` for
    ``/api/v1/git/execute`` failures and the downstream
    ``returncode != 1`` warning gate in ``merge_base`` /
    ``_sha_is_ancestor`` fired on every legitimate exit-1 (no shared
    ancestor / not-an-ancestor) case.

    Existing slice-4 tests construct ``GatewayError(..., details=...)``
    directly via the constructor, which bypasses the parse path
    entirely — so a regression in ``_make_request`` would not surface
    there. These tests exercise the parse path end-to-end via mocked
    ``urlopen`` to prevent silent regression if the gateway envelope
    shape ever changes.
    """

    @staticmethod
    def _http_error(body: dict, *, code: int = 500) -> HTTPError:
        return HTTPError(
            url="http://gateway/api/v1/git/execute",
            code=code,
            msg="error",
            hdrs={},  # type: ignore[arg-type]
            fp=BytesIO(json.dumps(body).encode()),
        )

    def test_parses_details_from_data_key(self, gateway_client):
        """Gateway's actual envelope shape: ``make_error`` routes details
        through ``make_response``'s ``data`` parameter, so the wire
        payload is ``{"success": false, "message": "...", "data":
        {...}}``. The fix MUST read from ``"data"``; the prior
        ``error_data.get("details")`` always returned ``None`` here.
        """
        err = self._http_error(
            {
                "success": False,
                "message": "git merge-base failed",
                "data": {"returncode": 1, "stderr": ""},
            }
        )
        with patch("gateway_client.urlopen", side_effect=err):
            with pytest.raises(GatewayError) as exc_info:
                gateway_client._make_request("/api/v1/git/execute", method="POST", data={})

        assert exc_info.value.details is not None, (
            "details must be populated from gateway's data-key envelope "
            "(the bug this test pins prevents the returncode=1 suppression "
            "in merge_base / _sha_is_ancestor from working)"
        )
        assert exc_info.value.details["returncode"] == 1
        assert exc_info.value.status_code == 500
        assert "git merge-base failed" in str(exc_info.value)

    def test_falls_back_to_details_key(self, gateway_client):
        """Some routes emit ``"details"`` directly at the top level
        (notably ``mode_gate``'s 403 private-mode rejection). The
        fallback in the parse path MUST preserve those details when
        ``"data"`` is absent.
        """
        err = self._http_error(
            {
                "success": False,
                "message": "private mode required",
                "details": {"reason": "private_mode_required"},
            },
            code=403,
        )
        with patch("gateway_client.urlopen", side_effect=err):
            with pytest.raises(GatewayError) as exc_info:
                gateway_client._make_request("/api/v1/jira/search", method="POST", data={})

        assert exc_info.value.details is not None
        assert exc_info.value.details["reason"] == "private_mode_required"
        assert exc_info.value.status_code == 403

    def test_prefers_data_when_both_keys_present(self, gateway_client):
        """If a hypothetical response carried both keys, ``"data"`` is
        canonical (it's what the standard ``make_response`` writes) —
        the ``or`` short-circuit MUST prefer it.
        """
        err = self._http_error(
            {
                "success": False,
                "message": "boom",
                "data": {"source": "data-key"},
                "details": {"source": "details-key"},
            }
        )
        with patch("gateway_client.urlopen", side_effect=err):
            with pytest.raises(GatewayError) as exc_info:
                gateway_client._make_request("/api/v1/health", method="GET")

        assert exc_info.value.details == {"source": "data-key"}

    def test_details_none_when_envelope_carries_neither_key(self, gateway_client):
        """An envelope with neither ``"data"`` nor ``"details"`` (e.g. a
        bare ``{"success": false, "message": "..."}`` from a route that
        didn't pass details) MUST surface ``exc.details = None`` rather
        than raise. Confirms ``or`` is the right operator (not ``and``,
        not a strict ``KeyError``-on-missing).
        """
        err = self._http_error({"success": False, "message": "rate limited"}, code=429)
        with patch("gateway_client.urlopen", side_effect=err):
            with pytest.raises(GatewayError) as exc_info:
                gateway_client._make_request("/api/v1/sessions/create", method="POST", data={})

        assert exc_info.value.details is None
        assert exc_info.value.status_code == 429
        assert "rate limited" in str(exc_info.value)


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

    def test_create_pr_registers_synthetic_session_without_phase(
        self, gateway_client, mock_gateway_server
    ):
        """Synthetic PR session is registered with no phase (#2777 TASK-2-2).

        The PR phase (``PipelinePhase.PR``) was removed; ``create_pr`` now
        registers the synthetic ``gh pr create`` session with ``phase=None``,
        which the gateway treats as the explicit phase-filter opt-out.
        """
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
            assert call_kwargs.kwargs.get("phase") is None


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
                slice_index=2,
                slice_count=15,
                context_pr_number=5,
            )
        # Slug should be truncated to the configured max (18 chars).
        assert captured["title"].startswith("[pipeline-f4c7d780")
        assert "[slice-2/15]" in captured["title"]
        assert "Bring up the orchestrator wire" in captured["title"]

    def test_create_slice_pr_does_not_emit_umbrella_banner(self, gateway_client):
        """#2777 cq-6 / task-3-1: ``create_slice_pr`` must NOT emit the
        legacy ``"Program-level umbrella PR ..."`` banner string for any
        slice — terminal or non-terminal. Program-level rollups now live
        on the up-front context PR opened by
        ``_open_context_pr_at_implement_start``; the slice PR body is
        slice-scoped only.

        This is the negative-assert counterpart to the umbrella-rollup
        deletion: a future re-introduction of the banner (e.g. a
        merge-of-shame from a stale branch) trips this test
        unconditionally, regardless of slice topology. We exercise both
        a terminal-shaped call (no ``context_pr_number``) AND a
        non-terminal shaped call (with ``context_pr_number``) to pin
        the parity across both branches.
        """
        captured, ctx = self._capture(gateway_client)
        # Terminal-shaped (no base PR pointer).
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
                program_manual_steps="Pre-merge: verify seam tables.",
                slice_index=3,
                slice_count=3,
            )
        assert "Program-level umbrella PR" not in captured["body"]
        assert "merge-gate" not in captured["title"]

        # Non-terminal-shaped (with context_pr_number).
        captured2, ctx2 = self._capture(gateway_client)
        with ctx2:
            gateway_client.create_slice_pr(
                pipeline_id="issue-42",
                repo="owner/repo",
                slice_id="slice-1",
                slice_name="Pattern adoption",
                slice_tasks=[{"id": "task-1-1", "description": "do X"}],
                head="egg/issue-42/slice-1",
                base="egg/issue-42/work",
                program_title="Decompose oversize files",
                slice_index=1,
                slice_count=3,
                context_pr_number=99,
            )
        assert "Program-level umbrella PR" not in captured2["body"]


class TestLookupOpenPr:
    """#2777 cq-8 / task-3-2: ``GatewayClient.lookup_open_pr`` is the
    server-side idempotency primitive used by ``create_slice_pr``.

    The lookup runs on the **control-plane** route
    ``/api/v1/gh/find_open_pr`` with launcher auth — the orchestrator is
    the server that manages pipelines, not an agent, so it does not
    register a synthetic agent session or impersonate a role on the
    per-agent ``/api/v1/gh/execute`` surface (the #2893 conflation this
    supersedes). These tests exercise the helper's contract in isolation
    against a mocked ``_make_request``: it POSTs ``{repo, head, base}``
    with ``use_launcher_auth=True`` and reads ``data.number`` from the
    response. The wider call-site behaviour (no ``gh pr create`` invoked
    when an existing PR matches) lives in the ``TestCreateSlicePR`` block
    above; these tests pin the primitive's own input/output contract.
    """

    def test_lookup_open_pr_uses_control_plane_route_with_launcher_auth(self, gateway_client):
        """The lookup must hit the launcher-authed control-plane route,
        never register a synthetic agent session."""
        from unittest.mock import patch

        captured: dict = {}

        def fake_make_request(endpoint, method, data, **kwargs):  # noqa: ARG001
            captured["endpoint"] = endpoint
            captured["data"] = data
            captured["use_launcher_auth"] = kwargs.get("use_launcher_auth")
            return {"success": True, "data": {"number": 4242}}

        with (
            patch.object(gateway_client, "register_session") as mock_register,
            patch.object(gateway_client, "_make_request", side_effect=fake_make_request),
        ):
            result = gateway_client.lookup_open_pr(
                pipeline_id="issue-42",
                repo="owner/repo",
                head="egg/issue-42/slice-1",
                base="egg/issue-42/work",
            )
        assert result == 4242
        assert captured["endpoint"] == "/api/v1/gh/find_open_pr"
        assert captured["use_launcher_auth"] is True
        assert captured["data"] == {
            "repo": "owner/repo",
            "head": "egg/issue-42/slice-1",
            "base": "egg/issue-42/work",
        }
        mock_register.assert_not_called()

    def test_lookup_open_pr_returns_none_on_miss(self, gateway_client):
        """A ``null`` number (no PR matches the head + base filter) is the
        canonical miss — ``lookup_open_pr`` returns None so the caller
        falls through to ``gh pr create``."""
        from unittest.mock import patch

        def fake_make_request(endpoint, method, data, **kwargs):  # noqa: ARG001
            return {"success": True, "data": {"number": None}}

        with patch.object(gateway_client, "_make_request", side_effect=fake_make_request):
            result = gateway_client.lookup_open_pr(
                pipeline_id="issue-42",
                repo="owner/repo",
                head="egg/issue-42/slice-1",
                base="egg/issue-42/work",
            )
        assert result is None

    def test_lookup_open_pr_returns_none_on_missing_head_or_base(self, gateway_client):
        """Defence-in-depth: ``lookup_open_pr`` must NOT invoke the lookup
        with an empty ``head`` or ``base`` filter (would surface every open
        PR in the repo and the caller's ``if existing is not None`` would
        match the first one, spuriously treating an unrelated PR as the
        idempotent hit)."""
        from unittest.mock import patch

        with patch.object(gateway_client, "_make_request") as mock_request:
            # Empty head.
            assert (
                gateway_client.lookup_open_pr(
                    pipeline_id="issue-42",
                    repo="owner/repo",
                    head="",
                    base="main",
                )
                is None
            )
            # Empty base.
            assert (
                gateway_client.lookup_open_pr(
                    pipeline_id="issue-42",
                    repo="owner/repo",
                    head="egg/issue-42/slice-1",
                    base="",
                )
                is None
            )
        mock_request.assert_not_called()

    def test_lookup_open_pr_returns_none_on_transport_error(self, gateway_client):
        """Transport failure returns None and the caller falls through to
        ``gh pr create``. Never raise — a transient lookup failure must not
        block PR creation."""
        from unittest.mock import patch

        def fake_make_request(endpoint, method, data, **kwargs):  # noqa: ARG001
            raise RuntimeError("gateway unreachable")

        with patch.object(gateway_client, "_make_request", side_effect=fake_make_request):
            result = gateway_client.lookup_open_pr(
                pipeline_id="issue-42",
                repo="owner/repo",
                head="egg/issue-42/slice-1",
                base="egg/issue-42/work",
            )
        assert result is None


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


class TestLsRemoteBranchStrict:
    """Tests for ``ls_remote_branch_strict`` — the strict tri-state
    ls-remote helper (#2928).

    The lenient ``ls_remote_branch`` swallows gateway/network/policy
    failures and returns ``False`` — collapsing "branch absent on
    origin" with "probe could not be performed". That is unsafe for
    the ``_resolve_slice_base_branch`` parent-existence gate, which
    treats ``False`` as "parent merged + cascade-deleted → route the
    slice onto ``pipeline_branch``" but a raised probe as "conservative
    default: assume the parent exists". The strict variant exists to
    preserve that distinction.
    """

    def test_present_branch_returns_true(self, gateway_client):
        """Success path: present branch returns True AND the synthetic
        session is torn down. Pinning the teardown on the success path
        — not just the error path (``test_session_deleted_on_error``) —
        guards against a future refactor that drops the ``finally``
        block and silently leaks gateway sessions on every successful
        probe.
        """
        session = MagicMock()
        session.session_token = "synthetic-token-xyz"
        with (
            patch.object(gateway_client, "register_session", return_value=session),
            patch.object(gateway_client, "delete_session") as mock_del,
            patch.object(
                gateway_client,
                "_make_request",
                return_value={"data": {"stdout": "abc123\trefs/heads/egg/issue-X/slice-1\n"}},
            ),
        ):
            result = gateway_client.ls_remote_branch_strict(
                pipeline_id="issue-X",
                repo_path="/tmp/x",
                ref="refs/heads/egg/issue-X/slice-1",
            )
        assert result is True
        mock_del.assert_called_once_with("synthetic-token-xyz")

    def test_absent_branch_returns_false(self, gateway_client):
        """An empty ls-remote stdout means the ref is absent on origin —
        this is the canonical FALSE return the resolver uses to route
        onto ``pipeline_branch``."""
        session = MagicMock()
        session.session_token = "synthetic-token-xyz"
        with (
            patch.object(gateway_client, "register_session", return_value=session),
            patch.object(gateway_client, "delete_session"),
            patch.object(
                gateway_client,
                "_make_request",
                return_value={"data": {"stdout": ""}},
            ),
        ):
            result = gateway_client.ls_remote_branch_strict(
                pipeline_id="issue-X",
                repo_path="/tmp/x",
                ref="refs/heads/egg/issue-X/slice-1",
            )
        assert result is False

    def test_gateway_error_raises(self, gateway_client):
        """The strict contract: a GatewayError from the underlying
        ``/git/fetch`` call MUST propagate to the caller — NOT be
        swallowed and returned as ``False``. This is what makes the
        method usable as the parent-existence probe in
        ``_resolve_slice_base_branch``: the resolver's ``try/except``
        catches the raised error and applies the conservative
        "assume parent exists" default, instead of mis-routing a
        real slice onto ``pipeline_branch`` on a flaky gateway (#2928).
        """
        session = MagicMock()
        session.session_token = "synthetic-token-xyz"
        with (
            patch.object(gateway_client, "register_session", return_value=session),
            patch.object(gateway_client, "delete_session"),
            patch.object(
                gateway_client,
                "_make_request",
                side_effect=GatewayError("gateway is down", status_code=502),
            ),
            pytest.raises(GatewayError, match="gateway is down"),
        ):
            gateway_client.ls_remote_branch_strict(
                pipeline_id="issue-X",
                repo_path="/tmp/x",
                ref="refs/heads/egg/issue-X/slice-1",
            )

    def test_register_session_error_raises(self, gateway_client):
        """A failure during session bootstrap (auth / network) MUST
        propagate. Lenient ``ls_remote_branch`` would log + return
        ``False``; the strict variant must NOT."""
        with (
            patch.object(
                gateway_client,
                "register_session",
                side_effect=GatewayError("register failed", status_code=500),
            ),
            patch.object(gateway_client, "delete_session"),
            pytest.raises(GatewayError, match="register failed"),
        ):
            gateway_client.ls_remote_branch_strict(
                pipeline_id="issue-X",
                repo_path="/tmp/x",
                ref="refs/heads/egg/issue-X/slice-1",
            )

    def test_session_deleted_on_error(self, gateway_client):
        """Even when ``_make_request`` raises, the synthetic session
        MUST be torn down in the ``finally`` block — otherwise we leak
        gateway state every time the probe encounters a flaky network.
        """
        session = MagicMock()
        session.session_token = "synthetic-token-xyz"
        with (
            patch.object(gateway_client, "register_session", return_value=session),
            patch.object(gateway_client, "delete_session") as mock_del,
            patch.object(
                gateway_client,
                "_make_request",
                side_effect=GatewayError("boom", status_code=502),
            ),
        ):
            with pytest.raises(GatewayError):
                gateway_client.ls_remote_branch_strict(
                    pipeline_id="issue-X",
                    repo_path="/tmp/x",
                    ref="refs/heads/egg/issue-X/slice-1",
                )
        mock_del.assert_called_once_with("synthetic-token-xyz")

    def test_envelope_success_false_raises(self, gateway_client):
        """A ``{"success": false, ...}`` envelope returned at HTTP 200 is
        a gateway-side failure surfaced via the response body rather
        than the status code. Without the envelope-success guard, the
        strict variant would silently collapse such a response to
        "branch absent" (empty ``stdout`` → ``False``) — directly
        contradicting its propagate-any-failure contract. Pin the
        invariant that envelope-level failures raise like HTTPError
        failures do.
        """
        session = MagicMock()
        session.session_token = "synthetic-token-xyz"
        with (
            patch.object(gateway_client, "register_session", return_value=session),
            patch.object(gateway_client, "delete_session"),
            patch.object(
                gateway_client,
                "_make_request",
                return_value={
                    "success": False,
                    "message": "git fetch failed: policy denied",
                    "data": {"stdout": ""},
                },
            ),
            pytest.raises(GatewayError, match="policy denied"),
        ):
            gateway_client.ls_remote_branch_strict(
                pipeline_id="issue-X",
                repo_path="/tmp/x",
                ref="refs/heads/egg/issue-X/slice-1",
            )


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


# ============================================================================
# register_session upstream kwargs — slice-1 of issue #2769 (TASK-1-7)
# ============================================================================
#
# Slice 1 extends ``GatewayClient.register_session`` with two optional
# kwargs:
#   - ``upstream: str | None = None``
#   - ``upstream_model: str | None = None``
#
# When set, they're included in the POST body to ``/api/v1/sessions/create``.
# When omitted, the body is unchanged from today's shape (back-compat
# guard — no caller in slice 1 actually sends them).
# ============================================================================


class TestRegisterSessionUpstreamKwargs:
    """Slice-1 wire-shape extensions to ``register_session``."""

    def test_omits_upstream_keys_when_not_provided(self, gateway_client):
        """Back-compat guard: no slice-1 caller passes the new kwargs.
        The wire body must be byte-identical to today's shape.
        """
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
                container_ip="172.18.0.5",
                mode="public",
            )

        assert "upstream" not in captured["data"], (
            "register_session must omit 'upstream' from the POST body when "
            "not provided — slice-1 no-op invariant"
        )
        assert "upstream_model" not in captured["data"], (
            "register_session must omit 'upstream_model' from the POST body "
            "when not provided — slice-1 no-op invariant"
        )

    def test_includes_upstream_litellm_when_provided(self, gateway_client):
        """Explicit LiteLLM kwargs land in the POST body."""
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
                container_id="qwen-agent",
                mode="private",
                upstream="litellm",
                upstream_model="qwen3-coder-30b",
            )

        assert captured["data"].get("upstream") == "litellm"
        assert captured["data"].get("upstream_model") == "qwen3-coder-30b"

    def test_includes_upstream_anthropic_explicit_when_provided(self, gateway_client):
        """Explicit ``upstream='anthropic'`` is a valid no-op that still
        lands in the body (so the audit log records the per-session
        upstream — TASK-1-5 AC).
        """
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
                container_id="explicit-anthropic-agent",
                mode="private",
                upstream="anthropic",
            )

        assert captured["data"].get("upstream") == "anthropic"
        # ``upstream_model`` not provided — should be omitted.
        assert "upstream_model" not in captured["data"]

    def test_upstream_only_without_upstream_model_emits_only_upstream(self, gateway_client):
        """Asymmetric kwargs: ``upstream='litellm'`` without
        ``upstream_model`` is valid (LiteLLM's default model_list will
        be consulted upstream).
        """
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
                container_id="litellm-default-agent",
                mode="private",
                upstream="litellm",
            )

        assert captured["data"].get("upstream") == "litellm"
        assert "upstream_model" not in captured["data"]
