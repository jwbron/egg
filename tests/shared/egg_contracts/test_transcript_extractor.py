"""Tests for transcript extraction from proxy buffer format."""

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from egg_contracts.transcript_extractor import (
    TranscriptExtractError,
    extract_messages_from_proxy_buffer,
    extract_session_metadata_from_proxy_buffer,
    extract_token_usage_from_proxy_buffer,
    extract_tool_calls_from_proxy_buffer,
    extract_transcript_from_proxy_buffer,
    get_proxy_buffer_path,
)
from egg_contracts.checkpoints import FileOperationType, MessageRole


class TestExtractSessionMetadataFromProxyBuffer:
    """Tests for extract_session_metadata_from_proxy_buffer."""

    def test_extract_basic_metadata(self):
        """Test extracting basic session metadata."""
        entries = [
            {
                "timestamp": "2026-02-11T10:00:00.000000Z",
                "type": "api_turn",
                "request": {"model": "claude-opus-4-5-20251101"},
                "response": {"model": "claude-opus-4-5-20251101"},
            },
            {
                "timestamp": "2026-02-11T10:05:00.000000Z",
                "type": "api_turn",
                "request": {"model": "claude-opus-4-5-20251101"},
            },
        ]

        metadata = extract_session_metadata_from_proxy_buffer(entries, "container-123")

        assert metadata.session_id == "container-123"
        assert metadata.container_id == "container-123"
        assert metadata.model == "claude-opus-4-5-20251101"
        assert metadata.duration_seconds == 300.0  # 5 minutes

    def test_extract_metadata_without_container_id(self):
        """Test extracting metadata without container ID."""
        entries = [
            {
                "timestamp": "2026-02-11T10:00:00.000000Z",
                "type": "api_turn",
                "request": {"model": "claude-3"},
            }
        ]

        metadata = extract_session_metadata_from_proxy_buffer(entries)

        assert metadata.session_id == "unknown"
        assert metadata.container_id is None

    def test_extract_metadata_empty_entries(self):
        """Test extracting metadata from empty entries."""
        metadata = extract_session_metadata_from_proxy_buffer([])

        assert metadata.session_id == "unknown"
        assert metadata.started_at is not None  # Falls back to now


class TestExtractMessagesFromProxyBuffer:
    """Tests for extract_messages_from_proxy_buffer."""

    def test_extract_user_message(self):
        """Test extracting user messages."""
        entries = [
            {
                "timestamp": "2026-02-11T10:00:00.000000Z",
                "type": "api_turn",
                "request": {
                    "messages": [{"role": "user", "content": "Hello, Claude!"}]
                },
            }
        ]

        messages = extract_messages_from_proxy_buffer(entries)

        assert len(messages) == 1
        assert messages[0].role == MessageRole.USER
        assert messages[0].content == "Hello, Claude!"

    def test_extract_assistant_text_response(self):
        """Test extracting assistant text responses."""
        entries = [
            {
                "timestamp": "2026-02-11T10:00:00.000000Z",
                "type": "api_turn",
                "request": {"messages": []},
                "response": {
                    "content": [{"type": "text", "text": "Hello! How can I help?"}]
                },
            }
        ]

        messages = extract_messages_from_proxy_buffer(entries)

        assert len(messages) == 1
        assert messages[0].role == MessageRole.ASSISTANT
        assert messages[0].content == "Hello! How can I help?"

    def test_extract_user_message_with_content_blocks(self):
        """Test extracting user messages with content blocks."""
        entries = [
            {
                "timestamp": "2026-02-11T10:00:00.000000Z",
                "type": "api_turn",
                "request": {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "Look at this: "},
                                {"type": "image", "source": {"data": "..."}},
                            ],
                        }
                    ]
                },
            }
        ]

        messages = extract_messages_from_proxy_buffer(entries)

        assert len(messages) == 1
        assert "Look at this" in messages[0].content

    def test_truncate_long_content(self):
        """Test that long content is truncated."""
        long_text = "x" * 15000
        entries = [
            {
                "timestamp": "2026-02-11T10:00:00.000000Z",
                "type": "api_turn",
                "request": {"messages": [{"role": "user", "content": long_text}]},
            }
        ]

        messages = extract_messages_from_proxy_buffer(entries, max_content_length=10000)

        assert len(messages[0].content) <= 10003  # 10000 + "..."
        assert messages[0].content.endswith("...")
        assert messages[0].content_summary is not None


