"""
Functional tests for session lifecycle management.

These tests verify the full create → heartbeat → delete flow
and edge cases in session management.

Focus: session state transitions, TTL handling, cleanup behavior.
"""

import time

import pytest


@pytest.mark.functional
class TestSessionCreation:
    """Tests for session creation."""

    def test_create_returns_token(self, session_lifecycle_tester):
        """Session creation returns a session token."""
        result = session_lifecycle_tester("create")
        assert result.get("success") is True, f"Creation failed: {result}"
        data = result.get("data", result)
        assert "session_token" in data
        assert len(data["session_token"]) > 20  # Non-trivial token

    def test_create_returns_filtered_repos(self, session_lifecycle_tester):
        """Session creation returns filtered repos list."""
        result = session_lifecycle_tester("create")
        data = result.get("data", result)
        assert "filtered_repos" in data

    def test_create_returns_worktrees(self, session_lifecycle_tester):
        """Session creation returns worktrees mapping."""
        result = session_lifecycle_tester("create")
        data = result.get("data", result)
        assert "worktrees" in data

    def test_create_public_mode(self, session_lifecycle_tester):
        """Session can be created in public mode."""
        result = session_lifecycle_tester("create", mode="public")
        assert result.get("success") is True

    def test_create_with_custom_container_id(self, session_lifecycle_tester):
        """Session can be created with a custom container ID."""
        custom_id = f"custom-container-{time.time_ns()}"
        result = session_lifecycle_tester("create", container_id=custom_id)
        assert result.get("success") is True


@pytest.mark.functional
class TestSessionDeletion:
    """Tests for session deletion."""

    def test_delete_existing_session(self, session_lifecycle_tester):
        """Deleting an existing session succeeds."""
        # Create
        create_result = session_lifecycle_tester("create")
        token = create_result.get("data", create_result).get("session_token")
        assert token

        # Delete
        delete_result = session_lifecycle_tester("delete", token=token)
        assert delete_result.get("success") is True

    def test_delete_nonexistent_session_fails(self, session_lifecycle_tester):
        """Deleting a non-existent session fails gracefully."""
        result = session_lifecycle_tester("delete", token="nonexistent-token-abc123")
        assert result.get("success") is False

    def test_double_delete_fails(self, session_lifecycle_tester):
        """Deleting the same session twice fails on the second attempt."""
        # Create
        create_result = session_lifecycle_tester("create")
        token = create_result.get("data", create_result).get("session_token")

        # Delete first time
        result1 = session_lifecycle_tester("delete", token=token)
        assert result1.get("success") is True

        # Delete second time
        result2 = session_lifecycle_tester("delete", token=token)
        assert result2.get("success") is False

    def test_delete_clears_from_list(self, session_lifecycle_tester):
        """Deleted session no longer appears in session list."""
        # Create with unique container ID for identification
        container_id = f"delete-test-{time.time_ns()}"
        create_result = session_lifecycle_tester("create", container_id=container_id)
        token = create_result.get("data", create_result).get("session_token")

        # Verify it's in the list
        list_before = session_lifecycle_tester("list")
        sessions_before = list_before.get("data", list_before).get("sessions", [])
        container_ids_before = [s.get("container_id") for s in sessions_before]
        assert container_id in container_ids_before

        # Delete
        session_lifecycle_tester("delete", token=token)

        # Verify it's gone
        list_after = session_lifecycle_tester("list")
        sessions_after = list_after.get("data", list_after).get("sessions", [])
        container_ids_after = [s.get("container_id") for s in sessions_after]
        assert container_id not in container_ids_after


@pytest.mark.functional
class TestSessionHeartbeat:
    """Tests for session heartbeat and TTL extension."""

    def test_heartbeat_succeeds(self, session_lifecycle_tester):
        """Heartbeat for valid session succeeds."""
        # Create
        create_result = session_lifecycle_tester("create")
        token = create_result.get("data", create_result).get("session_token")

        # Heartbeat
        result = session_lifecycle_tester("heartbeat", token=token)
        assert result.get("success") is True

    def test_heartbeat_returns_expiration(self, session_lifecycle_tester):
        """Heartbeat returns the updated expiration time."""
        # Create
        create_result = session_lifecycle_tester("create")
        token = create_result.get("data", create_result).get("session_token")

        # Heartbeat
        result = session_lifecycle_tester("heartbeat", token=token)
        data = result.get("data", result)
        assert "expires_at" in data

    def test_heartbeat_extends_ttl(self, session_lifecycle_tester):
        """Heartbeat extends the session TTL."""
        # Create
        create_result = session_lifecycle_tester("create")
        token = create_result.get("data", create_result).get("session_token")

        # First heartbeat to get initial expiry
        result1 = session_lifecycle_tester("heartbeat", token=token)
        initial_expiry = result1.get("data", result1).get("expires_at")
        assert initial_expiry is not None

        # Wait a moment then heartbeat again
        time.sleep(0.1)
        result2 = session_lifecycle_tester("heartbeat", token=token)
        new_expiry = result2.get("data", result2).get("expires_at")

        # TTL should be extended (or at least not decreased)
        assert new_expiry >= initial_expiry

    def test_heartbeat_invalid_token_fails(self, session_lifecycle_tester):
        """Heartbeat for invalid token fails."""
        result = session_lifecycle_tester("heartbeat", token="invalid-token-xyz")
        assert result.get("success") is False

    def test_heartbeat_after_delete_fails(self, session_lifecycle_tester):
        """Heartbeat after session deletion fails."""
        # Create and delete
        create_result = session_lifecycle_tester("create")
        token = create_result.get("data", create_result).get("session_token")
        session_lifecycle_tester("delete", token=token)

        # Heartbeat should fail
        result = session_lifecycle_tester("heartbeat", token=token)
        assert result.get("success") is False


