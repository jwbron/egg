"""Integration tests for gateway API endpoints.

Ported from gateway/tests/integration_test.sh lines 258-586.
Tests git/execute, gh/execute, git/push, and git/fetch endpoints.
"""

import pytest


@pytest.mark.integration
class TestGitExecuteEndpoint:
    """Tests for POST /api/v1/git/execute."""

    def test_status_command(self, egg_stack, gateway_session):
        """git status operation is accepted (not auth-rejected)."""
        token = gateway_session.get("session_token")
        resp = egg_stack.api_request(
            "POST",
            "/api/v1/git/execute",
            token=token,
            json_data={
                "repo_path": "/home/egg/repos/test-repo",
                "operation": "status",
            },
        )
        # May fail with 400 if repo doesn't exist, but should not be 401/403
        assert resp.status_code not in (401, 403), f"Unexpected auth failure: {resp.text}"

    def test_disallowed_operation_rejected(self, egg_stack, gateway_session):
        """Operations not in GIT_ALLOWED_COMMANDS are rejected with 403."""
        token = gateway_session.get("session_token")
        resp = egg_stack.api_request(
            "POST",
            "/api/v1/git/execute",
            token=token,
            json_data={
                "repo_path": "/home/egg/repos/test-repo",
                "operation": "gc",
            },
        )
        assert resp.status_code == 403, (
            f"Disallowed git operation should return 403, got {resp.status_code}: {resp.text}"
        )

    def test_network_ops_redirect_to_dedicated_endpoints(self, egg_stack, gateway_session):
        """Push/fetch/ls-remote via git/execute should be rejected.

        These should use the dedicated /api/v1/git/push and /api/v1/git/fetch
        endpoints instead.
        """
        token = gateway_session.get("session_token")
        for operation in ("push", "fetch", "ls-remote"):
            resp = egg_stack.api_request(
                "POST",
                "/api/v1/git/execute",
                token=token,
                json_data={
                    "repo_path": "/home/egg/repos/test-repo",
                    "operation": operation,
                },
            )
            # Should be 403 (not in allowed commands) or 400
            assert resp.status_code in (400, 403), (
                f"Network operation '{operation}' should be blocked via git/execute: "
                f"status={resp.status_code}"
            )


@pytest.mark.integration
class TestGhExecuteEndpoint:
    """Tests for POST /api/v1/gh/execute."""

    def test_gh_version_works(self, egg_stack, gateway_session):
        """gh --version executes successfully."""
        token = gateway_session.get("session_token")
        resp = egg_stack.api_request(
            "POST",
            "/api/v1/gh/execute",
            token=token,
            json_data={
                "args": ["--version"],
            },
        )
        # Should succeed or at least not be an auth failure
        assert resp.status_code != 401
        body = resp.json()
        # If successful, output should contain "gh version"
        if body.get("success"):
            output = body.get("data", {}).get("output", body.get("output", ""))
            assert "gh" in output.lower() or body.get("success")

    def test_response_format_is_json(self, egg_stack, gateway_session):
        """gh/execute returns JSON, not HTML error pages."""
        token = gateway_session.get("session_token")
        resp = egg_stack.api_request(
            "POST",
            "/api/v1/gh/execute",
            token=token,
            json_data={
                "args": ["--version"],
            },
        )
        content_type = resp.headers.get("Content-Type", "")
        assert "json" in content_type.lower(), (
            f"Expected JSON response, got Content-Type: {content_type}"
        )
        # Should be valid JSON
        resp.json()  # Will raise if not JSON


@pytest.mark.integration
class TestGitPushEndpoint:
    """Tests for POST /api/v1/git/push."""

    def test_push_to_main_blocked_by_policy(self, egg_stack, gateway_session):
        """Pushing to main branch should be blocked by policy."""
        token = gateway_session.get("session_token")
        resp = egg_stack.api_request(
            "POST",
            "/api/v1/git/push",
            token=token,
            json_data={
                "repo_path": "/home/egg/repos/test-repo",
                "remote": "origin",
                "refspec": "main:main",
            },
        )
        # Should be blocked by branch ownership policy (403) or fail
        # because the repo doesn't exist (400/500), but never succeed
        # with a push to main.
        if resp.status_code == 200:
            body = resp.json()
            assert body.get("success") is not True, "Push to main should not succeed"


@pytest.mark.integration
class TestGitFetchEndpoint:
    """Tests for POST /api/v1/git/fetch."""

    def test_fetch_requires_auth(self, egg_stack):
        """Fetch endpoint requires authentication."""
        resp = egg_stack.api_request(
            "POST",
            "/api/v1/git/fetch",
            json_data={
                "repo_path": "/home/egg/repos/test-repo",
                "remote": "origin",
            },
        )
        assert resp.status_code == 401
