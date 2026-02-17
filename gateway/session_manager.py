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

from __future__ import annotations

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


# Import transcript buffer cleanup (lazy to avoid circular imports)
def _cleanup_transcript_buffer(container_id: str) -> None:
    """Clean up transcript buffer for a container when session ends."""
    try:
        from transcript_buffer import cleanup_transcript_buffer

        cleanup_transcript_buffer(container_id)
        logger.debug("Transcript buffer cleaned up", container_id=container_id)
    except ImportError:
        # transcript_buffer may not be available in all contexts
        pass
    except Exception as e:
        logger.warning(
            "Failed to clean up transcript buffer",
            container_id=container_id,
            error=str(e),
        )


_captured_containers: set[str] = set()
_captured_containers_lock = threading.Lock()


def _capture_and_cleanup_session(
    session: Session,
    session_status: str,
) -> None:
    """Capture session-end checkpoint, then clean up transcript buffer.

    Ensures the transcript buffer is preserved until checkpoint capture completes.
    Falls back to immediate cleanup if checkpoint capture is unavailable.

    Uses a per-container deduplication guard to prevent multiple captures from
    racing code paths (delete_session, cleanup_orphaned_worktrees, prune_expired).
    """
    # Deduplicate: only capture once per container
    with _captured_containers_lock:
        if session.container_id in _captured_containers:
            logger.debug(
                "Session-end checkpoint already captured, skipping",
                container_id=session.container_id,
                session_status=session_status,
            )
            return
        _captured_containers.add(session.container_id)

    # Auto-commit any uncommitted work before capturing the checkpoint.
    # This preserves the agent's WIP so it can be recovered if the agent
    # exits without committing (e.g., timeout, crash, or oversight).
    if session.last_repo_path and session.pipeline_id:
        try:
            from post_agent_commit import auto_commit_worktree

            auto_commit_worktree(
                worktree_path=session.last_repo_path,
                container_id=session.container_id,
                agent_role=session.agent_role,
                pipeline_id=session.pipeline_id,
            )
        except ImportError:
            logger.debug(
                "post_agent_commit not available, skipping auto-commit",
                container_id=session.container_id,
            )
        except Exception as e:
            logger.warning(
                "Auto-commit failed during session cleanup",
                container_id=session.container_id,
                error=str(e),
            )

    try:
        from checkpoint_handler import (
            SESSION_END_CAPTURE_TIMEOUT,
            capture_session_end_checkpoint,
        )
        from egg_contracts.checkpoints import SessionStatus

        status = (
            session_status
            if isinstance(session_status, SessionStatus)
            else SessionStatus(session_status)
        )
        _checkpoint, completion_event = capture_session_end_checkpoint(
            session=session,
            session_status=status,
            repo_path=session.last_repo_path,
            checkpoint_repo=session.checkpoint_repo,
        )

        # Wait for async storage to complete before cleaning up the buffer
        if completion_event is not None:
            completion_event.wait(timeout=SESSION_END_CAPTURE_TIMEOUT)

    except ImportError:
        logger.debug(
            "checkpoint_handler not available, skipping session-end checkpoint",
            container_id=session.container_id,
        )
    except Exception as e:
        logger.warning(
            "Session-end checkpoint capture failed",
            container_id=session.container_id,
            error=str(e),
        )
    finally:
        # Always clean up the buffer after checkpoint capture
        _cleanup_transcript_buffer(session.container_id)

        # Remove from dedup set to prevent unbounded growth.
        # The session is fully processed (checkpoint captured, buffer cleaned up)
        # and no future code path will call this for the same container.
        with _captured_containers_lock:
            _captured_containers.discard(session.container_id)


# Session configuration
DEFAULT_SESSION_TTL_HOURS = 24
DEFAULT_CLEANUP_INTERVAL_MINUTES = 15
SESSION_TOKEN_BYTES = 32  # 256 bits

# Persistence file path - use a persistent volume so sessions survive gateway restarts
# The ~/.egg-state directory is mounted from the host (see start-gateway.sh, gateway.py)
SESSION_PERSISTENCE_DIR = Path("/home/egg/.egg-state/sessions")
SESSION_PERSISTENCE_FILE = SESSION_PERSISTENCE_DIR / "sessions.json"

