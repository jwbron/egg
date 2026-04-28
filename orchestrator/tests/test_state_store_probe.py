"""
Unit tests for :mod:`state_store_probe`.

Covers the cache lifecycle, watchdog staleness, exception isolation,
and clean shutdown semantics introduced in #2191. Does not exercise
the live state-store probe — that's covered transitively by
``test_state_store_wedge_propagation.py`` and by integration tests
against a real worktree.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# Add orchestrator + shared to path
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))


@pytest.fixture(autouse=True)
def _reset_singleton():
    from state_store_probe import reset_state_store_probe_for_test

    reset_state_store_probe_for_test()
    try:
        yield
    finally:
        reset_state_store_probe_for_test()


@pytest.fixture
def with_repo_path(monkeypatch):
    """Set ``EGG_REPO_PATH`` to a sentinel so ``probe_now`` takes the
    probe path (callers patch ``probe_state_store_at`` to control the
    return value). The path itself is never read because the patch
    intercepts before any filesystem access."""
    monkeypatch.setenv("EGG_REPO_PATH", "/sentinel/repo/path")
    yield


class TestCacheLifecycle:
    """The cache starts empty, populates on the first probe, and
    reflects subsequent observations."""

    def test_snapshot_before_first_probe_reports_starting(self):
        from state_store_probe import StateStoreProbe

        probe = StateStoreProbe()
        snap = probe.snapshot()
        assert snap["healthy"] is False
        assert snap["fresh"] is False
        assert snap["age_seconds"] is None
        assert "starting" in snap["message"]

    def test_probe_now_populates_cache(self, with_repo_path):
        from state_store_probe import StateStoreProbe

        probe = StateStoreProbe()
        with patch(
            "state_store_probe.probe_state_store_at",
            return_value=(True, "ok"),
        ):
            probe.probe_now()
        snap = probe.snapshot()
        assert snap["healthy"] is True
        assert snap["fresh"] is True
        assert snap["message"] == "ok"
        assert snap["age_seconds"] is not None
        assert snap["age_seconds"] >= 0

    def test_probe_now_reflects_unhealthy_observation(self, with_repo_path):
        from state_store_probe import StateStoreProbe

        probe = StateStoreProbe()
        with patch(
            "state_store_probe.probe_state_store_at",
            return_value=(False, "GitOperationError: wedged"),
        ):
            probe.probe_now()
        snap = probe.snapshot()
        assert snap["healthy"] is False
        assert "wedged" in snap["message"]

    def test_probe_now_skipped_when_egg_repo_path_unset(self, monkeypatch):
        from state_store_probe import StateStoreProbe

        monkeypatch.delenv("EGG_REPO_PATH", raising=False)
        probe = StateStoreProbe()
        healthy, message = probe.probe_now()
        assert healthy is True
        assert "probe-skipped" in message


class TestWatchdog:
    """If the cache goes stale (``age > interval * stale_multiplier``),
    ``snapshot()`` reports ``healthy=False`` regardless of the last
    observation. Covers the case where the BG thread itself wedges."""

    def test_fresh_observation_within_window(self, with_repo_path):
        from state_store_probe import StateStoreProbe

        probe = StateStoreProbe(interval=10.0, stale_multiplier=2.0)
        with patch(
            "state_store_probe.probe_state_store_at",
            return_value=(True, "ok"),
        ):
            probe.probe_now()
        snap = probe.snapshot()
        assert snap["fresh"] is True
        assert snap["healthy"] is True

    def test_stale_observation_flips_healthy_to_false(self, with_repo_path):
        """Drive the cache by hand and advance ``time.monotonic`` past
        the staleness threshold. Healthy observation must be ignored."""
        from state_store_probe import StateStoreProbe

        probe = StateStoreProbe(interval=1.0, stale_multiplier=2.0)
        # Manually populate the cache with a "healthy" observation, then
        # rewind ``last_check_monotonic`` so the snapshot sees it as stale.
        with patch(
            "state_store_probe.probe_state_store_at",
            return_value=(True, "ok"),
        ):
            probe.probe_now()

        # Force the cached timestamp into the past by more than 2*interval.
        with probe._lock:  # type: ignore[attr-defined]
            probe._last_check_monotonic -= 10.0  # type: ignore[attr-defined,operator]

        snap = probe.snapshot()
        assert snap["fresh"] is False
        assert snap["healthy"] is False
        assert "stale" in snap["message"]


class TestExceptionIsolation:
    """Exceptions in the underlying probe must not crash the BG thread
    or propagate to ``probe_now`` callers."""

    def test_probe_now_swallows_underlying_exception(self, with_repo_path):
        from state_store_probe import StateStoreProbe

        probe = StateStoreProbe()
        with patch(
            "state_store_probe.probe_state_store_at",
            side_effect=RuntimeError("boom"),
        ):
            healthy, message = probe.probe_now()
        assert healthy is False
        assert "probe-error" in message
        assert "boom" in message

    def test_bg_thread_survives_probe_exception(self, monkeypatch):
        """The thread must keep running even after the probe raises —
        otherwise a single transient git failure would silently kill
        self-heal for the rest of the pod's lifetime."""
        from state_store_probe import StateStoreProbe

        # Monkeypatch EGG_REPO_PATH so the probe path is taken.
        monkeypatch.setenv("EGG_REPO_PATH", "/nonexistent/path/probably")

        call_count = {"n": 0}

        def flaky_probe(_path: Path) -> tuple[bool, str]:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("transient failure")
            return True, "ok"

        with patch("state_store_probe.probe_state_store_at", side_effect=flaky_probe):
            probe = StateStoreProbe(interval=0.05)
            probe.start()
            try:
                # Wait long enough for at least 2 ticks (the failing one
                # plus a recovering one).
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    if call_count["n"] >= 2:
                        break
                    time.sleep(0.01)
            finally:
                probe.stop(timeout=1.0)

        assert call_count["n"] >= 2, (
            "BG thread did not recover after a probe exception; "
            "this would silently disable state-store self-heal."
        )


