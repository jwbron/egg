"""
Tests for ProgressStore — in-memory per-pipeline progress event storage.

Covers add/retrieve, filtering (agent_role, since, limit), get_latest_per_agent,
clear, max retention/pruning, thread safety, pipeline isolation, and combined filters.
"""

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

try:
    from progress_store import ProgressEvent, ProgressStore
except ImportError:
    pytestmark = pytest.mark.skip(
        reason="progress_store module not yet available"
    )
    ProgressEvent = None
    ProgressStore = None


def _make_event(
    pipeline_id="issue-100",
    agent_role="coder",
    step="implement",
    state="working",
    detail=None,
    blocker=None,
    timestamp=None,
    event_id=None,
):
    """Helper to build a ProgressEvent with sensible defaults."""
    kwargs = dict(
        pipeline_id=pipeline_id,
        agent_role=agent_role,
        step=step,
        state=state,
    )
    if detail is not None:
        kwargs["detail"] = detail
    if blocker is not None:
        kwargs["blocker"] = blocker
    if timestamp is not None:
        kwargs["timestamp"] = timestamp
    if event_id is not None:
        kwargs["id"] = event_id
    return ProgressEvent(**kwargs)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store():
    """Create a fresh ProgressStore with default settings."""
    if ProgressStore is None:
        pytest.skip("progress_store module not yet available")
    return ProgressStore()


@pytest.fixture
def small_store():
    """Create a ProgressStore with a small max retention for pruning tests."""
    if ProgressStore is None:
        pytest.skip("progress_store module not yet available")
    return ProgressStore(max_events_per_pipeline=5)


# ---------------------------------------------------------------------------
# Basic add and retrieve
# ---------------------------------------------------------------------------


class TestBasicAddAndRetrieve:
    """Test adding a single event and getting it back."""

    def test_add_and_get_single_event(self, store):
        event = _make_event(detail="writing code")
        store.add_event(event)

        events = store.get_events("issue-100")
        assert len(events) == 1
        assert events[0].agent_role == "coder"
        assert events[0].state == "working"
        assert events[0].detail == "writing code"

    def test_event_has_id(self, store):
        """Every stored event should have an id assigned."""
        event = _make_event()
        store.add_event(event)

        events = store.get_events("issue-100")
        assert len(events) == 1
        assert events[0].id is not None

    def test_event_has_timestamp(self, store):
        """Every stored event should have a timestamp."""
        event = _make_event()
        store.add_event(event)

        events = store.get_events("issue-100")
        assert len(events) == 1
        assert isinstance(events[0].timestamp, datetime)

    def test_events_returned_in_order(self, store):
        """Events should come back in chronological order."""
        for i in range(5):
            store.add_event(_make_event(detail=f"step-{i}"))

        events = store.get_events("issue-100")
        assert len(events) == 5
        for i, ev in enumerate(events):
            assert ev.detail == f"step-{i}"

    def test_add_event_with_blocker(self, store):
        event = _make_event(state="blocked", blocker="waiting for review")
        store.add_event(event)

        events = store.get_events("issue-100")
        assert events[0].state == "blocked"
        assert events[0].blocker == "waiting for review"


# ---------------------------------------------------------------------------
# Filter by agent_role
# ---------------------------------------------------------------------------


class TestFilterByAgentRole:
    """Test filtering events by agent_role."""

    def test_filter_single_role(self, store):
        store.add_event(_make_event(agent_role="coder"))
        store.add_event(_make_event(agent_role="tester"))
        store.add_event(_make_event(agent_role="coder"))

        coder_events = store.get_events("issue-100", agent_role="coder")
        assert len(coder_events) == 2
        assert all(e.agent_role == "coder" for e in coder_events)

    def test_filter_returns_empty_for_unknown_role(self, store):
        store.add_event(_make_event(agent_role="coder"))

        events = store.get_events("issue-100", agent_role="reviewer")
        assert events == []


# ---------------------------------------------------------------------------
# Filter by since timestamp
# ---------------------------------------------------------------------------


class TestFilterBySince:
    """Test filtering events by since timestamp."""

    def test_since_filters_old_events(self, store):
        old_time = datetime.now(timezone.utc) - timedelta(hours=1)
        recent_time = datetime.now(timezone.utc) - timedelta(seconds=5)

        store.add_event(_make_event(detail="old", timestamp=old_time))
        store.add_event(_make_event(detail="recent", timestamp=recent_time))

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=1)
        events = store.get_events("issue-100", since=cutoff)
        assert len(events) == 1
        assert events[0].detail == "recent"

    def test_since_with_no_matching_events(self, store):
        old_time = datetime.now(timezone.utc) - timedelta(hours=2)
        store.add_event(_make_event(timestamp=old_time))

        cutoff = datetime.now(timezone.utc)
        events = store.get_events("issue-100", since=cutoff)
        assert events == []

    def test_since_returns_all_if_none(self, store):
        for i in range(3):
            store.add_event(_make_event(detail=f"ev-{i}"))

        events = store.get_events("issue-100", since=None)
        assert len(events) == 3


