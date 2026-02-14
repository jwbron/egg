"""
Tests for session_manager module.

Tests the thread-safe session storage, validation, and persistence.
"""

import hashlib
import threading
from datetime import UTC, datetime, timedelta

import pytest

# Import from conftest-loaded module
import session_manager as session_manager_module
from session_manager import (
    Session,
    SessionManager,
    SessionValidationResult,
    _hash_token,
    get_session_manager,
)


class TestSession:
    """Tests for Session dataclass."""

    def test_session_creation(self):
        """Test basic session creation."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
        )
        assert session.container_id == "test-container"
        assert session.container_ip == "172.18.0.5"
        assert session.mode == "private"
        assert session.session_token == "test-token"
        assert session.session_token_hash == hashlib.sha256(b"test-token").hexdigest()
        assert not session.is_expired()

    def test_session_expiry(self):
        """Test session expiration."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now - timedelta(seconds=1),  # Already expired
        )
        assert session.is_expired()

    def test_session_extend_ttl(self):
        """Test session TTL extension."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=1),
        )
        original_expires = session.expires_at
        session.extend_ttl(hours=5)
        assert session.expires_at > original_expires

    def test_session_to_dict_for_persistence(self):
        """Test session serialization."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
        )
        d = session.to_dict_for_persistence()
        assert d["session_token_hash"] == session.session_token_hash
        assert d["container_id"] == "test-container"
        assert d["container_ip"] == "172.18.0.5"
        assert d["mode"] == "private"
        assert "session_token" not in d  # Token should NOT be serialized

    def test_session_from_persistence(self):
        """Test session deserialization."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
        )
        d = session.to_dict_for_persistence()
        restored = Session.from_persistence(d)
        assert restored.session_token_hash == session.session_token_hash
        assert restored.container_id == session.container_id
        assert restored.container_ip == session.container_ip
        assert restored.mode == session.mode
        assert restored.session_token is None  # Token not restored from disk


class TestSessionValidationResult:
    """Tests for SessionValidationResult dataclass."""

    def test_valid_result(self):
        """Test valid session result."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
        )
        result = SessionValidationResult(valid=True, session=session)
        assert result.valid is True
        assert result.session is session
        assert result.error is None

    def test_invalid_result(self):
        """Test invalid session result."""
        result = SessionValidationResult(valid=False, error="Invalid token")
        assert result.valid is False
        assert result.session is None
        assert result.error == "Invalid token"

    def test_to_dict(self):
        """Test result serialization."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
        )
        result = SessionValidationResult(valid=True, session=session)
        d = result.to_dict()
        assert d["valid"] is True
        assert d["mode"] == "private"
        assert d["container_id"] == "test-container"


class TestSessionManager:
    """Tests for SessionManager class."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create a session manager with a temporary persistence file."""
        return SessionManager(persistence_file=tmp_path / "sessions.json")

    def test_register_session(self, manager):
        """Test session registration."""
        token, session = manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
        )
        assert token is not None
        assert len(token) > 32  # Should be a substantial token
        assert session.container_id == "test-container"
        assert session.mode == "private"

    def test_validate_valid_session(self, manager):
        """Test validating a valid session."""
        token, _session = manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
        )
        result = manager.validate_session(token, source_ip="172.18.0.5")
        assert result.valid is True
        assert result.session.container_id == "test-container"

    def test_validate_invalid_token(self, manager):
        """Test validating with invalid token."""
        result = manager.validate_session("invalid-token")
        assert result.valid is False
        assert "invalid" in result.error.lower() or "expired" in result.error.lower()

    def test_validate_expired_session(self, manager):
        """Test validating an expired session."""
        token, session = manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
        )
        # Manually expire the session
        session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        result = manager.validate_session(token)
        assert result.valid is False
        assert "expired" in result.error.lower()

    def test_validate_ip_mismatch(self, manager):
        """Test IP verification rejects mismatched IP."""
        token, _session = manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
        )
        result = manager.validate_session(token, source_ip="172.18.0.99")
        assert result.valid is False
        assert "ip" in result.error.lower() or "binding" in result.error.lower()

    def test_validate_without_ip_check(self, manager):
        """Test validation without IP verification."""
        token, _session = manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
        )
        result = manager.validate_session(token, source_ip=None)
        assert result.valid is True

    def test_delete_session(self, manager):
        """Test session deletion."""
        token, _session = manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
        )
        assert manager.delete_session(token) is True
        result = manager.validate_session(token)
        assert result.valid is False

    def test_delete_nonexistent_session(self, manager):
        """Test deleting a non-existent session."""
        assert manager.delete_session("nonexistent-token") is False

    def test_get_session_by_container(self, manager):
        """Test finding session by container ID."""
        _token, session = manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
        )
        found = manager.get_session_by_container("test-container")
        assert found is not None
        assert found.session_token_hash == session.session_token_hash

    def test_get_nonexistent_container(self, manager):
        """Test finding non-existent container."""
        found = manager.get_session_by_container("nonexistent")
        assert found is None

    def test_get_session_by_ip(self, manager):
        """Test finding session by container IP address."""
        _token, session = manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
        )
        found = manager.get_session_by_ip("172.18.0.5")
        assert found is not None
        assert found.session_token_hash == session.session_token_hash
        assert found.mode == "private"

    def test_get_session_by_ip_nonexistent(self, manager):
        """Test finding session by non-existent IP."""
        found = manager.get_session_by_ip("192.168.1.100")
        assert found is None

    def test_get_session_by_ip_expired(self, manager):
        """Test that expired sessions are not returned by IP lookup."""
        _token, session = manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
        )
        # Manually expire the session
        session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        found = manager.get_session_by_ip("172.18.0.5")
        assert found is None

    def test_prune_expired_sessions(self, manager):
        """Test pruning expired sessions."""
        # Create sessions and expire them
        for i in range(5):
            token, session = manager.register_session(
                container_id=f"expired-{i}",
                container_ip="172.18.0.5",
                mode="private",
            )
            session.expires_at = datetime.now(UTC) - timedelta(seconds=1)

        # Create valid session
        token, _ = manager.register_session(
            container_id="valid",
            container_ip="172.18.0.5",
            mode="private",
        )
        pruned = manager.prune_expired_sessions()
        assert pruned == 5
        # Valid session should still work
        result = manager.validate_session(token)
        assert result.valid is True

    def test_list_sessions(self, manager):
        """Test listing sessions."""
        for i in range(3):
            manager.register_session(
                container_id=f"container-{i}",
                container_ip=f"172.18.0.{i}",
                mode="private",
            )
        sessions = manager.list_sessions()
        assert len(sessions) == 3

    def test_clear_all(self, manager):
        """Test clearing all sessions."""
        for i in range(3):
            manager.register_session(
                container_id=f"container-{i}",
                container_ip=f"172.18.0.{i}",
                mode="private",
            )
        count = manager.clear_all()
        assert count == 3
        assert manager.list_sessions() == []


