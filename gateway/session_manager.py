"""
Session Manager - Per-container session management for repository mode enforcement.

Provides thread-safe session storage with disk persistence for the gateway sidecar.
Sessions bind containers to specific repository visibility modes (private or public)
and are verified via container IP.

Security Properties:
- Session tokens are 256-bit random (cryptographically secure)
- Only token hashes stored on disk (sha256)
- Session-container binding verified by Docker network source IP
- Fail-closed: Invalid/missing sessions always denied
- Rate limiting prevents enumeration attacks
"""

import hashlib
import json
import os
import secrets
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

# Add shared directory to path for egg_logging
_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists():
    sys.path.insert(0, str(_shared_path))
from egg_logging import get_logger

logger = get_logger("gateway.session-manager")

# Session configuration
DEFAULT_SESSION_TTL_HOURS = 24
DEFAULT_CLEANUP_INTERVAL_MINUTES = 15
SESSION_TOKEN_BYTES = 32  # 256 bits

# Persistence file path - use /tmp since /secrets is mounted read-only
# Sessions are ephemeral (cleaned up when containers exit) so /tmp is fine
SESSION_PERSISTENCE_DIR = Path("/tmp/egg-sessions")
SESSION_PERSISTENCE_FILE = SESSION_PERSISTENCE_DIR / "sessions.json"

# Mode type alias
ModeType = Literal["private", "public"]


def _hash_token(token: str) -> str:
    """Compute SHA-256 hash of a token.

    Args:
        token: The raw session token

    Returns:
        Hex-encoded SHA-256 hash
    """
    return hashlib.sha256(token.encode()).hexdigest()


def _constant_time_compare(a: str, b: str) -> bool:
    """Compare two strings in constant time to prevent timing attacks.

    Args:
        a: First string
        b: Second string

    Returns:
        True if strings are equal
    """
    return secrets.compare_digest(a.encode(), b.encode())


@dataclass
class Session:
    """Session data for a container.

    Attributes:
        session_token: Raw token (in-memory only, not persisted)
        session_token_hash: SHA-256 hash of token (persisted)
        container_id: Docker container ID for audit and worktree cleanup
        container_ip: Expected source IP for verification
        mode: Repository visibility mode (private or public)
        created_at: Session creation timestamp
        last_seen: Last request timestamp (for heartbeat)
        expires_at: Session expiry timestamp
    """

    session_token: str | None  # Raw token, only in memory
    session_token_hash: str
    container_id: str
    container_ip: str
    mode: ModeType
    created_at: datetime
    last_seen: datetime
    expires_at: datetime

    def is_expired(self) -> bool:
        """Check if session has expired."""
        return datetime.now(UTC) > self.expires_at

    def extend_ttl(self, hours: int = DEFAULT_SESSION_TTL_HOURS) -> None:
        """Extend session TTL (heartbeat)."""
        self.last_seen = datetime.now(UTC)
        self.expires_at = self.last_seen + timedelta(hours=hours)

    def to_dict_for_persistence(self) -> dict[str, Any]:
        """Convert to dictionary for persistence (excludes raw token)."""
        return {
            "session_token_hash": self.session_token_hash,
            "container_id": self.container_id,
            "container_ip": self.container_ip,
            "mode": self.mode,
            "created_at": self.created_at.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }

    @classmethod
    def from_persistence(cls, data: dict[str, Any]) -> "Session":
        """Create Session from persisted data (no raw token)."""
        return cls(
            session_token=None,  # Raw token not persisted
            session_token_hash=data["session_token_hash"],
            container_id=data["container_id"],
            container_ip=data["container_ip"],
            mode=data["mode"],
            created_at=datetime.fromisoformat(data["created_at"]),
            last_seen=datetime.fromisoformat(data["last_seen"]),
            expires_at=datetime.fromisoformat(data["expires_at"]),
        )