class TestThreadLifecycle:
    """``start()`` is idempotent and ``stop()`` shuts the thread down
    cleanly on SIGTERM-equivalent code paths."""

    def test_start_is_idempotent(self, with_repo_path):
        from state_store_probe import StateStoreProbe

        probe = StateStoreProbe(interval=10.0)
        with patch(
            "state_store_probe.probe_state_store_at",
            return_value=(True, "ok"),
        ):
            probe.start()
            first_thread = probe._thread  # type: ignore[attr-defined]
            probe.start()  # No-op
            second_thread = probe._thread  # type: ignore[attr-defined]
        try:
            assert first_thread is second_thread
        finally:
            probe.stop(timeout=1.0)

    def test_stop_joins_thread(self, with_repo_path):
        from state_store_probe import StateStoreProbe

        probe = StateStoreProbe(interval=0.1)
        with patch(
            "state_store_probe.probe_state_store_at",
            return_value=(True, "ok"),
        ):
            probe.start()
            time.sleep(0.05)
            probe.stop(timeout=1.0)

        thread: threading.Thread | None = probe._thread  # type: ignore[attr-defined]
        assert thread is not None
        assert not thread.is_alive(), "BG thread did not exit after stop(); shutdown is broken."


class TestSingleton:
    """The module-level singleton accessor must return a stable instance
    until reset."""

    def test_get_state_store_probe_returns_same_instance(self):
        from state_store_probe import get_state_store_probe

        a = get_state_store_probe()
        b = get_state_store_probe()
        assert a is b

    def test_reset_drops_singleton(self):
        from state_store_probe import (
            get_state_store_probe,
            reset_state_store_probe_for_test,
        )

        a = get_state_store_probe()
        reset_state_store_probe_for_test()
        b = get_state_store_probe()
        assert a is not b