class TestSessionManagerPersistence:
    """Tests for session persistence."""

    def test_save_and_load(self, tmp_path):
        """Test session persistence to disk."""
        persist_path = tmp_path / "sessions.json"

        # Create manager and register sessions
        manager1 = SessionManager(persistence_file=persist_path)
        _token1, _ = manager1.register_session(
            container_id="container-1",
            container_ip="172.18.0.5",
            mode="private",
        )
        _token2, _ = manager1.register_session(
            container_id="container-2",
            container_ip="172.18.0.6",
            mode="public",
        )

        # Create new manager (simulating gateway restart)
        manager2 = SessionManager(persistence_file=persist_path)

        # Sessions should be loaded
        sessions = manager2.list_sessions()
        assert len(sessions) == 2

        # Validate sessions exist by container ID
        session1 = manager2.get_session_by_container("container-1")
        session2 = manager2.get_session_by_container("container-2")
        assert session1 is not None
        assert session2 is not None
        assert session1.mode == "private"
        assert session2.mode == "public"

    def test_atomic_persistence(self, tmp_path):
        """Test that persistence is atomic (write to temp then rename)."""
        persist_path = tmp_path / "sessions.json"
        manager = SessionManager(persistence_file=persist_path)
        manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
        )
        # File should exist and be valid JSON
        assert persist_path.exists()
        import json

        with open(persist_path) as f:
            data = json.load(f)
        assert "sessions" in data

    def test_validate_after_restart(self, tmp_path):
        """Test that tokens can be validated after a simulated gateway restart.

        After restart, sessions are loaded from disk without raw tokens.
        Validation via hash computation should succeed and repopulate the
        fast lookup cache for subsequent O(1) lookups.
        """
        persist_path = tmp_path / "sessions.json"

        # Register sessions with the original manager
        manager1 = SessionManager(persistence_file=persist_path)
        token1, _ = manager1.register_session(
            container_id="container-1",
            container_ip="172.18.0.5",
            mode="private",
        )
        token2, _ = manager1.register_session(
            container_id="container-2",
            container_ip="172.18.0.6",
            mode="public",
        )

        # Simulate gateway restart: new manager loads from same persistence file
        manager2 = SessionManager(persistence_file=persist_path)

        # Sessions loaded from disk have session_token=None and empty fast cache
        session1 = manager2.get_session_by_container("container-1")
        assert session1 is not None
        assert session1.session_token is None  # Not persisted

        # Validate with original raw tokens via hash computation
        result1 = manager2.validate_session(token1, source_ip="172.18.0.5")
        assert result1.valid is True
        assert result1.session.container_id == "container-1"
        assert result1.session.mode == "private"

        result2 = manager2.validate_session(token2, source_ip="172.18.0.6")
        assert result2.valid is True
        assert result2.session.container_id == "container-2"
        assert result2.session.mode == "public"

        # After successful validation, fast cache should be repopulated
        assert result1.session.session_token == token1
        assert result2.session.session_token == token2
        assert manager2._token_to_hash.get(token1) == _hash_token(token1)
        assert manager2._token_to_hash.get(token2) == _hash_token(token2)

        # Subsequent validations should use the fast cache (O(1) lookup)
        result1_again = manager2.validate_session(token1)
        assert result1_again.valid is True
        assert result1_again.session.container_id == "container-1"


