"""Tests for transcript_buffer module - API traffic capture."""

import threading
from unittest.mock import patch

from transcript_buffer import (
    MAX_MESSAGE_CONTENT_LENGTH,
    TranscriptBuffer,
    cleanup_transcript_buffer,
    get_buffer_path,
    get_transcript_buffer,
)


class TestTranscriptBuffer:
    """Tests for TranscriptBuffer class."""

    def test_buffer_path(self, tmp_path):
        """Test that buffer path is correctly computed."""
        buffer = TranscriptBuffer("test-container-123", buffer_dir=tmp_path)
        assert buffer.buffer_path == tmp_path / "test-container-123.jsonl"

    def test_write_api_turn_creates_file(self, tmp_path):
        """Test that writing an API turn creates the buffer file."""
        buffer = TranscriptBuffer("test-container", buffer_dir=tmp_path)

        success = buffer.write_api_turn(
            request_body={"model": "claude-opus-4-5", "messages": []},
            response_content=[{"type": "text", "text": "Hello!"}],
            response_usage={"input_tokens": 100, "output_tokens": 50},
            response_model="claude-opus-4-5",
            stop_reason="end_turn",
            duration_ms=150.0,
            streaming=False,
        )

        assert success is True
        assert buffer.buffer_path.exists()

    def test_write_api_turn_content(self, tmp_path):
        """Test that API turn content is correctly written."""
        buffer = TranscriptBuffer("test-container", buffer_dir=tmp_path)

        buffer.write_api_turn(
            request_body={
                "model": "claude-opus-4-5",
                "messages": [{"role": "user", "content": "Hello"}],
                "system": "You are helpful.",
            },
            response_content=[{"type": "text", "text": "Hi there!"}],
            response_usage={"input_tokens": 100, "output_tokens": 50},
            response_model="claude-opus-4-5",
            stop_reason="end_turn",
            duration_ms=150.0,
            streaming=False,
        )

        entries = buffer.read_entries()
        assert len(entries) == 1

        entry = entries[0]
        assert entry["type"] == "api_turn"
        assert "timestamp" in entry
        assert entry["streaming"] is False
        assert entry["duration_ms"] == 150.0

        # Check request
        assert entry["request"]["model"] == "claude-opus-4-5"
        assert len(entry["request"]["messages"]) == 1
        assert entry["request"]["system"] == "You are helpful."

        # Check response
        assert entry["response"]["content"] == [{"type": "text", "text": "Hi there!"}]
        assert entry["response"]["model"] == "claude-opus-4-5"
        assert entry["response"]["stop_reason"] == "end_turn"
        assert entry["response"]["usage"] == {"input_tokens": 100, "output_tokens": 50}

    def test_write_multiple_api_turns(self, tmp_path):
        """Test writing multiple API turns to the same buffer."""
        buffer = TranscriptBuffer("test-container", buffer_dir=tmp_path)

        for i in range(3):
            buffer.write_api_turn(
                request_body={"model": "claude", "messages": [{"role": "user", "content": f"Message {i}"}]},
                response_content=[{"type": "text", "text": f"Response {i}"}],
                response_usage={"input_tokens": 100 + i, "output_tokens": 50 + i},
                streaming=False,
            )

        entries = buffer.read_entries()
        assert len(entries) == 3

    def test_truncate_long_message_content(self, tmp_path):
        """Test that long message content is truncated."""
        buffer = TranscriptBuffer("test-container", buffer_dir=tmp_path)

        long_content = "x" * (MAX_MESSAGE_CONTENT_LENGTH + 1000)
        buffer.write_api_turn(
            request_body={
                "model": "claude",
                "messages": [{"role": "user", "content": long_content}],
            },
            response_content=None,
            response_usage=None,
            streaming=False,
        )

        entries = buffer.read_entries()
        message_content = entries[0]["request"]["messages"][0]["content"]
        assert len(message_content) <= MAX_MESSAGE_CONTENT_LENGTH + 10  # +10 for "..."
        assert message_content.endswith("...")

    def test_truncate_tools_to_names(self, tmp_path):
        """Test that tools are summarized to just names."""
        buffer = TranscriptBuffer("test-container", buffer_dir=tmp_path)

        buffer.write_api_turn(
            request_body={
                "model": "claude",
                "messages": [],
                "tools": [
                    {"name": "Bash", "type": "function", "description": "Run bash commands", "input_schema": {}},
                    {"name": "Read", "type": "function", "description": "Read files", "input_schema": {}},
                ],
            },
            response_content=None,
            response_usage=None,
            streaming=False,
        )

        entries = buffer.read_entries()
        tools = entries[0]["request"]["tools"]
        assert len(tools) == 2
        assert tools[0] == {"name": "Bash", "type": "function"}
        assert tools[1] == {"name": "Read", "type": "function"}

    def test_read_entries_empty_file(self, tmp_path):
        """Test reading from non-existent buffer returns empty list."""
        buffer = TranscriptBuffer("nonexistent", buffer_dir=tmp_path)
        entries = buffer.read_entries()
        assert entries == []

    def test_read_entries_handles_malformed_lines(self, tmp_path):
        """Test that malformed lines are skipped when reading."""
        buffer = TranscriptBuffer("test-container", buffer_dir=tmp_path)

        # Write some valid entries
        buffer.write_api_turn(
            request_body={"model": "claude", "messages": []},
            response_content=None,
            response_usage=None,
            streaming=False,
        )

        # Append malformed line directly
        with open(buffer.buffer_path, "a") as f:
            f.write("this is not json\n")
            f.write('{"valid": "entry"}\n')

        entries = buffer.read_entries()
        # Should have 2 entries: the API turn and the valid JSON (not malformed)
        assert len(entries) == 2

    def test_clear_removes_file(self, tmp_path):
        """Test that clear removes the buffer file."""
        buffer = TranscriptBuffer("test-container", buffer_dir=tmp_path)

        buffer.write_api_turn(
            request_body={"model": "claude", "messages": []},
            response_content=None,
            response_usage=None,
            streaming=False,
        )
        assert buffer.buffer_path.exists()

        success = buffer.clear()
        assert success is True
        assert not buffer.buffer_path.exists()

    def test_clear_nonexistent_file(self, tmp_path):
        """Test that clear succeeds even if file doesn't exist."""
        buffer = TranscriptBuffer("nonexistent", buffer_dir=tmp_path)
        success = buffer.clear()
        assert success is True

    def test_get_stats(self, tmp_path):
        """Test getting buffer statistics."""
        buffer = TranscriptBuffer("test-container", buffer_dir=tmp_path)

        # Stats for non-existent file
        stats = buffer.get_stats()
        assert stats["container_id"] == "test-container"
        assert stats["size_bytes"] == 0

        # Write an entry
        buffer.write_api_turn(
            request_body={"model": "claude", "messages": []},
            response_content=None,
            response_usage=None,
            streaming=False,
        )

        stats = buffer.get_stats()
        assert stats["size_bytes"] > 0
        assert "max_size" in stats


