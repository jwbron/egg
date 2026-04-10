"""Session persistence for the egg harness."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SessionMetadata:
    """Metadata for a harness session."""

    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    model: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    total_cost_usd: float = 0.0
    num_turns: int = 0
    compaction_count: int = 0
    anchor_ref: str | None = None


@dataclass
class SessionEntry:
    """A single entry in the session file."""

    timestamp: float
    entry_type: str  # "message", "tool_result", "compaction", "metadata"
    data: dict[str, Any]


class Session:
    """Manages session persistence for conversation state."""

    def __init__(self, file_path: str | None = None) -> None:
        self.metadata = SessionMetadata()
        self._file_path = file_path
        self._messages: list[dict[str, Any]] = []
        self._entries: list[SessionEntry] = []

    @property
    def session_id(self) -> str:
        return self.metadata.session_id

    @property
    def messages(self) -> list[dict[str, Any]]:
        return self._messages

    def add_message(self, message: dict[str, Any]) -> None:
        """Add a message to the session."""
        self._messages.append(message)
        self._entries.append(
            SessionEntry(
                timestamp=time.time(),
                entry_type="message",
                data=message,
            )
        )
        self.metadata.updated_at = time.time()

    def set_messages(self, messages: list[dict[str, Any]]) -> None:
        """Replace all messages (used after compaction)."""
        self._messages = messages
        self._entries.append(
            SessionEntry(
                timestamp=time.time(),
                entry_type="compaction",
                data={"message_count": len(messages)},
            )
        )

    def save(self, file_path: str | None = None) -> None:
        """Save session to a JSONL file."""
        path = file_path or self._file_path
        if not path:
            return

        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                # Write metadata as first line
                meta_line = json.dumps(
                    {
                        "type": "metadata",
                        "data": asdict(self.metadata),
                    }
                )
                f.write(meta_line + "\n")

                # Write all messages
                for msg in self._messages:
                    line = json.dumps(
                        {
                            "type": "message",
                            "data": msg,
                        }
                    )
                    f.write(line + "\n")

            logger.debug(f"Session saved to {path} ({len(self._messages)} messages)")
        except Exception as e:
            logger.error(f"Failed to save session: {e}")

    @classmethod
    def load(cls, file_path: str) -> Session:
        """Load a session from a JSONL file."""
        session = cls(file_path=file_path)

        try:
            with open(file_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)

                    if entry.get("type") == "metadata":
                        data = entry["data"]
                        session.metadata = SessionMetadata(
                            **{
                                k: v
                                for k, v in data.items()
                                if k in SessionMetadata.__dataclass_fields__
                            }
                        )
                    elif entry.get("type") == "message":
                        session._messages.append(entry["data"])
        except FileNotFoundError:
            logger.debug(f"No session file at {file_path}")
        except Exception as e:
            logger.error(f"Failed to load session from {file_path}: {e}")

        return session
