"""
Transcript extractor for Claude Code JSONL format.

Parses Claude Code session files (~/.claude/projects/{project}/{session}.jsonl)
and extracts structured transcript data including messages, tool calls, and
token usage.
"""

import json
from datetime import datetime
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


class TranscriptExtractError(Exception):
    """Error extracting transcript from session file."""

    pass


# Mapping of Claude Code tool names to file operation types
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


def extract_session_metadata(entries: list[dict[str, Any]]) -> SessionMetadata:
    """
    Extract session metadata from JSONL entries.

    Args:
        entries: List of parsed JSONL entries

    Returns:
        SessionMetadata with extracted information
    """
    session_id = ""
    started_at: datetime | None = None
    ended_at: datetime | None = None
    model: str | None = None
    claude_code_version: str | None = None

    for entry in entries:
        # Get session ID from any entry
        if "sessionId" in entry and not session_id:
            session_id = entry["sessionId"]

        # Get version from any entry
        if "version" in entry and not claude_code_version:
            claude_code_version = entry["version"]

        # Get timestamp for start/end tracking
        ts = parse_timestamp(entry.get("timestamp"))
        if ts:
            if started_at is None or ts < started_at:
                started_at = ts
            if ended_at is None or ts > ended_at:
                ended_at = ts

        # Get model from assistant messages
        if entry.get("type") == "assistant":
            msg = entry.get("message", {})
            if "model" in msg and not model:
                model = msg["model"]

    # Calculate duration
    duration_seconds: float | None = None
    if started_at and ended_at:
        duration_seconds = (ended_at - started_at).total_seconds()

    # Use current time if no started_at found
    if started_at is None:
        started_at = datetime.now()

    return SessionMetadata(
        session_id=session_id or "unknown",
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=duration_seconds,
        model=model,
        claude_code_version=claude_code_version,
    )


def extract_messages(
    entries: list[dict[str, Any]],
    max_content_length: int = 10000,
) -> list[Message]:
    """
    Extract messages from JSONL entries.

    Args:
        entries: List of parsed JSONL entries
        max_content_length: Maximum length for message content before summarizing

    Returns:
        List of Message objects
    """
    messages = []

    for entry in entries:
        entry_type = entry.get("type")
        ts = parse_timestamp(entry.get("timestamp"))

        if entry_type == "user":
            # User message
            msg = entry.get("message", {})
            content = msg.get("content", "")
            content_summary = None

            if len(content) > max_content_length:
                content_summary = f"[Content truncated: {len(content)} characters]"
                content = content[:max_content_length] + "..."

            if ts:
                messages.append(
                    Message(
                        role=MessageRole.USER,
                        content=content,
                        content_summary=content_summary,
                        timestamp=ts,
                        uuid=entry.get("uuid"),
                    )
                )

        elif entry_type == "assistant":
            # Assistant message (may contain tool calls or text)
            msg = entry.get("message", {})
            content_parts = msg.get("content", [])

            for part in content_parts:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        text = part.get("text", "")
                        content_summary = None
                        if len(text) > max_content_length:
                            content_summary = f"[Content truncated: {len(text)} characters]"
                            text = text[:max_content_length] + "..."

                        if ts:
                            messages.append(
                                Message(
                                    role=MessageRole.ASSISTANT,
                                    content=text,
                                    content_summary=content_summary,
                                    timestamp=ts,
                                    uuid=entry.get("uuid"),
                                )
                            )
                    elif part.get("type") == "tool_use":
                        # Tool use is captured separately in tool_calls
                        pass

        elif entry_type == "tool_result":
            # Tool result message
            msg = entry.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, list):
                # Content may be a list of content blocks
                content = " ".join(
                    str(c.get("text", c) if isinstance(c, dict) else c)
                    for c in content
                )
            content_summary = None
            if len(str(content)) > max_content_length:
                content_summary = f"[Result truncated: {len(str(content))} characters]"
                content = str(content)[:max_content_length] + "..."

            if ts:
                messages.append(
                    Message(
                        role=MessageRole.TOOL_RESULT,
                        content=str(content) if content else None,
                        content_summary=content_summary,
                        timestamp=ts,
                        uuid=entry.get("uuid"),
                    )
                )

    return messages


