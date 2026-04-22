"""Tests for the opt-in tracemalloc memory sampler (gateway/mem_trace.py)."""

from __future__ import annotations

import os
import tracemalloc
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _tracemalloc_cleanup():
    """Ensure tracemalloc and module-level _started state are restored between
    tests — start_if_enabled uses process-global tracemalloc and a module-level
    guard."""
    import gateway.mem_trace as _mt

    was_tracing = tracemalloc.is_tracing()
    was_started = _mt._started
    yield
    _mt._started = was_started
    if tracemalloc.is_tracing() and not was_tracing:
        tracemalloc.stop()


class TestStartIfEnabled:
    def test_returns_false_when_env_unset(self, monkeypatch):
        from gateway.mem_trace import ENABLE_ENV_VAR, start_if_enabled

        monkeypatch.delenv(ENABLE_ENV_VAR, raising=False)
        assert start_if_enabled() is False

    def test_returns_false_for_falsy_values(self, monkeypatch):
        import gateway.mem_trace as _mt
        from gateway.mem_trace import ENABLE_ENV_VAR, start_if_enabled

        _mt._started = False
        for value in ("", "0", "false", "no", "off"):
            monkeypatch.setenv(ENABLE_ENV_VAR, value)
            assert start_if_enabled() is False, f"expected False for {value!r}"

    def test_returns_true_and_starts_tracing_for_truthy_values(self, monkeypatch):
        import gateway.mem_trace as _mt
        from gateway.mem_trace import ENABLE_ENV_VAR, start_if_enabled

        # Reset module guard so this test can start fresh.
        _mt._started = False
        # Stop any prior sampler thread from bleeding into this test by
        # replacing the thread target with a no-op that returns immediately.
        with patch("gateway.mem_trace._sampler_loop") as mock_loop:
            mock_loop.return_value = None
            monkeypatch.setenv(ENABLE_ENV_VAR, "1")
            assert start_if_enabled() is True
            assert tracemalloc.is_tracing()

    def test_second_call_is_noop(self, monkeypatch):
        import gateway.mem_trace as _mt
        from gateway.mem_trace import ENABLE_ENV_VAR, start_if_enabled

        _mt._started = False
        with patch("gateway.mem_trace._sampler_loop") as mock_loop:
            mock_loop.return_value = None
            monkeypatch.setenv(ENABLE_ENV_VAR, "1")
            assert start_if_enabled() is True
            assert start_if_enabled() is False  # second call is a no-op

    def test_interval_clamped_to_minimum(self, monkeypatch):
        import gateway.mem_trace as _mt
        from gateway.mem_trace import ENABLE_ENV_VAR, INTERVAL_ENV_VAR, start_if_enabled

        _mt._started = False
        with patch("gateway.mem_trace._sampler_loop") as mock_loop:
            mock_loop.return_value = None
            monkeypatch.setenv(ENABLE_ENV_VAR, "1")
            monkeypatch.setenv(INTERVAL_ENV_VAR, "0")
            start_if_enabled()
            # The interval passed to _sampler_loop should be clamped to 1.0
            mock_loop.assert_called_once()
            actual_interval = mock_loop.call_args[0][0]
            assert actual_interval == 1.0


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
