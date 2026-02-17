"""
Transcript extractor for API proxy buffer format.

Extracts transcript data from the gateway's API proxy buffer files
(/tmp/egg-transcripts/{container_id}.jsonl). This provides a stable and
format-independent source for checkpoint creation, as the API request/response
format is stable and documented.

The proxy buffer is the primary and only source for transcript extraction.
Claude Code's internal JSONL files are no longer used.
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .checkpoints import (
    FileOperation,
    FileOperationType,
    Message,
    MessageRole,
    SessionMetadata,
    TokenUsage,
    ToolCall,
    Transcript,
)

logger = logging.getLogger(__name__)


class TranscriptExtractError(Exception):
    """Error extracting transcript from buffer file."""

    pass


# Mapping of tool names to file operation types
TOOL_TO_FILE_OP: dict[str, FileOperationType] = {
    "Read": FileOperationType.READ,
    "Write": FileOperationType.WRITE,
    "Edit": FileOperationType.EDIT,
    "Glob": FileOperationType.GLOB,
    "Grep": FileOperationType.GREP,
}


def parse_timestamp(ts: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp string."""
    if not ts:
        return None
    try:
        # Handle both formats: with and without timezone
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


# ==============================================================================
# Proxy Buffer Extraction Functions
#
# These functions extract transcript data from the API proxy buffer format.
# The proxy buffer captures API request/response pairs at the gateway layer,
# providing a stable and format-independent source for checkpoint creation.
# ==============================================================================


def extract_session_metadata_from_proxy_buffer(
    entries: list[dict[str, Any]],
    container_id: str | None = None,
) -> SessionMetadata:
    """
    Extract session metadata from proxy buffer entries.

    Args:
        entries: List of parsed proxy buffer entries
        container_id: Optional container ID to use as session ID

    Returns:
        SessionMetadata with extracted information
    """
    started_at: datetime | None = None
    ended_at: datetime | None = None
    model: str | None = None

    for entry in entries:
        # Get timestamp for start/end tracking
        ts = parse_timestamp(entry.get("timestamp"))
        if ts:
            if started_at is None or ts < started_at:
                started_at = ts
            if ended_at is None or ts > ended_at:
                ended_at = ts

        # Get model from request or response
        if not model:
            model = entry.get("request", {}).get("model")
        if not model:
            model = entry.get("response", {}).get("model")

    # Calculate duration
    duration_seconds: float | None = None
    if started_at and ended_at:
        duration_seconds = (ended_at - started_at).total_seconds()

    # Use current time if no started_at found
    if started_at is None:
        started_at = datetime.now(UTC)

    return SessionMetadata(
        session_id=container_id or "unknown",
        container_id=container_id,
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=duration_seconds,
        model=model,
    )


def extract_messages_from_proxy_buffer(
    entries: list[dict[str, Any]],
    max_content_length: int = 25000,
) -> list[Message]:
    """
    Extract messages from proxy buffer entries.

    Args:
        entries: List of parsed proxy buffer entries
        max_content_length: Maximum length for message content before summarizing

    Returns:
        List of Message objects
    """
    messages = []

    for entry in entries:
        ts = parse_timestamp(entry.get("timestamp"))

        # Extract user messages from request
        request = entry.get("request", {})
        req_messages = request.get("messages", [])

        for msg in req_messages:
            role_str = msg.get("role", "")
            content = msg.get("content", "")

            if role_str == "user":
                if isinstance(content, list):
                    # Content may be a list of content blocks
                    content = " ".join(
                        c.get("text", str(c)) if isinstance(c, dict) else str(c) for c in content
                    )

                content_summary = None
                if len(str(content)) > max_content_length:
                    content_summary = f"[Content truncated: {len(str(content))} characters]"
                    content = str(content)[:max_content_length] + "..."

                messages.append(
                    Message(
                        role=MessageRole.USER,
                        content=str(content),
                        content_summary=content_summary,
                        timestamp=ts or datetime.now(UTC),
                    )
                )

        # Extract assistant response
        response = entry.get("response", {})
        response_content = response.get("content", [])

        for block in response_content:
            if not isinstance(block, dict):
                continue

            block_type = block.get("type", "")

            if block_type == "text":
                text = block.get("text", "")
                content_summary = None
                if len(text) > max_content_length:
                    content_summary = f"[Content truncated: {len(text)} characters]"
                    text = text[:max_content_length] + "..."

                messages.append(
                    Message(
                        role=MessageRole.ASSISTANT,
                        content=text,
                        content_summary=content_summary,
                        timestamp=ts or datetime.now(UTC),
                    )
                )

            elif block_type == "tool_use":
                # Tool use is captured separately in tool_calls
                pass

    return messages


