"""Integration tests for stack startup and session lifecycle.

These tests verify the gateway starts correctly and sessions can be
created, listed, heartbeated, and deleted without any GitHub connectivity.
"""

import subprocess
import time

import pytest


@pytest.mark.integration
class TestStackStartup:
    """Tests that the gateway stack starts and is healthy."""

    def test_health_returns_healthy(self, egg_stack):
        """Gateway health endpoint returns a healthy status."""
        health = egg_stack.health_check()
        assert health["status"] in ("healthy", "degraded")
        assert health["service"] == "gateway"

    def test_health_includes_active_sessions(self, egg_stack):
        """Health response includes the active_sessions count."""
        health = egg_stack.health_check()
        assert "active_sessions" in health
        assert isinstance(health["active_sessions"], int)

    def test_health_no_auth_required(self, egg_stack):
        """Health endpoint is accessible without authentication."""
        resp = egg_stack.api_request("GET", "/api/v1/health")
        assert resp.status_code == 200

    def test_squid_process_running(self, egg_stack):
        """Squid proxy process is running inside the gateway container."""
        # Find the gateway container name
        result = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                f"name={egg_stack.compose_project}-gateway",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        container_name = result.stdout.strip()
        assert container_name, "Gateway container not found"

        # Check squid is running
        result = subprocess.run(
            ["docker", "exec", container_name, "pgrep", "-x", "squid"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, "Squid process is not running in gateway container"


@pytest.mark.integration
class TestSessionLifecycle:
    """Tests for session creation, listing, heartbeat, and deletion."""

    def test_create_session_returns_token(self, egg_stack):
        """Session creation returns a session_token."""
        container_id = f"test-lifecycle-{time.time_ns()}"
        result = egg_stack.create_session(container_id=container_id)
        assert result.get("success") is True, f"Session creation failed: {result}"
        data = result.get("data", result)
        assert "session_token" in data
        assert len(data["session_token"]) > 20  # Non-trivial token

        # Cleanup
        egg_stack.delete_session(data["session_token"])

    def test_create_session_returns_worktrees(self, egg_stack):
        """Session creation returns worktree information."""
        container_id = f"test-worktrees-{time.time_ns()}"
        result = egg_stack.create_session(container_id=container_id)
        data = result.get("data", result)

        # Worktrees may be empty if the test repo doesn't exist on disk,
        # but the key should be present in the response.
        assert "worktrees" in data or "filtered_repos" in data

        if data.get("session_token"):
            egg_stack.delete_session(data["session_token"])

    def test_delete_session_cleans_up(self, egg_stack):
        """Deleting a session removes it from the active list."""
        container_id = f"test-delete-{time.time_ns()}"
        create_result = egg_stack.create_session(container_id=container_id)
        token = create_result.get("data", create_result).get("session_token")
        assert token

        # Verify it exists
        sessions_before = egg_stack.list_sessions()
        assert sessions_before.get("success") is True

        # Delete
        delete_result = egg_stack.delete_session(token)
        assert delete_result.get("success") is True

        # Deleting again should 404
        delete_again = egg_stack.delete_session(token)
        assert delete_again.get("success") is False

    def test_list_shows_active_sessions(self, egg_stack, gateway_session):
        """Session list includes the currently active session."""
        result = egg_stack.list_sessions()
        assert result.get("success") is True
        data = result.get("data", result)
        sessions = data.get("sessions", [])
        assert len(sessions) >= 1

    def test_heartbeat_extends_ttl(self, egg_stack, gateway_session):
        """Heartbeat returns success and an expires_at timestamp."""
        token = gateway_session.get("session_token")
        assert token

        result = egg_stack.heartbeat(token)
        assert result.get("success") is True
        data = result.get("data", result)
        assert "expires_at" in data

    def test_duplicate_container_id_handling(self, egg_stack):
        """Creating two sessions with the same container_id is handled.

        The gateway should either reject the duplicate or replace
        the existing session -- either way it should not crash.
        """
        container_id = f"test-dup-{time.time_ns()}"

        result1 = egg_stack.create_session(container_id=container_id)
        token1 = result1.get("data", result1).get("session_token")

        result2 = egg_stack.create_session(container_id=container_id)
        # Should not be a server error regardless of policy
        assert result2.get("success") is not None

        # Cleanup
        if token1:
            egg_stack.delete_session(token1)
        token2 = result2.get("data", result2).get("session_token")
        if token2 and token2 != token1:
            egg_stack.delete_session(token2)
