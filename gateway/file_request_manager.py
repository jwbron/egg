"""In-memory store for pending file access requests.

Tracks requests from agents to access files that are blocked by phase or
agent-role restrictions.  Each request maps to an HITL decision in the
orchestrator, and when approved the corresponding file is added to the
session's ``file_exceptions`` list so subsequent pushes succeed.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime

from egg_logging import get_logger

logger = get_logger("gateway.file-request-manager")

_counter_lock = threading.Lock()
_counter = 0


def _next_request_id() -> str:
    global _counter
    with _counter_lock:
        _counter += 1
        return f"file-req-{_counter}"


@dataclass
class FileAccessRequest:
    """A request from an agent to access a blocked file."""

    request_id: str
    session_token_hash: str
    pipeline_id: str
    decision_id: str
    file_path: str
    reason: str
    status: str = "pending"  # "pending", "approved", "denied"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class FileRequestManager:
    """Thread-safe in-memory store for file access requests."""

    def __init__(self) -> None:
        self._requests: dict[str, FileAccessRequest] = {}
        self._lock = threading.Lock()

    def create_request(
        self,
        session_token_hash: str,
        pipeline_id: str,
        decision_id: str,
        file_path: str,
        reason: str,
    ) -> FileAccessRequest:
        """Create and store a new file access request."""
        req = FileAccessRequest(
            request_id=_next_request_id(),
            session_token_hash=session_token_hash,
            pipeline_id=pipeline_id,
            decision_id=decision_id,
            file_path=file_path,
            reason=reason,
        )
        with self._lock:
            self._requests[req.request_id] = req

        logger.info(
            "File access request created",
            event_type="file_request_created",
            request_id=req.request_id,
            pipeline_id=pipeline_id,
            file_path=file_path,
        )
        return req

    def get_request(self, request_id: str) -> FileAccessRequest | None:
        """Look up a request by ID."""
        with self._lock:
            return self._requests.get(request_id)

    def resolve_request(self, request_id: str, approved: bool) -> bool:
        """Mark a request as approved or denied.

        Returns True if the request was found and updated, False otherwise.
        """
        with self._lock:
            req = self._requests.get(request_id)
            if not req or req.status != "pending":
                return False
            req.status = "approved" if approved else "denied"
            resolved_status = req.status
            resolved_file_path = req.file_path

        logger.info(
            "File access request resolved",
            event_type="file_request_resolved",
            request_id=request_id,
            status=resolved_status,
            file_path=resolved_file_path,
        )
        return True

    def get_requests_for_session(self, session_token_hash: str) -> list[FileAccessRequest]:
        """Get all requests associated with a session."""
        with self._lock:
            return [
                r for r in self._requests.values() if r.session_token_hash == session_token_hash
            ]


# Module-level singleton
_file_request_manager: FileRequestManager | None = None
_singleton_lock = threading.Lock()


def get_file_request_manager() -> FileRequestManager:
    """Get or create the singleton FileRequestManager."""
    global _file_request_manager
    if _file_request_manager is None:
        with _singleton_lock:
            if _file_request_manager is None:
                _file_request_manager = FileRequestManager()
    return _file_request_manager
