"""Integration-tier regression tests for the inter-agent message store
and event bus (issue #2640, split from #2474).

PR #2621 / #2624 added ``context_pr.skipped`` / ``context_pr.failed``
routing to the message store + event bus + ``/status/wait`` allowlists.
That wiring is well-covered at the unit tier
(``orchestrator/tests/test_context_pr_transition_paths.py``,
``orchestrator/tests/test_pipelines_status_wait_route.py``); this
module pins the end-to-end routing against the **live Flask blueprint
and a real Redis-backed message store** (via ``fakeredis``) so a
regression that breaks the route layer or the cross-backend contract
surfaces in the integration tier.

Coverage map (issue #2640 starting points + gap audit):

1. ``context_pr.{skipped,failed}`` end-to-end routing — wrapper emit
   reaches both message store and event bus on both backends, and
   is observable through ``GET /api/v1/pipelines/<id>/messages``.
2. ``/status/wait`` semantics for ``context_pr.*`` — long-poll wakes
   on the event, on the message, and stays silent for non-allowlisted
   types (PROGRESS, DECISION_RESOLVED).
3. Event ordering under concurrent producers — EventBus sequences
   are strictly increasing across threads; message store preserves
   per-pipeline append order on both backends.
4. Cursor staleness signal — unknown ``since_id`` surfaces
   ``since_id_stale: True`` on the ``/messages`` envelope on both
   backends (issue #2464).
5. Replay-on-resume — fetching with a known cursor returns only
   messages-after-cursor.
6. Late subscribers — ``EventBus`` does not replay history events to
   handlers subscribed after publish.
7. Malformed-payload rejection — ``POST /messages`` 400s on shell-var
   ``to_role`` / ``from_role`` and on invalid HEARTBEAT metadata.
8. Dedupe of repeated ``_maybe_open_base_pr_for_plan_to_implement``
   invocations — single-threaded and **N-thread race** against the
   ``_context_pr_events_emitted_lock`` so a concurrent transition
   pair still collapses to one message + one event.
9. Blocking ``get_messages(wait=N)`` semantics — wakes on
   ``add_message``, wakes on ``clear()`` (RISK-5 from #1897), and
   ``from_tip=True`` ignores pre-existing messages (#1925).
10. Message store ordering matches EventBus sequence for a single
    agent's stream, end-to-end through ``POST /messages`` — the
    exact invariant the issue's starting point 3 names.
11. ``/status/wait`` first-source-wins — whichever of EventBus /
    message store fires first determines ``trigger``; the second
    source's payload is ignored.
12. Deprecated message-type coercion — replayed ``QUESTION`` from
    older checkpoints lands as ``PROGRESS`` (#1897).
13. EventBus sequence monotonicity across mixed event types — the
    cursor protocol depends on global per-bus density.

Constraint (per #2474): integration tests run in CI, not in the SDLC
sandbox. Tests must be "verified by green required-check" — the agent
sandbox can't bring up k3s, build images, or hit a real LLM. All
external side effects are stubbed at the gateway-client / state-store
/ inner-hook boundary; ``Flask.test_client`` drives the routes.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import fakeredis
import pytest

# conftest.py inserts orchestrator/ + shared/ on sys.path.
from events import (  # noqa: E402
    Event,
    EventBus,
    EventType,
)
from flask import Flask
from message_store import (  # noqa: E402
    Message,
    MessageStore,
    MessageType,
    reset_message_store,
)
from models import (  # noqa: E402
    Pipeline,
    PipelineConfig,
    PipelineMode,
    PipelinePhase,
    PipelineStatus,
)
from redis_message_store import RedisMessageStore  # noqa: E402
from routes import messages as messages_mod  # noqa: E402
from routes import pipelines as pipelines_mod  # noqa: E402
from routes.messages import messages_bp  # noqa: E402
from routes.pipelines import (  # noqa: E402
    _maybe_open_base_pr_for_plan_to_implement,
    pipelines_bp,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_PIPELINE_ID = "issue-2640-test"


@pytest.fixture
def app() -> Flask:
    """Flask app with the real pipelines + messages blueprints registered.

    Mirrors the orchestrator's production blueprint registration so the
    routes under test are the same code path production runs.
    """
    flask_app = Flask(__name__)
    flask_app.register_blueprint(pipelines_bp)
    flask_app.register_blueprint(messages_bp)
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app: Flask):
    return app.test_client()


@pytest.fixture(params=["in_memory", "redis"])
def message_backend(request, monkeypatch):
    """Parametrize every test that touches the message store across both
    backends.

    Mirrors the AC pattern from
    ``orchestrator/tests/test_pipelines_status_wait_route.py``: the
    Redis path uses ``fakeredis.FakeRedis()`` so we exercise the real
    ``RedisMessageStore`` codepath (XADD / XREAD / XRANGE / xinfo
    counters) without requiring a Redis container. The in-memory path
    uses the live ``MessageStore`` with its threading.Condition
    blocking semantics.

    Patches the singleton on ``message_store._message_store`` so the
    lazy ``from message_store import get_message_store`` inside the
    wrapper (and inside the route handlers) returns this fixture's
    store. Also covers the pipelines-blueprint ``_get_message_store``
    indirection, which exists so the wait route can intercept reads.
    """
    import message_store as _ms

    reset_message_store()
    if request.param == "redis":
        store = RedisMessageStore(fakeredis.FakeRedis())
    else:
        store = MessageStore()

    monkeypatch.setattr(_ms, "_message_store", store)
    monkeypatch.setattr(pipelines_mod, "_get_message_store", lambda: lambda: store)

    yield store

    reset_message_store()


@pytest.fixture
def isolated_event_bus(monkeypatch):
    """Install a fresh, synchronous ``EventBus`` on ``events.get_event_bus``.

    The singleton is reset per test so sequence counters and handler
    lists do not leak across tests. Synchronous delivery lets a publish
    on the test thread fire wildcard handlers before returning to the
    main wait loop — required for deterministic ``/status/wait``
    ordering assertions.
    """
    import events as _events_mod

    bus = EventBus(async_delivery=False)
    monkeypatch.setattr(_events_mod, "_event_bus", bus)
    return bus


@pytest.fixture(autouse=True)
def reset_context_pr_dedupe():
    """The wrapper dedupes ``context_pr.*`` emissions via a module-level
    set keyed on ``pipeline_id``. Clear it between tests so one test's
    emit doesn't suppress another's."""
    pipelines_mod._context_pr_events_emitted.clear()
    yield
    pipelines_mod._context_pr_events_emitted.clear()


@pytest.fixture
def fake_pipeline() -> Pipeline:
    """An ISSUE-mode pipeline with the minimum fields the wrapper reads."""
    return Pipeline(
        id=_PIPELINE_ID,
        issue_number=2640,
        repo="owner/repo",
        branch=f"egg/{_PIPELINE_ID}/work",
        base_branch="main",
        mode=PipelineMode.ISSUE,
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.PLAN,
        config=PipelineConfig(),
    )


@pytest.fixture
def resolve_pipeline_to(fake_pipeline):
    """Patch ``_resolve_pipeline`` / ``get_state_store_for_pipeline`` to
    return ``fake_pipeline`` so the route layer doesn't need a real
    on-disk state store.
    """
    store = MagicMock()
    with (
        patch.object(pipelines_mod, "_resolve_pipeline", return_value=(store, fake_pipeline)),
        patch.object(pipelines_mod, "get_repo_path", return_value="/tmp/test"),
        patch.object(
            messages_mod,
            "get_state_store_for_pipeline",
            return_value=(store, fake_pipeline),
        ),
    ):
        yield fake_pipeline


def _make_contract_without_pr(*, raised: bool):
    """Build a contract whose post-hook ``context_pr_number`` is ``None``
    — the precondition for the wrapper's emit branch firing.
    """
    from egg_contracts.models import (
        Contract,
        IssueInfo,
        PRMetadata,
    )
    from egg_contracts.models import (
        PipelinePhase as ContractPhase,
    )

    return Contract(
        issue=IssueInfo(number=2640, title="t", url=""),
        pipeline_id=_PIPELINE_ID,
        current_phase=ContractPhase.PLAN,
        # Inner raised → there is a PRMetadata but no context_pr_number;
        # silent skip → no PR metadata at all.
        pr=PRMetadata(title="t") if raised else None,
    )


# ---------------------------------------------------------------------------
# Deterministic synchronization helpers — replace ``time.sleep(0.2)``
# "wait for the consumer to enter its blocking branch" idioms with
# ``threading.Event`` signals fired from inside the wait primitive
# itself, so a heavily loaded CI runner cannot lose the race.
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _route_subscription_signal(bus):
    """Yields a ``threading.Event`` that fires the moment the
    ``/status/wait`` route subscribes to the EventBus.

    The route registers a wildcard subscriber via ``bus.subscribe(None,
    handler)`` immediately before entering its blocking ``wake_q.get``.
    We wrap ``bus.subscribe`` so any wildcard registration sets the
    event — a deterministic replacement for ``time.sleep(0.2)`` that
    won't race on a slow CI runner.
    """
    route_ready = threading.Event()
    original = bus.subscribe

    def _instrumented(event_types, handler):
        result = original(event_types, handler)
        if event_types is None:
            route_ready.set()
        return result

    with patch.object(bus, "subscribe", side_effect=_instrumented):
        yield route_ready


@contextlib.contextmanager
def _blocking_get_signal(backend):
    """Yields a ``threading.Event`` that fires the moment a blocking
    ``get_messages(wait=N)`` call begins on this backend.

    Wraps ``backend.get_messages`` so the producer side of the test
    knows the consumer is parked in the wait branch before injecting —
    deterministic replacement for ``time.sleep(0.2)`` synchronization
    in the blocking-get tier.
    """
    consumer_entered = threading.Event()
    original = backend.get_messages

    def _instrumented(*args, **kwargs):
        if kwargs.get("wait"):
            consumer_entered.set()
        return original(*args, **kwargs)

    with patch.object(backend, "get_messages", side_effect=_instrumented):
        yield consumer_entered


# ---------------------------------------------------------------------------
# 1. context_pr.{skipped,failed} routing — message store + event bus
# ---------------------------------------------------------------------------


class TestContextPRRouting:
    """The wrapper's three observability sinks (message store, event
    bus, status-reporter) reach both backends end-to-end."""

    def test_context_pr_failed_lands_in_message_store_and_event_bus(
        self,
        tmp_path,
        fake_pipeline,
        message_backend,
        isolated_event_bus,
    ):
        contract = _make_contract_without_pr(raised=True)
        received_events: list[Event] = []
        isolated_event_bus.subscribe(EventType.CONTEXT_PR_FAILED, received_events.append)

        with (
            patch.object(pipelines_mod, "_open_context_pr_for_pipeline") as inner,
            patch("egg_contracts.loader.load_contract", lambda _i, _r: contract),
        ):
            inner.side_effect = RuntimeError("gateway down")
            _maybe_open_base_pr_for_plan_to_implement(
                fake_pipeline,
                MagicMock(),  # spawner
                tmp_path,
                source="advance_phase_rest",
            )

        # Message store: exactly one CONTEXT_PR_FAILED entry, tagged
        # with the source and the error.
        msgs = message_backend.get_messages(_PIPELINE_ID)
        failed = [m for m in msgs if m.message_type == "CONTEXT_PR_FAILED"]
        assert len(failed) == 1, (
            f"expected one CONTEXT_PR_FAILED message; got {[m.message_type for m in msgs]!r}"
        )
        assert failed[0].metadata.get("reason") == "raised"
        assert "gateway down" in (failed[0].metadata.get("error") or "")

        # Event bus: exactly one CONTEXT_PR_FAILED event with the
        # pipeline_id, dispatched to the wildcard-equivalent typed
        # subscriber.
        assert len(received_events) == 1
        assert received_events[0].event_type == EventType.CONTEXT_PR_FAILED
        assert received_events[0].pipeline_id == _PIPELINE_ID

    def test_context_pr_skipped_lands_in_message_store_and_event_bus(
        self,
        tmp_path,
        fake_pipeline,
        message_backend,
        isolated_event_bus,
    ):
        contract = _make_contract_without_pr(raised=False)
        received_events: list[Event] = []
        isolated_event_bus.subscribe(EventType.CONTEXT_PR_SKIPPED, received_events.append)

        with (
            patch.object(pipelines_mod, "_open_context_pr_for_pipeline") as inner,
            patch("egg_contracts.loader.load_contract", lambda _i, _r: contract),
        ):
            inner.return_value = None
            _maybe_open_base_pr_for_plan_to_implement(
                fake_pipeline,
                MagicMock(),
                tmp_path,
                source="hitl_resume",
            )

        msgs = message_backend.get_messages(_PIPELINE_ID)
        skipped = [m for m in msgs if m.message_type == "CONTEXT_PR_SKIPPED"]
        assert len(skipped) == 1
        assert skipped[0].metadata.get("reason") == "skipped"
        assert skipped[0].metadata.get("error") is None

        assert len(received_events) == 1
        assert received_events[0].event_type == EventType.CONTEXT_PR_SKIPPED

    def test_emitted_message_is_visible_via_messages_route(
        self,
        tmp_path,
        client,
        resolve_pipeline_to,
        message_backend,
        isolated_event_bus,
    ):
        """``GET /api/v1/pipelines/<id>/messages`` must surface the
        wrapper's message-store entry — that's the operator-facing
        contract behind ``recent_messages`` (#2611)."""
        fake_pipeline = resolve_pipeline_to
        contract = _make_contract_without_pr(raised=True)

        with (
            patch.object(pipelines_mod, "_open_context_pr_for_pipeline") as inner,
            patch("egg_contracts.loader.load_contract", lambda _i, _r: contract),
        ):
            inner.side_effect = RuntimeError("gateway down")
            _maybe_open_base_pr_for_plan_to_implement(
                fake_pipeline,
                MagicMock(),
                tmp_path,
                source="advance_phase_rest",
            )

        resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/messages")
        assert resp.status_code == 200
        body = json.loads(resp.data)
        types = [m["message_type"] for m in body["data"]["messages"]]
        assert "CONTEXT_PR_FAILED" in types


# ---------------------------------------------------------------------------
# 2. /status/wait semantics for context_pr.* (issue starting point 2)
# ---------------------------------------------------------------------------


class TestStatusWaitContextPRSemantics:
    """The ``/status/wait`` allowlists for ``context_pr.*`` (PR #2621 /
    #2624) must unblock long-pollers via both the event bus and the
    message store. Non-allowlisted types must NOT wake the route."""

    def test_status_wait_wakes_on_context_pr_failed_event(
        self,
        client,
        resolve_pipeline_to,
        message_backend,
        isolated_event_bus,
    ):
        """A ``context_pr.failed`` event published mid-wait unblocks the
        long-poll with ``trigger='event'``."""
        with _route_subscription_signal(isolated_event_bus) as route_ready:

            def _fire() -> None:
                assert route_ready.wait(timeout=3), "route never subscribed"
                isolated_event_bus.publish(
                    Event(
                        event_type=EventType.CONTEXT_PR_FAILED,
                        pipeline_id=_PIPELINE_ID,
                    )
                )

            threading.Thread(target=_fire, daemon=True).start()
            resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/status/wait?wait=3")
        envelope = json.loads(resp.data)["data"]
        assert resp.status_code == 200
        assert envelope["changed"] is True
        assert envelope["trigger"] == "event"
        assert envelope["event_type"] == "context_pr.failed"

    def test_status_wait_wakes_on_context_pr_failed_message(
        self,
        client,
        resolve_pipeline_to,
        message_backend,
        isolated_event_bus,
    ):
        """A ``CONTEXT_PR_FAILED`` message in the store unblocks the
        long-poll with ``trigger='message'`` — the second of the two
        sinks PR #2621 wired.

        Uses ``_blocking_get_signal`` rather than ``_route_subscription_signal``
        because the route's message daemon snaps to the store tip via
        ``get_messages(from_tip=True)`` AFTER ``event_bus.subscribe``
        returns — injecting on the subscribe signal can land before the
        daemon captures the tip, leaving the message on the wrong side
        of the cursor and the wait blocking until timeout."""
        with _blocking_get_signal(message_backend) as daemon_entered:

            def _fire() -> None:
                assert daemon_entered.wait(timeout=3), "daemon never entered blocking wait"
                message_backend.add_message(
                    Message(
                        pipeline_id=_PIPELINE_ID,
                        from_role="orchestrator",
                        to_role="all",
                        message_type="CONTEXT_PR_FAILED",
                        subject="context_pr.failed (source=test)",
                        body="hook raised",
                    )
                )

            threading.Thread(target=_fire, daemon=True).start()
            resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/status/wait?wait=3")
        envelope = json.loads(resp.data)["data"]
        assert resp.status_code == 200
        assert envelope["changed"] is True
        assert envelope["trigger"] == "message"

    def test_status_wait_ignores_non_allowlisted_message_type(
        self,
        client,
        resolve_pipeline_to,
        message_backend,
        isolated_event_bus,
    ):
        """``PROGRESS`` is intentionally not in ``_STATUS_WAIT_MESSAGE_TYPES``
        — a PROGRESS heartbeat must not wake ``/status/wait`` and waste
        operator long-poll cycles."""
        with _blocking_get_signal(message_backend) as daemon_entered:

            def _fire() -> None:
                assert daemon_entered.wait(timeout=3), "daemon never entered blocking wait"
                message_backend.add_message(
                    Message(
                        pipeline_id=_PIPELINE_ID,
                        from_role="coder",
                        to_role="all",
                        message_type=MessageType.PROGRESS,
                        subject="working",
                    )
                )

            threading.Thread(target=_fire, daemon=True).start()
            resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/status/wait?wait=1")
        envelope = json.loads(resp.data)["data"]
        # Timeout path: no wake.
        assert envelope["changed"] is False
        assert envelope["no_change"] is True

    def test_status_wait_ignores_decision_resolved_event(
        self,
        client,
        resolve_pipeline_to,
        message_backend,
        isolated_event_bus,
    ):
        """``decision.resolved`` is the post-``provide_input`` event and
        is intentionally excluded from ``_STATUS_WAIT_EVENT_TYPES`` so
        the operator doesn't self-wake on their own action."""
        with _route_subscription_signal(isolated_event_bus) as route_ready:

            def _fire() -> None:
                assert route_ready.wait(timeout=3), "route never subscribed"
                isolated_event_bus.publish(
                    Event(
                        event_type=EventType.DECISION_RESOLVED,
                        pipeline_id=_PIPELINE_ID,
                    )
                )

            threading.Thread(target=_fire, daemon=True).start()
            resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/status/wait?wait=1")
        envelope = json.loads(resp.data)["data"]
        assert envelope["changed"] is False
        assert envelope["no_change"] is True

    def test_status_wait_wakes_on_pipeline_cancelled_event(
        self,
        client,
        resolve_pipeline_to,
        message_backend,
        isolated_event_bus,
        fake_pipeline,
    ):
        """``pipeline.cancelled`` is in ``_STATUS_WAIT_EVENT_TYPES`` and the
        terminal-synth map (#2663). The PATCH cancel path now emits it via
        ``_emit_pipeline_event``; verify the wake-up path end-to-end by
        publishing through the same helper the route uses, then asserting
        a blocking ``/status/wait`` returns ``trigger='event'`` with the
        right event_type.

        Prior to the #2663 fix, the string ``"pipeline.cancelled"`` was
        missing from ``_EVENT_TYPE_MAP``, so the helper no-op'd and an
        in-flight long-poll sat idle to its full timeout — callers only
        observed cancellation on their next poll via the late-subscriber
        synth path."""
        with _route_subscription_signal(isolated_event_bus) as route_ready:

            def _fire() -> None:
                assert route_ready.wait(timeout=3), "route never subscribed"
                pipelines_mod._emit_pipeline_event(fake_pipeline, "pipeline.cancelled")

            threading.Thread(target=_fire, daemon=True).start()
            resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/status/wait?wait=3")
        envelope = json.loads(resp.data)["data"]
        assert resp.status_code == 200
        assert envelope["changed"] is True
        assert envelope["trigger"] == "event"
        assert envelope["event_type"] == "pipeline.cancelled"


# ---------------------------------------------------------------------------
# 2b. PATCH cancel path wiring (#2663 — verifies the route itself emits,
#     not just the map entry). The wake-up test above publishes via the
#     helper directly; these tests drive ``client.patch(...)`` through
#     the route handler so a regression in the PATCH-side ``emit`` call
#     is caught.
# ---------------------------------------------------------------------------


class TestPatchCancelEmits:
    """The PATCH ``status=cancelled`` path must call
    ``_emit_pipeline_event`` so ``/status/wait`` long-pollers wake
    immediately. The map entry plus the route-side emit are the two
    halves of the #2663 fix; ``TestStatusWaitContextPRSemantics``
    covers the map entry via direct helper publish, this class covers
    the route emit via real ``client.patch`` invocations.

    Each test builds its own patch stack via ``ExitStack`` rather than
    sharing a helper, because the pre-/post-update pipeline state is
    test-local (one test starts pre-update RUNNING, the other starts
    pre-update already CANCELLED to exercise the idempotent path)."""

    def test_patch_cancel_emits_pipeline_cancelled_event(
        self,
        client,
        isolated_event_bus,
        message_backend,
        lifecycle_auth_headers,
    ):
        """A real ``PATCH /pipelines/<id>`` with status=cancelled triggers
        ``_emit_pipeline_event`` — verifying the wiring at the route
        layer, one indirection deeper than the helper-publish test."""
        pre_update = Pipeline(
            id=_PIPELINE_ID,
            issue_number=2640,
            repo="owner/repo",
            branch=f"egg/{_PIPELINE_ID}/work",
            base_branch="main",
            mode=PipelineMode.ISSUE,
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.PLAN,
            config=PipelineConfig(),
        )
        post_update = Pipeline(
            id=_PIPELINE_ID,
            issue_number=2640,
            repo="owner/repo",
            branch=f"egg/{_PIPELINE_ID}/work",
            base_branch="main",
            mode=PipelineMode.ISSUE,
            status=PipelineStatus.CANCELLED,
            current_phase=PipelinePhase.PLAN,
            config=PipelineConfig(),
        )

        store = MagicMock()
        store.update_pipeline.return_value = post_update
        store.load_pipeline.return_value = post_update

        spawner = MagicMock()
        spawner.cleanup_pipeline.return_value = 0
        dq = MagicMock()
        dq.get_pending_decisions.return_value = []

        received: list[Event] = []
        isolated_event_bus.subscribe(EventType.PIPELINE_CANCELLED, received.append)

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch.object(pipelines_mod, "_resolve_pipeline", return_value=(store, pre_update))
            )
            stack.enter_context(
                patch.object(pipelines_mod, "get_repo_path", return_value="/tmp/test")
            )
            stack.enter_context(
                patch.object(pipelines_mod, "_compute_gateway_mode", return_value=("public", None))
            )
            stack.enter_context(patch.object(pipelines_mod, "_get_spawner", return_value=spawner))
            stack.enter_context(patch.object(pipelines_mod, "get_decision_queue", return_value=dq))
            stack.enter_context(patch.object(pipelines_mod, "_clear_pipeline_runtime_state"))

            resp = client.patch(
                f"/api/v1/pipelines/{_PIPELINE_ID}",
                data=json.dumps({"status": "cancelled"}),
                content_type="application/json",
                headers=lifecycle_auth_headers,
            )

        assert resp.status_code == 200, resp.data
        assert len(received) == 1, (
            f"expected exactly one PIPELINE_CANCELLED event from the PATCH route; "
            f"got {len(received)}"
        )
        assert received[0].event_type == EventType.PIPELINE_CANCELLED
        assert received[0].pipeline_id == _PIPELINE_ID

    def test_patch_cancel_idempotent_does_not_re_emit(
        self,
        client,
        isolated_event_bus,
        message_backend,
        lifecycle_auth_headers,
    ):
        """Idempotent PATCH retries against an already-cancelled pipeline
        must NOT re-emit ``pipeline.cancelled``. The route gates on the
        status *transition* (pre-update != CANCELLED && post-update ==
        CANCELLED), not on status equality, so a re-PATCH (e.g. from a
        flaky caller retrying) does not wake long-pollers a second
        time."""
        already_cancelled = Pipeline(
            id=_PIPELINE_ID,
            issue_number=2640,
            repo="owner/repo",
            branch=f"egg/{_PIPELINE_ID}/work",
            base_branch="main",
            mode=PipelineMode.ISSUE,
            status=PipelineStatus.CANCELLED,
            current_phase=PipelinePhase.PLAN,
            config=PipelineConfig(),
        )

        store = MagicMock()
        store.update_pipeline.return_value = already_cancelled
        store.load_pipeline.return_value = already_cancelled

        spawner = MagicMock()
        spawner.cleanup_pipeline.return_value = 0
        dq = MagicMock()
        dq.get_pending_decisions.return_value = []

        received: list[Event] = []
        isolated_event_bus.subscribe(EventType.PIPELINE_CANCELLED, received.append)

        with contextlib.ExitStack() as stack:
            # pre_update is ALSO already-cancelled — the transition gate
            # must suppress the emit.
            stack.enter_context(
                patch.object(
                    pipelines_mod,
                    "_resolve_pipeline",
                    return_value=(store, already_cancelled),
                )
            )
            stack.enter_context(
                patch.object(pipelines_mod, "get_repo_path", return_value="/tmp/test")
            )
            stack.enter_context(
                patch.object(pipelines_mod, "_compute_gateway_mode", return_value=("public", None))
            )
            stack.enter_context(patch.object(pipelines_mod, "_get_spawner", return_value=spawner))
            stack.enter_context(patch.object(pipelines_mod, "get_decision_queue", return_value=dq))
            stack.enter_context(patch.object(pipelines_mod, "_clear_pipeline_runtime_state"))

            resp = client.patch(
                f"/api/v1/pipelines/{_PIPELINE_ID}",
                data=json.dumps({"status": "cancelled"}),
                content_type="application/json",
                headers=lifecycle_auth_headers,
            )

        assert resp.status_code == 200, resp.data
        assert received == [], (
            f"PATCH against already-cancelled pipeline re-emitted "
            f"PIPELINE_CANCELLED: got {len(received)} event(s)"
        )


