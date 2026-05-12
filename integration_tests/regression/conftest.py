"""Shared fixtures for ``integration_tests/regression``.

The regression tier covers two complementary surfaces:

* **Message store + event bus routing** (issue #2640) — exercises the
  load-bearing seams between the orchestrator's Flask blueprints, the
  inter-agent message store (in-memory + Redis Streams), and the
  in-process ``EventBus``.
* **BRC consensus** (issue #2635) — covers behaviours that have been
  hand-rolled into postmortems: BRC single-cycle, phase-aware timeouts,
  NACK round-trip, reviewer disagreement, etc. Tests live here (not
  under ``orchestrator/tests/``) because they exercise the
  orchestrator's Python API at the integration boundary — the shape
  #2474 recommends after the ScriptedProvider pod-injection avenue was
  ruled out.

These tests follow the integration-tier pattern established by
``integration_tests/test_slice_pipeline_e2e.py``:

* ``@pytest.mark.integration`` (applied at module level in each test
  file) so ``make test-integration`` picks them up.
* In-process fakes for any boundary that would otherwise require k3s,
  Docker, or live LLM calls — ``fakeredis`` for the Redis backend,
  ``unittest.mock.patch`` for the pipeline state-store and the inner
  context-PR hook. BRC tests drive ``PeerConsensusTracker`` and the
  timeout-handler entry points in-process against real implementations
  and never call into the ``egg_stack`` fixture.
* Real ``Flask`` blueprint and real ``EventBus`` so the routing path
  under test is the same one production runs.

The dual-backend parametrization for message-store tests mirrors the
AC pattern from
``orchestrator/tests/test_pipelines_status_wait_route.py``: every test
that touches the message store runs against both ``MessageStore``
(in-memory) and ``RedisMessageStore`` backed by ``fakeredis.FakeRedis``
so a regression in either backend surfaces.

Plain helper functions (``make_tracker``, ``propose_payload``,
``filter_events``) live in ``_helpers.py``; pytest's conftest discovery
only surfaces fixtures cross-module.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Generator
from pathlib import Path

# sys.path setup: include _REGRESSION_DIR so ``_helpers`` is importable,
# plus orchestrator + shared + project root so test modules can import
# the orchestrator's internal modules (events, message_store,
# redis_message_store, routes.pipelines, peer_consensus, review_graph).
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

# Lifecycle-secret-gated routes (PATCH /pipelines/<id>, DELETE
# /pipelines/<id>, signals, etc.) require ``EGG_LIFECYCLE_SECRET`` to be
# set + ``Authorization: Bearer <secret>`` on the request. The
# orchestrator's own test suite sets this session-wide via
# ``orchestrator/tests/conftest.py``; mirror the pattern here so the
# regression tier can drive PATCH-side routes the same way.
_TEST_LIFECYCLE_SECRET = "test-lifecycle-secret-regression"


@pytest.fixture(autouse=True, scope="session")
def _set_lifecycle_secret_env():
    """Set ``EGG_LIFECYCLE_SECRET`` for the regression test session."""
    prev = os.environ.get("EGG_LIFECYCLE_SECRET")
    os.environ["EGG_LIFECYCLE_SECRET"] = _TEST_LIFECYCLE_SECRET
    yield
    if prev is None:
        os.environ.pop("EGG_LIFECYCLE_SECRET", None)
    else:
        os.environ["EGG_LIFECYCLE_SECRET"] = prev


@pytest.fixture
def lifecycle_auth_headers() -> dict[str, str]:
    """Valid ``Authorization`` header for lifecycle-control endpoints."""
    return {"Authorization": f"Bearer {_TEST_LIFECYCLE_SECRET}"}


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
