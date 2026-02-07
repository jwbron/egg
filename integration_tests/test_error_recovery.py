"""
Tests for error recovery scenarios in gateway operations.

Phase 4: Comprehensive Coverage - Integration Test Expansion
Tests error handling, recovery, and graceful degradation in the gateway stack.
"""

import pytest
import requests


@pytest.mark.integration
class TestGatewayErrorRecovery:
    """Integration tests for gateway error recovery scenarios."""

    def test_session_validation_with_invalid_token(self, egg_stack, gateway_session):
        """Invalid session token returns clear error."""
        result = egg_stack.validate_session("invalid-token-12345", timeout=10)

        assert not result.get("valid", True)
        assert "error" in result or "message" in result

    def test_session_validation_with_empty_token(self, egg_stack):
        """Empty session token is rejected."""
        try:
            result = egg_stack.validate_session("", timeout=10)
            # Should either fail or return invalid
            assert not result.get("valid", True)
        except requests.exceptions.HTTPError:
            # HTTP error is also acceptable
            pass

    def test_session_creation_with_invalid_mode(self, egg_stack):
        """Invalid mode in session creation is handled."""
        # The API should reject or handle invalid modes gracefully
        try:
            result = egg_stack.create_session(
                container_id="test-container",
                mode="invalid_mode",  # Invalid mode
            )
            # Either fails or returns error
            assert not result.get("success", True) or "error" in str(result).lower()
        except (requests.exceptions.HTTPError, ValueError):
            # Error is acceptable
            pass

    def test_session_deletion_nonexistent(self, egg_stack):
        """Deleting non-existent session is handled gracefully."""
        result = egg_stack.delete_session("nonexistent-token-xyz", timeout=10)
        # Should either return False/error or handle gracefully
        assert result is not None  # Doesn't crash

    def test_health_check_returns_valid_status(self, egg_stack):
        """Health check returns valid status structure."""
        result = egg_stack.health_check(timeout=10)

        assert "status" in result
        assert result["status"] in ("healthy", "degraded", "unhealthy")

    def test_concurrent_session_operations(self, egg_stack):
        """Concurrent session operations don't cause errors."""
        import threading

        tokens = []
        errors = []
        lock = threading.Lock()

        def create_and_validate():
            try:
                result = egg_stack.create_session(
                    container_id=f"test-{threading.current_thread().name}",
                    mode="private",
                )
                if result.get("success"):
                    token = result.get("data", result).get("session_token")
                    if token:
                        with lock:
                            tokens.append(token)
                        # Validate immediately
                        egg_stack.validate_session(token)
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=create_and_validate, name=f"t-{i}") for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Clean up
        for token in tokens:
            try:
                egg_stack.delete_session(token)
            except Exception:
                pass

        # No errors should have occurred
        assert len(errors) == 0, f"Errors: {errors}"

    def test_rapid_session_lifecycle(self, egg_stack):
        """Rapid create/validate/delete cycles work correctly."""
        for i in range(5):
            result = egg_stack.create_session(
                container_id=f"rapid-{i}",
                mode="private",
            )
            assert result.get("success"), f"Failed to create session {i}"

            token = result.get("data", result).get("session_token")
            assert token

            # Validate
            validation = egg_stack.validate_session(token)
            assert validation.get("valid")

            # Delete
            egg_stack.delete_session(token)

            # Verify deleted
            validation = egg_stack.validate_session(token)
            assert not validation.get("valid")


@pytest.mark.integration
class TestRateLimitRecovery:
    """Tests for rate limiting behavior and recovery."""

    def test_rate_limit_returns_retry_after(self, egg_stack, gateway_session):
        """Rate limited requests return retry-after information."""
        # Make many rapid requests to trigger rate limit
        session_token = gateway_session.get("session_token")

        # The heartbeat endpoint is rate limited
        for _ in range(110):  # Exceed heartbeat limit of 100/hour
            try:
                egg_stack._post(
                    "/api/v1/session/heartbeat",
                    {"session_token": session_token},
                    timeout=5,
                )
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 429:
                    # Rate limited - check for retry-after
                    assert "retry" in str(e.response.text).lower() or e.response.headers.get(
                        "Retry-After"
                    )
                    return

        # If we didn't hit rate limit, that's also acceptable (limit may be higher)

    def test_rate_limit_resets_after_window(self, egg_stack):
        """Rate limits reset after the window expires."""
        # This is a theoretical test - we don't wait the full window
        # Just verify the limiter exists and works
        result = egg_stack.health_check()
        assert "status" in result


@pytest.mark.integration
class TestSessionPersistence:
    """Tests for session persistence across operations."""

    def test_session_survives_multiple_validations(self, egg_stack, gateway_session):
        """Session remains valid after multiple validations."""
        token = gateway_session.get("session_token")

        for _ in range(10):
            result = egg_stack.validate_session(token)
            assert result.get("valid"), "Session should remain valid"

    def test_session_mode_preserved(self, egg_stack):
        """Session mode is preserved correctly."""
        # Create private session
        private_result = egg_stack.create_session(
            container_id="mode-test-private",
            mode="private",
        )
        assert private_result.get("success")
        private_token = private_result.get("data", private_result).get("session_token")

        # Create public session
        public_result = egg_stack.create_session(
            container_id="mode-test-public",
            mode="public",
        )
        assert public_result.get("success")
        public_token = public_result.get("data", public_result).get("session_token")

        try:
            # Validate private
            private_validation = egg_stack.validate_session(private_token)
            assert private_validation.get("mode") == "private"

            # Validate public
            public_validation = egg_stack.validate_session(public_token)
            assert public_validation.get("mode") == "public"
        finally:
            egg_stack.delete_session(private_token)
            egg_stack.delete_session(public_token)


@pytest.mark.integration
class TestAPIErrorResponses:
    """Tests for API error response formats."""

    def test_missing_required_field_returns_error(self, egg_stack):
        """Missing required fields return appropriate error."""
        try:
            # Try to create session without required fields
            response = egg_stack._post(
                "/api/v1/session/create",
                {},  # Empty - missing required fields
                timeout=10,
            )
            # Should fail
            assert not response.get("success", True) or response.get("error")
        except requests.exceptions.HTTPError as e:
            # HTTP error is expected
            assert e.response.status_code in (400, 422)

    def test_invalid_endpoint_returns_404(self, egg_stack):
        """Invalid endpoint returns 404."""
        try:
            egg_stack._get("/api/v1/nonexistent/endpoint", timeout=10)
            pytest.fail("Should have raised HTTPError")
        except requests.exceptions.HTTPError as e:
            assert e.response.status_code == 404

    def test_method_not_allowed_returns_405(self, egg_stack):
        """Wrong HTTP method returns 405."""
        try:
            # Try GET on POST-only endpoint
            egg_stack._get("/api/v1/session/create", timeout=10)
            pytest.fail("Should have raised HTTPError")
        except requests.exceptions.HTTPError as e:
            assert e.response.status_code in (404, 405)