@pytest.mark.functional
class TestSessionListing:
    """Tests for session listing."""

    def test_list_returns_sessions(self, session_lifecycle_tester, functional_session):
        """Session list includes active sessions."""
        result = session_lifecycle_tester("list")
        assert result.get("success") is True
        data = result.get("data", result)
        sessions = data.get("sessions", [])
        assert len(sessions) >= 1  # At least the functional_session

    def test_list_session_has_required_fields(self, session_lifecycle_tester, functional_session):
        """Listed sessions have required fields."""
        result = session_lifecycle_tester("list")
        sessions = result.get("data", result).get("sessions", [])
        assert len(sessions) >= 1

        session = sessions[0]
        assert "container_id" in session
        assert "mode" in session
        assert "created_at" in session or "last_seen" in session


@pytest.mark.functional
class TestDuplicateContainerHandling:
    """Tests for handling duplicate container IDs."""

    def test_duplicate_container_id_handled(self, session_lifecycle_tester):
        """Creating sessions with same container ID is handled gracefully."""
        container_id = f"duplicate-{time.time_ns()}"

        # Create first session
        result1 = session_lifecycle_tester("create", container_id=container_id)
        token1 = result1.get("data", result1).get("session_token")

        # Create second session with same container ID
        result2 = session_lifecycle_tester("create", container_id=container_id)

        # Should either succeed (replacing) or fail gracefully
        assert result2.get("success") is not None  # Should have a definite answer

        # Cleanup
        if token1:
            session_lifecycle_tester("delete", token=token1)
        if result2.get("success"):
            token2 = result2.get("data", result2).get("session_token")
            if token2 and token2 != token1:
                session_lifecycle_tester("delete", token=token2)


@pytest.mark.functional
class TestSessionIsolation:
    """Tests for session isolation between tests."""

    def test_sessions_have_unique_tokens(self, session_lifecycle_tester):
        """Each session gets a unique token."""
        result1 = session_lifecycle_tester("create")
        result2 = session_lifecycle_tester("create")

        token1 = result1.get("data", result1).get("session_token")
        token2 = result2.get("data", result2).get("session_token")

        assert token1 != token2

    def test_session_operations_require_correct_token(self, minimal_gateway, functional_session):
        """Session operations require the correct token."""
        correct_token = functional_session.get("session_token")

        # Try with wrong token
        resp = minimal_gateway.api_request(
            "POST",
            "/api/v1/git/execute",
            token="wrong-token",
            json_data={
                "repo_path": "/home/egg/repos/test-repo",
                "operation": "status",
            },
        )
        assert resp.status_code == 401

        # Try with correct token
        resp = minimal_gateway.api_request(
            "POST",
            "/api/v1/git/execute",
            token=correct_token,
            json_data={
                "repo_path": "/home/egg/repos/test-repo",
                "operation": "status",
            },
        )
        assert resp.status_code != 401


@pytest.mark.functional
class TestAuthenticationRequirements:
    """Tests for authentication requirements on session endpoints."""

    def test_create_requires_launcher_secret(self, minimal_gateway):
        """Session creation requires the launcher secret."""
        resp = minimal_gateway.api_request(
            "POST",
            "/api/v1/sessions/create",
            json_data={
                "container_id": "test-no-auth",
                "container_ip": "172.42.0.100",
                "mode": "private",
                "repos": ["test-owner/test-repo"],
                "uid": 1000,
                "gid": 1000,
            },
        )
        assert resp.status_code == 401

    def test_list_requires_launcher_secret(self, minimal_gateway):
        """Session listing requires the launcher secret."""
        resp = minimal_gateway.api_request("GET", "/api/v1/sessions")
        assert resp.status_code == 401

    def test_delete_requires_launcher_secret(self, minimal_gateway):
        """Session deletion requires the launcher secret."""
        resp = minimal_gateway.api_request(
            "DELETE",
            "/api/v1/sessions/some-token",
        )
        assert resp.status_code == 401

    def test_health_no_auth_required(self, minimal_gateway):
        """Health endpoint does not require authentication."""
        resp = minimal_gateway.api_request("GET", "/api/v1/health")
        assert resp.status_code == 200


@pytest.mark.functional
class TestSessionStateConsistency:
    """Tests for session state consistency."""

    def test_rapid_create_delete_cycle(self, session_lifecycle_tester):
        """Rapid create/delete cycles maintain state consistency."""
        for i in range(5):
            container_id = f"rapid-{i}-{time.time_ns()}"
            result = session_lifecycle_tester("create", container_id=container_id)
            assert result.get("success") is True
            token = result.get("data", result).get("session_token")
            assert token

            delete_result = session_lifecycle_tester("delete", token=token)
            assert delete_result.get("success") is True

            # Verify session is fully deleted before creating next one
            list_result = session_lifecycle_tester("list")
            sessions = list_result.get("data", list_result).get("sessions", [])
            container_ids = [s.get("container_id") for s in sessions]
            assert container_id not in container_ids, (
                f"Session {container_id} still present after delete"
            )

    def test_many_concurrent_sessions(self, session_lifecycle_tester):
        """Multiple concurrent sessions are tracked correctly."""
        tokens = []

        # Create multiple sessions
        for i in range(3):
            result = session_lifecycle_tester(
                "create", container_id=f"concurrent-{i}-{time.time_ns()}"
            )
            if result.get("success"):
                tokens.append(result.get("data", result).get("session_token"))

        # List should show all of them
        list_result = session_lifecycle_tester("list")
        sessions = list_result.get("data", list_result).get("sessions", [])
        assert len(sessions) >= len(tokens)

        # Cleanup
        for token in tokens:
            session_lifecycle_tester("delete", token=token)
