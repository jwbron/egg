"""
Tests for error path coverage in gateway modules.

Phase 4: Comprehensive Coverage - Error Path Testing
Tests all exception handlers to ensure proper error handling and graceful degradation.
"""

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

# Import from conftest-loaded modules
from rate_limiter import SlidingWindowRateLimiter
from session_manager import (
    SessionManager,
    validate_session_for_request,
)


class TestSessionManagerDiskIOErrors:
    """Tests for session manager disk I/O error handling."""

    def test_load_handles_json_decode_error(self, tmp_path):
        """Manager handles corrupted JSON gracefully."""
        persist_path = tmp_path / "sessions.json"
        persist_path.write_text("{ this is not valid json }")

        # Should not raise - starts fresh with empty sessions
        manager = SessionManager(persistence_file=persist_path)
        assert manager.list_sessions() == []

    def test_load_handles_empty_file(self, tmp_path):
        """Manager handles empty file gracefully."""
        persist_path = tmp_path / "sessions.json"
        persist_path.write_text("")

        manager = SessionManager(persistence_file=persist_path)
        assert manager.list_sessions() == []

    def test_load_handles_partial_json(self, tmp_path):
        """Manager handles truncated/partial JSON gracefully."""
        persist_path = tmp_path / "sessions.json"
        persist_path.write_text('{"version": 1, "sessions": [{"incomplete":')

        manager = SessionManager(persistence_file=persist_path)
        assert manager.list_sessions() == []

    def test_load_handles_oserror(self, tmp_path):
        """Manager handles OS errors during load gracefully."""
        persist_path = tmp_path / "sessions.json"

        # Create a directory where a file is expected (causes OSError on read)
        persist_path.mkdir()

        manager = SessionManager(persistence_file=persist_path)
        assert manager.list_sessions() == []

    def test_load_handles_permission_denied(self, tmp_path):
        """Manager handles permission denied on load."""
        persist_path = tmp_path / "sessions.json"
        persist_path.write_text('{"version": 1, "sessions": []}')

        # Make file unreadable
        persist_path.chmod(0o000)
        try:
            manager = SessionManager(persistence_file=persist_path)
            # Should start fresh, not raise
            assert manager.list_sessions() == []
        finally:
            # Restore permissions for cleanup
            persist_path.chmod(0o644)

    def test_save_handles_permission_denied(self, tmp_path):
        """Manager handles permission denied on save.

        Note: The current implementation propagates PermissionError from
        the cleanup path (temp_file.exists() check). This test documents
        that behavior. Future improvement could catch this in _save_to_disk.
        """
        persist_path = tmp_path / "subdir" / "sessions.json"
        persist_path.parent.mkdir()

        manager = SessionManager(persistence_file=persist_path)

        # Make directory unwritable
        persist_path.parent.chmod(0o444)
        try:
            # Current behavior: raises PermissionError from cleanup path
            # This documents the current behavior - improvement would be
            # to handle this gracefully in _save_to_disk
            with pytest.raises(PermissionError):
                manager.register_session(
                    container_id="test-container",
                    container_ip="172.18.0.5",
                    mode="private",
                )
        finally:
            persist_path.parent.chmod(0o755)

    def test_save_handles_disk_full_scenario(self, tmp_path, monkeypatch):
        """Manager handles disk full errors during save."""
        persist_path = tmp_path / "sessions.json"
        manager = SessionManager(persistence_file=persist_path)

        # Mock open to raise OSError simulating disk full
        original_open = open

        def mock_open_error(*args, **kwargs):
            if "sessions.json.tmp" in str(args[0]):
                raise OSError(28, "No space left on device")
            return original_open(*args, **kwargs)

        with patch("builtins.open", side_effect=mock_open_error):
            # Should not raise
            token, _ = manager.register_session(
                container_id="test",
                container_ip="127.0.0.1",
                mode="private",
            )
            # Session still in memory
            assert manager.validate_session(token).valid

    def test_save_cleans_up_temp_file_on_failure(self, tmp_path, monkeypatch):
        """Temp file is cleaned up when save fails."""
        persist_path = tmp_path / "sessions.json"
        manager = SessionManager(persistence_file=persist_path)

        # First save succeeds
        manager.register_session(
            container_id="first",
            container_ip="127.0.0.1",
            mode="private",
        )
        assert persist_path.exists()

        # Track if cleanup was attempted
        cleanup_called = []
        original_unlink = Path.unlink

        def tracking_unlink(self, *args, **kwargs):
            if ".tmp" in str(self):
                cleanup_called.append(str(self))
            return original_unlink(self, *args, **kwargs)

        # Mock chmod to fail during save
        def mock_chmod_error(*args, **kwargs):
            raise OSError(1, "Operation not permitted")

        with patch("os.chmod", side_effect=mock_chmod_error):
            with patch.object(Path, "unlink", tracking_unlink):
                manager.register_session(
                    container_id="second",
                    container_ip="127.0.0.2",
                    mode="private",
                )

        # Cleanup should have been attempted
        assert any(".tmp" in path for path in cleanup_called)