def extract_tool_calls_from_proxy_buffer(
    entries: list[dict[str, Any]],
    max_param_length: int = 2500,
    max_result_length: int = 1500,
) -> tuple[list[ToolCall], list[FileOperation]]:
    """
    Extract tool calls and file operations from proxy buffer entries.

    Args:
        entries: List of parsed proxy buffer entries
        max_param_length: Maximum length for parameter values
        max_result_length: Maximum length for result summaries

    Returns:
        Tuple of (tool_calls, file_operations)

    Note:
        Tool result matching is order-dependent: tool_use blocks from responses are
        processed first, then tool_result blocks from subsequent requests are matched.
        This works correctly because the proxy buffer is append-only and entries are
        read sequentially. Tool results always appear in requests that follow the
        response containing the corresponding tool_use.
    """
    tool_calls = []
    file_operations = []

    # Track tool_use_id -> (tool_call, response_timestamp) for result matching
    tool_use_map: dict[str, ToolCall] = {}
    tool_use_timestamps: dict[str, datetime] = {}

    for entry in entries:
        ts = parse_timestamp(entry.get("timestamp"))

        # Extract tool uses from response
        response = entry.get("response", {})
        response_content = response.get("content", [])

        for block in response_content:
            if not isinstance(block, dict):
                continue

            if block.get("type") == "tool_use":
                tool_name = block.get("name", "")
                tool_use_id = block.get("id", "")
                params = block.get("input", {})

                # Log warning if tool input failed to parse during streaming
                if block.get("input_parse_error"):
                    raw_input = block.get("raw_partial_input", "")
                    logger.warning(
                        "Tool call has incomplete input due to streaming parse failure: "
                        "tool=%s id=%s raw_input_preview=%s",
                        tool_name,
                        tool_use_id,
                        raw_input[:100] + "..." if len(raw_input) > 100 else raw_input,
                    )

                # Truncate large parameter values
                truncated_params = {}
                for key, value in params.items():
                    str_value = str(value)
                    if len(str_value) > max_param_length:
                        truncated_params[key] = str_value[:max_param_length] + "..."
                    else:
                        truncated_params[key] = value

                tool_call = ToolCall(
                    name=tool_name,
                    tool_use_id=tool_use_id,
                    parameters=truncated_params,
                    timestamp=ts or datetime.now(UTC),
                )
                tool_calls.append(tool_call)
                tool_use_map[tool_use_id] = tool_call
                if ts and tool_use_id:
                    tool_use_timestamps[tool_use_id] = ts

                # Extract file operations from tool calls
                if tool_name in TOOL_TO_FILE_OP:
                    file_path = params.get("file_path") or params.get("path", "")
                    if file_path:
                        file_operations.append(
                            FileOperation(
                                path=file_path,
                                operation=TOOL_TO_FILE_OP[tool_name],
                                timestamp=ts,
                            )
                        )

        # Extract tool results from request messages
        request = entry.get("request", {})
        req_messages = request.get("messages", [])

        for msg in req_messages:
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue

            for block in content:
                if not isinstance(block, dict):
                    continue

                if block.get("type") == "tool_result":
                    tool_use_id = block.get("tool_use_id", "")
                    is_error = block.get("is_error", False)
                    result_content = block.get("content", "")

                    if isinstance(result_content, list):
                        result_content = " ".join(
                            c.get("text", str(c)) if isinstance(c, dict) else str(c)
                            for c in result_content
                        )

                    if tool_use_id and tool_use_id in tool_use_map:
                        tool_call = tool_use_map[tool_use_id]
                        result_str = str(result_content)
                        if len(result_str) > max_result_length:
                            tool_call.result_summary = result_str[:max_result_length] + "..."
                        else:
                            tool_call.result_summary = result_str if result_str else None
                        tool_call.success = not is_error

                        # Compute approximate duration from tool_use response
                        # timestamp to tool_result request timestamp
                        if ts and tool_use_id in tool_use_timestamps:
                            use_ts = tool_use_timestamps[tool_use_id]
                            delta = (ts - use_ts).total_seconds()
                            if delta >= 0:
                                tool_call.duration_ms = delta * 1000

    return tool_calls, file_operations


