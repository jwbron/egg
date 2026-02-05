"""Integration tests for gateway authentication flows.

Ported from gateway/tests/integration_test.sh lines 197-253.
Verifies session and launcher auth behavior without requiring GitHub.
"""

import pytest
import requests


@pytest.mark.integration
class TestAuthentication:
    """Tests for authentication enforcement on gateway endpoints."""

    def test_no_auth_returns_401(self, egg_stack):
        """Requests without Authorization header get 401."""
        resp = egg_stack.api_request(
            "POST",
            "/api/v1/git/execute",
            json_data={
                "repo_path": "/home/egg/repos/test-repo",
                "operation": "status",
            },
        )
        assert resp.status_code == 401

    def test_invalid_token_returns_401(self, egg_stack):
        """Requests with an invalid Bearer token get 401."""
        resp = egg_stack.api_request(
            "POST",
            "/api/v1/git/execute",
            token="invalid-token-abc123",
            json_data={
                "repo_path": "/home/egg/repos/test-repo",
                "operation": "status",
            },
        )
        assert resp.status_code == 401

    def test_valid_session_token_accepted(self, egg_stack, gateway_session):
        """Requests with a valid session token are accepted (not 401)."""
        token = gateway_session.get("session_token")
        assert token

        resp = egg_stack.api_request(
            "POST",
            "/api/v1/git/execute",
            token=token,
            json_data={
                "repo_path": "/home/egg/repos/test-repo",
                "operation": "status",
            },
        )
        # Should not be 401 -- may be 400/500 if repo doesn't exist,
        # but authentication itself should pass.
        assert resp.status_code != 401, f"Valid session token was rejected with 401: {resp.text}"

    def test_launcher_secret_works_for_admin(self, egg_stack):
        """Launcher secret authenticates admin endpoints (sessions list)."""
        resp = egg_stack.api_request(
            "GET",
            "/api/v1/sessions",
            token=egg_stack.launcher_secret,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("success") is True

    def test_session_token_rejected_for_admin(self, egg_stack, gateway_session):
        """Session tokens cannot access admin-only endpoints."""
        token = gateway_session.get("session_token")
        assert token

        resp = egg_stack.api_request(
            "GET",
            "/api/v1/sessions",
            token=token,
        )
        assert resp.status_code == 401, "Session token should not be accepted for admin endpoints"

    def test_missing_bearer_prefix_returns_401(self, egg_stack):
        """Authorization header without 'Bearer ' prefix is rejected."""
        resp = requests.post(
            f"{egg_stack.gateway_url}/api/v1/git/execute",
            headers={"Authorization": egg_stack.launcher_secret},
            json={
                "repo_path": "/home/egg/repos/test-repo",
                "operation": "status",
            },
            timeout=10,
        )
        assert resp.status_code == 401