class TestBufferRotation:
    """Tests for buffer rotation (size limiting)."""

    def test_rotation_when_size_exceeded(self, tmp_path):
        """Test that buffer is rotated when it exceeds max size."""
        # Use small max size for testing
        small_max = 1000
        buffer = TranscriptBuffer("test-container", buffer_dir=tmp_path, max_size=small_max)

        # Write entries until we exceed the limit
        for i in range(20):
            buffer.write_api_turn(
                request_body={"model": "claude", "messages": [{"role": "user", "content": f"Message {i} with padding"}]},
                response_content=[{"type": "text", "text": f"Response {i} with some padding text"}],
                response_usage={"input_tokens": 100, "output_tokens": 50},
                streaming=False,
            )

        # Check that file size is under the limit
        # (may slightly exceed due to last write before rotation)
        entries = buffer.read_entries()
        # Should have fewer entries than we wrote due to rotation
        assert len(entries) < 20

        # Check stats show entries were dropped
        stats = buffer.get_stats()
        assert stats["entries_dropped"] > 0

    def test_rotation_keeps_newest_entries(self, tmp_path):
        """Test that rotation keeps the newest entries."""
        small_max = 500
        buffer = TranscriptBuffer("test-container", buffer_dir=tmp_path, max_size=small_max)

        # Write entries with identifiable content
        for i in range(10):
            buffer.write_api_turn(
                request_body={"model": "claude", "messages": [{"role": "user", "content": f"Message-{i}"}]},
                response_content=[{"type": "text", "text": f"Response-{i}"}],
                response_usage={"input_tokens": i},
                streaming=False,
            )

        entries = buffer.read_entries()
        # Newest entries should be at the end
        # Check that the last entry is the most recent one
        if entries:
            last_entry = entries[-1]
            # Should be one of the higher numbered entries
            usage = last_entry.get("response", {}).get("usage", {})
            assert usage.get("input_tokens", 0) >= 5