class TestSessionManagerThreadSafety:
    """Tests for thread safety."""

    def test_concurrent_registration(self, tmp_path):
        """Test concurrent session registration."""
        manager = SessionManager(persistence_file=tmp_path / "sessions.json")
        tokens = []
        errors = []

        def register_session(i):
            try:
                token, _ = manager.register_session(
                    container_id=f"container-{i}",
                    container_ip=f"172.18.0.{i % 256}",
                    mode="private",
                )
                tokens.append(token)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=register_session, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(tokens) == 50
        assert len(set(tokens)) == 50  # All unique

    def test_concurrent_validation(self, tmp_path):
        """Test concurrent session validation."""
        manager = SessionManager(persistence_file=tmp_path / "sessions.json")
        token, _ = manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
        )
        results = []
        errors = []

        def validate_session():
            try:
                result = manager.validate_session(token, source_ip="172.18.0.5")
                results.append(result.valid)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=validate_session) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert all(results)  # All validations should succeed


class TestGetSessionManager:
    """Tests for global session manager singleton."""

    def test_returns_singleton(self):
        """get_session_manager should return the same instance."""
        # Reset the global (if possible)
        import session_manager

        session_manager._session_manager = None

        manager1 = get_session_manager()
        manager2 = get_session_manager()
        assert manager1 is manager2


class TestTokenSecurity:
    """Tests for session token security properties."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create a session manager with a temporary persistence file."""
        return SessionManager(persistence_file=tmp_path / "sessions.json")

    def test_token_length_minimum(self, manager):
        """Session tokens should have sufficient length for security."""
        token, _session = manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
        )
        # 32 bytes base64 encoded = ~43 characters
        assert len(token) >= 40

    def test_tokens_are_unique(self, manager):
        """Each session should have a unique token."""
        tokens = set()
        for i in range(100):
            token, _ = manager.register_session(
                container_id=f"container-{i}",
                container_ip=f"172.18.0.{i % 256}",
                mode="private",
            )
            assert token not in tokens, f"Duplicate token generated at iteration {i}"
            tokens.add(token)

    def test_token_not_persisted_raw(self, tmp_path):
        """Raw tokens should never be persisted to disk."""
        import json

        persist_path = tmp_path / "sessions.json"
        manager = SessionManager(persistence_file=persist_path)

        token, _ = manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
        )

        # Read persisted file
        with open(persist_path) as f:
            data = json.load(f)

        # Token should not appear anywhere in the persisted data
        persisted_str = json.dumps(data)
        assert token not in persisted_str

        # But hash should be present
        for session in data.get("sessions", []):
            assert "session_token_hash" in session
            assert "session_token" not in session

    def test_hash_function_produces_consistent_results(self):
        """Hash function should produce consistent results for same input."""
        token = "test-token-12345"
        hash1 = _hash_token(token)
        hash2 = _hash_token(token)
        assert hash1 == hash2

    def test_hash_function_produces_different_results_for_different_input(self):
        """Hash function should produce different results for different input."""
        hash1 = _hash_token("token-a")
        hash2 = _hash_token("token-b")
        assert hash1 != hash2


