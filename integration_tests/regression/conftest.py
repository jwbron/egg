"""Shared fixtures for regression integration tests.

This folder hosts two regression-test families:

* **Pipeline recovery / unpushed-commit salvage (#2633).** These tests
  sit between the unit-tier (``orchestrator/tests/``) and the k3s-tier
  (``integration_tests/test_*.py`` against a live cluster): they
  import the real ``pipelines_bp`` blueprint and exercise routes
  through an in-process Flask test client (same shape as
  ``integration_tests/test_babysit_pr/``), use the real
  ``agent_salvage`` module, the real git binary, and real worktree
  directories on ``tmp_path``. Only the gateway HTTP client and the
  spawner backend (the network/k8s boundaries) are stubbed out. The
  point is to catch regressions in the *wiring* between the route
  layer, the salvage helpers, and the git plumbing — exactly the
  seams that the per-module unit tests in ``orchestrator/tests/``
  mock out.

* **BRC consensus regression tests (issue #2635).** The regression
  tier covers behaviours that have been hand-rolled into postmortems —
  BRC single-cycle, phase-aware timeouts, NACK round-trip, reviewer
  disagreement, etc.  Tests live here (not under
  ``orchestrator/tests/``) because they exercise the orchestrator's
  Python API at the integration boundary — the same shape #2474
  recommends after the ScriptedProvider pod-injection avenue was ruled
  out (the constraint write-up referenced from issue #2635). They do
  NOT require k3s and never call into the ``egg_stack`` fixture —
  they drive ``PeerConsensusTracker`` and the timeout-handler entry
  points in-process against real implementations.

Tests in this folder are marker-gated under
``@pytest.mark.integration`` so they run under
``make test-integration`` / the ``Test / integration`` CI required
check alongside the k3s tier.

Plain helper functions (``make_tracker``, ``propose_payload``,
``filter_events``, the git/worktree builders, …) live in
``_helpers.py``; pytest's conftest discovery only surfaces fixtures
cross-module.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Generator
from pathlib import Path
from unittest.mock import MagicMock

# Mirror integration_tests/test_babysit_pr/conftest.py: make sibling
# ``_helpers.py`` plus the orchestrator/shared trees importable, and
# stub the kubernetes / docker SDKs so the blueprint loads without
# those packages installed.
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

sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

import pytest  # noqa: E402
from _helpers import EventFilter, filter_events  # noqa: E402
from events import Event, get_event_bus  # noqa: E402
from peer_consensus import _trackers as _global_trackers  # noqa: E402
from peer_consensus import _trackers_lock as _global_trackers_lock  # noqa: E402
from review_graph import ReviewCriticality, ReviewEdge, ReviewGraph  # noqa: E402

_TEST_LIFECYCLE_SECRET = "test-lifecycle-secret-regression"


def pytest_configure(config: pytest.Config) -> None:
    """Register the ``no_lifecycle_auth`` opt-out marker."""
    config.addinivalue_line(
        "markers",
        (
            "no_lifecycle_auth: opt out of the autouse Authorization-header "
            "injection (use for tests that want to assert the route's own "
            "auth handling, e.g. rejecting requests missing the header)."
        ),
    )


@pytest.fixture(autouse=True)
def _set_lifecycle_secret_env():
    """Set ``EGG_LIFECYCLE_SECRET`` so lifecycle-gated routes don't 503.

    Function-scoped per the same reasoning as
    ``test_babysit_pr/conftest.py``: a session-scoped autouse would leak
    the env var to any sibling suite that happens to read it directly.
    """
    overrides = {
        "EGG_LIFECYCLE_SECRET": _TEST_LIFECYCLE_SECRET,
        # In-process Flask client never talks to a real gateway; short-
        # circuit the gateway-readiness gate that ``create_pipeline``
        # otherwise waits 60s for.
        "EGG_GATEWAY_READY_TIMEOUT_SECONDS": "0",
    }
    prev = {k: os.environ.get(k) for k in overrides}
    for k, v in overrides.items():
        os.environ[k] = v
    yield
    for k, v in prev.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture(autouse=True)
def _inject_lifecycle_auth(monkeypatch, request):
    """Auto-attach ``Authorization: Bearer …`` to every FlaskClient request.

    Tests can opt out per-test with ``@pytest.mark.no_lifecycle_auth``
    so the route's own auth-rejection paths can be exercised without
    the wrapper transparently injecting the header. The per-call
    ``_lifecycle_auth=False`` kwarg is still honored when the wrapper
    is active.
    """
    if request.node.get_closest_marker("no_lifecycle_auth") is not None:
        yield
        return

    try:
        from flask.testing import FlaskClient
    except ImportError:
        yield
        return

    original_open = FlaskClient.open

    def wrapper(self, *args, **kwargs):
        if kwargs.pop("_lifecycle_auth", True):
            headers = kwargs.get("headers")
            auth_header = ("Authorization", f"Bearer {_TEST_LIFECYCLE_SECRET}")
            if headers is None:
                kwargs["headers"] = dict([auth_header])
            else:
                existing: dict[str, object] = {}
                if hasattr(headers, "items"):
                    existing = dict(headers.items())
                elif isinstance(headers, (list, tuple)):
                    existing = dict(headers)
                if not any(k.lower() == "authorization" for k in existing):
                    if isinstance(headers, dict):
                        kwargs["headers"] = {**headers, auth_header[0]: auth_header[1]}
                    else:
                        kwargs["headers"] = list(headers) + [auth_header]
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(FlaskClient, "open", wrapper)
    yield


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
def app():
    """A Flask app with the real pipelines blueprint mounted."""
    from flask import Flask
    from routes.pipelines import pipelines_bp

    app = Flask(__name__)
    app.register_blueprint(pipelines_bp)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def fake_gateway() -> MagicMock:
    """A gateway stub whose ``push_worktree_branch`` records every call.

    Shared across the salvage regression tests so the stub shape stays
    uniform — every test asserts against ``push_worktree_branch`` calls
    with the same kwargs contract.
    """
    from gateway_client import PushResult

    gw = MagicMock()
    gw.push_worktree_branch.return_value = PushResult(ok=True)
    return gw


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