class TestConcurrency:
    """Tests for concurrent buffer access."""

    def test_concurrent_writes(self, tmp_path):
        """Test that concurrent writes don't corrupt the buffer."""
        buffer = TranscriptBuffer("test-container", buffer_dir=tmp_path)
        num_threads = 5
        writes_per_thread = 10
        results = []

        def writer(thread_id):
            for i in range(writes_per_thread):
                success = buffer.write_api_turn(
                    request_body={"model": "claude", "messages": [{"role": "user", "content": f"Thread-{thread_id}-{i}"}]},
                    response_content=[{"type": "text", "text": f"Response-{thread_id}-{i}"}],
                    response_usage={"thread": thread_id, "write": i},
                    streaming=False,
                )
                results.append(success)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All writes should succeed
        assert all(results)

        # All entries should be valid JSON
        entries = buffer.read_entries()
        # Should have at least most of the writes (some might be lost in concurrent rotation)
        assert len(entries) >= num_threads * writes_per_thread // 2


class TestGlobalBufferFunctions:
    """Tests for global buffer management functions."""

    def test_get_transcript_buffer_caching(self, tmp_path):
        """Test that get_transcript_buffer caches buffers."""
        import transcript_buffer

        # Patch buffer dir
        with patch.object(transcript_buffer, "BUFFER_DIR", tmp_path):
            # Clear cache
            transcript_buffer._buffer_cache.clear()

            buffer1 = get_transcript_buffer("container-1")
            buffer2 = get_transcript_buffer("container-1")
            buffer3 = get_transcript_buffer("container-2")

            assert buffer1 is buffer2  # Same container, should be cached
            assert buffer1 is not buffer3  # Different container

    def test_cleanup_transcript_buffer(self, tmp_path):
        """Test that cleanup removes the buffer file."""
        import transcript_buffer

        with patch.object(transcript_buffer, "BUFFER_DIR", tmp_path):
            transcript_buffer._buffer_cache.clear()

            buffer = get_transcript_buffer("cleanup-test")
            buffer.write_api_turn(
                request_body={"model": "claude", "messages": []},
                response_content=None,
                response_usage=None,
                streaming=False,
            )
            assert buffer.buffer_path.exists()

            success = cleanup_transcript_buffer("cleanup-test")
            assert success is True
            assert not buffer.buffer_path.exists()

            # Should also be removed from cache
            assert "cleanup-test" not in transcript_buffer._buffer_cache

    def test_get_buffer_path(self, tmp_path):
        """Test get_buffer_path utility function."""
        import transcript_buffer

        with patch.object(transcript_buffer, "BUFFER_DIR", tmp_path):
            path = get_buffer_path("my-container")
            assert path == tmp_path / "my-container.jsonl"


class TestStreamingCapture:
    """Tests for streaming response handling."""

    def test_streaming_flag_in_entry(self, tmp_path):
        """Test that streaming flag is correctly recorded."""
        buffer = TranscriptBuffer("test-container", buffer_dir=tmp_path)

        buffer.write_api_turn(
            request_body={"model": "claude", "messages": []},
            response_content=[{"type": "text", "text": "Streamed response"}],
            response_usage={"input_tokens": 100, "output_tokens": 50},
            streaming=True,
        )

        entries = buffer.read_entries()
        assert entries[0]["streaming"] is True

    def test_write_without_response(self, tmp_path):
        """Test writing an entry without response (e.g., for errors)."""
        buffer = TranscriptBuffer("test-container", buffer_dir=tmp_path)

        success = buffer.write_api_turn(
            request_body={"model": "claude", "messages": [{"role": "user", "content": "Hello"}]},
            response_content=None,
            response_usage=None,
            streaming=False,
        )

        assert success is True
        entries = buffer.read_entries()
        assert len(entries) == 1
        assert "response" not in entries[0]  # No response key when content and usage are None