class TestSessionManagerMalformedData:
    """Tests for handling malformed session data during load."""

    def test_load_skips_session_missing_hash(self, tmp_path):
        """Sessions missing token hash are skipped."""
        persist_path = tmp_path / "sessions.json"
        now = datetime.now(UTC)

        data = {
            "version": 1,
            "sessions": [
                {
                    # Missing session_token_hash
                    "container_id": "test",
                    "container_ip": "127.0.0.1",
                    "mode": "private",
                    "created_at": now.isoformat(),
                    "last_seen": now.isoformat(),
                    "expires_at": (now + timedelta(hours=24)).isoformat(),
                }
            ],
        }
        persist_path.write_text(json.dumps(data))

        manager = SessionManager(persistence_file=persist_path)
        assert manager.list_sessions() == []

    def test_load_skips_session_invalid_datetime(self, tmp_path):
        """Sessions with invalid datetime are skipped."""
        persist_path = tmp_path / "sessions.json"

        data = {
            "version": 1,
            "sessions": [
                {
                    "session_token_hash": "abc123",
                    "container_id": "test",
                    "container_ip": "127.0.0.1",
                    "mode": "private",
                    "created_at": "not-a-datetime",
                    "last_seen": "also-not-valid",
                    "expires_at": "nope",
                }
            ],
        }
        persist_path.write_text(json.dumps(data))

        manager = SessionManager(persistence_file=persist_path)
        assert manager.list_sessions() == []

    def test_load_skips_session_invalid_mode(self, tmp_path):
        """Sessions with invalid mode are loaded but may fail validation later."""
        persist_path = tmp_path / "sessions.json"
        now = datetime.now(UTC)

        data = {
            "version": 1,
            "sessions": [
                {
                    "session_token_hash": "abc123",
                    "container_id": "test",
                    "container_ip": "127.0.0.1",
                    "mode": "invalid_mode",  # Invalid mode
                    "created_at": now.isoformat(),
                    "last_seen": now.isoformat(),
                    "expires_at": (now + timedelta(hours=24)).isoformat(),
                }
            ],
        }
        persist_path.write_text(json.dumps(data))

        # Should load (mode validation is at application level, not storage)
        manager = SessionManager(persistence_file=persist_path)
        # Session loaded but with invalid mode
        session = manager.get_session_by_container("test")
        assert session is not None
        assert session.mode == "invalid_mode"

    def test_load_handles_missing_sessions_key(self, tmp_path):
        """Manager handles JSON without sessions key."""
        persist_path = tmp_path / "sessions.json"
        data = {"version": 1}  # No sessions key
        persist_path.write_text(json.dumps(data))

        manager = SessionManager(persistence_file=persist_path)
        assert manager.list_sessions() == []

    def test_load_handles_null_sessions_value(self, tmp_path):
        """Manager handles null sessions value.

        Note: The current implementation uses data.get("sessions", [])
        which returns None when sessions is explicitly None, causing
        a TypeError in the for loop. This test documents that behavior.
        """
        persist_path = tmp_path / "sessions.json"
        data = {"version": 1, "sessions": None}
        persist_path.write_text(json.dumps(data))

        # Current behavior: raises TypeError because None is not iterable
        # data.get("sessions", []) returns None (not []) when key exists with None value
        with pytest.raises(TypeError):
            SessionManager(persistence_file=persist_path)


class TestValidateSessionForRequest:
    """Tests for the validate_session_for_request convenience function."""

    def test_empty_token_returns_error(self):
        """Empty token returns invalid result."""
        result = validate_session_for_request(None)
        assert not result.valid
        assert "required" in result.error.lower()

    def test_empty_string_token_returns_error(self):
        """Empty string token returns invalid result."""
        result = validate_session_for_request("")
        assert not result.valid
        assert "required" in result.error.lower()


