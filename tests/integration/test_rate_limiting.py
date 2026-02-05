"""Integration tests for rate limiting behavior.

Ported from gateway/tests/integration_test.sh lines 462-505.
Verifies the gateway rate-limits excessive session registrations and heartbeats.
"""

import time

import pytest


@pytest.mark.integration
class TestRateLimiting:
    """Tests for rate limiting on gateway endpoints."""

    def test_normal_volume_not_rate_limited(self, egg_stack):
        """A small number of requests are not rate-limited."""
        tokens = []
        for i in range(5):
            container_id = f"test-rate-normal-{i}-{time.time_ns()}"
            result = egg_stack.create_session(container_id=container_id)
            assert result.get("success") is True, (
                f"Request {i + 1} was unexpectedly blocked: {result}"
            )
            token = result.get("data", result).get("session_token")
            if token:
                tokens.append(token)

        # Cleanup
        for token in tokens:
            egg_stack.delete_session(token)

    def test_excessive_registrations_trigger_429(self, egg_stack):
        """Rapid session registrations eventually trigger 429 Too Many Requests.

        The gateway rate-limits session registrations to 10 per minute per source IP.
        """
        tokens = []
        got_429 = False

        for i in range(20):
            container_id = f"test-rate-flood-{i}-{time.time_ns()}"
            resp = egg_stack.api_request(
                "POST",
                "/api/v1/sessions/create",
                token=egg_stack.launcher_secret,
                json_data={
                    "container_id": container_id,
                    "container_ip": "172.40.0.100",
                    "mode": "private",
                    "repos": ["test-owner/test-repo"],
                    "uid": 1000,
                    "gid": 1000,
                },
            )

            if resp.status_code == 429:
                got_429 = True
                break

            if resp.status_code == 200:
                try:
                    body = resp.json()
                    token = body.get("data", body).get("session_token")
                    if token:
                        tokens.append(token)
                except ValueError:
                    pass

        # Cleanup
        for token in tokens:
            egg_stack.delete_session(token)

        assert got_429, (
            f"Expected 429 after excessive registrations, but all {len(tokens)} requests succeeded"
        )

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
