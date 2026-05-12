"""Shared helpers for the regression tier (#2474, #2634, #2635).

The parent ``integration_tests/conftest.py`` owns the k3s harness —
``egg_stack``, ``orchestrator_url``, kubectl-skip semantics — so we
re-export nothing here. This file adds two distinct fixture sets:

HITL HTTP round-trip helpers (#2474, #2634)
-------------------------------------------
* :func:`deterministic_pipeline_id` — derives a stable 8-hex-char
  pipeline id from a pytest nodeid so re-runs of the same test reuse
  the same id (easier post-mortem in ``kubectl get pods``).
* :func:`lifecycle_secret` — reads the orchestrator's lifecycle
  bearer from ``gateway-secrets/lifecycle-secret`` in ``egg-system``.
  Returns ``None`` (and the caller should ``pytest.skip``) when the
  secret is unavailable — happy-path tests need it, auth-rejection
  tests do not.
* :func:`lifecycle_bearer`, :func:`regression_pipeline_id` — fixtures
  consumed by ``test_hitl_round_trip.py``.

BRC consensus fixtures (#2635)
------------------------------
The BRC tests exercise the orchestrator's Python API at the
integration boundary — the same shape #2474 recommends after the
ScriptedProvider pod-injection avenue was ruled out. They run under
``make test-integration`` alongside the k3s tier, but do NOT require
k3s and never call into the ``egg_stack`` fixture — they drive
``PeerConsensusTracker`` and the timeout-handler entry points
in-process against real implementations.

Plain helper functions (``make_tracker``, ``propose_payload``,
``filter_events``) live in ``_helpers.py``; pytest's conftest
discovery only surfaces fixtures cross-module.

Tests in this tier MUST be marked ``@pytest.mark.integration`` so
``make test-integration`` (`-m "integration or security"`) picks them
up. The parent conftest's k3s harness drives the skip behaviour when
kubectl is missing for the HTTP-tier tests; the BRC tests are pure
in-process and skip nothing.
"""

from __future__ import annotations

import base64
import hashlib
import subprocess
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

_LIFECYCLE_SECRET_NAMESPACE = "egg-system"
_LIFECYCLE_SECRET_NAME = "gateway-secrets"
_LIFECYCLE_SECRET_KEY = "lifecycle-secret"


def deterministic_pipeline_id(test_nodeid: str) -> str:
    """Return a stable, **syntactically valid** pipeline id from a nodeid.

    The id matches the ``pipeline-{8 hex chars}`` arm of
    ``state_store.PIPELINE_ID_PATTERN`` — any other shape (e.g. the
    ``regression-<hex>`` shape from #2474's recovered attempt) trips
    ``InvalidPipelineIdError`` → 400 before the 404 path runs, masking
    "pipeline not found" assertions.

    SHA-1 is used as a stable digest, not a cryptographic hash, so the
    Bandit warning is suppressed.
    """
    digest = hashlib.sha1(test_nodeid.encode("utf-8")).hexdigest()  # noqa: S324
    return f"pipeline-{digest[:8]}"


def lifecycle_secret() -> str | None:
    """Return the orchestrator's ``EGG_LIFECYCLE_SECRET`` if reachable.

    Reads ``gateway-secrets/lifecycle-secret`` from the ``egg-system``
    namespace. Returns ``None`` if kubectl is missing, the secret is
    absent, or the value cannot be decoded — callers should
    ``pytest.skip`` rather than fail in that case so happy-path tests
    are skipped cleanly when run by a developer without read access on
    the secret (CI has it).
    """
    cmd = [
        "kubectl",
        "-n",
        _LIFECYCLE_SECRET_NAMESPACE,
        "get",
        "secret",
        _LIFECYCLE_SECRET_NAME,
        "-o",
        f"jsonpath={{.data.{_LIFECYCLE_SECRET_KEY}}}",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    try:
        # ``.strip()`` because ``kubectl create secret --from-file`` keeps
        # every byte of the source file including the trailing newline;
        # a ``\n`` inside ``f"Bearer {secret}"`` is rejected by
        # ``http.client.putheader``.
        return base64.b64decode(result.stdout).decode("utf-8").strip()
    except ValueError, UnicodeDecodeError:
        return None


@pytest.fixture
def lifecycle_bearer() -> str:
    """Return an ``Authorization: Bearer ...`` value or skip the test.

    Used by happy-path tests that need to call
    ``@require_lifecycle_secret`` endpoints. When the secret is not
    reachable from the test runner (developer laptop without rbac on
    the secret) the test is skipped, not failed.
    """
    secret = lifecycle_secret()
    if not secret:
        pytest.skip(
            "lifecycle-secret not readable from gateway-secrets in "
            f"namespace {_LIFECYCLE_SECRET_NAMESPACE} — happy-path "
            "lifecycle endpoint tests skipped"
        )
    return f"Bearer {secret}"


@pytest.fixture
def regression_pipeline_id(request: pytest.FixtureRequest) -> str:
    """Stable pipeline id derived from the calling test's pytest nodeid."""
    return deterministic_pipeline_id(request.node.nodeid)


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


__all__ = [
    "deterministic_pipeline_id",
    "lifecycle_bearer",
    "lifecycle_secret",
    "regression_pipeline_id",
]
