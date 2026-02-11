"""
Transcript buffer for capturing Anthropic API request/response pairs.

This module provides a per-session buffer for storing API traffic at the gateway
proxy layer. The buffer is written to /tmp/egg-transcripts/{container_id}.jsonl
and is used by the checkpoint handler to extract transcripts without depending
on Claude Code's internal JSONL file format.

Buffer entry schema (one JSON object per API turn):
{
    "timestamp": "2026-02-11T00:00:00.000000Z",  # ISO 8601 timestamp
    "type": "api_turn",                           # Entry type
    "request": {
        "model": "claude-opus-4-5-20251101",      # Model ID
        "messages": [...],                        # Request messages (truncated)
        "system": "...",                          # System prompt (truncated/redacted)
        "tools": [...],                           # Tools definition
        "max_tokens": 4096                        # Max tokens
    },
    "response": {
        "content": [...],                         # Response content blocks
        "model": "claude-opus-4-5-20251101",      # Model used
        "stop_reason": "end_turn",                # Why the response ended
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_read_input_tokens": 200,
            "cache_creation_input_tokens": 100
        }
    },
    "duration_ms": 1500.0,                        # Request duration
    "streaming": false                            # Whether streaming was used
}

Buffer lifecycle:
- Created on first API call for a session
- Rotated when size exceeds MAX_BUFFER_SIZE (10MB)
- Cleaned up when session ends (via session_manager cleanup hook)
- Auto-cleaned on container restart (files are in /tmp)
"""

import fcntl
import json
import os
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Add shared directory to path for egg_logging
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists():
    sys.path.insert(0, str(_shared_path))
from egg_logging import get_logger

logger = get_logger("gateway.transcript-buffer")

# Buffer configuration
BUFFER_DIR = Path("/tmp/egg-transcripts")
MAX_BUFFER_SIZE = 10 * 1024 * 1024  # 10MB
MAX_MESSAGE_CONTENT_LENGTH = 5000  # Truncate message content to this length
MAX_SYSTEM_PROMPT_LENGTH = 2000  # Truncate system prompts
MAX_TOOL_RESULT_LENGTH = 2000  # Truncate tool results


