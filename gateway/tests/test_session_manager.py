"""
Tests for session_manager module.

Tests the thread-safe session storage, validation, and persistence.
"""

import hashlib
import threading
from datetime import UTC, datetime, timedelta

import pytest

# Import from conftest-loaded module
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
