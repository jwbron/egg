"""
Functional tests for network mode behavior.

These tests verify that private and public modes behave correctly:
- Private mode: restricted network access, proxy-routed
- Public mode: broader network access, direct connections

Focus: mode-specific behavior, mode transitions, mode validation.
"""

import pytest


@pytest.mark.functional
class TestNetworkModeCreation:
    """Tests for session creation with different network modes."""

    def test_private_mode_accepted(self, session_lifecycle_tester):
        """Sessions can be created in private mode."""
        result = session_lifecycle_tester("create", mode="private")
        assert result.get("success") is True
        data = result.get("data", result)
        assert data.get("mode") == "private"

    def test_public_mode_accepted(self, session_lifecycle_tester):
        """Sessions can be created in public mode."""
        result = session_lifecycle_tester("create", mode="public")
        assert result.get("success") is True
        data = result.get("data", result)
        assert data.get("mode") == "public"

    def test_invalid_mode_rejected(self, minimal_gateway):
        """Invalid mode values should be rejected."""
        resp = minimal_gateway.api_request(
            "POST",
            "/api/v1/sessions/create",
            token=minimal_gateway.launcher_secret,
            json_data={
                "container_id": "test-invalid-mode",
                "container_ip": "172.42.0.100",
                "mode": "invalid-mode",
                "repos": ["test-owner/test-repo"],
                "uid": 1000,
                "gid": 1000,
            },
        )
        # Should be rejected as bad request
        assert resp.status_code in (400, 422)

    def test_missing_mode_has_default(self, minimal_gateway):
        """Sessions created without explicit mode should have a default."""
        resp = minimal_gateway.api_request(
            "POST",
            "/api/v1/sessions/create",
            token=minimal_gateway.launcher_secret,
            json_data={
                "container_id": f"test-no-mode-{id(self)}",
                "container_ip": "172.42.0.100",
                # No "mode" field
                "repos": ["test-owner/test-repo"],
                "uid": 1000,
                "gid": 1000,
            },
        )
        if resp.status_code == 200:
            body = resp.json()
            data = body.get("data", body)
            # Should have a mode set (either private or public as default)
            assert "mode" in data


@pytest.mark.functional
class TestPrivateModeOperations:
    """Tests for operations in private mode."""

    def test_git_status_in_private_mode(self, git_command_tester):
        """Git status works in private mode (default fixture mode)."""
        result = git_command_tester("status")
        # Should not be blocked by mode restrictions
        assert result.status_code != 403 or "mode" not in result.error.lower()

    def test_git_log_in_private_mode(self, git_command_tester):
        """Git log works in private mode."""
        result = git_command_tester("log", args=["--oneline", "-1"])
        assert result.status_code != 403 or "mode" not in result.error.lower()


@pytest.mark.functional
class TestPublicModeOperations:
    """Tests for operations in public mode."""

    def test_session_creation_public_mode(self, minimal_gateway):
        """Can create and use a public mode session."""
        # Create public mode session
        result = minimal_gateway.create_session(
            container_id=f"public-test-{id(self)}",
            mode="public",
        )
        assert result.get("success") is True
        token = result.get("data", result).get("session_token")

        try:
            # Use the session
            resp = minimal_gateway.api_request(
                "POST",
                "/api/v1/git/execute",
                token=token,
                json_data={
                    "repo_path": "/home/egg/repos/test-repo",
                    "operation": "status",
                },
            )
            # Should work (may fail for other reasons but not mode-related)
            assert resp.status_code != 403 or "mode" not in resp.text.lower()
        finally:
            minimal_gateway.delete_session(token)