def extract_tool_calls(
    entries: list[dict[str, Any]],
    max_param_length: int = 1000,
    max_result_length: int = 500,
) -> tuple[list[ToolCall], list[FileOperation]]:
    """
    Extract tool calls and file operations from JSONL entries.

    Args:
        entries: List of parsed JSONL entries
        max_param_length: Maximum length for parameter values
        max_result_length: Maximum length for result summaries

    Returns:
        Tuple of (tool_calls, file_operations)
    """
    tool_calls = []
    file_operations = []

    # Track tool_use_id -> entry for matching results
    tool_use_map: dict[str, dict[str, Any]] = {}

    for entry in entries:
        entry_type = entry.get("type")
        ts = parse_timestamp(entry.get("timestamp"))

        if entry_type == "assistant":
            msg = entry.get("message", {})
            content_parts = msg.get("content", [])

            for part in content_parts:
                if isinstance(part, dict) and part.get("type") == "tool_use":
                    tool_name = part.get("name", "")
                    tool_use_id = part.get("id", "")
                    params = part.get("input", {})

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
                        timestamp=ts or datetime.now(),
                    )
                    tool_calls.append(tool_call)
                    tool_use_map[tool_use_id] = {"tool_call": tool_call, "params": params}

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
                    elif tool_name == "Bash":
                        # Try to extract file paths from bash commands
                        # This is heuristic and won't catch everything
                        pass

        elif entry_type == "tool_result":
            # Match result to tool call
            msg = entry.get("message", {})
            tool_use_id = msg.get("tool_use_id", "")

            if tool_use_id and tool_use_id in tool_use_map:
                tool_data = tool_use_map[tool_use_id]
                tool_call = tool_data["tool_call"]

                # Get result content
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        str(c.get("text", c) if isinstance(c, dict) else c)
                        for c in content
                    )

                # Check for errors
                is_error = msg.get("is_error", False)

                # Create result summary
                result_str = str(content)
                if len(result_str) > max_result_length:
                    result_summary = result_str[:max_result_length] + "..."
                else:
                    result_summary = result_str if result_str else None

                # Update tool call with result info
                tool_call.result_summary = result_summary
                tool_call.success = not is_error

    return tool_calls, file_operations


def extract_token_usage(entries: list[dict[str, Any]]) -> TokenUsage:
    """
    Extract token usage from JSONL entries.

    Args:
        entries: List of parsed JSONL entries

    Returns:
        TokenUsage with aggregated totals
    """
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_creation = 0

    for entry in entries:
        if entry.get("type") != "assistant":
            continue

        msg = entry.get("message", {})
        usage = msg.get("usage", {})

        if not usage:
            continue

        # Aggregate token counts
        total_input += usage.get("input_tokens", 0)
        total_output += usage.get("output_tokens", 0)
        total_cache_read += usage.get("cache_read_input_tokens", 0)

        # Handle nested cache_creation structure
        cache_creation = usage.get("cache_creation", {})
        if isinstance(cache_creation, dict):
            total_cache_creation += cache_creation.get("ephemeral_5m_input_tokens", 0)
            total_cache_creation += cache_creation.get("ephemeral_1h_input_tokens", 0)
        else:
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


def extract_transcript_from_jsonl(
    jsonl_path: Path,
    max_content_length: int = 10000,
    max_param_length: int = 1000,
    max_result_length: int = 500,
) -> tuple[SessionMetadata, Transcript, list[ToolCall], list[FileOperation], TokenUsage]:
    """
    Extract full transcript data from a Claude Code JSONL session file.

    Args:
        jsonl_path: Path to the JSONL session file
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
        with open(jsonl_path) as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    entries.append(entry)
                except json.JSONDecodeError as e:
                    # Skip malformed lines but continue
                    continue

        if not entries:
            msg = f"No valid entries found in {jsonl_path}"
            raise TranscriptExtractError(msg)

        # Extract all components
        session_metadata = extract_session_metadata(entries)
        messages = extract_messages(entries, max_content_length)
        tool_calls, file_operations = extract_tool_calls(
            entries, max_param_length, max_result_length
        )
        token_usage = extract_token_usage(entries)

        # Build transcript
        transcript = Transcript(
            messages=messages,
            message_count=len(messages),
            truncated=False,
        )

        return session_metadata, transcript, tool_calls, file_operations, token_usage

    except FileNotFoundError as e:
        msg = f"Session file not found: {jsonl_path}"
        raise TranscriptExtractError(msg) from e
    except Exception as e:
        if isinstance(e, TranscriptExtractError):
            raise
        msg = f"Error extracting transcript from {jsonl_path}: {e}"
        raise TranscriptExtractError(msg) from e


def find_session_file(
    session_id: str | None = None,
    project_path: Path | None = None,
    claude_projects_dir: Path | None = None,
) -> Path | None:
    """
    Find the JSONL session file for the current or specified session.

    Args:
        session_id: Optional specific session ID to find
        project_path: Optional project path to search under
        claude_projects_dir: Optional base directory for Claude projects

    Returns:
        Path to the session JSONL file, or None if not found
    """
    # Default Claude projects directory
    if claude_projects_dir is None:
        claude_projects_dir = Path.home() / ".claude" / "projects"

    if not claude_projects_dir.exists():
        return None

    # If session_id is provided, search for that specific session
    if session_id:
        for project_dir in claude_projects_dir.iterdir():
            if not project_dir.is_dir():
                continue
            session_file = project_dir / f"{session_id}.jsonl"
            if session_file.exists():
                return session_file
        return None

    # If project_path is provided, use it to find the project directory
    if project_path:
        # Claude Code encodes project paths by replacing / with -
        project_name = str(project_path).replace("/", "-")
        if project_name.startswith("-"):
            project_name = project_name[1:]
        project_dir = claude_projects_dir / project_name

        if project_dir.exists():
            # Find the most recent session file
            session_files = list(project_dir.glob("*.jsonl"))
            if session_files:
                # Sort by modification time, most recent first
                session_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                return session_files[0]

    # Fallback: find the most recently modified session file across all projects
    most_recent: Path | None = None
    most_recent_time = 0.0

    for project_dir in claude_projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        for session_file in project_dir.glob("*.jsonl"):
            mtime = session_file.stat().st_mtime
            if mtime > most_recent_time:
                most_recent = session_file
                most_recent_time = mtime

    return most_recent
