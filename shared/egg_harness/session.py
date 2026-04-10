"""Session persistence via JSONL for agent conversation state.

Provides :class:`SessionState` for capturing the full state of an agent
session and :class:`SessionManager` for saving/loading that state to
JSONL files on disk.

File format (JSONL)::

    Line 1:  JSON metadata (everything except messages)
    Line 2+: One JSON line per message from the messages list
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SessionState
# ---------------------------------------------------------------------------


@dataclass
class SessionState:
    """Snapshot of an agent session's full state.

    Attributes:
        session_id: Unique identifier for this session.
        model: Canonical model identifier.
        messages: Conversation message list.
        system_prompt: Optional system prompt text.
        total_cost_usd: Cumulative cost in USD.
        turn_count: Number of conversation turns completed.
        duration_ms: Total elapsed time in milliseconds.
        compaction_count: Number of context compactions performed.
        created_at: ISO 8601 timestamp of session creation.
        updated_at: ISO 8601 timestamp of last update.
    """

    session_id: str
    model: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    system_prompt: str | None = None
    total_cost_usd: float = 0.0
    turn_count: int = 0
    duration_ms: int = 0
    compaction_count: int = 0
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
    )


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------


class SessionManager:
    """Manages persistence of agent session state to JSONL files.

    Each session is stored as a single JSONL file where the first line
    contains session metadata and subsequent lines contain individual
    messages.

    Args:
        session_id: Unique session identifier.  A UUID4 is generated
            automatically if not provided.
        storage_dir: Directory for session files.
        auto_save_interval: Save automatically every *N* turns.
    """

    def __init__(
        self,
        session_id: str | None = None,
        storage_dir: str = "/tmp/egg-sessions",
        auto_save_interval: int = 5,
    ) -> None:
        self._session_id = session_id or str(uuid.uuid4())
        self._storage_dir = storage_dir
        self._auto_save_interval = auto_save_interval

    # -- properties -----------------------------------------------------------

    @property
    def session_id(self) -> str:
        """The unique identifier for this session."""
        return self._session_id

    @property
    def storage_dir(self) -> str:
        """The directory where session files are stored."""
        return self._storage_dir

    # -- public interface -----------------------------------------------------

    def save(self, state: SessionState) -> None:
        """Save session state to a JSONL file.

        The file is written to ``{storage_dir}/{session_id}.jsonl``.
        Line 1 contains all metadata fields (everything except
        ``messages``).  Lines 2+ contain one JSON object per message.

        The storage directory is created automatically if it does not
        exist.

        Args:
            state: The session state to persist.
        """
        os.makedirs(self._storage_dir, exist_ok=True)
        filepath = self._session_path(state.session_id)

        state_dict = asdict(state)
        messages = state_dict.pop("messages", [])

        # Update timestamp on save.
        state_dict["updated_at"] = datetime.now(UTC).isoformat()

        with open(filepath, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(state_dict, separators=(",", ":")) + "\n")
            for msg in messages:
                fh.write(json.dumps(msg, separators=(",", ":")) + "\n")

        logger.info(
            "Saved session %s (%d messages) to %s",
            state.session_id,
            len(messages),
            filepath,
        )

    def load(self, session_id: str) -> SessionState | None:
        """Load session state from a JSONL file.

        Args:
            session_id: The session identifier to load.

        Returns:
            The restored :class:`SessionState`, or ``None`` if the
            session file does not exist.
        """
        filepath = self._session_path(session_id)
        if not os.path.isfile(filepath):
            logger.debug("Session file not found: %s", filepath)
            return None

        with open(filepath, encoding="utf-8") as fh:
            lines = fh.read().splitlines()

        if not lines:
            logger.warning("Empty session file: %s", filepath)
            return None

        metadata: dict[str, Any] = json.loads(lines[0])
        messages: list[dict[str, Any]] = [json.loads(line) for line in lines[1:] if line.strip()]

        return SessionState(
            session_id=metadata.get("session_id", session_id),
            model=metadata.get("model", ""),
            messages=messages,
            system_prompt=metadata.get("system_prompt"),
            total_cost_usd=metadata.get("total_cost_usd", 0.0),
            turn_count=metadata.get("turn_count", 0),
            duration_ms=metadata.get("duration_ms", 0),
            compaction_count=metadata.get("compaction_count", 0),
            created_at=metadata.get("created_at", ""),
            updated_at=metadata.get("updated_at", ""),
        )

    def should_auto_save(self, turn_count: int) -> bool:
        """Check whether an auto-save should be triggered.

        Args:
            turn_count: The current turn number.

        Returns:
            True if *turn_count* is a positive multiple of the
            configured ``auto_save_interval``.
        """
        if turn_count <= 0:
            return False
        return turn_count % self._auto_save_interval == 0

    # -- internal helpers -----------------------------------------------------

    def _session_path(self, session_id: str) -> str:
        """Return the filesystem path for a session's JSONL file."""
        return os.path.join(self._storage_dir, f"{session_id}.jsonl")