class TestPersistenceEdgeCases:
    """Tests for session persistence edge cases."""

    def test_corrupted_json_handled_gracefully(self, tmp_path):
        """Manager should handle corrupted persistence file gracefully."""
        persist_path = tmp_path / "sessions.json"

        # Write corrupted JSON
        persist_path.write_text("{ invalid json [")

        # Should not raise - just log warning and start fresh
        manager = SessionManager(persistence_file=persist_path)
        assert manager.list_sessions() == []

    def test_missing_fields_handled_gracefully(self, tmp_path):
        """Manager should handle sessions with missing fields."""
        import json

        persist_path = tmp_path / "sessions.json"

        # Write session with missing fields
        data = {
            "version": 1,
            "saved_at": "2024-01-01T00:00:00+00:00",
            "sessions": [
                {
                    "session_token_hash": "abc123",
                    # Missing other required fields
                }
            ],
        }
        persist_path.write_text(json.dumps(data))

        # Should not raise - just skip invalid session
        manager = SessionManager(persistence_file=persist_path)
        assert manager.list_sessions() == []

    def test_expired_sessions_pruned_on_load(self, tmp_path):
        """Expired sessions should be pruned when loading from disk."""
        import json
        from datetime import UTC, datetime, timedelta

        persist_path = tmp_path / "sessions.json"
        now = datetime.now(UTC)

        # Write one valid and one expired session
        data = {
            "version": 1,
            "saved_at": now.isoformat(),
            "sessions": [
                {
                    "session_token_hash": "valid_hash",
                    "container_id": "valid-container",
                    "container_ip": "172.18.0.5",
                    "mode": "private",
                    "created_at": now.isoformat(),
                    "last_seen": now.isoformat(),
                    "expires_at": (now + timedelta(hours=24)).isoformat(),
                },
                {
                    "session_token_hash": "expired_hash",
                    "container_id": "expired-container",
                    "container_ip": "172.18.0.6",
                    "mode": "private",
                    "created_at": (now - timedelta(hours=48)).isoformat(),
                    "last_seen": (now - timedelta(hours=48)).isoformat(),
                    "expires_at": (now - timedelta(hours=24)).isoformat(),  # Expired
                },
            ],
        }
        persist_path.write_text(json.dumps(data))

        manager = SessionManager(persistence_file=persist_path)
        sessions = manager.list_sessions()

        # Only the valid session should be loaded
        assert len(sessions) == 1
        assert sessions[0]["container_id"] == "valid-container"

    def test_persistence_file_permissions(self, tmp_path):
        """Persisted file should have restrictive permissions."""
        persist_path = tmp_path / "sessions.json"
        manager = SessionManager(persistence_file=persist_path)

        manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
        )

        # Check file permissions (should be 0o600 - owner read/write only)
        file_stat = persist_path.stat()
        mode = file_stat.st_mode & 0o777
        assert mode == 0o600, f"Expected permissions 0o600, got {oct(mode)}"

    def test_persistence_directory_created_if_missing(self, tmp_path):
        """Persistence directory should be created if it doesn't exist."""
        persist_path = tmp_path / "subdir" / "nested" / "sessions.json"

        manager = SessionManager(persistence_file=persist_path)
        manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
        )

        assert persist_path.exists()


class TestDeleteByContainer:
    """Tests for delete_session_by_container method."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create a session manager with a temporary persistence file."""
        return SessionManager(persistence_file=tmp_path / "sessions.json")

    def test_delete_existing_container(self, manager):
        """Delete session by container ID when it exists."""
        _token, session = manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
        )

        result = manager.delete_session_by_container("test-container")
        assert result is True

        # Session should no longer be findable
        assert manager.get_session_by_container("test-container") is None

    def test_delete_nonexistent_container(self, manager):
        """Delete returns False for non-existent container."""
        result = manager.delete_session_by_container("nonexistent")
        assert result is False

    def test_delete_clears_token_cache(self, manager):
        """Deleting by container should also clear the token lookup cache."""
        token, _session = manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
        )

        # Token should work before delete
        result = manager.validate_session(token)
        assert result.valid

        # Delete by container
        manager.delete_session_by_container("test-container")

        # Token should no longer work
        result = manager.validate_session(token)
        assert not result.valid


class TestSessionModes:
    """Tests for session mode handling."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create a session manager with a temporary persistence file."""
        return SessionManager(persistence_file=tmp_path / "sessions.json")

    def test_private_mode(self, manager):
        """Private mode sessions are created correctly."""
        token, session = manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
        )
        assert session.mode == "private"

    def test_public_mode(self, manager):
        """Public mode sessions are created correctly."""
        token, session = manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="public",
        )
        assert session.mode == "public"

    def test_mode_persists(self, tmp_path):
        """Session mode is preserved after reload."""
        persist_path = tmp_path / "sessions.json"

        manager1 = SessionManager(persistence_file=persist_path)
        _token, _ = manager1.register_session(
            container_id="container-1",
            container_ip="172.18.0.5",
            mode="private",
        )
        _token, _ = manager1.register_session(
            container_id="container-2",
            container_ip="172.18.0.6",
            mode="public",
        )

        # Reload
        manager2 = SessionManager(persistence_file=persist_path)

        session1 = manager2.get_session_by_container("container-1")
        session2 = manager2.get_session_by_container("container-2")

        assert session1 is not None and session1.mode == "private"
        assert session2 is not None and session2.mode == "public"


