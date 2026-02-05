"""Tests for gateway session_manager module."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Add gateway to path for imports
gateway_path = Path(__file__).parent.parent.parent / "gateway"
if str(gateway_path) not in sys.path:
    sys.path.insert(0, str(gateway_path))

from session_manager import (
    Session,
    SessionManager,
    SessionValidationResult,
    _constant_time_compare,
    _hash_token,
)


class TestHashToken:
    """Tests for _hash_token function."""

    def test_produces_hex_string(self):
        """Hash produces hex string."""
        h = _hash_token("test-token")
        assert all(c in "0123456789abcdef" for c in h)
        assert len(h) == 64  # SHA-256 produces 64 hex chars

    def test_deterministic(self):
        """Same input produces same hash."""
        assert _hash_token("abc") == _hash_token("abc")

    def test_different_inputs(self):
        """Different inputs produce different hashes."""
        assert _hash_token("abc") != _hash_token("def")


class TestConstantTimeCompare:
    """Tests for _constant_time_compare function."""

    def test_equal_strings(self):
        """Equal strings return True."""
        assert _constant_time_compare("hello", "hello") is True

    def test_different_strings(self):
        """Different strings return False."""
        assert _constant_time_compare("hello", "world") is False

    def test_empty_strings(self):
        """Empty strings are equal."""
        assert _constant_time_compare("", "") is True

    def test_different_lengths(self):
        """Different length strings return False."""
        assert _constant_time_compare("ab", "abc") is False


class TestSession:
    """Tests for Session dataclass."""

    def _make_session(self, **kwargs):
        """Create a test session."""
        defaults = {
            "session_token": "test-token",
            "session_token_hash": _hash_token("test-token"),
            "container_id": "container-123",
            "container_ip": "10.0.0.1",
            "mode": "private",
            "created_at": datetime.now(UTC),
            "last_seen": datetime.now(UTC),
            "expires_at": datetime.now(UTC) + timedelta(hours=24),
        }
        defaults.update(kwargs)
        return Session(**defaults)

    def test_not_expired(self):
        """Session in the future is not expired."""
        session = self._make_session()
        assert session.is_expired() is False

    def test_expired(self):
        """Expired session."""
        session = self._make_session(expires_at=datetime.now(UTC) - timedelta(hours=1))
        assert session.is_expired() is True

    def test_extend_ttl(self):
        """Extending TTL updates last_seen and expires_at."""
        session = self._make_session(expires_at=datetime.now(UTC) + timedelta(hours=1))
        old_expires = session.expires_at
        session.extend_ttl(hours=48)
        assert session.expires_at > old_expires

    def test_to_dict_for_persistence(self):
        """Persistence dict excludes raw token."""
        session = self._make_session()
        d = session.to_dict_for_persistence()
        assert "session_token" not in d
        assert "session_token_hash" in d
        assert d["container_id"] == "container-123"
        assert d["container_ip"] == "10.0.0.1"
        assert d["mode"] == "private"
        assert "created_at" in d
        assert "last_seen" in d
        assert "expires_at" in d

    def test_from_persistence(self):
        """Restore session from persistence data."""
        now = datetime.now(UTC)
        data = {
            "session_token_hash": "abc123hash",
            "container_id": "container-456",
            "container_ip": "10.0.0.2",
            "mode": "public",
            "created_at": now.isoformat(),
            "last_seen": now.isoformat(),
            "expires_at": (now + timedelta(hours=24)).isoformat(),
        }
        session = Session.from_persistence(data)
        assert session.session_token is None  # Raw token not restored
        assert session.session_token_hash == "abc123hash"
        assert session.container_id == "container-456"
        assert session.mode == "public"

    def test_roundtrip_persistence(self):
        """Session survives to_dict/from_persistence roundtrip."""
        original = self._make_session()
        d = original.to_dict_for_persistence()
        restored = Session.from_persistence(d)
        assert restored.session_token_hash == original.session_token_hash
        assert restored.container_id == original.container_id
        assert restored.mode == original.mode


class TestSessionValidationResult:
    """Tests for SessionValidationResult dataclass."""

    def test_valid_result(self):
        """Valid session result."""
        session = Session(
            session_token="tok",
            session_token_hash="hash",
            container_id="c1",
            container_ip="10.0.0.1",
            mode="private",
            created_at=datetime.now(UTC),
            last_seen=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        result = SessionValidationResult(valid=True, session=session)
        d = result.to_dict()
        assert d["valid"] is True
        assert d["mode"] == "private"
        assert d["container_id"] == "c1"
        assert "error" not in d

    def test_invalid_result(self):
        """Invalid session result."""
        result = SessionValidationResult(valid=False, error="Token expired")
        d = result.to_dict()
        assert d["valid"] is False
        assert d["error"] == "Token expired"
        assert "mode" not in d

    def test_invalid_no_error(self):
        """Invalid result without error message."""
        result = SessionValidationResult(valid=False)
        d = result.to_dict()
        assert d["valid"] is False
        assert "error" not in d


class TestSessionManager:
    """Tests for SessionManager class."""

    def _make_manager(self, tmp_path):
        """Create a SessionManager with temp persistence file."""
        return SessionManager(
            persistence_file=tmp_path / "sessions.json",
            ttl_hours=24,
        )

    def test_register_session(self, tmp_path):
        """Register and retrieve a session."""
        mgr = self._make_manager(tmp_path)
        token, session = mgr.register_session("container-1", "10.0.0.1", "private")
        assert token != ""
        assert session.container_id == "container-1"
        assert session.mode == "private"

    def test_validate_valid_session(self, tmp_path):
        """Validate a registered session."""
        mgr = self._make_manager(tmp_path)
        token, _ = mgr.register_session("container-1", "10.0.0.1", "private")
        result = mgr.validate_session(token)
        assert result.valid is True
        assert result.session.container_id == "container-1"

    def test_validate_invalid_token(self, tmp_path):
        """Validate with invalid token."""
        mgr = self._make_manager(tmp_path)
        result = mgr.validate_session("bogus-token")
        assert result.valid is False

    def test_validate_ip_mismatch(self, tmp_path):
        """Validate with wrong source IP."""
        mgr = self._make_manager(tmp_path)
        token, _ = mgr.register_session("container-1", "10.0.0.1", "private")
        result = mgr.validate_session(token, source_ip="10.0.0.99")
        assert result.valid is False
        assert "binding" in result.error.lower() or "mismatch" in result.error.lower()

    def test_validate_correct_ip(self, tmp_path):
        """Validate with correct source IP."""
        mgr = self._make_manager(tmp_path)
        token, _ = mgr.register_session("container-1", "10.0.0.1", "private")
        result = mgr.validate_session(token, source_ip="10.0.0.1")
        assert result.valid is True

    def test_get_session(self, tmp_path):
        """Get session by token."""
        mgr = self._make_manager(tmp_path)
        token, _ = mgr.register_session("container-1", "10.0.0.1", "private")
        session = mgr.get_session(token)
        assert session is not None
        assert session.container_id == "container-1"

    def test_get_session_invalid(self, tmp_path):
        """Get session with invalid token returns None."""
        mgr = self._make_manager(tmp_path)
        assert mgr.get_session("bogus") is None

    def test_get_session_by_container(self, tmp_path):
        """Get session by container ID."""
        mgr = self._make_manager(tmp_path)
        mgr.register_session("container-1", "10.0.0.1", "private")
        session = mgr.get_session_by_container("container-1")
        assert session is not None
        assert session.mode == "private"

    def test_get_session_by_container_not_found(self, tmp_path):
        """Get session by non-existent container ID."""
        mgr = self._make_manager(tmp_path)
        assert mgr.get_session_by_container("nonexistent") is None

    def test_get_session_by_ip(self, tmp_path):
        """Get session by IP address."""
        mgr = self._make_manager(tmp_path)
        mgr.register_session("container-1", "10.0.0.1", "public")
        session = mgr.get_session_by_ip("10.0.0.1")
        assert session is not None
        assert session.mode == "public"

    def test_get_session_by_ip_not_found(self, tmp_path):
        """Get session by non-existent IP."""
        mgr = self._make_manager(tmp_path)
        assert mgr.get_session_by_ip("10.0.0.99") is None

    def test_delete_session(self, tmp_path):
        """Delete a session by token."""
        mgr = self._make_manager(tmp_path)
        token, _ = mgr.register_session("container-1", "10.0.0.1", "private")
        assert mgr.delete_session(token) is True
        assert mgr.get_session(token) is None

    def test_delete_session_not_found(self, tmp_path):
        """Delete non-existent session returns False."""
        mgr = self._make_manager(tmp_path)
        assert mgr.delete_session("bogus") is False

    def test_delete_session_by_container(self, tmp_path):
        """Delete session by container ID."""
        mgr = self._make_manager(tmp_path)
        mgr.register_session("container-1", "10.0.0.1", "private")
        assert mgr.delete_session_by_container("container-1") is True
        assert mgr.get_session_by_container("container-1") is None

    def test_delete_session_by_container_not_found(self, tmp_path):
        """Delete session for non-existent container."""
        mgr = self._make_manager(tmp_path)
        assert mgr.delete_session_by_container("nonexistent") is False

    def test_list_sessions(self, tmp_path):
        """List active sessions."""
        mgr = self._make_manager(tmp_path)
        mgr.register_session("c1", "10.0.0.1", "private")
        mgr.register_session("c2", "10.0.0.2", "public")
        sessions = mgr.list_sessions()
        assert len(sessions) == 2
        container_ids = {s["container_id"] for s in sessions}
        assert container_ids == {"c1", "c2"}

    def test_clear_all(self, tmp_path):
        """Clear all sessions."""
        mgr = self._make_manager(tmp_path)
        mgr.register_session("c1", "10.0.0.1", "private")
        mgr.register_session("c2", "10.0.0.2", "public")
        count = mgr.clear_all()
        assert count == 2
        assert mgr.list_sessions() == []

    def test_prune_expired_sessions(self, tmp_path):
        """Prune expired sessions."""
        mgr = self._make_manager(tmp_path)
        token, session = mgr.register_session("c1", "10.0.0.1", "private")
        # Force the session to expire
        session.expires_at = datetime.now(UTC) - timedelta(hours=1)
        pruned = mgr.prune_expired_sessions()
        assert pruned == 1
        assert mgr.list_sessions() == []

    def test_persistence_roundtrip(self, tmp_path):
        """Sessions survive save/load cycle."""
        mgr1 = self._make_manager(tmp_path)
        mgr1.register_session("c1", "10.0.0.1", "private")

        # Create new manager that loads from same file
        mgr2 = SessionManager(
            persistence_file=tmp_path / "sessions.json",
            ttl_hours=24,
        )
        sessions = mgr2.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["container_id"] == "c1"

    def test_validate_extends_ttl(self, tmp_path):
        """Validating a session extends its TTL."""
        mgr = self._make_manager(tmp_path)
        token, session = mgr.register_session("c1", "10.0.0.1", "private")
        old_expires = session.expires_at

        import time

        time.sleep(0.01)  # Small delay to ensure time difference
        mgr.validate_session(token)
        assert session.expires_at >= old_expires