class TestExtractToolCallsFromProxyBuffer:
    """Tests for extract_tool_calls_from_proxy_buffer."""

    def test_extract_tool_use(self):
        """Test extracting tool use from response."""
        entries = [
            {
                "timestamp": "2026-02-11T10:00:00.000000Z",
                "type": "api_turn",
                "request": {"messages": []},
                "response": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_123",
                            "name": "Bash",
                            "input": {"command": "ls -la"},
                        }
                    ]
                },
            }
        ]

        tool_calls, file_ops = extract_tool_calls_from_proxy_buffer(entries)

        assert len(tool_calls) == 1
        assert tool_calls[0].name == "Bash"
        assert tool_calls[0].tool_use_id == "toolu_123"
        assert tool_calls[0].parameters == {"command": "ls -la"}

    def test_extract_file_operation_from_read(self):
        """Test extracting file operations from Read tool."""
        entries = [
            {
                "timestamp": "2026-02-11T10:00:00.000000Z",
                "type": "api_turn",
                "request": {"messages": []},
                "response": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_456",
                            "name": "Read",
                            "input": {"file_path": "/home/user/test.py"},
                        }
                    ]
                },
            }
        ]

        tool_calls, file_ops = extract_tool_calls_from_proxy_buffer(entries)

        assert len(file_ops) == 1
        assert file_ops[0].path == "/home/user/test.py"
        assert file_ops[0].operation == FileOperationType.READ

    def test_match_tool_result_to_call(self):
        """Test matching tool results to tool calls."""
        entries = [
            {
                "timestamp": "2026-02-11T10:00:00.000000Z",
                "type": "api_turn",
                "request": {"messages": []},
                "response": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_789",
                            "name": "Bash",
                            "input": {"command": "echo hello"},
                        }
                    ]
                },
            },
            {
                "timestamp": "2026-02-11T10:00:01.000000Z",
                "type": "api_turn",
                "request": {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "toolu_789",
                                    "content": "hello",
                                    "is_error": False,
                                }
                            ],
                        }
                    ]
                },
            },
        ]

        tool_calls, _ = extract_tool_calls_from_proxy_buffer(entries)

        assert len(tool_calls) == 1
        assert tool_calls[0].result_summary == "hello"
        assert tool_calls[0].success is True


class TestExtractTokenUsageFromProxyBuffer:
    """Tests for extract_token_usage_from_proxy_buffer."""

    def test_aggregate_token_usage(self):
        """Test aggregating token usage across entries."""
        entries = [
            {
                "type": "api_turn",
                "response": {
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cache_read_input_tokens": 20,
                    }
                },
            },
            {
                "type": "api_turn",
                "response": {
                    "usage": {
                        "input_tokens": 200,
                        "output_tokens": 100,
                    }
                },
            },
        ]

        usage = extract_token_usage_from_proxy_buffer(entries)

        assert usage.input_tokens == 300
        assert usage.output_tokens == 150
        assert usage.cache_read_tokens == 20
        assert usage.total_tokens == 450

    def test_empty_entries_returns_zero(self):
        """Test that empty entries returns zero usage."""
        usage = extract_token_usage_from_proxy_buffer([])

        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.total_tokens == 0


class TestExtractTranscriptFromProxyBuffer:
    """Tests for extract_transcript_from_proxy_buffer."""

    def test_full_extraction(self, tmp_path):
        """Test full transcript extraction from buffer file."""
        buffer_path = tmp_path / "test-container.jsonl"

        entries = [
            {
                "timestamp": "2026-02-11T10:00:00.000000Z",
                "type": "api_turn",
                "request": {
                    "model": "claude-opus-4-5",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
                "response": {
                    "content": [{"type": "text", "text": "Hi there!"}],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }
        ]

        with open(buffer_path, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

        (
            session_metadata,
            transcript,
            tool_calls,
            file_operations,
            token_usage,
        ) = extract_transcript_from_proxy_buffer(buffer_path)

        assert session_metadata.session_id == "test-container"
        assert transcript.message_count == 2  # user + assistant
        assert len(transcript.messages) == 2
        assert token_usage.input_tokens == 10
        assert token_usage.output_tokens == 5

    def test_file_not_found(self, tmp_path):
        """Test error when file not found."""
        with pytest.raises(TranscriptExtractError, match="not found"):
            extract_transcript_from_proxy_buffer(tmp_path / "nonexistent.jsonl")

    def test_empty_file_raises_error(self, tmp_path):
        """Test error when file has no valid entries."""
        buffer_path = tmp_path / "empty.jsonl"
        buffer_path.write_text("")

        with pytest.raises(TranscriptExtractError, match="No valid"):
            extract_transcript_from_proxy_buffer(buffer_path)

    def test_skips_non_api_turn_entries(self, tmp_path):
        """Test that non-api_turn entries are skipped."""
        buffer_path = tmp_path / "mixed.jsonl"

        entries = [
            {"type": "marker", "data": "something"},  # Not an api_turn
            {
                "timestamp": "2026-02-11T10:00:00.000000Z",
                "type": "api_turn",
                "request": {"messages": [{"role": "user", "content": "Hello"}]},
                "response": {"content": [{"type": "text", "text": "Hi"}]},
            },
        ]

        with open(buffer_path, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

        _, transcript, _, _, _ = extract_transcript_from_proxy_buffer(buffer_path)

        # Should only have messages from the api_turn entry
        assert transcript.message_count == 2


class TestGetProxyBufferPath:
    """Tests for get_proxy_buffer_path."""

    def test_returns_correct_path(self):
        """Test that correct buffer path is returned."""
        path = get_proxy_buffer_path("my-container-id")

        assert path == Path("/tmp/egg-transcripts/my-container-id.jsonl")