class TestFastLookupCache:
    """Tests for the fast token lookup cache behavior."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create a session manager with a temporary persistence file."""
        return SessionManager(persistence_file=tmp_path / "sessions.json")

    def test_fast_cache_populated_on_register(self, manager):
        """Fast lookup cache is populated when session is registered."""
        token, _ = manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
        )

        # Token should be in fast cache
        assert token in manager._token_to_hash

    def test_fast_cache_used_for_validation(self, manager):
        """Validation uses fast lookup cache when available."""
        token, _ = manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
        )

        # Clear the hash from fast cache to test fallback
        manager._token_to_hash.pop(token)

        # Validation should still work (via hash computation)
        result = manager.validate_session(token)
        assert result.valid

        # Fast cache should be repopulated
        assert token in manager._token_to_hash

    def test_fast_cache_repopulated_after_restart(self, tmp_path):
        """Fast cache is repopulated when validating after restart."""
        persist_path = tmp_path / "sessions.json"

        # Create session
        manager1 = SessionManager(persistence_file=persist_path)
        token, _ = manager1.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
        )

        # Simulate restart
        manager2 = SessionManager(persistence_file=persist_path)

        # Fast cache should be empty (tokens not persisted)
        assert token not in manager2._token_to_hash

        # Validate - should work via hash computation
        result = manager2.validate_session(token)
        assert result.valid

        # Fast cache should now be populated
        assert token in manager2._token_to_hash

    def test_fast_cache_cleared_on_delete(self, manager):
        """Fast lookup cache is cleared when session is deleted."""
        token, _ = manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
        )

        # Token in cache
        assert token in manager._token_to_hash

        # Delete session
        manager.delete_session(token)

        # Token should be removed from cache
        assert token not in manager._token_to_hash


class TestValidationEdgeCases:
    """Tests for session validation edge cases."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create a session manager with a temporary persistence file."""
        return SessionManager(persistence_file=tmp_path / "sessions.json")

    def test_empty_token_rejected(self, manager):
        """Empty token is rejected."""
        result = manager.validate_session("")
        assert not result.valid

    def test_whitespace_token_rejected(self, manager):
        """Whitespace-only token is rejected."""
        result = manager.validate_session("   ")
        assert not result.valid

    def test_similar_tokens_distinguished(self, manager):
        """Similar but different tokens are correctly distinguished."""
        token1, _ = manager.register_session(
            container_id="container-1",
            container_ip="172.18.0.5",
            mode="private",
        )
        token2, _ = manager.register_session(
            container_id="container-2",
            container_ip="172.18.0.6",
            mode="private",
        )

        # Each token should only validate its own session
        result1 = manager.validate_session(token1)
        result2 = manager.validate_session(token2)

        assert result1.valid and result1.session.container_id == "container-1"
        assert result2.valid and result2.session.container_id == "container-2"

    def test_ttl_extended_on_validation(self, manager):
        """Session TTL is extended on successful validation (heartbeat)."""
        token, session = manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
        )

        # Artificially age the session by setting expires_at to 1 hour ago
        # This ensures a measurable difference after TTL extension
        aged_expires = session.expires_at - timedelta(hours=1)
        session.expires_at = aged_expires
        session.last_seen = session.last_seen - timedelta(hours=1)

        # Validate - this should extend the TTL
        result = manager.validate_session(token)
        assert result.valid

        # Expiration should be extended beyond the aged value
        assert result.session.expires_at > aged_expires


