"""Unit tests for shared.egg_health.HealthTracker."""

from datetime import UTC, datetime, timedelta

import pytest
from egg_health import HealthTracker


def _at(seconds: int) -> datetime:
    """Helper: build a deterministic timestamp N seconds after a fixed epoch."""
    return datetime(2026, 4, 22, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=seconds)


class TestFirstObservation:
    def test_first_observation_healthy_uses_process_start(self):
        tracker = HealthTracker()
        tracker.record(True, now=_at(5))
        snap = tracker.snapshot()

        # Per issue #1855: if never unhealthy this run, healthy_since is
        # the process start time — NOT the observation time.
        assert snap["healthy_since"] == snap["process_start_time"]
        assert snap["last_unhealthy_at"] is None
        assert [t["state"] for t in snap["recent_transitions"]] == ["healthy"]

    def test_first_observation_unhealthy(self):
        tracker = HealthTracker()
        tracker.record(False, now=_at(5))
        snap = tracker.snapshot()

        assert snap["healthy_since"] is None
        assert snap["last_unhealthy_at"] == _at(5).isoformat()
        assert [t["state"] for t in snap["recent_transitions"]] == ["unhealthy"]


class TestTransitions:
    def test_healthy_to_unhealthy_resets_healthy_since(self):
        tracker = HealthTracker()
        tracker.record(True, now=_at(0))
        tracker.record(False, now=_at(10))
        snap = tracker.snapshot()

        assert snap["healthy_since"] is None
        assert snap["last_unhealthy_at"] == _at(10).isoformat()
        assert [t["state"] for t in snap["recent_transitions"]] == ["healthy", "unhealthy"]

    def test_unhealthy_to_healthy_records_transition_time(self):
        tracker = HealthTracker()
        tracker.record(False, now=_at(0))
        tracker.record(True, now=_at(30))
        snap = tracker.snapshot()

        # healthy_since should be the transition time, not process_start.
        assert snap["healthy_since"] == _at(30).isoformat()
        assert snap["last_unhealthy_at"] == _at(0).isoformat()
        assert [t["state"] for t in snap["recent_transitions"]] == ["unhealthy", "healthy"]

    def test_repeated_healthy_does_not_move_healthy_since(self):
        tracker = HealthTracker()
        tracker.record(True, now=_at(0))
        original_since = tracker.snapshot()["healthy_since"]
        tracker.record(True, now=_at(5))
        tracker.record(True, now=_at(10))
        snap = tracker.snapshot()

        assert snap["healthy_since"] == original_since
        # No new transitions — still just the initial one.
        assert len(snap["recent_transitions"]) == 1

    def test_repeated_unhealthy_advances_last_unhealthy_at(self):
        tracker = HealthTracker()
        tracker.record(False, now=_at(0))
        tracker.record(False, now=_at(5))
        tracker.record(False, now=_at(12))
        snap = tracker.snapshot()

        assert snap["last_unhealthy_at"] == _at(12).isoformat()
        # No transition — still just the first unhealthy event.
        assert len(snap["recent_transitions"]) == 1


class TestRingBuffer:
    def test_ring_buffer_is_bounded(self):
        tracker = HealthTracker(max_transitions=3)
        # Four transitions: unhealthy, healthy, unhealthy, healthy
        tracker.record(False, now=_at(0))
        tracker.record(True, now=_at(1))
        tracker.record(False, now=_at(2))
        tracker.record(True, now=_at(3))
        snap = tracker.snapshot()

        assert len(snap["recent_transitions"]) == 3
        # Oldest ("unhealthy" at t=0) should have been dropped.
        assert snap["recent_transitions"][0]["ts"] == _at(1).isoformat()


def test_snapshot_shape():
    tracker = HealthTracker()
    tracker.record(True)
    snap = tracker.snapshot()
    assert set(snap.keys()) == {
        "process_start_time",
        "healthy_since",
        "last_unhealthy_at",
        "recent_transitions",
    }


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