# ---------------------------------------------------------------------------
# Filter with limit
# ---------------------------------------------------------------------------


class TestFilterWithLimit:
    """Test limiting the number of returned events."""

    def test_limit_caps_results(self, store):
        for i in range(10):
            store.add_event(_make_event(detail=f"ev-{i}"))

        events = store.get_events("issue-100", limit=3)
        assert len(events) == 3

    def test_limit_larger_than_total_returns_all(self, store):
        store.add_event(_make_event())
        store.add_event(_make_event())

        events = store.get_events("issue-100", limit=100)
        assert len(events) == 2

    def test_limit_zero_or_none_returns_all(self, store):
        for _ in range(5):
            store.add_event(_make_event())

        events = store.get_events("issue-100", limit=None)
        assert len(events) == 5


# ---------------------------------------------------------------------------
# get_latest_per_agent
# ---------------------------------------------------------------------------


class TestGetLatestPerAgent:
    """Test getting the most recent event per agent role."""

    def test_returns_latest_per_role(self, store):
        store.add_event(_make_event(agent_role="coder", detail="c1"))
        store.add_event(_make_event(agent_role="tester", detail="t1"))
        store.add_event(_make_event(agent_role="coder", detail="c2"))
        store.add_event(_make_event(agent_role="tester", detail="t2"))
        store.add_event(_make_event(agent_role="documenter", detail="d1"))

        latest = store.get_latest_per_agent("issue-100")

        # Should have exactly one event per role
        roles = {e.agent_role for e in latest}
        assert roles == {"coder", "tester", "documenter"}

        by_role = {e.agent_role: e for e in latest}
        assert by_role["coder"].detail == "c2"
        assert by_role["tester"].detail == "t2"
        assert by_role["documenter"].detail == "d1"

    def test_returns_empty_for_unknown_pipeline(self, store):
        latest = store.get_latest_per_agent("nonexistent-pipeline")
        assert latest == []

    def test_single_agent_returns_one(self, store):
        store.add_event(_make_event(agent_role="coder", detail="only"))

        latest = store.get_latest_per_agent("issue-100")
        assert len(latest) == 1
        assert latest[0].detail == "only"


# ---------------------------------------------------------------------------
# Clear
# ---------------------------------------------------------------------------


class TestClear:
    """Test clearing events for a pipeline."""

    def test_clear_removes_all_events(self, store):
        for _ in range(5):
            store.add_event(_make_event())

        store.clear("issue-100")

        events = store.get_events("issue-100")
        assert events == []

    def test_clear_nonexistent_pipeline_is_noop(self, store):
        # Should not raise
        store.clear("nonexistent-pipeline")

    def test_clear_does_not_affect_other_pipelines(self, store):
        store.add_event(_make_event(pipeline_id="issue-100"))
        store.add_event(_make_event(pipeline_id="issue-200"))

        store.clear("issue-100")

        assert store.get_events("issue-100") == []
        assert len(store.get_events("issue-200")) == 1


# ---------------------------------------------------------------------------
# Max retention / pruning
# ---------------------------------------------------------------------------


class TestMaxRetentionPruning:
    """Test that old events are pruned when max is exceeded."""

    def test_prunes_when_exceeding_max(self, small_store):
        """Adding more than max_events_per_pipeline should trigger pruning."""
        for i in range(10):
            small_store.add_event(_make_event(detail=f"ev-{i}"))

        events = small_store.get_events("issue-100")
        assert len(events) <= 5

    def test_most_recent_events_survive_pruning(self, small_store):
        """After pruning, the most recent events should still be present."""
        for i in range(10):
            small_store.add_event(_make_event(detail=f"ev-{i}"))

        events = small_store.get_events("issue-100")
        # The latest events should be among the survivors
        details = {e.detail for e in events}
        assert "ev-9" in details

    def test_pruning_does_not_affect_other_pipelines(self, small_store):
        """Pruning one pipeline should not touch another."""
        # Fill up pipeline A past the limit
        for i in range(10):
            small_store.add_event(
                _make_event(pipeline_id="issue-100", detail=f"a-{i}")
            )

        # Pipeline B has fewer events
        for i in range(3):
            small_store.add_event(
                _make_event(pipeline_id="issue-200", detail=f"b-{i}")
            )

        assert len(small_store.get_events("issue-200")) == 3


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    """Test concurrent access does not crash or corrupt data."""

    def test_concurrent_add_events(self, store):
        """Multiple threads adding events concurrently should not crash."""
        errors = []
        num_threads = 10
        events_per_thread = 50

        def add_events(role_index):
            try:
                for i in range(events_per_thread):
                    store.add_event(
                        _make_event(
                            agent_role=f"agent-{role_index}",
                            detail=f"t{role_index}-e{i}",
                        )
                    )
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=add_events, args=(i,))
            for i in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == [], f"Concurrent add_event raised: {errors}"

        all_events = store.get_events("issue-100")
        assert len(all_events) == num_threads * events_per_thread

    def test_concurrent_add_and_read(self, store):
        """Reads during concurrent writes should not crash."""
        errors = []
        stop = threading.Event()

        def writer():
            try:
                for i in range(100):
                    store.add_event(_make_event(detail=f"w-{i}"))
            except Exception as exc:
                errors.append(exc)
            finally:
                stop.set()

        def reader():
            try:
                while not stop.is_set():
                    store.get_events("issue-100")
                    store.get_latest_per_agent("issue-100")
            except Exception as exc:
                errors.append(exc)

        writer_t = threading.Thread(target=writer)
        reader_t = threading.Thread(target=reader)
        writer_t.start()
        reader_t.start()
        writer_t.join(timeout=10)
        reader_t.join(timeout=10)

        assert errors == [], f"Concurrent read/write raised: {errors}"