class TestRateLimiterBoundaryConditions:
    """Tests for rate limiter edge cases and boundary conditions."""

    def test_retry_after_calculation_at_boundary(self):
        """Retry-after is correctly calculated at window boundary."""
        limiter = SlidingWindowRateLimiter(
            max_requests=3,
            window_seconds=60,
            name="boundary_test",
        )

        # Use up the limit
        for _ in range(3):
            limiter.is_allowed("key")

        # Next request should be denied with valid retry_after
        result = limiter.is_allowed("key")
        assert not result.allowed
        assert result.retry_after_seconds is not None
        assert 1 <= result.retry_after_seconds <= 61

    def test_retry_after_never_zero(self):
        """Retry-after is never less than 1 second."""
        limiter = SlidingWindowRateLimiter(
            max_requests=1,
            window_seconds=1,
            name="min_retry_test",
        )

        limiter.is_allowed("key")
        result = limiter.is_allowed("key")

        assert not result.allowed
        assert result.retry_after_seconds >= 1

    def test_check_only_rate_limit_exceeded(self):
        """check_only returns correct info when rate limit exceeded."""
        limiter = SlidingWindowRateLimiter(
            max_requests=2,
            window_seconds=60,
            name="check_only_exceeded",
        )

        # Use up the limit
        limiter.is_allowed("key")
        limiter.is_allowed("key")

        # check_only should also show exceeded
        result = limiter.check_only("key")
        assert not result.allowed
        assert result.remaining == 0
        assert result.retry_after_seconds is not None

    def test_empty_requests_list_edge_case(self):
        """Handles edge case where requests list could be empty after pruning."""
        limiter = SlidingWindowRateLimiter(
            max_requests=2,
            window_seconds=60,
            name="empty_list_test",
        )

        # First request should work
        result = limiter.is_allowed("key")
        assert result.allowed
        assert result.remaining == 1

    def test_stats_with_no_requests(self):
        """Stats work correctly when no requests have been made."""
        limiter = SlidingWindowRateLimiter(
            max_requests=10,
            window_seconds=60,
            name="empty_stats",
        )

        stats = limiter.get_stats()
        assert stats["active_keys"] == 0
        assert stats["total_active_requests"] == 0

    def test_reset_nonexistent_key(self):
        """Resetting a non-existent key doesn't raise."""
        limiter = SlidingWindowRateLimiter(
            max_requests=5,
            window_seconds=60,
            name="reset_test",
        )

        # Should not raise
        limiter.reset("never-used-key")

    def test_reset_all_empty(self):
        """reset_all on empty limiter returns 0."""
        limiter = SlidingWindowRateLimiter(
            max_requests=5,
            window_seconds=60,
            name="empty_reset_all",
        )

        count = limiter.reset_all()
        assert count == 0


class TestSessionValidationEdgeCases:
    """Tests for session validation error paths."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create a session manager with temporary persistence."""
        return SessionManager(persistence_file=tmp_path / "sessions.json")

    def test_validate_with_very_long_token(self, manager):
        """Validation handles very long tokens gracefully."""
        # 10KB token
        long_token = "x" * 10240
        result = manager.validate_session(long_token)
        assert not result.valid
        assert "invalid" in result.error.lower() or "expired" in result.error.lower()

    def test_validate_with_null_bytes(self, manager):
        """Validation handles tokens with null bytes."""
        token_with_nulls = "token\x00with\x00nulls"
        result = manager.validate_session(token_with_nulls)
        assert not result.valid

    def test_validate_with_unicode(self, manager):
        """Validation handles unicode tokens."""
        unicode_token = "tökën_with_émojis_🔑"
        result = manager.validate_session(unicode_token)
        assert not result.valid

    def test_delete_session_by_container_concurrent(self, manager):
        """Delete by container handles race conditions."""
        token1, _ = manager.register_session(
            container_id="test-container",
            container_ip="127.0.0.1",
            mode="private",
        )

        results = []

        def delete_by_container():
            results.append(manager.delete_session_by_container("test-container"))

        # Try to delete from multiple threads
        threads = [threading.Thread(target=delete_by_container) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly one should succeed
        assert results.count(True) == 1
        assert results.count(False) == 9


class TestSessionExpiryEdgeCases:
    """Tests for session expiry edge cases."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create session manager."""
        return SessionManager(persistence_file=tmp_path / "sessions.json")

    def test_session_expires_exactly_at_boundary(self, manager):
        """Session expires exactly at expires_at timestamp."""
        token, session = manager.register_session(
            container_id="test",
            container_ip="127.0.0.1",
            mode="private",
        )

        # Set expiry to exactly now (edge case)
        session.expires_at = datetime.now(UTC)

        # Should be expired
        assert session.is_expired()

    def test_prune_with_no_expired_sessions(self, manager):
        """Prune returns 0 when no sessions are expired."""
        manager.register_session(
            container_id="active",
            container_ip="127.0.0.1",
            mode="private",
        )

        pruned = manager.prune_expired_sessions()
        assert pruned == 0

    def test_prune_all_expired(self, manager):
        """Prune correctly handles all sessions being expired."""
        for i in range(5):
            token, session = manager.register_session(
                container_id=f"container-{i}",
                container_ip=f"127.0.0.{i}",
                mode="private",
            )
            session.expires_at = datetime.now(UTC) - timedelta(seconds=1)

        pruned = manager.prune_expired_sessions()
        assert pruned == 5
        assert manager.list_sessions() == []


class TestGetSessionManagerSingleton:
    """Tests for the global session manager singleton."""

    def test_get_session_manager_thread_safe(self):
        """get_session_manager is thread-safe."""
        import session_manager

        # Reset the global
        session_manager._session_manager = None

        managers = []
        errors = []

        def get_manager():
            try:
                m = session_manager.get_session_manager()
                managers.append(m)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=get_manager) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(managers) == 20
        # All should be the same instance
        assert all(m is managers[0] for m in managers)
