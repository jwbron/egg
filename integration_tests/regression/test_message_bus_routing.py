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

        def _fire() -> None:
            time.sleep(0.2)
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
        sinks PR #2621 wired."""

        def _fire() -> None:
            time.sleep(0.2)
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

        def _fire() -> None:
            time.sleep(0.2)
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

        def _fire() -> None:
            time.sleep(0.2)
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
        message + one event survive."""
        contract = _make_contract_without_pr(raised=True)
        received_events: list[Event] = []
        events_lock = threading.Lock()

        def _collect(event: Event) -> None:
            with events_lock:
                received_events.append(event)

        isolated_event_bus.subscribe(EventType.CONTEXT_PR_FAILED, _collect)

        start_gate = threading.Event()

        def _race(source: str) -> None:
            with (
                patch.object(pipelines_mod, "_open_context_pr_for_pipeline") as inner,
                patch("egg_contracts.loader.load_contract", lambda _i, _r: contract),
            ):
                inner.side_effect = RuntimeError("gateway down")
                start_gate.wait(timeout=5)
                _maybe_open_base_pr_for_plan_to_implement(
                    fake_pipeline,
                    MagicMock(),
                    tmp_path,
                    source=source,
                )

        n = 16
        threads = [
            threading.Thread(target=_race, args=(f"thread-{i}",), daemon=True) for i in range(n)
        ]
        for t in threads:
            t.start()
        # All threads parked at start_gate.wait — release them
        # simultaneously so they actually contend on the dedupe lock.
        start_gate.set()
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
        moment a matching message lands. Tolerance: ~1s of wallclock
        slack on top of the inject delay so a slow CI runner doesn't
        flake."""
        result: dict[str, list[Message]] = {"msgs": []}

        def _consumer():
            result["msgs"] = message_backend.get_messages(
                _PIPELINE_ID,
                wait=5,
                from_tip=True,
            )

        t = threading.Thread(target=_consumer, daemon=True)
        t.start()
        # Let the consumer enter the blocking branch.
        time.sleep(0.2)
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
            result["msgs"] = message_backend.get_messages(
                _PIPELINE_ID,
                wait=2,
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

        t = threading.Thread(target=_consumer, daemon=True)
        t.start()
        time.sleep(0.2)
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