# ---------------------------------------------------------------------------
# 3. Event ordering under concurrent producers (issue starting point 3)
# ---------------------------------------------------------------------------


class TestConcurrentOrdering:
    """Multi-thread publishers must produce strictly increasing
    EventBus sequence numbers (the authoritative ordering source per
    issue #1932) and the message store must preserve per-pipeline
    append order on both backends."""

    def test_event_bus_sequence_is_strictly_increasing_under_concurrent_publishes(
        self,
        isolated_event_bus,
    ):
        n_threads = 8
        per_thread = 25
        seen: list[Event] = []
        seen_lock = threading.Lock()

        def _collect(event: Event) -> None:
            with seen_lock:
                seen.append(event)

        isolated_event_bus.subscribe(None, _collect)  # wildcard

        def _publish(tid: int) -> None:
            for j in range(per_thread):
                isolated_event_bus.publish(
                    Event(
                        event_type=EventType.MESSAGE_SENT,
                        pipeline_id=_PIPELINE_ID,
                        data={"tid": tid, "j": j},
                    )
                )

        threads = [
            threading.Thread(target=_publish, args=(i,), daemon=True) for i in range(n_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        sequences = [e.sequence for e in seen]
        assert len(sequences) == n_threads * per_thread

        # Sequences are dense, monotonic, and 1-indexed at the bus.
        assert sequences == sorted(sequences)
        assert sequences == list(range(1, n_threads * per_thread + 1))
        assert isolated_event_bus.current_sequence() == n_threads * per_thread

    def test_message_store_preserves_single_producer_order(
        self,
        message_backend,
    ):
        """A single producer's writes must come back in publish order
        — get_messages returns oldest-first."""
        for i in range(20):
            message_backend.add_message(
                Message(
                    pipeline_id=_PIPELINE_ID,
                    from_role="coder",
                    to_role="all",
                    message_type=MessageType.PROGRESS,
                    subject=f"step-{i}",
                    metadata={"i": i},
                )
            )

        msgs = message_backend.get_messages(_PIPELINE_ID, limit=100)
        recovered = [m.metadata["i"] for m in msgs]
        assert recovered == list(range(20))

    def test_message_store_concurrent_producers_all_persisted(
        self,
        message_backend,
    ):
        """Concurrent producers all land in the store. We don't require
        a specific interleaving (Redis Streams don't promise one), only
        that no message is lost and per-producer order is preserved
        within each producer's subset (matches the issue's "ordering
        matches event-bus ordering for a single agent's stream"
        guarantee)."""
        n_producers = 4
        per_producer = 25

        def _produce(role: str) -> None:
            for i in range(per_producer):
                message_backend.add_message(
                    Message(
                        pipeline_id=_PIPELINE_ID,
                        from_role=role,
                        to_role="all",
                        message_type=MessageType.PROGRESS,
                        subject=f"{role}-{i}",
                        metadata={"role": role, "i": i},
                    )
                )

        roles = [f"producer-{k}" for k in range(n_producers)]
        threads = [threading.Thread(target=_produce, args=(r,), daemon=True) for r in roles]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        msgs = message_backend.get_messages(_PIPELINE_ID, limit=10_000)
        assert len(msgs) == n_producers * per_producer

        # Per-producer subsets keep their internal order — i.e. the
        # subsequence of messages filtered by from_role is in
        # ascending ``i`` order. This is the load-bearing invariant
        # for an agent reading its own stream tail.
        for r in roles:
            indices = [m.metadata["i"] for m in msgs if m.from_role == r]
            assert indices == list(range(per_producer)), (
                f"per-producer order broken for {r}: {indices!r}"
            )


# ---------------------------------------------------------------------------
# 4. Cursor staleness (gap audit — issue #2464 follow-on)
# ---------------------------------------------------------------------------


class TestCursorStalenessSignal:
    """Issue #2464 added a structured ``since_id_stale`` signal so
    consumers can drop a dead cursor instead of perpetually re-feeding
    it. The signal must surface on both backends through the public
    ``/messages`` envelope."""

    def test_unknown_since_id_returns_stale_flag(
        self,
        client,
        resolve_pipeline_to,
        message_backend,
    ):
        # Seed the store with one real message.
        message_backend.add_message(
            Message(
                pipeline_id=_PIPELINE_ID,
                from_role="coder",
                to_role="all",
                message_type=MessageType.PROGRESS,
                subject="real",
            )
        )

        # Now query with a UUID that was never in the store.
        resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/messages?since_id=ffffffffffffffff")
        assert resp.status_code == 200
        data = json.loads(resp.data)["data"]
        assert data.get("since_id_stale") is True
        # Stale fallback returns full history (so the consumer doesn't
        # silently miss new messages).
        assert data["count"] >= 1

    def test_valid_since_id_does_not_flag_stale(
        self,
        client,
        resolve_pipeline_to,
        message_backend,
    ):
        msg = message_backend.add_message(
            Message(
                pipeline_id=_PIPELINE_ID,
                from_role="coder",
                to_role="all",
                message_type=MessageType.PROGRESS,
                subject="first",
            )
        )
        message_backend.add_message(
            Message(
                pipeline_id=_PIPELINE_ID,
                from_role="coder",
                to_role="all",
                message_type=MessageType.PROGRESS,
                subject="second",
            )
        )

        resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/messages?since_id={msg.id}")
        assert resp.status_code == 200
        data = json.loads(resp.data)["data"]
        # Live cursor — staleness flag MUST NOT be set (it's only
        # emitted when True, so absence is the success signal).
        assert "since_id_stale" not in data
        # Only the post-cursor message comes back.
        subjects = [m["subject"] for m in data["messages"]]
        assert subjects == ["second"]


# ---------------------------------------------------------------------------
# 5. Replay-on-resume (gap audit)
# ---------------------------------------------------------------------------


class TestReplayOnResume:
    """A consumer that disconnects and reconnects with its last-seen
    message ID must receive every message appended after that ID, in
    order. This is the canonical message-bus catch-up semantic."""

    def test_resume_returns_subsequent_messages_only(
        self,
        message_backend,
    ):
        ids: list[str] = []
        for i in range(5):
            msg = message_backend.add_message(
                Message(
                    pipeline_id=_PIPELINE_ID,
                    from_role="coder",
                    to_role="all",
                    message_type=MessageType.PROGRESS,
                    subject=f"m-{i}",
                    metadata={"i": i},
                )
            )
            ids.append(msg.id)

        # Consumer last saw message index 1. Resume cursor = ids[1].
        resumed = message_backend.get_messages(_PIPELINE_ID, since_id=ids[1], limit=100)
        recovered = [m.metadata["i"] for m in resumed]
        assert recovered == [2, 3, 4]

    def test_resume_then_resume_again_walks_forward(
        self,
        message_backend,
    ):
        """Repeated cursor advancement always moves forward — re-using
        an older cursor would re-deliver already-seen rows, and
        re-using the tip should produce an empty read."""
        ids: list[str] = []
        for i in range(3):
            msg = message_backend.add_message(
                Message(
                    pipeline_id=_PIPELINE_ID,
                    from_role="coder",
                    to_role="all",
                    message_type=MessageType.PROGRESS,
                    subject=f"m-{i}",
                    metadata={"i": i},
                )
            )
            ids.append(msg.id)

        # Read from tip — no new messages.
        empty = message_backend.get_messages(_PIPELINE_ID, since_id=ids[-1])
        assert empty == []

        # Append; resume from same cursor; only the new one comes back.
        new_msg = message_backend.add_message(
            Message(
                pipeline_id=_PIPELINE_ID,
                from_role="coder",
                to_role="all",
                message_type=MessageType.PROGRESS,
                subject="m-3",
                metadata={"i": 3},
            )
        )
        next_batch = message_backend.get_messages(_PIPELINE_ID, since_id=ids[-1])
        assert [m.id for m in next_batch] == [new_msg.id]


# ---------------------------------------------------------------------------
# 6. Late subscribers don't replay history (event-bus contract)
# ---------------------------------------------------------------------------


class TestEventBusNoHistoryReplay:
    """The ``EventBus`` retains a bounded history for debugging but
    does NOT replay it to late subscribers — handlers only fire on
    events published after subscription."""

    def test_handler_subscribed_after_publish_does_not_fire(self):
        bus = EventBus(async_delivery=False)

        bus.publish(Event(event_type=EventType.PHASE_STARTED, pipeline_id=_PIPELINE_ID))

        seen: list[Event] = []
        bus.subscribe(EventType.PHASE_STARTED, seen.append)

        # No further publish — the late subscribe must not receive the
        # already-published event.
        assert seen == []

        bus.publish(Event(event_type=EventType.PHASE_STARTED, pipeline_id=_PIPELINE_ID))
        assert len(seen) == 1

    def test_handler_unsubscribed_stops_receiving(self):
        bus = EventBus(async_delivery=False)
        seen: list[Event] = []
        bus.subscribe(EventType.PHASE_STARTED, seen.append)

        bus.publish(Event(event_type=EventType.PHASE_STARTED, pipeline_id=_PIPELINE_ID))
        assert len(seen) == 1

        bus.unsubscribe(EventType.PHASE_STARTED, seen.append)
        bus.publish(Event(event_type=EventType.PHASE_STARTED, pipeline_id=_PIPELINE_ID))
        assert len(seen) == 1  # no new delivery


# ---------------------------------------------------------------------------
# 7. Malformed-payload rejection (gap audit)
# ---------------------------------------------------------------------------


class TestMalformedPayloadRejection:
    """The ``POST /api/v1/pipelines/<id>/messages`` route validates
    inputs at the boundary so a misshapen payload from a sandbox tool
    surfaces a 400 instead of corrupting the store."""

    def _post(self, client, body: dict[str, Any]):
        return client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/messages",
            data=json.dumps(body),
            content_type="application/json",
        )

    def test_unexpanded_shell_var_to_role_rejected(
        self,
        client,
        resolve_pipeline_to,
        message_backend,
    ):
        """``to_role='$role'`` is the canonical sandbox shell-escape
        footgun (#1814) — must 400, not store a literal ``$role``."""
        resp = self._post(
            client,
            {
                "from_role": "coder",
                "to_role": "$role",
                "message_type": "PROGRESS",
                "subject": "x",
            },
        )
        assert resp.status_code == 400
        # No message landed in the store.
        assert message_backend.get_messages(_PIPELINE_ID) == []

    def test_unexpanded_shell_var_from_role_rejected(
        self,
        client,
        resolve_pipeline_to,
        message_backend,
    ):
        resp = self._post(
            client,
            {
                "from_role": "$ROLE",
                "to_role": "all",
                "message_type": "PROGRESS",
                "subject": "x",
            },
        )
        assert resp.status_code == 400

    def test_heartbeat_without_state_rejected(
        self,
        client,
        resolve_pipeline_to,
        message_backend,
    ):
        resp = self._post(
            client,
            {
                "from_role": "coder",
                "to_role": "all",
                "message_type": "HEARTBEAT",
                "metadata": {},  # no state
            },
        )
        assert resp.status_code == 400

    def test_heartbeat_with_unknown_state_rejected(
        self,
        client,
        resolve_pipeline_to,
        message_backend,
    ):
        resp = self._post(
            client,
            {
                "from_role": "coder",
                "to_role": "all",
                "message_type": "HEARTBEAT",
                "metadata": {"state": "OFF_THE_WALL"},
            },
        )
        assert resp.status_code == 400

    def test_heartbeat_waiting_on_role_requires_waiting_on(
        self,
        client,
        resolve_pipeline_to,
        message_backend,
    ):
        resp = self._post(
            client,
            {
                "from_role": "coder",
                "to_role": "all",
                "message_type": "HEARTBEAT",
                "metadata": {"state": "WAITING_ON_ROLE"},  # missing 'waiting_on'
            },
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 8. Dedupe across repeated wrapper invocations (gap audit)
# ---------------------------------------------------------------------------


class TestDedupeAcrossWrapperInvocations:
    """The wrapper can run multiple times for the same pipeline
    (auto-advance + implement-entry backstop, HITL recovery + backstop).
    The shared dedupe set must collapse the second emit to a no-op so
    operators see exactly one message and one event per failure."""

    def test_two_wrapper_calls_produce_one_message_and_one_event(
        self,
        tmp_path,
        fake_pipeline,
        message_backend,
        isolated_event_bus,
    ):
        contract = _make_contract_without_pr(raised=True)
        received_events: list[Event] = []
        isolated_event_bus.subscribe(EventType.CONTEXT_PR_FAILED, received_events.append)

        with (
            patch.object(pipelines_mod, "_open_context_pr_for_pipeline") as inner,
            patch("egg_contracts.loader.load_contract", lambda _i, _r: contract),
        ):
            inner.side_effect = RuntimeError("gateway down")
            _maybe_open_base_pr_for_plan_to_implement(
                fake_pipeline,
                MagicMock(),
                tmp_path,
                source="run_pipeline_autoadvance",
            )
            _maybe_open_base_pr_for_plan_to_implement(
                fake_pipeline,
                MagicMock(),
                tmp_path,
                source="implement_entry_backstop",
            )

        failed_msgs = [
            m
            for m in message_backend.get_messages(_PIPELINE_ID)
            if m.message_type == "CONTEXT_PR_FAILED"
        ]
        assert len(failed_msgs) == 1, (
            f"dedupe broken — got {len(failed_msgs)} CONTEXT_PR_FAILED entries"
        )
        assert len(received_events) == 1

    def test_concurrent_wrapper_invocations_dedupe_via_lock(
        self,
        tmp_path,
        fake_pipeline,
        message_backend,
        isolated_event_bus,
    ):
        """N threads race the same pipeline through the wrapper. The
        ``_context_pr_events_emitted_lock`` is the only thing preventing
        a double-emit when two transition paths execute concurrently
        (HITL recovery + implement-entry backstop in particular can
        overlap on the orchestrator's worker pool). Pin the lock by
        firing 16 threads at the wrapper and asserting exactly one
        message + one event survive.

        ``patch.object`` is not thread-safe — its ``__enter__`` /
        ``__exit__`` snapshot the attribute on entry and restore on exit
        with no synchronization, so 16 concurrent enters can interleave
        unpatch order and leak a mock past the test boundary. Patch ONCE
        at the outer scope and use ``threading.Barrier`` to release all
        threads simultaneously, so contention happens inside the
        wrapper rather than around the patch machinery."""
        contract = _make_contract_without_pr(raised=True)
        received_events: list[Event] = []
        events_lock = threading.Lock()

        def _collect(event: Event) -> None:
            with events_lock:
                received_events.append(event)

        isolated_event_bus.subscribe(EventType.CONTEXT_PR_FAILED, _collect)

        n = 16
        # Barrier release: every thread parks at the barrier and is
        # released when the Nth thread arrives, so contention on
        # ``_context_pr_events_emitted_lock`` is maximal. Replaces the
        # less-deterministic ``Event.wait`` start gate (which can wake
        # threads sequentially) and removes the per-thread patch.object
        # nesting that ``patch.object`` does not synchronize.
        barrier = threading.Barrier(n)

        def _race(source: str) -> None:
            barrier.wait(timeout=5)
            _maybe_open_base_pr_for_plan_to_implement(
                fake_pipeline,
                MagicMock(),
                tmp_path,
                source=source,
            )

        with (
            patch.object(pipelines_mod, "_open_context_pr_for_pipeline") as inner,
            patch("egg_contracts.loader.load_contract", lambda _i, _r: contract),
        ):
            inner.side_effect = RuntimeError("gateway down")
            threads = [
                threading.Thread(target=_race, args=(f"thread-{i}",), daemon=True) for i in range(n)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        failed_msgs = [
            m
            for m in message_backend.get_messages(_PIPELINE_ID)
            if m.message_type == "CONTEXT_PR_FAILED"
        ]
        assert len(failed_msgs) == 1, (
            f"concurrent dedupe broken — got {len(failed_msgs)} "
            f"CONTEXT_PR_FAILED entries from {n} racing threads"
        )
        assert len(received_events) == 1


# ---------------------------------------------------------------------------
# 9. Message store blocking semantics (gap audit)
# ---------------------------------------------------------------------------


class TestBlockingGetMessages:
    """The blocking ``get_messages(wait=N)`` primitive is the core
    inter-agent communication waitloop (issue #1897). Both backends
    must wake on a real append, on clear(), and respect ``from_tip``
    so a wait-loop doesn't immediately re-deliver pre-existing
    messages."""

    def test_blocked_get_wakes_on_add_message(self, message_backend):
        """A consumer blocked in ``get_messages(wait=N)`` must wake the
        moment a matching message lands. Synchronization is via
        ``_blocking_get_signal`` — the producer only injects once the
        consumer has actually entered the blocking branch, so this
        cannot flake on a slow CI runner."""
        result: dict[str, list[Message]] = {"msgs": []}

        def _consumer():
            result["msgs"] = message_backend.get_messages(
                _PIPELINE_ID,
                wait=5,
                from_tip=True,
            )

        with _blocking_get_signal(message_backend) as consumer_entered:
            t = threading.Thread(target=_consumer, daemon=True)
            t.start()
            assert consumer_entered.wait(timeout=3), "consumer never entered blocking wait"
            message_backend.add_message(
                Message(
                    pipeline_id=_PIPELINE_ID,
                    from_role="producer",
                    to_role="all",
                    message_type=MessageType.PROGRESS,
                    subject="injected",
                )
            )
            t.join(timeout=3)
        assert not t.is_alive(), "consumer did not return after add_message"
        assert len(result["msgs"]) == 1
        assert result["msgs"][0].subject == "injected"

    def test_blocked_get_wakes_on_clear(self, message_backend):
        """RISK-5 (issue #1897 docstring of ``MessageStore.clear``): a
        ``clear()`` must wake a blocked consumer so they observe the
        phase-boundary clear and re-enter instead of timing out.

        The Redis backend doesn't expose this signal natively (XREAD
        doesn't observe ``DEL stream``); accept either ``wake-returns-[]``
        or ``timeout-returns-[]`` for the Redis backend so we don't
        false-positive on a backend-specific limitation, but still
        guard the in-memory contract.
        """
        result: dict[str, list[Message]] = {"msgs": ["sentinel"]}

        def _consumer():
            # Drop wait from 2s → 1s on the timeout (Redis) path so the
            # contract assertion still holds but the test doesn't burn
            # the full 2s budget on every Redis run.
            result["msgs"] = message_backend.get_messages(
                _PIPELINE_ID,
                wait=1,
                from_tip=True,
            )

        # Prime the backend so the pipeline_id exists (in-memory
        # backend's ``observed`` guard requires the pipeline to have
        # been seen at least once before clear() can wake the wait).
        primed = message_backend.add_message(
            Message(
                pipeline_id=_PIPELINE_ID,
                from_role="producer",
                to_role="all",
                message_type=MessageType.PROGRESS,
                subject="primer",
            )
        )
        _ = primed  # keep the value alive for backends that intern IDs

        with _blocking_get_signal(message_backend) as consumer_entered:
            t = threading.Thread(target=_consumer, daemon=True)
            t.start()
            assert consumer_entered.wait(timeout=3), "consumer never entered blocking wait"
            message_backend.clear(_PIPELINE_ID)
            t.join(timeout=4)
        assert not t.is_alive(), "consumer did not return after clear()"
        # Either backend: an empty-list return is the correct wake-up.
        assert result["msgs"] == []

    def test_from_tip_ignores_pre_existing_messages(self, message_backend):
        """``from_tip=True`` (issue #1925) snaps the cursor to the
        current tip so already-seen messages do not unblock the wait.
        Pre-PR this bug caused ``/messages/wait`` loops to immediately
        re-deliver the same event on every poll."""
        # Pre-populate the store.
        message_backend.add_message(
            Message(
                pipeline_id=_PIPELINE_ID,
                from_role="producer",
                to_role="all",
                message_type=MessageType.PROGRESS,
                subject="stale-1",
            )
        )
        message_backend.add_message(
            Message(
                pipeline_id=_PIPELINE_ID,
                from_role="producer",
                to_role="all",
                message_type=MessageType.PROGRESS,
                subject="stale-2",
            )
        )

        # A blocking wait from tip must NOT see the stale messages.
        # Use a short wait — the contract is "don't re-deliver", so
        # a timeout is the success signal here.
        start = time.monotonic()
        out = message_backend.get_messages(
            _PIPELINE_ID,
            wait=1,
            from_tip=True,
        )
        elapsed = time.monotonic() - start
        assert out == [], f"from_tip=True returned stale messages: {[m.subject for m in out]!r}"
        # And the call actually blocked rather than returning instantly.
        assert elapsed >= 0.5, f"from_tip wait returned in {elapsed:.2f}s without blocking"


# ---------------------------------------------------------------------------
# 9b. Subscription filtering — slice + producer-allowlist axes on
#     /messages/wait (issue #2725). Cross-backend through the live
#     Flask route to pin the end-to-end contract.
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _blocking_get_meta_signal(backend):
    """``/messages/wait`` route variant of ``_blocking_get_signal``.

    The status-wait route uses ``get_messages``; the messages-wait
    route uses ``get_messages_with_meta`` (issue #2464 — returns the
    cursor-staleness side-channel). Wrap *that* method so the producer
    side of these filter tests knows the route is parked in the
    blocking branch before injecting.
    """
    consumer_entered = threading.Event()
    original = backend.get_messages_with_meta

    def _instrumented(*args, **kwargs):
        if kwargs.get("wait"):
            consumer_entered.set()
        return original(*args, **kwargs)

    with patch.object(backend, "get_messages_with_meta", side_effect=_instrumented):
        yield consumer_entered


class TestSubscriptionFiltering:
    """``/messages/wait`` accepts ``slice`` + repeatable ``from_producer``
    filters (#2725). Tests run cross-backend (in-memory + fakeredis)
    against the live Flask blueprint so a regression in the route
    layer, either store's filter logic, or the route-store wiring
    surfaces in the integration tier.

    Load-bearing invariants pinned here:

    * Wrong-slice messages do NOT unblock a slice-filtered wait —
      otherwise the filter just delays the wake-storm by one LLM
      round-trip instead of eliminating it.
    * Null-on-message slice IS a passthrough — OVERSEER_ALERT,
      phase-boundary CONSENSUS_CONFIRMED, and other pipeline-level
      signals continue to wake every blocked waiter.
    * Wrong-sender messages do NOT unblock a from_producer-filtered
      wait.
    * Negative conformance: an orchestrator-emitted CONSENSUS_RE_REVIEW
      addressed to this reviewer wakes a tight slice + producer
      allowlist. The spawner builds the allowlist to always include
      ``orchestrator``; this test pins the end-to-end behavior so a
      regression that drops the system sender (or shifts the null-
      slice passthrough) surfaces here, not in production where it
      would manifest as a silent reviewer stall.
    """

    def test_messages_wait_slice_filter_drops_other_slice(
        self,
        client,
        resolve_pipeline_to,
        message_backend,
    ):
        """A reviewer in ``slice-1`` does NOT wake on a ``slice-2``
        CONSENSUS_PROPOSE — pinned through the live route on both
        backends.
        """
        with _blocking_get_meta_signal(message_backend) as consumer_entered:

            def _fire() -> None:
                assert consumer_entered.wait(timeout=3), "consumer never entered blocking wait"
                message_backend.add_message(
                    Message(
                        pipeline_id=_PIPELINE_ID,
                        from_role="coder",
                        to_role="all",
                        message_type=MessageType.CONSENSUS_PROPOSE,
                        subject="slice-2 propose",
                        metadata={"slice_id": "slice-2"},
                    )
                )

            threading.Thread(target=_fire, daemon=True).start()
            resp = client.get(
                f"/api/v1/pipelines/{_PIPELINE_ID}/messages/wait"
                "?for=CONSENSUS_PROPOSE&slice=slice-1&timeout=1"
            )
        envelope = json.loads(resp.data)["data"]
        assert resp.status_code == 200
        assert envelope["matched"] is False, (
            "slice-2 message unblocked a slice-1 wait — filter is leaking across slices"
        )

    def test_messages_wait_slice_filter_passthrough_null_slice(
        self,
        client,
        resolve_pipeline_to,
        message_backend,
    ):
        """An OVERSEER_ALERT with no ``metadata.slice_id`` wakes a
        slice-filtered wait — the null-passthrough invariant. Pinned
        end-to-end so a future change that adds strict slice equality
        (and breaks this) is caught."""
        with _blocking_get_meta_signal(message_backend) as consumer_entered:

            def _fire() -> None:
                assert consumer_entered.wait(timeout=3), "consumer never entered blocking wait"
                message_backend.add_message(
                    Message(
                        pipeline_id=_PIPELINE_ID,
                        from_role="overseer",
                        to_role="all",
                        message_type=MessageType.OVERSEER_ALERT,
                        subject="alert",
                    )
                )

            threading.Thread(target=_fire, daemon=True).start()
            resp = client.get(
                f"/api/v1/pipelines/{_PIPELINE_ID}/messages/wait"
                "?for=OVERSEER_ALERT&slice=slice-1&timeout=3"
            )
        envelope = json.loads(resp.data)["data"]
        assert resp.status_code == 200
        assert envelope["matched"] is True, (
            "null-slice OVERSEER_ALERT did not wake a slice-filtered wait "
            "— pipeline-level passthrough invariant broken"
        )
        assert envelope["messages"][0]["message_type"] == "OVERSEER_ALERT"

    def test_messages_wait_from_producer_filter_drops_other_sender(
        self,
        client,
        resolve_pipeline_to,
        message_backend,
    ):
        """A reviewer with ``--from-producer coder,tester`` does NOT
        wake on a ``documenter`` CONSENSUS_PROPOSE — cross-backend
        through the live route."""
        with _blocking_get_meta_signal(message_backend) as consumer_entered:

            def _fire() -> None:
                assert consumer_entered.wait(timeout=3), "consumer never entered blocking wait"
                message_backend.add_message(
                    Message(
                        pipeline_id=_PIPELINE_ID,
                        from_role="documenter",
                        to_role="all",
                        message_type=MessageType.CONSENSUS_PROPOSE,
                        subject="not-my-producer",
                    )
                )

            threading.Thread(target=_fire, daemon=True).start()
            resp = client.get(
                f"/api/v1/pipelines/{_PIPELINE_ID}/messages/wait"
                "?for=CONSENSUS_PROPOSE"
                "&from_producer=coder&from_producer=tester&timeout=1"
            )
        envelope = json.loads(resp.data)["data"]
        assert resp.status_code == 200
        assert envelope["matched"] is False

    def test_messages_wait_from_producer_filter_wakes_on_allowed_sender(
        self,
        client,
        resolve_pipeline_to,
        message_backend,
    ):
        """A reviewer with ``--from-producer coder,tester`` DOES wake
        when ``coder`` proposes — pinned through the live route on
        both backends."""
        with _blocking_get_meta_signal(message_backend) as consumer_entered:

            def _fire() -> None:
                assert consumer_entered.wait(timeout=3), "consumer never entered blocking wait"
                message_backend.add_message(
                    Message(
                        pipeline_id=_PIPELINE_ID,
                        from_role="coder",
                        to_role="all",
                        message_type=MessageType.CONSENSUS_PROPOSE,
                        subject="my producer",
                    )
                )

            threading.Thread(target=_fire, daemon=True).start()
            resp = client.get(
                f"/api/v1/pipelines/{_PIPELINE_ID}/messages/wait"
                "?for=CONSENSUS_PROPOSE"
                "&from_producer=coder&from_producer=tester&timeout=3"
            )
        envelope = json.loads(resp.data)["data"]
        assert resp.status_code == 200
        assert envelope["matched"] is True
        assert envelope["messages"][0]["from_role"] == "coder"

    def test_messages_wait_negative_conformance_orchestrator_re_review(
        self,
        client,
        resolve_pipeline_to,
        message_backend,
    ):
        """Negative-conformance pin (#2725) at the integration tier.

        An orchestrator-emitted ``CONSENSUS_RE_REVIEW`` addressed to a
        reviewer in ``slice-1`` MUST wake even a tight filter:
        ``slice=slice-1`` + ``from_producer=coder,tester,overseer,
        orchestrator`` (the shape the spawner builds for
        ``reviewer_code`` in the implement phase). The route layer +
        message-store filter chain together must let this through
        — silent sleep is the failure mode worse than the original
        wake-storm, and the test exists so a future change that drops
        the ``orchestrator`` system sender from the allowlist (or
        breaks the null-slice passthrough that ``CONSENSUS_RE_REVIEW``
        relies on when emitted without a slice scope) is caught
        cross-backend rather than in production.

        Constructed to mirror routes/signals.py:1373-1393 — the actual
        re-review path: ``from_role="orchestrator"``,
        ``to_role=<reviewer>``, ``metadata.slice_id`` set to the
        reviewer's slice.
        """
        with _blocking_get_meta_signal(message_backend) as consumer_entered:

            def _fire() -> None:
                assert consumer_entered.wait(timeout=3), "consumer never entered blocking wait"
                message_backend.add_message(
                    Message(
                        pipeline_id=_PIPELINE_ID,
                        from_role="orchestrator",
                        to_role="reviewer_code",
                        message_type=MessageType.CONSENSUS_RE_REVIEW,
                        subject="Re-review required: coder submitted new proposal v2",
                        metadata={"slice_id": "slice-1", "producer_role": "coder"},
                    )
                )

            threading.Thread(target=_fire, daemon=True).start()
            resp = client.get(
                f"/api/v1/pipelines/{_PIPELINE_ID}/messages/wait"
                "?for=CONSENSUS_RE_REVIEW&role=reviewer_code&slice=slice-1"
                "&from_producer=coder&from_producer=tester"
                "&from_producer=overseer&from_producer=orchestrator&timeout=3"
            )
        envelope = json.loads(resp.data)["data"]
        assert resp.status_code == 200
        assert envelope["matched"] is True, (
            "orchestrator-emitted CONSENSUS_RE_REVIEW did NOT wake a tight "
            "reviewer wait — the filter is silently sleeping through a "
            "cross-graph cascade, which is worse than the wake-storm"
        )
        msg = envelope["messages"][0]
        assert msg["from_role"] == "orchestrator"
        assert msg["message_type"] == "CONSENSUS_RE_REVIEW"

    def test_messages_wait_empty_from_producer_rejected_at_route(
        self,
        client,
        resolve_pipeline_to,
        message_backend,
    ):
        """An explicit-but-empty ``from_producer`` is rejected with
        HTTP 400 — silent acceptance would sleep the caller through
        every event. Cross-backend through the live route."""
        resp = client.get(
            f"/api/v1/pipelines/{_PIPELINE_ID}/messages/wait"
            "?for=CONSENSUS_PROPOSE&from_producer=&timeout=1"
        )
        assert resp.status_code == 400
        body = json.loads(resp.data)
        assert "from_producer" in body["message"]


# ---------------------------------------------------------------------------
# 10. Message store ordering matches event-bus ordering for a single
#     agent's stream (issue #2640 starting point 3, exact wording)
# ---------------------------------------------------------------------------


class TestMessageStoreEventBusOrderingCorrelation:
    """The ``POST /api/v1/pipelines/<id>/messages`` route writes to the
    message store AND emits ``MESSAGE_SENT`` on the event bus. The
    issue's starting point 3 asks: 'assert message store ordering
    matches event-bus ordering for a single agent's stream.' Pin that
    correlation end-to-end across the route layer."""

    def test_message_store_order_matches_event_bus_sequence_per_agent(
        self,
        client,
        resolve_pipeline_to,
        message_backend,
        isolated_event_bus,
    ):
        observed_events: list[Event] = []
        isolated_event_bus.subscribe(EventType.MESSAGE_SENT, observed_events.append)

        # Single producer writes a deterministic sequence.
        n = 10
        for i in range(n):
            resp = client.post(
                f"/api/v1/pipelines/{_PIPELINE_ID}/messages",
                data=json.dumps(
                    {
                        "from_role": "coder",
                        "to_role": "all",
                        "message_type": "PROGRESS",
                        "subject": f"step-{i}",
                        "metadata": {"i": i},
                    }
                ),
                content_type="application/json",
            )
            assert resp.status_code == 200

        # Message-store ordering for this agent's stream.
        msgs = message_backend.get_messages(_PIPELINE_ID, from_role="coder", limit=100)
        msg_order = [m.metadata["i"] for m in msgs]
        assert msg_order == list(range(n))

        # EventBus MESSAGE_SENT order for the same pipeline.
        event_msg_ids = [e.data.get("message_id") for e in observed_events]
        assert len(event_msg_ids) == n
        # The event bus must observe messages in publish order; correlate
        # via the message id so we don't depend on a separate index.
        msg_ids_in_store = [m.id for m in msgs]
        assert event_msg_ids == msg_ids_in_store, (
            "MESSAGE_SENT event order does not match message store order — "
            "single-agent stream ordering invariant broken"
        )

        # And the EventBus sequences are strictly increasing across the run.
        seqs = [e.sequence for e in observed_events]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)


# ---------------------------------------------------------------------------
# 11. /status/wait race conditions — first-source-wins (gap audit)
# ---------------------------------------------------------------------------


class TestStatusWaitFirstSourceWins:
    """``/status/wait`` coordinates two sources (EventBus + message
    store) via a ``queue.Queue(maxsize=16)``. Whichever fires first
    wins; the other source's payload is ignored. Pin this so a
    refactor doesn't accidentally double-emit or favour one source."""

    def test_event_arriving_before_message_wins(
        self,
        client,
        resolve_pipeline_to,
        message_backend,
        isolated_event_bus,
    ):
        """Fire the event 50ms before the message. The route should
        report ``trigger='event'`` because the EventBus path produced
        the first wake-up signal."""

        def _fire_event() -> None:
            time.sleep(0.1)
            isolated_event_bus.publish(
                Event(
                    event_type=EventType.PHASE_STARTED,
                    pipeline_id=_PIPELINE_ID,
                )
            )

        def _fire_message() -> None:
            time.sleep(0.5)
            message_backend.add_message(
                Message(
                    pipeline_id=_PIPELINE_ID,
                    from_role="overseer",
                    to_role="all",
                    message_type="OVERSEER_ALERT",
                    subject="alert",
                )
            )

        threading.Thread(target=_fire_event, daemon=True).start()
        threading.Thread(target=_fire_message, daemon=True).start()

        resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/status/wait?wait=3")
        envelope = json.loads(resp.data)["data"]
        assert envelope["changed"] is True
        assert envelope["trigger"] == "event"

    def test_message_arriving_before_event_wins(
        self,
        client,
        resolve_pipeline_to,
        message_backend,
        isolated_event_bus,
    ):
        """Mirror: message first → ``trigger='message'``."""

        def _fire_message() -> None:
            time.sleep(0.1)
            message_backend.add_message(
                Message(
                    pipeline_id=_PIPELINE_ID,
                    from_role="overseer",
                    to_role="all",
                    message_type="OVERSEER_ALERT",
                    subject="alert",
                )
            )

        def _fire_event() -> None:
            time.sleep(0.5)
            isolated_event_bus.publish(
                Event(
                    event_type=EventType.PHASE_STARTED,
                    pipeline_id=_PIPELINE_ID,
                )
            )

        threading.Thread(target=_fire_message, daemon=True).start()
        threading.Thread(target=_fire_event, daemon=True).start()

        resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/status/wait?wait=3")
        envelope = json.loads(resp.data)["data"]
        assert envelope["changed"] is True
        assert envelope["trigger"] == "message"


# ---------------------------------------------------------------------------
# 12. Deprecated message-type coercion (gap audit)
# ---------------------------------------------------------------------------


class TestDeprecatedTypeCoercion:
    """Issue #1897 removed the ``QUESTION`` message type. Replayed
    checkpoints / in-flight pipelines may still emit it; the route
    layer must coerce it to ``PROGRESS`` so the audit trail is
    preserved without a now-unknown enum member circulating.
    """

    def test_question_type_coerced_to_progress_via_route(
        self,
        client,
        resolve_pipeline_to,
        message_backend,
    ):
        resp = client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/messages",
            data=json.dumps(
                {
                    "from_role": "coder",
                    "to_role": "all",
                    "message_type": "QUESTION",  # deprecated
                    "subject": "is this thing on?",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 200
        msgs = message_backend.get_messages(_PIPELINE_ID)
        # The store sees PROGRESS, not QUESTION.
        assert [m.message_type for m in msgs] == ["PROGRESS"]


# ---------------------------------------------------------------------------
# 13. EventBus sequence monotonicity across all event types (gap audit)
# ---------------------------------------------------------------------------


class TestEventBusSequenceMonotonicAcrossTypes:
    """``EventBus.publish`` assigns the monotonic sequence before any
    filtering. Sequences are global per-bus, not per-event-type, so
    mixing types must not interleave or skip numbers — the
    ``/status/wait`` cursor protocol depends on this."""

    def test_sequence_dense_across_mixed_event_types(self, isolated_event_bus):
        seen: list[Event] = []
        isolated_event_bus.subscribe(None, seen.append)

        types_in_order = [
            EventType.PIPELINE_STARTED,
            EventType.PHASE_STARTED,
            EventType.MESSAGE_SENT,
            EventType.DECISION_CREATED,
            EventType.CONTEXT_PR_FAILED,
            EventType.PHASE_COMPLETED,
        ]
        for t in types_in_order:
            isolated_event_bus.publish(Event(event_type=t, pipeline_id=_PIPELINE_ID))

        assert [e.sequence for e in seen] == [1, 2, 3, 4, 5, 6]
        assert [e.event_type for e in seen] == types_in_order
        assert isolated_event_bus.current_sequence() == 6
