"""Tests for ``Event.sequence`` + ``EventBus._sequence`` (issue #1932 TASK-1-1).

HANDOFF NOTE to tester: pins the contract for the per-bus monotonic
counter that powers the EventBus half of the ``msg:<id>|evt:<seq>``
cursor.  All 7 cases pass on commit ``1258ff399``.  Drop in as
``orchestrator/tests/test_events_event_sequence.py`` — coder
cannot push ``tests/`` per the role allowlist.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

from events import Event, EventBus, EventType  # noqa: E402


def test_event_default_sequence_is_zero() -> None:
    e = Event(event_type=EventType.PHASE_STARTED, pipeline_id="pid")
    assert e.sequence == 0


def test_to_dict_includes_sequence() -> None:
    e = Event(event_type=EventType.PHASE_STARTED, pipeline_id="pid")
    e.sequence = 42
    d = e.to_dict()
    assert d["sequence"] == 42
    assert d["event_type"] == EventType.PHASE_STARTED.value
    assert d["pipeline_id"] == "pid"


def test_publish_assigns_monotonic_sequence() -> None:
    bus = EventBus(async_delivery=False)
    events = [
        Event(event_type=EventType.PHASE_STARTED, pipeline_id="pid"),
        Event(event_type=EventType.PHASE_COMPLETED, pipeline_id="pid"),
        Event(event_type=EventType.DECISION_CREATED, pipeline_id="pid"),
    ]
    for e in events:
        bus.publish(e)
    assert [e.sequence for e in events] == [1, 2, 3]
    assert bus.current_sequence() == 3


def test_publish_overwrites_caller_supplied_sequence() -> None:
    bus = EventBus(async_delivery=False)
    e = Event(
        event_type=EventType.PHASE_STARTED,
        pipeline_id="pid",
        sequence=9999,
    )
    bus.publish(e)
    assert e.sequence == 1


def test_concurrent_publishes_are_monotonic_and_unique() -> None:
    """100 concurrent publishes across 8 threads produce exactly
    100 distinct, strictly-increasing sequence numbers 1..100.
    """
    bus = EventBus(async_delivery=False)
    per_thread_events: list[list[int]] = [[] for _ in range(8)]
    start = threading.Event()

    def _worker(idx: int, count: int) -> None:
        start.wait()
        for _ in range(count):
            e = Event(
                event_type=EventType.PHASE_STARTED,
                pipeline_id=f"pid-{idx}",
            )
            bus.publish(e)
            per_thread_events[idx].append(e.sequence)

    threads = [
        threading.Thread(target=_worker, args=(i, 100 // 8), daemon=True)
        for i in range(8)
    ]
    threads[-1]._args = (7, 100 - (100 // 8) * 7)  # type: ignore[attr-defined]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join(timeout=10)

    all_seqs = [s for sublist in per_thread_events for s in sublist]
    assert len(all_seqs) == 100
    assert len(set(all_seqs)) == 100
    assert sorted(all_seqs) == list(range(1, 101))
    assert bus.current_sequence() == 100


def test_current_sequence_reflects_latest_publish() -> None:
    bus = EventBus(async_delivery=False)
    assert bus.current_sequence() == 0
    bus.publish(Event(event_type=EventType.PHASE_STARTED, pipeline_id="pid"))
    assert bus.current_sequence() == 1
    bus.publish(Event(event_type=EventType.PHASE_COMPLETED, pipeline_id="pid"))
    assert bus.current_sequence() == 2


def test_existing_event_consumers_still_work() -> None:
    bus = EventBus(async_delivery=False)
    captured: list[Event] = []

    def _handler(event: Event) -> None:
        captured.append(event)

    bus.subscribe(None, _handler)
    bus.publish(Event(event_type=EventType.PHASE_STARTED, pipeline_id="pid"))
    assert len(captured) == 1
    assert captured[0].sequence == 1
    assert captured[0].to_dict()["sequence"] == 1