# Mode type alias
ModeType = Literal["private", "public", "local"]


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
        agent_role: Role set by workflow context for contract operations
        phase: SDLC pipeline phase (refine, plan, implement, pr) for operation filtering
    """

    session_token: str | None  # Raw token, only in memory
    session_token_hash: str
    container_id: str
    container_ip: str
    mode: ModeType
    created_at: datetime
    last_seen: datetime
    expires_at: datetime
    agent_role: str | None = None  # Role set by workflow context
    phase: str | None = None  # SDLC pipeline phase for operation filtering
    issue_number: int | None = None  # GitHub issue number for checkpoint linkage
    pr_number: int | None = None  # GitHub PR number for checkpoint linkage
    pipeline_id: str | None = None  # Pipeline run ID for multi-agent correlation
    checkpoint_repo: str | None = None  # External checkpoint repo (owner/repo)
    last_repo_path: str | None = None  # Last known repo path from git operations
    last_branch: str | None = None  # Last known branch from git push
    claude_code_version: str | None = None  # Claude Code version from container
    assigned_branch: str | None = None  # Worktree branch locked to this session

    def is_expired(self) -> bool:
        """Check if session has expired."""
        return datetime.now(UTC) > self.expires_at

    def extend_ttl(self, hours: int = DEFAULT_SESSION_TTL_HOURS) -> None:
        """Extend session TTL (heartbeat)."""
        self.last_seen = datetime.now(UTC)
        self.expires_at = self.last_seen + timedelta(hours=hours)

    def to_dict_for_persistence(self) -> dict[str, Any]:
        """Convert to dictionary for persistence (excludes raw token)."""
        result = {
            "session_token_hash": self.session_token_hash,
            "container_id": self.container_id,
            "container_ip": self.container_ip,
            "mode": self.mode,
            "created_at": self.created_at.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }
        if self.agent_role is not None:
            result["agent_role"] = self.agent_role
        if self.phase is not None:
            result["phase"] = self.phase
        if self.issue_number is not None:
            result["issue_number"] = self.issue_number
        if self.pr_number is not None:
            result["pr_number"] = self.pr_number
        if self.pipeline_id is not None:
            result["pipeline_id"] = self.pipeline_id
        if self.checkpoint_repo is not None:
            result["checkpoint_repo"] = self.checkpoint_repo
        if self.last_repo_path is not None:
            result["last_repo_path"] = self.last_repo_path
        if self.last_branch is not None:
            result["last_branch"] = self.last_branch
        if self.claude_code_version is not None:
            result["claude_code_version"] = self.claude_code_version
        if self.assigned_branch is not None:
            result["assigned_branch"] = self.assigned_branch
        return result

    @classmethod
    def from_persistence(cls, data: dict[str, Any]) -> Session:
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
            agent_role=data.get("agent_role"),
            phase=data.get("phase"),
            issue_number=data.get("issue_number"),
            pr_number=data.get("pr_number"),
            pipeline_id=data.get("pipeline_id"),
            checkpoint_repo=data.get("checkpoint_repo"),
            last_repo_path=data.get("last_repo_path"),
            last_branch=data.get("last_branch"),
            claude_code_version=data.get("claude_code_version"),
            assigned_branch=data.get("assigned_branch"),
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
            # Ensure directory exists
            self._persistence_file.parent.mkdir(parents=True, exist_ok=True)

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
        phase: str | None = None,
        issue_number: int | None = None,
        pr_number: int | None = None,
        pipeline_id: str | None = None,
        agent_role: str | None = None,
        claude_code_version: str | None = None,
        branch: str | None = None,
    ) -> tuple[str, Session]:
        """
        Register a new session for a container.

        Args:
            container_id: Docker container ID
            container_ip: Container's IP address on the Docker network
            mode: Repository visibility mode (private or public)
            phase: SDLC pipeline phase (e.g., "refine", "plan", "implement", "pr")
            issue_number: Optional GitHub issue number for checkpoint linkage
            pr_number: Optional GitHub PR number for checkpoint linkage
            pipeline_id: Optional pipeline run ID for multi-agent correlation
            agent_role: Optional agent role (e.g., "coder", "tester") for checkpoint metadata
            claude_code_version: Optional Claude Code version string

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
            phase=phase,
            issue_number=issue_number,
            pr_number=pr_number,
            pipeline_id=pipeline_id,
            agent_role=agent_role,
            claude_code_version=claude_code_version,
        )

        if branch:
            session.last_branch = branch
            # Lock pipeline sessions to their assigned branch
            if pipeline_id:
                session.assigned_branch = branch

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
            phase=phase,
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

            # Repopulate fast lookup cache after restart (session loaded from
            # disk has session_token=None, so we store the raw token now)
            if session.session_token is None:
                session.session_token = token
                self._token_to_hash[token] = token_hash
            elif session.session_token not in self._token_to_hash:
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

    def update_session(
        self,
        token: str,
        container_id: str | None = None,
        container_ip: str | None = None,
    ) -> bool:
        """
        Update session container binding (container_id and/or container_ip).

        Used by the orchestrator to bind a session to the real container
        after pre-registering with a placeholder ID.

        Only the launcher (with launcher_secret) should call this.

        Args:
            token: The session token
            container_id: New container ID (optional)
            container_ip: New container IP (optional)

        Returns:
            True if session was updated, False if session not found
        """
        if not container_id and not container_ip:
            return False  # Nothing to update

        token_hash = self._token_to_hash.get(token) or _hash_token(token)

        with self._lock:
            session = self._sessions.get(token_hash)
            if not session:
                logger.warning(
                    "Failed to update session - not found",
                    session_token_hash=token_hash[:16],
                )
                return False

            if session.is_expired():
                logger.warning(
                    "Failed to update session - expired",
                    session_token_hash=token_hash[:16],
                    container_id=session.container_id,
                )
                return False

            old_container_id = session.container_id
            old_container_ip = session.container_ip

            if container_id:
                session.container_id = container_id
            if container_ip:
                session.container_ip = container_ip

            self._save_to_disk()

            logger.info(
                "Session container binding updated",
                event_type="session_container_updated",
                session_token_hash=token_hash[:16],
                old_container_id=old_container_id,
                new_container_id=session.container_id,
                old_container_ip=old_container_ip,
                new_container_ip=session.container_ip,
            )

            return True

    def update_phase(self, token: str, phase: str) -> bool:
        """
        Update the SDLC pipeline phase for a session.

        Only the launcher (with launcher_secret) should call this to update
        the phase as the pipeline progresses through stages.

        Args:
            token: The session token
            phase: The new phase value (e.g., "refine", "plan", "implement", "pr")

        Returns:
            True if phase was updated, False if session not found
        """
        token_hash = self._token_to_hash.get(token) or _hash_token(token)

        with self._lock:
            session = self._sessions.get(token_hash)
            if not session:
                logger.warning(
                    "Failed to update phase - session not found",
                    session_token_hash=token_hash[:16],
                )
                return False

            if session.is_expired():
                logger.warning(
                    "Failed to update phase - session expired",
                    session_token_hash=token_hash[:16],
                    container_id=session.container_id,
                )
                return False

            old_phase = session.phase
            session.phase = phase
            self._save_to_disk()

            logger.info(
                "Session phase updated",
                event_type="session_phase_updated",
                session_token_hash=token_hash[:16],
                container_id=session.container_id,
                old_phase=old_phase,
                new_phase=phase,
            )

            return True

    def delete_session(self, token: str) -> bool:
        """
        Delete a session by token.

        Captures a session-end checkpoint (COMPLETED) before cleaning up.
        Only the launcher (with launcher_secret) should call this.

        Args:
            token: The session token to delete

        Returns:
            True if session was deleted, False if not found
        """
        token_hash = self._token_to_hash.get(token) or _hash_token(token)
        session = None

        with self._lock:
            session = self._sessions.get(token_hash)
            if not session:
                return False

            del self._sessions[token_hash]
            self._token_to_hash.pop(token, None)
            self._save_to_disk()

        # Capture session-end checkpoint outside the lock to avoid
        # blocking other session operations during the up-to-30s wait
        _capture_and_cleanup_session(session, "completed")

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

        Captures a session-end checkpoint (COMPLETED) before cleaning up.

        Args:
            container_id: Docker container ID

        Returns:
            True if session was deleted, False if not found
        """
        session = None
        token_hash = None

        with self._lock:
            for th, s in self._sessions.items():
                if s.container_id == container_id:
                    token_hash = th
                    session = s
                    break

            if token_hash:
                self._sessions.pop(token_hash)
                if session.session_token:
                    self._token_to_hash.pop(session.session_token, None)
                self._save_to_disk()

        if session:
            # Capture session-end checkpoint outside the lock to avoid
            # blocking other session operations during the up-to-30s wait
            _capture_and_cleanup_session(session, "completed")

            logger.info(
                "Session deleted by container ID",
                event_type="session_deleted",
                session_token_hash=token_hash[:16],
                container_id=container_id,
            )
            return True

        return False

    def prune_expired_sessions(self) -> int:
        """
        Remove all expired sessions.

        Captures session-end checkpoints (EXPIRED) for each pruned session
        before cleaning up transcript buffers.

        Called periodically and on gateway startup.

        Returns:
            Number of sessions pruned
        """
        expired_sessions: list[tuple[str, Session]] = []

        with self._lock:
            expired_hashes = [
                token_hash for token_hash, session in self._sessions.items() if session.is_expired()
            ]

            for token_hash in expired_hashes:
                session = self._sessions.pop(token_hash)
                if session.session_token:
                    self._token_to_hash.pop(session.session_token, None)
                expired_sessions.append((token_hash, session))

            if expired_sessions:
                self._save_to_disk()

        # Capture checkpoints concurrently to avoid blocking N×30s sequentially.
        # Each capture waits up to 30s for async storage, so we use threads.
        if expired_sessions:
            threads = []
            for token_hash, session in expired_sessions:
                t = threading.Thread(
                    target=_capture_and_cleanup_session,
                    args=(session, "expired"),
                    daemon=True,
                )
                t.start()
                threads.append((t, token_hash, session))

            for t, token_hash, session in threads:
                t.join(timeout=35)  # 30s capture timeout + 5s buffer
                if t.is_alive():
                    logger.warning(
                        "Session expired, checkpoint capture still in progress",
                        event_type="session_expired",
                        session_token_hash=token_hash[:16],
                        container_id=session.container_id,
                        capture_timed_out=True,
                    )
                else:
                    logger.info(
                        "Session expired and pruned",
                        event_type="session_expired",
                        session_token_hash=token_hash[:16],
                        container_id=session.container_id,
                    )

        return len(expired_sessions)

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
