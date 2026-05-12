"""Shared fixtures for BRC consensus regression tests (issue #2635).

The regression tier covers behaviours that have been hand-rolled into
postmortems — BRC single-cycle, phase-aware timeouts, NACK round-trip,
reviewer disagreement, etc.  Tests live here (not under
``orchestrator/tests/``) because they exercise the orchestrator's
Python API at the integration boundary — the same shape #2474
recommends after the ScriptedProvider pod-injection avenue was ruled
out (the constraint write-up referenced from issue #2635).

Tests in this folder are marked ``integration`` so they run under
``make test-integration`` alongside the k3s tier, but they do NOT
require k3s and never call into the ``egg_stack`` fixture — they
drive ``PeerConsensusTracker`` and the timeout-handler entry points
in-process against real implementations.

Plain helper functions (``make_tracker``, ``propose_payload``,
``filter_events``) live in ``_helpers.py``; pytest's conftest
discovery only surfaces fixtures cross-module.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Generator
from pathlib import Path

# Make sibling ``_helpers.py`` and the orchestrator/shared trees
# importable before any conftest-level imports below land.
_REGRESSION_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _REGRESSION_DIR.parent.parent
for _p in (
    _REGRESSION_DIR,
    _PROJECT_ROOT / "orchestrator",
    _PROJECT_ROOT / "shared",
    _PROJECT_ROOT,
):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pytest  # noqa: E402
from _helpers import EventFilter, filter_events  # noqa: E402
from events import Event, get_event_bus  # noqa: E402
from peer_consensus import _trackers as _global_trackers  # noqa: E402
from peer_consensus import _trackers_lock as _global_trackers_lock  # noqa: E402
from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_tracker_registry() -> Generator[None]:
    """Snapshot + restore the global ``_trackers`` registry around each test.

    ``create_peer_consensus_tracker`` stores trackers in a module-level
    dict keyed by pipeline_id.  Without cleanup these survive across
    tests and a later test's ``get_peer_consensus_tracker(same_id)``
    can find leftover state from a previous test (the surfaced gap
    in #2635 PR review).  Snapshot+restore is safer than ``clear()``
    in case a parent test suite seeded trackers we shouldn't remove.
    """
    with _global_trackers_lock:
        snapshot = dict(_global_trackers)
    try:
        yield
    finally:
        with _global_trackers_lock:
            _global_trackers.clear()
            _global_trackers.update(snapshot)


@pytest.fixture
def event_capture() -> Generator[Callable[[], list[Event]]]:
    """Snapshot events published after the fixture is acquired.

    The orchestrator's event bus runs with ``async_delivery=True``,
    so handlers are dispatched on a worker thread — subscribing and
    immediately reading the buffer races the delivery loop and is
    flaky.  ``get_history()`` is updated **synchronously** inside the
    publish path's lock, so reading it gives a deterministic snapshot.

    The fixture captures the bus's sequence-tip before the test runs
    and returns a callable that yields only events appended since,
    isolating the test from events emitted by unrelated tests in the
    same session.

    Note: ``get_history()`` is bounded by ``EventBus._max_history``
    (default 100 — see ``orchestrator/events.py:154``).  All tests in
    this folder stay well under that bound between fixture entry and
    snapshot, but a future test that emits >100 events would lose its
    earliest ones to history eviction; widen ``max_history`` on the
    bus or capture more granularly if you anticipate larger volumes.
    """
    bus = get_event_bus()
    seq_before = bus.current_sequence()

    def snapshot() -> list[Event]:
        history = bus.get_history()  # newest first per the bus contract
        # Restore publish order and filter to events emitted during this test.
        return [e for e in reversed(history) if e.sequence > seq_before]

    yield snapshot


@pytest.fixture(name="filter_events")
def filter_events_fixture() -> EventFilter:
    """``filter_events`` as a fixture so tests can take it via injection.

    Exposed under the bare name ``filter_events`` so tests read naturally
    (``def test_x(self, event_capture, filter_events): ...``).  The
    underlying helper is also importable from ``_helpers`` for sites
    that don't want fixture injection.
    """
    return filter_events


@pytest.fixture
def single_reviewer_graph() -> ReviewGraph:
    """1 producer, 1 critical reviewer — the minimal BRC topology."""
    return ReviewGraph([ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL)])


@pytest.fixture
def two_reviewer_graph() -> ReviewGraph:
    """1 producer, 2 critical reviewers — exercises disagreement paths."""
    return ReviewGraph(
        [
            ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ReviewEdge("reviewer_contract", "coder", ReviewCriticality.CRITICAL),
        ]
    )


@pytest.fixture
def advisory_blocker_graph() -> ReviewGraph:
    """1 producer, 1 critical + 1 advisory reviewer — timeout-handler triage path."""
    return ReviewGraph(
        [
            ReviewEdge("reviewer_code", "coder", ReviewCriticality.CRITICAL),
            ReviewEdge("reviewer_contract", "coder", ReviewCriticality.ADVISORY),
        ]
    )