class TranscriptBuffer:
    """
    Thread-safe buffer for capturing API request/response pairs.

    Each session (identified by container_id) has its own buffer file.
    Writes are append-only and use file locking for thread safety.
    Buffer is rotated when it exceeds MAX_BUFFER_SIZE.
    """

    def __init__(
        self,
        container_id: str,
        buffer_dir: Path | None = None,
        max_size: int = MAX_BUFFER_SIZE,
    ):
        """
        Initialize the transcript buffer.

        Args:
            container_id: Unique identifier for the session (container ID)
            buffer_dir: Directory for buffer files (default: /tmp/egg-transcripts)
            max_size: Maximum buffer size before rotation (default: 10MB)
        """
        self._container_id = container_id
        self._buffer_dir = buffer_dir or BUFFER_DIR
        self._max_size = max_size
        self._lock = threading.Lock()
        self._entries_dropped = 0

        # Ensure buffer directory exists with restricted permissions (0o700)
        # Buffer contains API request/response data that should not be world-readable
        self._buffer_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Also fix permissions if directory already exists with wrong mode
        try:
            os.chmod(self._buffer_dir, 0o700)
        except OSError:
            pass  # May fail if not owner, but that's ok

    @property
    def buffer_path(self) -> Path:
        """Get the path to this session's buffer file."""
        return self._buffer_dir / f"{self._container_id}.jsonl"

    def write_api_turn(
        self,
        request_body: dict[str, Any],
        response_content: list[dict[str, Any]] | None,
        response_usage: dict[str, Any] | None,
        response_model: str | None = None,
        stop_reason: str | None = None,
        duration_ms: float | None = None,
        streaming: bool = False,
    ) -> bool:
        """
        Write an API request/response pair to the buffer.

        Args:
            request_body: The API request body (messages, model, tools, etc.)
            response_content: Response content blocks (may be None if error)
            response_usage: Token usage from response
            response_model: Model ID from response
            stop_reason: Why the response ended (end_turn, tool_use, etc.)
            duration_ms: Request duration in milliseconds
            streaming: Whether this was a streaming request

        Returns:
            True if write succeeded, False otherwise
        """
        try:
            # Build entry
            entry = self._build_entry(
                request_body=request_body,
                response_content=response_content,
                response_usage=response_usage,
                response_model=response_model,
                stop_reason=stop_reason,
                duration_ms=duration_ms,
                streaming=streaming,
            )

            # Serialize to JSON
            entry_json = json.dumps(entry, default=str) + "\n"
            entry_bytes = entry_json.encode("utf-8")

            # Check if rotation is needed and write
            with self._lock:
                self._maybe_rotate()
                self._append_entry(entry_bytes)

            logger.debug(
                "API turn captured",
                container_id=self._container_id,
                streaming=streaming,
                entry_size=len(entry_bytes),
            )
            return True

        except Exception as e:
            logger.warning(
                "Failed to write API turn to buffer",
                container_id=self._container_id,
                error=str(e),
            )
            return False

    def _build_entry(
        self,
        request_body: dict[str, Any],
        response_content: list[dict[str, Any]] | None,
        response_usage: dict[str, Any] | None,
        response_model: str | None,
        stop_reason: str | None,
        duration_ms: float | None,
        streaming: bool,
    ) -> dict[str, Any]:
        """Build a buffer entry from request/response data."""
        timestamp = datetime.now(UTC).isoformat()

        # Truncate/sanitize request data
        sanitized_request = self._sanitize_request(request_body)

        # Build response object
        response_obj: dict[str, Any] | None = None
        if response_content is not None or response_usage is not None:
            response_obj = {}
            if response_content is not None:
                response_obj["content"] = self._truncate_content(response_content)
            if response_model:
                response_obj["model"] = response_model
            if stop_reason:
                response_obj["stop_reason"] = stop_reason
            if response_usage:
                response_obj["usage"] = response_usage

        entry: dict[str, Any] = {
            "timestamp": timestamp,
            "type": "api_turn",
            "request": sanitized_request,
        }

        if response_obj:
            entry["response"] = response_obj

        if duration_ms is not None:
            entry["duration_ms"] = duration_ms

        entry["streaming"] = streaming

        return entry

    def _sanitize_request(self, request_body: dict[str, Any]) -> dict[str, Any]:
        """Sanitize and truncate request data for storage."""
        sanitized: dict[str, Any] = {}

        # Copy model
        if "model" in request_body:
            sanitized["model"] = request_body["model"]

        # Truncate messages content
        if "messages" in request_body:
            sanitized["messages"] = self._truncate_messages(request_body["messages"])

        # Truncate system prompt
        if "system" in request_body:
            system = request_body["system"]
            if isinstance(system, str):
                sanitized["system"] = (
                    system[:MAX_SYSTEM_PROMPT_LENGTH] + "..."
                    if len(system) > MAX_SYSTEM_PROMPT_LENGTH
                    else system
                )
            elif isinstance(system, list):
                # System can be a list of content blocks
                sanitized["system"] = self._truncate_content(system)

        # Copy tools (don't truncate - they're important for understanding)
        if "tools" in request_body:
            # Just copy tool names and types, not full schemas
            tools = request_body["tools"]
            if isinstance(tools, list):
                sanitized["tools"] = [
                    {"name": t.get("name"), "type": t.get("type", "function")}
                    for t in tools
                    if isinstance(t, dict)
                ]

        # Copy max_tokens
        if "max_tokens" in request_body:
            sanitized["max_tokens"] = request_body["max_tokens"]

        return sanitized

    def _truncate_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Truncate message content while preserving structure."""
        truncated = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue

            new_msg: dict[str, Any] = {"role": msg.get("role")}

            content = msg.get("content")
            if isinstance(content, str):
                new_msg["content"] = (
                    content[:MAX_MESSAGE_CONTENT_LENGTH] + "..."
                    if len(content) > MAX_MESSAGE_CONTENT_LENGTH
                    else content
                )
            elif isinstance(content, list):
                new_msg["content"] = self._truncate_content(content)
            else:
                new_msg["content"] = content

            truncated.append(new_msg)

        return truncated

    def _truncate_content(self, content: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Truncate content blocks while preserving structure."""
        truncated = []
        for block in content:
            if not isinstance(block, dict):
                continue

            new_block = dict(block)
            block_type = block.get("type")

            # Truncate text content
            if block_type == "text" and "text" in block:
                text = block["text"]
                if isinstance(text, str) and len(text) > MAX_MESSAGE_CONTENT_LENGTH:
                    new_block["text"] = text[:MAX_MESSAGE_CONTENT_LENGTH] + "..."

            # Truncate tool results
            elif block_type == "tool_result" and "content" in block:
                result_content = block["content"]
                if isinstance(result_content, str) and len(result_content) > MAX_TOOL_RESULT_LENGTH:
                    new_block["content"] = result_content[:MAX_TOOL_RESULT_LENGTH] + "..."
                elif isinstance(result_content, list):
                    new_block["content"] = self._truncate_content(result_content)

            # Keep tool_use blocks as-is (they're small and important)
            elif block_type == "tool_use":
                pass

            truncated.append(new_block)

        return truncated

    def _maybe_rotate(self) -> None:
        """Rotate the buffer if it exceeds max size."""
        if not self.buffer_path.exists():
            return

        try:
            current_size = self.buffer_path.stat().st_size
            if current_size >= self._max_size:
                self._rotate_buffer()
        except OSError:
            pass

    def _rotate_buffer(self) -> None:
        """
        Rotate the buffer by keeping only the newest entries.

        Strategy: Read all entries, drop the oldest half, write to a temp file,
        then atomically replace the original. This avoids data loss from
        concurrent writes during rotation.
        """
        try:
            # Read all entries with exclusive lock to prevent concurrent writes
            entries = []
            with open(self.buffer_path, "r+b") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    for line in f:
                        line = line.decode("utf-8", errors="replace").strip()
                        if line:
                            entries.append(line)

                    if not entries:
                        return

                    # Keep the newest half
                    keep_count = len(entries) // 2
                    if keep_count == 0:
                        keep_count = 1

                    dropped_count = len(entries) - keep_count
                    self._entries_dropped += dropped_count

                    logger.info(
                        "Rotating transcript buffer",
                        container_id=self._container_id,
                        entries_before=len(entries),
                        entries_after=keep_count,
                        entries_dropped=dropped_count,
                        total_dropped=self._entries_dropped,
                    )

                    # Write kept entries to temp file
                    temp_path = self.buffer_path.with_suffix(".jsonl.tmp")
                    with open(temp_path, "w") as tmp:
                        for entry in entries[-keep_count:]:
                            tmp.write(entry + "\n")

                    # Atomic replace - still holding lock on original file
                    os.replace(temp_path, self.buffer_path)

                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

        except Exception as e:
            logger.warning(
                "Failed to rotate buffer",
                container_id=self._container_id,
                error=str(e),
            )
            # Clean up temp file if it exists
            temp_path = self.buffer_path.with_suffix(".jsonl.tmp")
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    def _append_entry(self, entry_bytes: bytes) -> None:
        """Append an entry to the buffer file with file locking."""
        with open(self.buffer_path, "ab") as f:
            # Use advisory locking for thread safety across processes
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.write(entry_bytes)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def read_entries(self) -> list[dict[str, Any]]:
        """
        Read all entries from the buffer.

        Returns:
            List of parsed entry dictionaries.
        """
        entries = []
        if not self.buffer_path.exists():
            return entries

        try:
            with open(self.buffer_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        entries.append(entry)
                    except json.JSONDecodeError:
                        # Skip malformed lines
                        continue
        except Exception as e:
            logger.warning(
                "Failed to read buffer entries",
                container_id=self._container_id,
                error=str(e),
            )

        return entries

    def clear(self) -> bool:
        """
        Clear the buffer file.

        Returns:
            True if cleared successfully, False otherwise.
        """
        try:
            if self.buffer_path.exists():
                self.buffer_path.unlink()
                logger.debug("Buffer cleared", container_id=self._container_id)
            return True
        except Exception as e:
            logger.warning(
                "Failed to clear buffer",
                container_id=self._container_id,
                error=str(e),
            )
            return False

    def get_stats(self) -> dict[str, Any]:
        """Get buffer statistics."""
        try:
            if self.buffer_path.exists():
                stat = self.buffer_path.stat()
                return {
                    "container_id": self._container_id,
                    "path": str(self.buffer_path),
                    "size_bytes": stat.st_size,
                    "entries_dropped": self._entries_dropped,
                    "max_size": self._max_size,
                }
            return {
                "container_id": self._container_id,
                "path": str(self.buffer_path),
                "size_bytes": 0,
                "entries_dropped": self._entries_dropped,
                "max_size": self._max_size,
            }
        except Exception:
            return {
                "container_id": self._container_id,
                "error": "Failed to get stats",
            }


# Global buffer cache to avoid creating multiple buffers for the same session
_buffer_cache: dict[str, TranscriptBuffer] = {}
_cache_lock = threading.Lock()


def get_transcript_buffer(container_id: str) -> TranscriptBuffer:
    """
    Get or create a transcript buffer for a container.

    Args:
        container_id: The container ID to get/create a buffer for.

    Returns:
        TranscriptBuffer instance for the container.
    """
    with _cache_lock:
        if container_id not in _buffer_cache:
            _buffer_cache[container_id] = TranscriptBuffer(container_id)
        return _buffer_cache[container_id]


def cleanup_transcript_buffer(container_id: str) -> bool:
    """
    Clean up the transcript buffer for a container.

    Called when a session ends to remove the buffer file.

    Args:
        container_id: The container ID to clean up.

    Returns:
        True if cleanup succeeded, False otherwise.
    """
    with _cache_lock:
        if container_id in _buffer_cache:
            buffer = _buffer_cache.pop(container_id)
            return buffer.clear()
        # Try to clean up even if not in cache (might have been created by another process)
        buffer = TranscriptBuffer(container_id)
        return buffer.clear()


def get_buffer_path(container_id: str) -> Path:
    """Get the buffer file path for a container (for use by transcript extractor)."""
    return BUFFER_DIR / f"{container_id}.jsonl"