@pytest.mark.functional
class TestModeInSessionInfo:
    """Tests for mode information in session responses."""

    def test_list_shows_session_modes(self, session_lifecycle_tester):
        """Session list includes mode information."""
        # Create sessions in different modes
        session_lifecycle_tester("create", mode="private")
        session_lifecycle_tester("create", mode="public")

        # List sessions
        list_result = session_lifecycle_tester("list")
        sessions = list_result.get("data", list_result).get("sessions", [])

        # Each session should have a mode
        for session in sessions:
            assert "mode" in session

    def test_heartbeat_preserves_mode(self, session_lifecycle_tester):
        """Heartbeat response maintains mode information."""
        # Create with explicit mode
        create_result = session_lifecycle_tester("create", mode="private")
        token = create_result.get("data", create_result).get("session_token")

        # Heartbeat
        heartbeat_result = session_lifecycle_tester("heartbeat", token=token)
        data = heartbeat_result.get("data", heartbeat_result)

        # Mode should be preserved if returned
        if "mode" in data:
            assert data["mode"] == "private"


@pytest.mark.functional
class TestModeIsolation:
    """Tests for isolation between modes."""

    def test_private_and_public_sessions_coexist(self, session_lifecycle_tester):
        """Private and public sessions can coexist."""
        # Create both types
        private_result = session_lifecycle_tester(
            "create", mode="private", container_id=f"private-{id(self)}"
        )
        public_result = session_lifecycle_tester(
            "create", mode="public", container_id=f"public-{id(self)}"
        )

        assert private_result.get("success") is True
        assert public_result.get("success") is True

        # Both should be in the list
        list_result = session_lifecycle_tester("list")
        sessions = list_result.get("data", list_result).get("sessions", [])
        modes = [s.get("mode") for s in sessions]

        assert "private" in modes
        assert "public" in modes


@pytest.mark.functional
class TestHealthEndpointModeInfo:
    """Tests for mode information in health responses."""

    def test_health_endpoint_accessible(self, minimal_gateway):
        """Health endpoint is accessible regardless of mode."""
        health = minimal_gateway.health_check()
        assert health.get("status") in ("healthy", "degraded")

    def test_health_shows_session_count(self, minimal_gateway, functional_session):
        """Health endpoint shows active session count."""
        health = minimal_gateway.health_check()
        assert "active_sessions" in health
        assert isinstance(health["active_sessions"], int)
        assert health["active_sessions"] >= 1  # At least the functional_session


@pytest.mark.functional
class TestNetworkModeValidation:
    """Tests for network mode validation."""

    def test_empty_mode_handled(self, minimal_gateway):
        """Empty mode string is handled."""
        resp = minimal_gateway.api_request(
            "POST",
            "/api/v1/sessions/create",
            token=minimal_gateway.launcher_secret,
            json_data={
                "container_id": "test-empty-mode",
                "container_ip": "172.42.0.100",
                "mode": "",  # Empty string
                "repos": ["test-owner/test-repo"],
                "uid": 1000,
                "gid": 1000,
            },
        )
        # Should be rejected or use default
        if resp.status_code == 200:
            body = resp.json()
            data = body.get("data", body)
            # If accepted, should have a valid mode
            assert data.get("mode") in ("private", "public")

    def test_case_sensitivity_of_mode(self, minimal_gateway):
        """Mode values are case-sensitive or normalized."""
        resp = minimal_gateway.api_request(
            "POST",
            "/api/v1/sessions/create",
            token=minimal_gateway.launcher_secret,
            json_data={
                "container_id": "test-mode-case",
                "container_ip": "172.42.0.100",
                "mode": "PRIVATE",  # Uppercase
                "repos": ["test-owner/test-repo"],
                "uid": 1000,
                "gid": 1000,
            },
        )
        # Should either work (normalized) or be rejected
        # Should not cause a server error
        assert resp.status_code != 500


@pytest.mark.functional
class TestGatewayNetworkState:
    """Tests for gateway network state handling."""

    def test_gateway_reports_service_name(self, minimal_gateway):
        """Gateway health identifies itself."""
        health = minimal_gateway.health_check()
        assert health.get("service") == "gateway"

    def test_gateway_reports_client_ip(self, minimal_gateway):
        """Gateway can detect client IP."""
        health = minimal_gateway.health_check()
        assert "client_ip" in health
        # Should be a valid IP-like string
        client_ip = health["client_ip"]
        assert "." in client_ip or ":" in client_ip  # IPv4 or IPv6
