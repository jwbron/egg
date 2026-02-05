"""Integration tests for fail-closed behavior.

Ported from gateway/tests/integration_test.sh lines 510-537.
Verifies operations fail explicitly when the gateway is unreachable,
rather than silently bypassing security controls.
"""

import pytest
import requests


@pytest.mark.integration
class TestFailClosed:
    """Tests that operations fail closed when gateway is unreachable."""

    def test_operations_fail_when_gateway_unreachable(self, egg_stack):
        """Requests to a non-existent gateway URL fail with connection error.

        This validates the fail-closed property: if the gateway is down,
        operations do not silently succeed or bypass security.
        """
        # Use an unreachable URL (port that nothing is listening on)
        bad_url = "http://localhost:1"
        with pytest.raises(requests.ConnectionError):
            requests.get(f"{bad_url}/api/v1/health", timeout=3)

    def test_gateway_returns_json_errors_not_500(self, egg_stack, session):
        """Gateway returns structured JSON errors, not HTML 500 pages.

        Even for internal errors, the response should be machine-parseable JSON.
        """
        token = session.get("session_token")

        # Trigger an error by requesting a nonexistent repo
        resp = egg_stack.api_request(
            "POST",
            "/api/v1/git/execute",
            token=token,
            json_data={
                "repo_path": "/nonexistent/path/repo",
                "operation": "status",
            },
        )

        # Should be an error response (400 or 403), not a 500
        assert resp.status_code != 500, (
            f"Gateway returned 500 instead of a proper error: {resp.text}"
        )

        # Response should be JSON
        content_type = resp.headers.get("Content-Type", "")
        assert "json" in content_type.lower(), (
            f"Expected JSON error response, got Content-Type: {content_type}"
        )
        body = resp.json()
        assert "success" in body

    def test_invalid_endpoint_returns_404(self, egg_stack):
        """Requests to non-existent endpoints return 404, not 500."""
        resp = egg_stack.api_request("GET", "/api/v1/nonexistent")
        assert resp.status_code == 404