class TestSessionPhase:
    """Tests for session phase field and update_phase method."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create a session manager with a temporary persistence file."""
        return SessionManager(persistence_file=tmp_path / "sessions.json")

    def test_register_session_with_phase(self, manager):
        """Session can be registered with a phase."""
        token, session = manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            phase="implement",
        )
        assert session.phase == "implement"

    def test_register_session_without_phase(self, manager):
        """Session registered without phase defaults to None."""
        token, session = manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
        )
        assert session.phase is None

    def test_phase_persistence(self, tmp_path):
        """Phase survives save/load cycle."""
        persist_path = tmp_path / "sessions.json"

        # Create session with phase
        manager1 = SessionManager(persistence_file=persist_path)
        token, session = manager1.register_session(
            container_id="container-1",
            container_ip="172.18.0.5",
            mode="private",
            phase="implement",
        )

        # Simulate restart - create new manager loading from disk
        manager2 = SessionManager(persistence_file=persist_path)

        # Session should be loaded with phase intact
        session_loaded = manager2.get_session_by_container("container-1")
        assert session_loaded is not None
        assert session_loaded.phase == "implement"

    def test_update_phase_success(self, manager):
        """update_phase updates the session phase."""
        token, session = manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            phase="implement",
        )

        # Update phase
        result = manager.update_phase(token, "pr")
        assert result is True

        # Verify phase was updated
        session = manager.get_session(token)
        assert session is not None
        assert session.phase == "pr"

    def test_update_phase_invalid_token(self, manager):
        """update_phase returns False for invalid token."""
        result = manager.update_phase("invalid-token", "pr")
        assert result is False

    def test_update_phase_expired_session(self, manager):
        """update_phase returns False for expired session."""
        token, session = manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            phase="implement",
        )

        # Manually expire the session
        session.expires_at = datetime.now(UTC) - timedelta(seconds=1)

        result = manager.update_phase(token, "pr")
        assert result is False

    def test_update_phase_persists(self, tmp_path):
        """update_phase persists the change to disk."""
        persist_path = tmp_path / "sessions.json"

        manager1 = SessionManager(persistence_file=persist_path)
        token, _ = manager1.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            phase="implement",
        )

        # Update phase
        manager1.update_phase(token, "pr")

        # Simulate restart
        manager2 = SessionManager(persistence_file=persist_path)

        # Validate with original token should work and show updated phase
        result = manager2.validate_session(token)
        assert result.valid
        assert result.session.phase == "pr"

    def test_phase_serialization(self):
        """Phase is correctly serialized/deserialized."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
            phase="implement",
        )

        # Serialize
        d = session.to_dict_for_persistence()
        assert d["phase"] == "implement"

        # Deserialize
        restored = Session.from_persistence(d)
        assert restored.phase == "implement"

    def test_phase_not_serialized_when_none(self):
        """Phase field is not included when None."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
            phase=None,
        )

        d = session.to_dict_for_persistence()
        assert "phase" not in d

    def test_from_persistence_handles_missing_phase(self):
        """from_persistence handles sessions without phase field."""
        now = datetime.now(UTC)
        data = {
            "session_token_hash": "abc123",
            "container_id": "test-container",
            "container_ip": "172.18.0.5",
            "mode": "private",
            "created_at": now.isoformat(),
            "last_seen": now.isoformat(),
            "expires_at": (now + timedelta(hours=24)).isoformat(),
            # No phase field - simulates legacy session
        }

        session = Session.from_persistence(data)
        assert session.phase is None


class TestSessionMetadataFields:
    """Tests for issue_number and pr_number fields on Session."""

    def test_session_with_issue_and_pr(self):
        """Session can be created with issue_number and pr_number."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
            issue_number=530,
            pr_number=42,
        )
        assert session.issue_number == 530
        assert session.pr_number == 42

    def test_session_default_metadata_none(self):
        """issue_number and pr_number default to None."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
        )
        assert session.issue_number is None
        assert session.pr_number is None

    def test_to_dict_includes_metadata(self):
        """to_dict_for_persistence includes issue_number and pr_number."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
            issue_number=530,
            pr_number=42,
        )
        d = session.to_dict_for_persistence()
        assert d["issue_number"] == 530
        assert d["pr_number"] == 42

    def test_to_dict_excludes_none_metadata(self):
        """to_dict_for_persistence excludes None issue_number/pr_number."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
        )
        d = session.to_dict_for_persistence()
        assert "issue_number" not in d
        assert "pr_number" not in d

    def test_roundtrip_with_metadata(self):
        """issue_number and pr_number survive serialization roundtrip."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
            issue_number=530,
            pr_number=42,
        )
        d = session.to_dict_for_persistence()
        restored = Session.from_persistence(d)

        assert restored.issue_number == 530
        assert restored.pr_number == 42

    def test_backward_compatibility_without_metadata(self):
        """from_persistence handles sessions without issue_number/pr_number."""
        now = datetime.now(UTC)
        data = {
            "session_token_hash": "abc123",
            "container_id": "test-container",
            "container_ip": "172.18.0.5",
            "mode": "private",
            "created_at": now.isoformat(),
            "last_seen": now.isoformat(),
            "expires_at": (now + timedelta(hours=24)).isoformat(),
            # No issue_number or pr_number - simulates legacy session
        }

        session = Session.from_persistence(data)
        assert session.issue_number is None
        assert session.pr_number is None

    def test_register_session_with_metadata(self, tmp_path):
        """register_session passes issue_number and pr_number."""
        manager = SessionManager(persistence_file=tmp_path / "sessions.json")
        token, session = manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            issue_number=530,
            pr_number=42,
        )
        assert session.issue_number == 530
        assert session.pr_number == 42

    def test_metadata_persists_through_restart(self, tmp_path):
        """issue_number and pr_number persist across gateway restarts."""
        persist_path = tmp_path / "sessions.json"

        manager1 = SessionManager(persistence_file=persist_path)
        token, _ = manager1.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            issue_number=530,
            pr_number=42,
        )

        # Simulate restart
        manager2 = SessionManager(persistence_file=persist_path)
        session = manager2.get_session_by_container("test-container")

        assert session is not None
        assert session.issue_number == 530
        assert session.pr_number == 42


class TestSessionPipelineId:
    """Tests for pipeline_id field on Session."""

    def test_session_with_pipeline_id(self):
        """Session can be created with pipeline_id."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
            pipeline_id="issue-42",
        )
        assert session.pipeline_id == "issue-42"

    def test_pipeline_id_defaults_none(self):
        """pipeline_id defaults to None."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
        )
        assert session.pipeline_id is None

    def test_to_dict_includes_pipeline_id(self):
        """to_dict_for_persistence includes pipeline_id when set."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
            pipeline_id="issue-42",
        )
        d = session.to_dict_for_persistence()
        assert d["pipeline_id"] == "issue-42"

    def test_to_dict_excludes_none_pipeline_id(self):
        """to_dict_for_persistence excludes pipeline_id when None."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
        )
        d = session.to_dict_for_persistence()
        assert "pipeline_id" not in d

    def test_roundtrip_with_pipeline_id(self):
        """pipeline_id survives serialization roundtrip."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
            pipeline_id="issue-42",
        )
        d = session.to_dict_for_persistence()
        restored = Session.from_persistence(d)
        assert restored.pipeline_id == "issue-42"

    def test_register_session_with_pipeline_id(self, tmp_path):
        """register_session passes pipeline_id."""
        manager = SessionManager(persistence_file=tmp_path / "sessions.json")
        token, session = manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            pipeline_id="issue-42",
        )
        assert session.pipeline_id == "issue-42"

    def test_backward_compatibility_without_pipeline_id(self):
        """from_persistence handles sessions without pipeline_id."""
        now = datetime.now(UTC)
        data = {
            "session_token_hash": "abc123",
            "container_id": "test-container",
            "container_ip": "172.18.0.5",
            "mode": "private",
            "created_at": now.isoformat(),
            "last_seen": now.isoformat(),
            "expires_at": (now + timedelta(hours=24)).isoformat(),
        }
        session = Session.from_persistence(data)
        assert session.pipeline_id is None


