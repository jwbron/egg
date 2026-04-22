"""Tests for the opt-in tracemalloc memory sampler (gateway/mem_trace.py)."""

from __future__ import annotations

import os
import tracemalloc
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _tracemalloc_cleanup():
    """Ensure tracemalloc state is restored between tests — start_if_enabled
    calls tracemalloc.start(), which is process-global."""
    was_tracing = tracemalloc.is_tracing()
    yield
    if tracemalloc.is_tracing() and not was_tracing:
        tracemalloc.stop()


class TestStartIfEnabled:
    def test_returns_false_when_env_unset(self, monkeypatch):
        from gateway.mem_trace import ENABLE_ENV_VAR, start_if_enabled

        monkeypatch.delenv(ENABLE_ENV_VAR, raising=False)
        assert start_if_enabled() is False

    def test_returns_false_for_falsy_values(self, monkeypatch):
        from gateway.mem_trace import ENABLE_ENV_VAR, start_if_enabled

        for value in ("", "0", "false", "no", "off"):
            monkeypatch.setenv(ENABLE_ENV_VAR, value)
            assert start_if_enabled() is False, f"expected False for {value!r}"

    def test_returns_true_and_starts_tracing_for_truthy_values(self, monkeypatch):
        from gateway.mem_trace import ENABLE_ENV_VAR, start_if_enabled

        # Stop any prior sampler thread from bleeding into this test by
        # replacing the thread target with a no-op that returns immediately.
        with patch("gateway.mem_trace._sampler_loop") as mock_loop:
            mock_loop.return_value = None
            monkeypatch.setenv(ENABLE_ENV_VAR, "1")
            assert start_if_enabled() is True
            assert tracemalloc.is_tracing()


class TestReadRssMb:
    def test_reads_rss_on_linux(self):
        """This test only runs meaningfully on Linux (where /proc/self/status
        exists). On other platforms the function returns None, which we accept."""
        from gateway.mem_trace import _read_rss_mb

        result = _read_rss_mb()
        if os.path.exists("/proc/self/status"):
            assert result is not None
            assert result > 0
        else:
            assert result is None


class TestSampleOnce:
    def test_produces_expected_shape(self):
        """Verify the sample record matches the documented schema."""
        from gateway.mem_trace import _sample_once

        # Needs tracemalloc active to take a snapshot.
        started_here = not tracemalloc.is_tracing()
        if started_here:
            tracemalloc.start(5)
        try:
            record = _sample_once(top_n=3)
        finally:
            if started_here:
                tracemalloc.stop()

        assert "rss_mb" in record
        assert "top" in record
        assert len(record["top"]) <= 3
        for entry in record["top"]:
            assert set(entry.keys()) == {"loc", "size_kb", "count"}
            assert isinstance(entry["size_kb"], int)
            assert isinstance(entry["count"], int)