# ---------------------------------------------------------------------------
# Multiple pipelines isolation
# ---------------------------------------------------------------------------


class TestMultiplePipelines:
    """Test that events for different pipelines are isolated."""

    def test_events_are_pipeline_scoped(self, store):
        store.add_event(_make_event(pipeline_id="issue-100", detail="a"))
        store.add_event(_make_event(pipeline_id="issue-200", detail="b"))
        store.add_event(_make_event(pipeline_id="issue-100", detail="c"))

        events_100 = store.get_events("issue-100")
        events_200 = store.get_events("issue-200")

        assert len(events_100) == 2
        assert len(events_200) == 1
        assert events_200[0].detail == "b"

    def test_get_latest_per_agent_is_pipeline_scoped(self, store):
        store.add_event(
            _make_event(pipeline_id="issue-100", agent_role="coder", detail="a")
        )
        store.add_event(
            _make_event(pipeline_id="issue-200", agent_role="coder", detail="b")
        )

        latest_100 = store.get_latest_per_agent("issue-100")
        latest_200 = store.get_latest_per_agent("issue-200")

        assert len(latest_100) == 1
        assert latest_100[0].detail == "a"
        assert len(latest_200) == 1
        assert latest_200[0].detail == "b"


# ---------------------------------------------------------------------------
# Combined filters
# ---------------------------------------------------------------------------


class TestCombinedFilters:
    """Test using agent_role + since + limit together."""

    def test_agent_role_and_limit(self, store):
        for i in range(10):
            store.add_event(_make_event(agent_role="coder", detail=f"c-{i}"))
        for i in range(5):
            store.add_event(_make_event(agent_role="tester", detail=f"t-{i}"))

        events = store.get_events("issue-100", agent_role="coder", limit=3)
        assert len(events) == 3
        assert all(e.agent_role == "coder" for e in events)

    def test_agent_role_and_since(self, store):
        old_time = datetime.now(timezone.utc) - timedelta(hours=1)
        recent_time = datetime.now(timezone.utc) - timedelta(seconds=5)
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=1)

        store.add_event(
            _make_event(agent_role="coder", detail="old", timestamp=old_time)
        )
        store.add_event(
            _make_event(
                agent_role="coder", detail="recent", timestamp=recent_time
            )
        )
        store.add_event(
            _make_event(
                agent_role="tester", detail="recent-t", timestamp=recent_time
            )
        )

        events = store.get_events("issue-100", agent_role="coder", since=cutoff)
        assert len(events) == 1
        assert events[0].detail == "recent"

    def test_all_filters_combined(self, store):
        old_time = datetime.now(timezone.utc) - timedelta(hours=1)
        recent_time = datetime.now(timezone.utc) - timedelta(seconds=5)
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=1)

        # Old coder event (filtered by since)
        store.add_event(
            _make_event(agent_role="coder", detail="old-c", timestamp=old_time)
        )
        # Recent coder events
        for i in range(5):
            store.add_event(
                _make_event(
                    agent_role="coder",
                    detail=f"recent-c-{i}",
                    timestamp=recent_time + timedelta(milliseconds=i),
                )
            )
        # Recent tester event (filtered by agent_role)
        store.add_event(
            _make_event(
                agent_role="tester", detail="recent-t", timestamp=recent_time
            )
        )

        events = store.get_events(
            "issue-100", agent_role="coder", since=cutoff, limit=3
        )
        assert len(events) == 3
        assert all(e.agent_role == "coder" for e in events)
        assert all(e.timestamp >= cutoff for e in events)


# ---------------------------------------------------------------------------
# Empty results
# ---------------------------------------------------------------------------


class TestEmptyResults:
    """Test queries against non-existent or empty pipelines."""

    def test_get_events_nonexistent_pipeline(self, store):
        events = store.get_events("nonexistent-pipeline")
        assert events == []

    def test_get_latest_per_agent_nonexistent_pipeline(self, store):
        latest = store.get_latest_per_agent("nonexistent-pipeline")
        assert latest == []

    def test_get_events_after_clear(self, store):
        store.add_event(_make_event())
        store.clear("issue-100")

        assert store.get_events("issue-100") == []
        assert store.get_latest_per_agent("issue-100") == []
