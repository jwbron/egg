"""Integration tests for rate limiting behavior.

Verifies the gateway rate-limits excessive heartbeats.
Session registration is not rate-limited because it requires the
launcher secret (an admin-only endpoint).
"""

import pytest


@pytest.mark.integration
class TestRateLimiting:
    """Tests for rate limiting on gateway endpoints."""

    def test_excessive_heartbeats_trigger_429(self, egg_stack, gateway_session):
        """Rapid heartbeats eventually trigger 429."""
        token = gateway_session.get("session_token")
        assert token

        got_429 = False
        for _i in range(150):
            resp = egg_stack.api_request(
                "POST",
                f"/api/v1/sessions/{token}/heartbeat",
                token=egg_stack.launcher_secret,
            )
            if resp.status_code == 429:
                got_429 = True
                break

        assert got_429, "Expected 429 after excessive heartbeats"
