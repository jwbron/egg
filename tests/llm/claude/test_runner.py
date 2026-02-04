"""
Tests for llm.claude.runner module.

Tests the _read_lines_unbuffered function which handles reading large outputs
from Claude Code without hitting asyncio's 64KB buffer limit.
"""

import asyncio
import logging

import pytest  # noqa: F401 (used by caplog fixture)
from llm.claude.runner import (
    _BUFFER_WARNING_THRESHOLD,
    _READ_CHUNK_SIZE,
    _read_lines_unbuffered,
)


class MockStreamReader:
    """Mock asyncio.StreamReader for testing.

    Simulates a stream that returns predefined chunks of data.
    """

    def __init__(self, chunks: list[bytes]):
        """Initialize with a list of chunks to return.

        Args:
            chunks: List of byte chunks to return on successive read() calls.
                   Empty bytes (b"") signals EOF.
        """
        self._chunks = list(chunks)
        self._index = 0

    async def read(self, n: int) -> bytes:
        """Return the next chunk of data.

        Args:
            n: Number of bytes to read (ignored, returns full chunk).

        Returns:
            Next chunk of data, or b"" if no more chunks.
        """
        if self._index >= len(self._chunks):
            return b""
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk


class MockStreamReaderWithError:
    """Mock StreamReader that raises an exception on read."""

    def __init__(self, error: Exception):
        self._error = error

    async def read(self, n: int) -> bytes:
        raise self._error


def _run_async(coro):
    """Helper to run async code in tests without pytest-asyncio."""
    return asyncio.get_event_loop().run_until_complete(coro)


async def _collect_lines(stream) -> list[bytes]:
    """Collect all lines from an async generator."""
    return [line async for line in _read_lines_unbuffered(stream)]


class TestReadLinesUnbuffered:
    """Tests for _read_lines_unbuffered function."""

    def test_single_line(self):
        """Test reading a single line."""
        stream = MockStreamReader([b"hello world\n", b""])
        lines = _run_async(_collect_lines(stream))
        assert lines == [b"hello world"]

    def test_multiple_lines_single_chunk(self):
        """Test reading multiple lines from a single chunk."""
        stream = MockStreamReader([b"line1\nline2\nline3\n", b""])
        lines = _run_async(_collect_lines(stream))
        assert lines == [b"line1", b"line2", b"line3"]

    def test_multiple_lines_multiple_chunks(self):
        """Test reading lines split across multiple chunks."""
        stream = MockStreamReader([b"line1\nli", b"ne2\nline3\n", b""])
        lines = _run_async(_collect_lines(stream))
        assert lines == [b"line1", b"line2", b"line3"]

    def test_line_split_across_chunks(self):
        """Test a single line split across multiple chunks."""
        stream = MockStreamReader([b"hello ", b"world ", b"test\n", b""])
        lines = _run_async(_collect_lines(stream))
        assert lines == [b"hello world test"]

    def test_empty_lines(self):
        """Test handling of empty lines."""
        stream = MockStreamReader([b"line1\n\nline2\n", b""])
        lines = _run_async(_collect_lines(stream))
        assert lines == [b"line1", b"", b"line2"]

    def test_no_trailing_newline(self):
        """Test handling data without trailing newline (yields remaining buffer)."""
        stream = MockStreamReader([b"line1\nlast line", b""])
        lines = _run_async(_collect_lines(stream))
        assert lines == [b"line1", b"last line"]

    def test_empty_stream(self):
        """Test handling empty stream."""
        stream = MockStreamReader([b""])
        lines = _run_async(_collect_lines(stream))
        assert lines == []

    def test_large_line(self):
        """Test handling a line larger than the read chunk size."""
        # Create a line larger than _READ_CHUNK_SIZE
        large_content = b"x" * (_READ_CHUNK_SIZE + 1000)
        large_line = large_content + b"\n"

        # Split into multiple chunks
        chunks = [
            large_line[:_READ_CHUNK_SIZE],
            large_line[_READ_CHUNK_SIZE:],
            b"",
        ]
        stream = MockStreamReader(chunks)
        lines = _run_async(_collect_lines(stream))
        assert lines == [large_content]

    def test_read_error_handling(self, caplog):
        """Test that read errors are logged and iteration stops."""
        with caplog.at_level(logging.WARNING):
            stream = MockStreamReaderWithError(OSError("Connection lost"))
            lines = _run_async(_collect_lines(stream))
            assert lines == []
            assert "Error reading stream chunk" in caplog.text

    def test_json_lines(self):
        """Test reading newline-delimited JSON (common Claude output format)."""
        json_data = (
            b'{"type": "system", "subtype": "init"}\n'
            b'{"type": "assistant", "message": {"content": []}}\n'
            b'{"type": "result", "result": "Done"}\n'
        )
        stream = MockStreamReader([json_data, b""])
        lines = _run_async(_collect_lines(stream))
        assert len(lines) == 3
        assert b'"type": "system"' in lines[0]
        assert b'"type": "assistant"' in lines[1]
        assert b'"type": "result"' in lines[2]


class TestBufferWarningThreshold:
    """Tests for buffer size warning functionality."""

    def test_warning_logged_for_large_buffer(self, caplog):
        """Test that a warning is logged when buffer exceeds threshold."""
        with caplog.at_level(logging.WARNING):
            # Create content larger than warning threshold without newlines
            large_chunk_size = _BUFFER_WARNING_THRESHOLD + 1024
            large_content = b"x" * large_chunk_size

            stream = MockStreamReader([large_content, b""])
            lines = _run_async(_collect_lines(stream))

            # Should yield the content (as remaining buffer at EOF)
            assert len(lines) == 1
            assert len(lines[0]) == large_chunk_size

            # Should have logged a warning
            assert "Stream buffer exceeded" in caplog.text
            assert "without newline" in caplog.text

    def test_no_warning_for_normal_buffer(self, caplog):
        """Test that no warning is logged for normal-sized lines."""
        with caplog.at_level(logging.WARNING):
            # Create content smaller than warning threshold
            normal_content = b"x" * 1000 + b"\n"

            stream = MockStreamReader([normal_content, b""])
            lines = _run_async(_collect_lines(stream))

            assert len(lines) == 1
            assert "Stream buffer exceeded" not in caplog.text

    def test_warning_resets_after_newline(self, caplog):
        """Test that warning state resets after a line is extracted.

        When a newline is found and the buffer is processed, the warning
        state should reset so it can warn again if the buffer grows large
        again.
        """
        with caplog.at_level(logging.WARNING):
            # First chunk: large content that triggers warning
            large_part = b"x" * (_BUFFER_WARNING_THRESHOLD + 1024)
            # Then a newline and more content
            with_newline = large_part + b"\nnormal line\n"

            stream = MockStreamReader([with_newline, b""])
            lines = _run_async(_collect_lines(stream))

            assert len(lines) == 2
            # Warning should have been logged once
            assert caplog.text.count("Stream buffer exceeded") == 1


class TestConstants:
    """Tests for module constants."""

    def test_read_chunk_size(self):
        """Test that read chunk size is 1MB."""
        assert _READ_CHUNK_SIZE == 1024 * 1024

    def test_buffer_warning_threshold(self):
        """Test that buffer warning threshold is 50MB."""
        assert _BUFFER_WARNING_THRESHOLD == 50 * 1024 * 1024