def extract_token_usage_from_proxy_buffer(entries: list[dict[str, Any]]) -> TokenUsage:
    """
    Extract token usage from proxy buffer entries.

    Args:
        entries: List of parsed proxy buffer entries

    Returns:
        TokenUsage with aggregated totals
    """
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_creation = 0

    for entry in entries:
        response = entry.get("response", {})
        usage = response.get("usage", {})

        if not usage:
            continue

        # Aggregate token counts
        total_input += usage.get("input_tokens", 0)
        total_output += usage.get("output_tokens", 0)
        total_cache_read += usage.get("cache_read_input_tokens", 0)
        total_cache_creation += usage.get("cache_creation_input_tokens", 0)

    # Calculate totals
    total_tokens = total_input + total_output

    # Estimate cost (based on Claude Opus 4.5 pricing as of early 2026)
    # Input: $15/MTok, Output: $75/MTok, Cache read: $1.5/MTok
    estimated_cost = None
    if total_tokens > 0:
        input_cost = (total_input / 1_000_000) * 15.0
        output_cost = (total_output / 1_000_000) * 75.0
        cache_read_cost = (total_cache_read / 1_000_000) * 1.5
        estimated_cost = input_cost + output_cost + cache_read_cost

    return TokenUsage(
        input_tokens=total_input,
        output_tokens=total_output,
        cache_read_tokens=total_cache_read,
        cache_creation_tokens=total_cache_creation,
        total_tokens=total_tokens,
        estimated_cost_usd=estimated_cost,
    )


def extract_transcript_from_proxy_buffer(
    buffer_path: Path,
    container_id: str | None = None,
    max_content_length: int = 25000,
    max_param_length: int = 2500,
    max_result_length: int = 1500,
) -> tuple[SessionMetadata, Transcript, list[ToolCall], list[FileOperation], TokenUsage]:
    """
    Extract full transcript data from a proxy buffer file.

    The proxy buffer contains API request/response pairs captured at the gateway.
    This is the primary source for transcript extraction as it provides a stable
    format independent of Claude Code internals.

    Args:
        buffer_path: Path to the proxy buffer JSONL file
        container_id: Optional container ID for session metadata. If not provided,
            it's inferred from the buffer filename stem (e.g., "abc123" from
            "/tmp/egg-transcripts/abc123.jsonl"). This fallback supports standalone
            testing and ad-hoc transcript extraction where the container context
            is not available.
        max_content_length: Maximum length for message content
        max_param_length: Maximum length for tool parameter values
        max_result_length: Maximum length for tool result summaries

    Returns:
        Tuple of (session_metadata, transcript, tool_calls, file_operations, token_usage)

    Raises:
        TranscriptExtractError: If the file cannot be parsed
    """
    try:
        entries = []
        with open(buffer_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    # Only process api_turn entries
                    if entry.get("type") == "api_turn":
                        entries.append(entry)
                except json.JSONDecodeError:
                    # Skip malformed lines
                    continue

        if not entries:
            msg = f"No valid API turn entries found in {buffer_path}"
            raise TranscriptExtractError(msg)

        # Infer container_id from path if not provided
        if container_id is None:
            container_id = buffer_path.stem  # Filename without extension

        # Extract all components
        session_metadata = extract_session_metadata_from_proxy_buffer(entries, container_id)
        messages = extract_messages_from_proxy_buffer(entries, max_content_length)
        tool_calls, file_operations = extract_tool_calls_from_proxy_buffer(
            entries, max_param_length, max_result_length
        )
        token_usage = extract_token_usage_from_proxy_buffer(entries)

        # Build transcript
        transcript = Transcript(
            messages=messages,
            message_count=len(messages),
            truncated=False,
        )

        return session_metadata, transcript, tool_calls, file_operations, token_usage

    except FileNotFoundError as e:
        msg = f"Proxy buffer file not found: {buffer_path}"
        raise TranscriptExtractError(msg) from e
    except Exception as e:
        if isinstance(e, TranscriptExtractError):
            raise
        msg = f"Error extracting transcript from proxy buffer {buffer_path}: {e}"
        raise TranscriptExtractError(msg) from e


def get_proxy_buffer_path(container_id: str) -> Path:
    """
    Get the default proxy buffer path for a container ID.

    Args:
        container_id: Container ID (validated to prevent path traversal)

    Returns:
        Path to the buffer file

    Raises:
        ValueError: If container_id contains path traversal characters
    """
    # Defense in depth: validate container_id to prevent path traversal
    # Even though container IDs come from session manager, we should still validate
    if not container_id or "/" in container_id or "\\" in container_id or ".." in container_id:
        raise ValueError(f"Invalid container_id: {container_id!r}")

    base_dir = Path("/tmp/egg-transcripts")
    buffer_path = base_dir / f"{container_id}.jsonl"

    # Additional check: ensure resolved path is within base directory
    try:
        buffer_path.resolve().relative_to(base_dir.resolve())
    except ValueError as e:
        raise ValueError(
            f"Invalid container_id results in path outside buffer directory: {container_id!r}"
        ) from e

    return buffer_path