@dataclass
class SessionValidationResult:
    """Result of session validation."""

    valid: bool
    session: Session | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API response."""
        result: dict[str, Any] = {"valid": self.valid}
        if self.error:
            result["error"] = self.error
        if self.session:
            result["mode"] = self.session.mode
            result["container_id"] = self.session.container_id
        return result


class SessionManager:
    """
    Thread-safe session manager with disk persistence.

    Sessions are stored in memory with periodic persistence to disk.
    Only token hashes are persisted; raw tokens are kept in memory only.
    """

    def __init__(
        self,
        persistence_file: Path | None = None,
        ttl_hours: int = DEFAULT_SESSION_TTL_HOURS,
    ):
        """
        Initialize the session manager.

        Args:
            persistence_file: Path to persistence file (default: ~/.config/egg/sessions.json)
            ttl_hours: Default session TTL in hours
        """
        self._persistence_file = persistence_file or SESSION_PERSISTENCE_FILE
        self._ttl_hours = ttl_hours

        # Session storage: token_hash -> Session
        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()

        # Token lookup: raw_token -> token_hash (for fast validation in memory)
        # This enables O(1) lookup when validating tokens
        self._token_to_hash: dict[str, str] = {}

        # Load persisted sessions on startup
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        """Load sessions from persistence file."""
        if not self._persistence_file.exists():
            logger.debug("No session persistence file found, starting fresh")
            return

        try:
            with open(self._persistence_file) as f:
                data = json.load(f)

            loaded = 0
            pruned = 0
            for session_data in data.get("sessions", []):
                try:
                    session = Session.from_persistence(session_data)
                    if session.is_expired():
                        pruned += 1
                        continue
                    self._sessions[session.session_token_hash] = session
                    loaded += 1
                except (KeyError, ValueError) as e:
                    logger.warning(
                        "Failed to load session from persistence",
                        error=str(e),
                    )

            logger.info(
                "Loaded sessions from disk",
                loaded=loaded,
                pruned_expired=pruned,
            )
        except json.JSONDecodeError as e:
            logger.warning(
                "Failed to parse session persistence file",
                error=str(e),
            )
        except OSError as e:
            logger.warning(
                "Failed to read session persistence file",
                error=str(e),
            )

    def _save_to_disk(self) -> None:
        """Save sessions to disk with atomic write."""
        # Ensure directory exists
        self._persistence_file.parent.mkdir(parents=True, exist_ok=True)

        # Prepare data for persistence
        sessions_data = [session.to_dict_for_persistence() for session in self._sessions.values()]
        data = {
            "version": 1,
            "saved_at": datetime.now(UTC).isoformat(),
            "sessions": sessions_data,
        }

        # Atomic write: write to temp file, then rename
        temp_file = self._persistence_file.with_suffix(".tmp")
        try:
            with open(temp_file, "w") as f:
                json.dump(data, f, indent=2)

            # Set restrictive permissions before rename
            os.chmod(temp_file, 0o600)

            # Atomic rename
            temp_file.rename(self._persistence_file)

            logger.debug(
                "Saved sessions to disk",
                session_count=len(sessions_data),
            )
        except OSError as e:
            logger.error(
                "Failed to save sessions to disk",
                error=str(e),
            )
            # Clean up temp file if it exists
            if temp_file.exists():
                temp_file.unlink(missing_ok=True)

    def register_session(
        self,
        container_id: str,
        container_ip: str,
        mode: ModeType,
    ) -> tuple[str, Session]:
        """
        Register a new session for a container.

        Args:
            container_id: Docker container ID
            container_ip: Container's IP address on the Docker network
            mode: Repository visibility mode (private or public)

        Returns:
            Tuple of (session_token, Session)
        """
        # Generate cryptographically secure token
        token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
        token_hash = _hash_token(token)

        now = datetime.now(UTC)
        session = Session(
            session_token=token,
            session_token_hash=token_hash,
            container_id=container_id,
            container_ip=container_ip,
            mode=mode,
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=self._ttl_hours),
        )

        with self._lock:
            self._sessions[token_hash] = session
            self._token_to_hash[token] = token_hash
            self._save_to_disk()

        logger.info(
            "Session registered",
            event_type="session_registered",
            session_token_hash=token_hash[:16],
            container_id=container_id,
            container_ip=container_ip,
            mode=mode,
        )

        return token, session

    def validate_session(
        self,
        token: str,
        source_ip: str | None = None,
    ) -> SessionValidationResult:
        """
        Validate a session token and optionally verify source IP.

        Args:
            token: The session token to validate
            source_ip: The source IP to verify against (optional)

        Returns:
            SessionValidationResult with validation status
        """
        with self._lock:
            # First try fast lookup via in-memory token cache
            token_hash = self._token_to_hash.get(token)

            if not token_hash:
                # Token not in fast cache, compute hash and check
                token_hash = _hash_token(token)

            session = self._sessions.get(token_hash)

            if not session:
                logger.warning(
                    "Session validation failed - invalid token",
                    event_type="session_auth_failed",
                    session_token_hash=token_hash[:16],
                )
                return SessionValidationResult(
                    valid=False,
                    error="Invalid or expired session token",
                )

            if session.is_expired():
                logger.warning(
                    "Session validation failed - expired",
                    event_type="session_expired",
                    session_token_hash=token_hash[:16],
                    container_id=session.container_id,
                )
                # Clean up expired session
                del self._sessions[token_hash]
                if session.session_token is not None:
                    self._token_to_hash.pop(session.session_token, None)
                self._save_to_disk()
                return SessionValidationResult(
                    valid=False,
                    error="Session has expired",
                )

            # Verify source IP if provided
            if source_ip and session.container_ip != source_ip:
                logger.warning(
                    "Session validation failed - IP mismatch",
                    event_type="session_ip_mismatch",
                    session_token_hash=token_hash[:16],
                    container_id=session.container_id,
                    expected_ip=session.container_ip,
                    actual_ip=source_ip,
                )
                return SessionValidationResult(
                    valid=False,
                    error="Session-container binding verification failed",
                )

            # Extend session TTL (heartbeat on successful validation)
            session.extend_ttl(self._ttl_hours)

            # Update fast lookup cache if this was a hash lookup
            if session.session_token and session.session_token not in self._token_to_hash:
                self._token_to_hash[session.session_token] = token_hash

            return SessionValidationResult(
                valid=True,
                session=session,
            )

    def get_session(self, token: str) -> Session | None:
        """
        Get session by token without IP verification.

        Args:
            token: The session token

        Returns:
            Session if found and not expired, None otherwise
        """
        result = self.validate_session(token)
        return result.session if result.valid else None

    def get_session_by_container(self, container_id: str) -> Session | None:
        """
        Get session by container ID.

        Args:
            container_id: Docker container ID

        Returns:
            Session if found and not expired, None otherwise
        """
        with self._lock:
            for session in self._sessions.values():
                if session.container_id == container_id and not session.is_expired():
                    return session
        return None

    def get_session_by_ip(self, ip_address: str) -> Session | None:
        """
        Get session by container IP address.

        Used for endpoints that don't have session token auth (e.g., Anthropic API proxy)
        to determine the requesting container's session mode.

        Args:
            ip_address: The IP address of the requesting container

        Returns:
            Session if found and not expired, None otherwise
        """
        with self._lock:
            for session in self._sessions.values():
                if session.container_ip == ip_address and not session.is_expired():
                    return session
        return None

    def delete_session(self, token: str) -> bool:
        """
        Delete a session by token.

        Only the launcher (with launcher_secret) should call this.

        Args:
            token: The session token to delete

        Returns:
            True if session was deleted, False if not found
        """
        token_hash = self._token_to_hash.get(token) or _hash_token(token)

        with self._lock:
            session = self._sessions.get(token_hash)
            if not session:
                return False

            del self._sessions[token_hash]
            self._token_to_hash.pop(token, None)
            self._save_to_disk()

            logger.info(
                "Session deleted",
                event_type="session_deleted",
                session_token_hash=token_hash[:16],
                container_id=session.container_id,
            )

            return True

    def delete_session_by_container(self, container_id: str) -> bool:
        """
        Delete session by container ID.

        Args:
            container_id: Docker container ID

        Returns:
            True if session was deleted, False if not found
        """
        with self._lock:
            to_delete = None
            for token_hash, session in self._sessions.items():
                if session.container_id == container_id:
                    to_delete = token_hash
                    break

            if to_delete:
                session = self._sessions.pop(to_delete)
                if session.session_token:
                    self._token_to_hash.pop(session.session_token, None)
                self._save_to_disk()

                logger.info(
                    "Session deleted by container ID",
                    event_type="session_deleted",
                    session_token_hash=to_delete[:16],
                    container_id=container_id,
                )
                return True

            return False

    def prune_expired_sessions(self) -> int:
        """
        Remove all expired sessions.

        Called periodically and on gateway startup.

        Returns:
            Number of sessions pruned
        """
        pruned = 0
        with self._lock:
            expired_hashes = [
                token_hash for token_hash, session in self._sessions.items() if session.is_expired()
            ]

            for token_hash in expired_hashes:
                session = self._sessions.pop(token_hash)
                if session.session_token:
                    self._token_to_hash.pop(session.session_token, None)
                pruned += 1

                logger.info(
                    "Session expired and pruned",
                    event_type="session_expired",
                    session_token_hash=token_hash[:16],
                    container_id=session.container_id,
                )

            if pruned > 0:
                self._save_to_disk()

        return pruned

    def list_sessions(self) -> list[dict[str, Any]]:
        """
        List all active (non-expired) sessions.

        Returns:
            List of session info dictionaries (without tokens)
        """
        with self._lock:
            return [
                {
                    "container_id": session.container_id,
                    "container_ip": session.container_ip,
                    "mode": session.mode,
                    "created_at": session.created_at.isoformat(),
                    "expires_at": session.expires_at.isoformat(),
                }
                for session in self._sessions.values()
                if not session.is_expired()
            ]

    def clear_all(self) -> int:
        """
        Clear all sessions.

        Used for testing and emergency cleanup.

        Returns:
            Number of sessions cleared
        """
        with self._lock:
            count = len(self._sessions)
            self._sessions.clear()
            self._token_to_hash.clear()
            self._save_to_disk()
            return count


# Global session manager instance with thread-safe initialization
_session_manager: SessionManager | None = None
_session_manager_lock = threading.Lock()


def get_session_manager() -> SessionManager:
    """Get the global session manager instance (thread-safe)."""
    global _session_manager
    if _session_manager is None:
        with _session_manager_lock:
            # Double-checked locking pattern
            if _session_manager is None:
                _session_manager = SessionManager()
    return _session_manager


def validate_session_for_request(
    token: str | None,
    source_ip: str | None = None,
) -> SessionValidationResult:
    """
    Validate session for a request. All containers must have a valid session.

    Args:
        token: Session token from Authorization header
        source_ip: Request source IP

    Returns:
        SessionValidationResult
    """
    if not token:
        return SessionValidationResult(
            valid=False,
            error="Session token required but not provided",
        )

    return get_session_manager().validate_session(token, source_ip)