class TestSessionCheckpointFields:
    """Tests for Session checkpoint_repo and last_repo_path fields."""

    def test_session_defaults_checkpoint_fields_none(self):
        """checkpoint_repo and last_repo_path default to None."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
        )
        assert session.checkpoint_repo is None
        assert session.last_repo_path is None

    def test_session_with_checkpoint_fields(self):
        """Session can be created with checkpoint_repo and last_repo_path."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
            checkpoint_repo="jwbron/egg-checkpoints",
            last_repo_path="/home/egg/repos/egg",
        )
        assert session.checkpoint_repo == "jwbron/egg-checkpoints"
        assert session.last_repo_path == "/home/egg/repos/egg"

    def test_to_dict_includes_checkpoint_fields(self):
        """to_dict_for_persistence includes checkpoint fields when set."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
            checkpoint_repo="jwbron/egg-checkpoints",
            last_repo_path="/home/egg/repos/egg",
        )
        d = session.to_dict_for_persistence()
        assert d["checkpoint_repo"] == "jwbron/egg-checkpoints"
        assert d["last_repo_path"] == "/home/egg/repos/egg"

    def test_to_dict_excludes_none_checkpoint_fields(self):
        """to_dict_for_persistence excludes None checkpoint fields."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
        )
        d = session.to_dict_for_persistence()
        assert "checkpoint_repo" not in d
        assert "last_repo_path" not in d

    def test_roundtrip_with_checkpoint_fields(self):
        """checkpoint_repo and last_repo_path survive serialization roundtrip."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
            checkpoint_repo="jwbron/egg-checkpoints",
            last_repo_path="/home/egg/repos/egg",
        )
        d = session.to_dict_for_persistence()
        restored = Session.from_persistence(d)
        assert restored.checkpoint_repo == "jwbron/egg-checkpoints"
        assert restored.last_repo_path == "/home/egg/repos/egg"

    def test_backward_compatibility_without_checkpoint_fields(self):
        """from_persistence handles sessions without checkpoint fields."""
        now = datetime.now(UTC)
        data = {
            "session_token_hash": "abc123",
            "container_id": "test-container",
            "container_ip": "172.18.0.5",
            "mode": "private",
            "created_at": now.isoformat(),
            "last_seen": now.isoformat(),
            "expires_at": (now + timedelta(hours=24)).isoformat(),
        }
        session = Session.from_persistence(data)
        assert session.checkpoint_repo is None
        assert session.last_repo_path is None


class TestSessionEndCheckpointCapture:
    """Tests for session-end checkpoint capture during deletion/expiry."""

    @pytest.fixture(autouse=True)
    def clear_captured_containers(self):
        """Clear the capture dedup set before each test."""
        session_manager_module._captured_containers.clear()

    @pytest.fixture
    def manager(self, tmp_path):
        """Create a session manager with a temporary persistence file."""
        return SessionManager(persistence_file=tmp_path / "sessions.json")

    def test_delete_session_by_token_captures_checkpoint(self, manager):
        """delete_session(token) captures a session-end checkpoint."""
        token, session = manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
        )

        from unittest.mock import patch

        with patch.object(session_manager_module, "_capture_and_cleanup_session") as mock_capture:
            result = manager.delete_session(token)
            assert result is True
            mock_capture.assert_called_once()
            args = mock_capture.call_args[0]
            assert args[0].container_id == "test-container"
            assert args[1] == "completed"

    def test_delete_session_by_container_captures_checkpoint(self, manager):
        """delete_session_by_container captures a session-end checkpoint."""
        _token, session = manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
        )

        from unittest.mock import patch

        with patch.object(session_manager_module, "_capture_and_cleanup_session") as mock_capture:
            result = manager.delete_session_by_container("test-container")
            assert result is True
            mock_capture.assert_called_once()
            args = mock_capture.call_args[0]
            assert args[0].container_id == "test-container"
            assert args[1] == "completed"

    def test_prune_captures_expired_checkpoints(self, manager):
        """prune_expired_sessions captures checkpoints with EXPIRED status."""
        # Create and expire sessions
        for i in range(3):
            _token, session = manager.register_session(
                container_id=f"expired-{i}",
                container_ip="172.18.0.5",
                mode="private",
            )
            session.expires_at = datetime.now(UTC) - timedelta(seconds=1)

        from unittest.mock import patch

        with patch.object(session_manager_module, "_capture_and_cleanup_session") as mock_capture:
            pruned = manager.prune_expired_sessions()
            assert pruned == 3
            assert mock_capture.call_count == 3
            # All calls should use "expired" status
            for c in mock_capture.call_args_list:
                assert c[0][1] == "expired"

    def test_delete_by_token_not_found_skips_capture(self, manager):
        """delete_session with invalid token doesn't capture checkpoint."""
        from unittest.mock import patch

        with patch.object(session_manager_module, "_capture_and_cleanup_session") as mock_capture:
            result = manager.delete_session("nonexistent-token")
            assert result is False
            mock_capture.assert_not_called()

    def test_delete_by_container_not_found_skips_capture(self, manager):
        """delete_session_by_container with invalid container doesn't capture."""
        from unittest.mock import patch

        with patch.object(session_manager_module, "_capture_and_cleanup_session") as mock_capture:
            result = manager.delete_session_by_container("nonexistent")
            assert result is False
            mock_capture.assert_not_called()

    def test_capture_called_outside_lock(self, manager):
        """Verify that checkpoint capture doesn't hold the session lock."""
        token, session = manager.register_session(
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
        )

        # Register another session to test lock contention
        token2, _ = manager.register_session(
            container_id="other-container",
            container_ip="172.18.0.6",
            mode="private",
        )

        from unittest.mock import patch

        def capture_mock(session_obj, status):
            # While capture is running, other session operations should work
            # If the lock were held, this would deadlock
            result = manager.validate_session(token2)
            assert result.valid

        with patch.object(
            session_manager_module, "_capture_and_cleanup_session", side_effect=capture_mock
        ):
            result = manager.delete_session_by_container("test-container")
            assert result is True

    def test_capture_and_cleanup_handles_import_error(self):
        """_capture_and_cleanup_session handles ImportError gracefully."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
        )

        from unittest.mock import patch

        from session_manager import _capture_and_cleanup_session

        with patch.object(session_manager_module, "_cleanup_transcript_buffer") as mock_cleanup:
            with patch.dict("sys.modules", {"checkpoint_handler": None}):
                # Should not raise - just log and clean up
                _capture_and_cleanup_session(session, "completed")
            # Buffer cleanup should always happen
            mock_cleanup.assert_called_once_with("test-container")

    def test_capture_and_cleanup_handles_capture_failure(self):
        """_capture_and_cleanup_session cleans up buffer even on failure."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
        )

        from unittest.mock import patch

        from session_manager import _capture_and_cleanup_session

        with patch.object(session_manager_module, "_cleanup_transcript_buffer") as mock_cleanup:
            with patch(
                "checkpoint_handler.capture_session_end_checkpoint",
                side_effect=RuntimeError("capture failed"),
            ):
                # Should not raise
                _capture_and_cleanup_session(session, "completed")
            # Buffer cleanup should still happen
            mock_cleanup.assert_called_once_with("test-container")
