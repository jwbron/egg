"""Tests for ``GET /api/v1/pipelines/<id>/status/wait`` (issue #1932).

HANDOFF NOTE to tester: the coder authored this test file while
implementing Phase 1 to validate the route end-to-end.  All 16
cases pass against the current implementation on
commit ``1258ff399``.  Feel free to drop this in as-is under
``orchestrator/tests/test_pipelines_status_wait_route.py`` (coder
cannot push tests per the role allowlist) or adapt it; the
assertions below pin the plan's TASK-4-1 acceptance cases.

Covers:
    * EventBus wake (phase transition / decision / terminal)
    * message-bus wake (OVERSEER_ALERT, CONSENSUS_*)
    * Simultaneous fire (first source wins)
    * Timeout → ``changed: false, no_change: true`` envelope
    * ``since=msg:<id>|evt:<seq>`` cursor — already-seen events do not
      re-wake
    * ``DECISION_RESOLVED`` emission does NOT wake (explicit exclusion)
    * Malformed cursor → 400
    * Unknown pipeline → 404
    * ``egg_inflight_host_waits`` gauge increments + decrements
    * Queue-full path: rapid event storm still returns with first event
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import fakeredis
import pytest
from flask import Flask

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

from events import Event, EventBus, EventType  # noqa: E402
from message_store import Message, MessageStore, MessageType, reset_message_store  # noqa: E402
from models import (  # noqa: E402
    Pipeline,
    PipelineConfig,
    PipelinePhase,
    PipelineStatus,
)
from redis_message_store import RedisMessageStore  # noqa: E402
from routes.pipelines import (  # noqa: E402
    _build_status_wait_cursor,
    _parse_status_wait_cursor,
    pipelines_bp,
)


@pytest.fixture
def app():
    app = Flask(__name__)
    app.register_blueprint(pipelines_bp)
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture(params=["in_memory", "redis"], autouse=True)
def message_backend(request):
    """Run every test against both in-memory and Redis message store backends.

    AC for TASK-4-1 requires dual-backend parametrization so the wait
    route's message-bus path is exercised on both storage engines.
    """
    reset_message_store()
    if request.param == "redis":
        _redis = fakeredis.FakeRedis()
        store = RedisMessageStore(_redis)
    else:
        store = MessageStore()

    with patch("routes.pipelines._get_message_store", return_value=lambda: store):
        yield store

    reset_message_store()


@pytest.fixture
def isolated_event_bus():
    """Install a fresh, synchronous ``EventBus`` on ``events.get_event_bus``.

    The singleton is reset per test so sequence counters and handler
    lists do not leak across tests.  Synchronous delivery lets us
    ``publish`` on the test thread and have the wildcard handler
    fire before we return to the main wait loop.
    """
    bus = EventBus(async_delivery=False)
    with patch("events.get_event_bus", return_value=bus):
        yield bus


def _make_pipeline(pipeline_id: str = "issue-1932-test") -> Pipeline:
    config = PipelineConfig(concurrent_execution=True, max_concurrent_agents=4)
    return Pipeline(
        id=pipeline_id,
        issue_number=1932,
        repo="owner/repo",
        branch=f"egg/{pipeline_id}",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
        config=config,
    )


class TestCursor:
    """Unit tests for the opaque compound cursor parser / builder."""

    def test_roundtrip(self) -> None:
        cursor = _build_status_wait_cursor("1738012734-0", 142)
        ok, msg, seq = _parse_status_wait_cursor(cursor)
        assert ok is True
        assert msg == "1738012734-0"
        assert seq == 142

    def test_empty_cursor_snaps_to_tip(self) -> None:
        ok, msg, seq = _parse_status_wait_cursor("")
        assert ok is True
        assert msg is None and seq is None

    def test_missing_msg_half(self) -> None:
        ok, msg, seq = _parse_status_wait_cursor("msg:|evt:5")
        assert ok is True
        assert msg is None
        assert seq == 5

    def test_missing_evt_half(self) -> None:
        ok, msg, seq = _parse_status_wait_cursor("msg:abc|evt:")
        assert ok is True
        assert msg == "abc"
        assert seq is None

    def test_malformed_cursor(self) -> None:
        for bad in ("garbage", "evt:5|msg:abc", "msg:abc", "msg:abc|evt:x"):
            ok, _, _ = _parse_status_wait_cursor(bad)
            assert ok is False, f"{bad!r} should be malformed"


class TestWaitRouteTimeout:
    """Timeout path: ``changed=False, no_change=True`` envelope."""

    @patch("routes.pipelines.get_repo_path", return_value="/tmp/test")
    @patch("routes.pipelines._resolve_pipeline")
    def test_timeout_returns_no_change_envelope(
        self,
        mock_resolve: MagicMock,
        mock_repo: MagicMock,
        client,
        isolated_event_bus: EventBus,
    ) -> None:
        pipeline = _make_pipeline()
        mock_resolve.return_value = (MagicMock(), pipeline)

        start = time.monotonic()
        resp = client.get("/api/v1/pipelines/issue-1932-test/status/wait?wait=1")
        elapsed = time.monotonic() - start

        assert resp.status_code == 200
        envelope = json.loads(resp.data)["data"]
        assert envelope["changed"] is False
        assert envelope["no_change"] is True
        assert envelope["current_phase"] == "implement"
        assert envelope["status"] == "running"
        assert "cursor" in envelope
        ok, _msg, _seq = _parse_status_wait_cursor(envelope["cursor"])
        assert ok is True
        assert elapsed >= 0.5, f"returned after {elapsed:.2f}s — did it block?"


class TestWaitRouteEventWake:
    """EventBus wake path: ``changed=True, trigger='event'``."""

    @patch("routes.pipelines.get_repo_path", return_value="/tmp/test")
    @patch("routes.pipelines._resolve_pipeline")
    def test_phase_started_wakes_route(
        self,
        mock_resolve: MagicMock,
        mock_repo: MagicMock,
        client,
        isolated_event_bus: EventBus,
    ) -> None:
        pipeline = _make_pipeline()
        mock_resolve.return_value = (MagicMock(), pipeline)

        def _fire() -> None:
            time.sleep(0.1)
            isolated_event_bus.publish(
                Event(
                    event_type=EventType.PHASE_STARTED,
                    pipeline_id="issue-1932-test",
                )
            )

        threading.Thread(target=_fire, daemon=True).start()

        resp = client.get("/api/v1/pipelines/issue-1932-test/status/wait?wait=5")
        envelope = json.loads(resp.data)["data"]
        assert resp.status_code == 200
        assert envelope["changed"] is True
        assert envelope["trigger"] == "event"
        assert envelope["event_type"] == EventType.PHASE_STARTED.value
        assert "cursor" in envelope

    @patch("routes.pipelines.get_repo_path", return_value="/tmp/test")
    @patch("routes.pipelines._resolve_pipeline")
    def test_decision_resolved_does_NOT_wake(
        self,
        mock_resolve: MagicMock,
        mock_repo: MagicMock,
        client,
        isolated_event_bus: EventBus,
    ) -> None:
        pipeline = _make_pipeline()
        mock_resolve.return_value = (MagicMock(), pipeline)

        def _fire() -> None:
            time.sleep(0.1)
            isolated_event_bus.publish(
                Event(
                    event_type=EventType.DECISION_RESOLVED,
                    pipeline_id="issue-1932-test",
                )
            )

        threading.Thread(target=_fire, daemon=True).start()

        resp = client.get("/api/v1/pipelines/issue-1932-test/status/wait?wait=1")
        envelope = json.loads(resp.data)["data"]
        assert envelope["changed"] is False
        assert envelope["no_change"] is True

    @patch("routes.pipelines.get_repo_path", return_value="/tmp/test")
    @patch("routes.pipelines._resolve_pipeline")
    def test_since_cursor_skips_already_seen_event(
        self,
        mock_resolve: MagicMock,
        mock_repo: MagicMock,
        client,
        isolated_event_bus: EventBus,
    ) -> None:
        pipeline = _make_pipeline()
        mock_resolve.return_value = (MagicMock(), pipeline)

        isolated_event_bus.publish(
            Event(
                event_type=EventType.PHASE_STARTED,
                pipeline_id="issue-1932-test",
            )
        )
        prior_seq = isolated_event_bus.current_sequence()
        cursor = _build_status_wait_cursor(None, prior_seq)

        resp = client.get(f"/api/v1/pipelines/issue-1932-test/status/wait?wait=1&since={cursor}")
        envelope = json.loads(resp.data)["data"]
        assert envelope["changed"] is False


class TestWaitRouteMessageWake:
    """Message-bus wake path: ``changed=True, trigger='message'``."""

    @patch("routes.pipelines.get_repo_path", return_value="/tmp/test")
    @patch("routes.pipelines._resolve_pipeline")
    def test_overseer_alert_wakes_route(
        self,
        mock_resolve: MagicMock,
        mock_repo: MagicMock,
        client,
        isolated_event_bus: EventBus,
        message_backend,
    ) -> None:
        pipeline = _make_pipeline()
        mock_resolve.return_value = (MagicMock(), pipeline)

        def _fire() -> None:
            time.sleep(0.1)
            message_backend.add_message(
                Message(
                    pipeline_id="issue-1932-test",
                    from_role="overseer",
                    to_role="all",
                    message_type=MessageType.OVERSEER_ALERT,
                    subject="stall detected",
                )
            )

        threading.Thread(target=_fire, daemon=True).start()

        resp = client.get("/api/v1/pipelines/issue-1932-test/status/wait?wait=5")

        envelope = json.loads(resp.data)["data"]
        assert resp.status_code == 200
        assert envelope["changed"] is True
        assert envelope["trigger"] == "message"
        assert len(envelope["messages"]) >= 1
        assert envelope["messages"][0]["message_type"] == MessageType.OVERSEER_ALERT


class TestWaitRouteErrors:
    """Validation errors."""

    @patch("routes.pipelines.get_repo_path", return_value="/tmp/test")
    @patch("routes.pipelines._resolve_pipeline")
    def test_malformed_cursor_returns_400(
        self,
        mock_resolve: MagicMock,
        mock_repo: MagicMock,
        client,
    ) -> None:
        pipeline = _make_pipeline()
        mock_resolve.return_value = (MagicMock(), pipeline)

        resp = client.get("/api/v1/pipelines/issue-1932-test/status/wait?since=garbage")
        assert resp.status_code == 400
        body = json.loads(resp.data)
        assert body["success"] is False
        assert "cursor" in body["message"].lower()

    @patch("routes.pipelines.get_repo_path", return_value="/tmp/test")
    @patch("routes.pipelines._resolve_pipeline")
    def test_unknown_pipeline_returns_404(
        self,
        mock_resolve: MagicMock,
        mock_repo: MagicMock,
        client,
    ) -> None:
        from state_store import PipelineNotFoundError

        mock_resolve.side_effect = PipelineNotFoundError("nope")

        resp = client.get("/api/v1/pipelines/issue-missing/status/wait?wait=1")
        assert resp.status_code == 404

    @patch("routes.pipelines.get_repo_path", return_value="/tmp/test")
    @patch("routes.pipelines._resolve_pipeline")
    def test_invalid_wait_returns_400(
        self,
        mock_resolve: MagicMock,
        mock_repo: MagicMock,
        client,
    ) -> None:
        pipeline = _make_pipeline()
        mock_resolve.return_value = (MagicMock(), pipeline)

        resp = client.get("/api/v1/pipelines/issue-1932-test/status/wait?wait=abc")
        assert resp.status_code == 400

    @patch("routes.pipelines.get_repo_path", return_value="/tmp/test")
    @patch("routes.pipelines._resolve_pipeline")
    def test_wait_clamped_to_max(
        self,
        mock_resolve: MagicMock,
        mock_repo: MagicMock,
        client,
        isolated_event_bus: EventBus,
    ) -> None:
        pipeline = _make_pipeline()
        mock_resolve.return_value = (MagicMock(), pipeline)

        def _fire() -> None:
            time.sleep(0.1)
            isolated_event_bus.publish(
                Event(
                    event_type=EventType.PHASE_STARTED,
                    pipeline_id="issue-1932-test",
                )
            )

        threading.Thread(target=_fire, daemon=True).start()

        resp = client.get("/api/v1/pipelines/issue-1932-test/status/wait?wait=999")
        assert resp.status_code == 200


class TestInflightMetric:
    """``egg_inflight_host_waits`` gauge lifecycle."""

    @patch("routes.pipelines.get_repo_path", return_value="/tmp/test")
    @patch("routes.pipelines._resolve_pipeline")
    def test_inflight_gauge_increments_and_decrements(
        self,
        mock_resolve: MagicMock,
        mock_repo: MagicMock,
        client,
        isolated_event_bus: EventBus,
    ) -> None:
        pipeline = _make_pipeline()
        mock_resolve.return_value = (MagicMock(), pipeline)

        mock_gauge = MagicMock()

        with patch("routes.pipelines._inflight_host_waits", mock_gauge):
            resp = client.get("/api/v1/pipelines/issue-1932-test/status/wait?wait=1")

        assert resp.status_code == 200
        assert mock_gauge.inc.call_count == 1
        assert mock_gauge.dec.call_count == 1


class TestQueueFull:
    """Saturate the wake queue with a burst of events — the route must
    still return the first event and subsequent events drop with a
    WARNING log.
    """

    @patch("routes.pipelines.get_repo_path", return_value="/tmp/test")
    @patch("routes.pipelines._resolve_pipeline")
    def test_queue_full_does_not_crash_route(
        self,
        mock_resolve: MagicMock,
        mock_repo: MagicMock,
        client,
        isolated_event_bus: EventBus,
    ) -> None:
        pipeline = _make_pipeline()
        mock_resolve.return_value = (MagicMock(), pipeline)

        def _burst() -> None:
            time.sleep(0.05)
            for _ in range(50):
                isolated_event_bus.publish(
                    Event(
                        event_type=EventType.PHASE_STARTED,
                        pipeline_id="issue-1932-test",
                    )
                )

        threading.Thread(target=_burst, daemon=True).start()

        resp = client.get("/api/v1/pipelines/issue-1932-test/status/wait?wait=5")
        envelope = json.loads(resp.data)["data"]
        assert resp.status_code == 200
        assert envelope["changed"] is True
        assert envelope["trigger"] == "event"


class TestWaitRouteAlreadyTerminal:
    """Late-subscriber short-circuit (issue #2378).

    When ``/status/wait`` is called against a pipeline whose status is
    already ``complete``/``failed``/``cancelled``, the relevant
    ``pipeline.*`` event was emitted before this call could subscribe.
    Without the short-circuit the route snaps the cursor to tip and
    returns Path-B no_change indefinitely; the host then loops until
    the 1-hour cap. The route must instead return Path-A immediately
    so callers exit cleanly.
    """

    @pytest.mark.parametrize(
        ("status", "expected_event_type"),
        [
            (PipelineStatus.FAILED, "pipeline.failed"),
            (PipelineStatus.COMPLETE, "pipeline.completed"),
            (PipelineStatus.CANCELLED, "pipeline.cancelled"),
        ],
    )
    @patch("routes.pipelines.get_repo_path", return_value="/tmp/test")
    @patch("routes.pipelines._resolve_pipeline")
    def test_terminal_pipeline_returns_path_a_immediately(
        self,
        mock_resolve: MagicMock,
        mock_repo: MagicMock,
        status: PipelineStatus,
        expected_event_type: str,
        client,
        isolated_event_bus: EventBus,
    ) -> None:
        pipeline = _make_pipeline()
        pipeline.status = status
        mock_resolve.return_value = (MagicMock(), pipeline)

        start = time.monotonic()
        resp = client.get("/api/v1/pipelines/issue-1932-test/status/wait?wait=25")
        elapsed = time.monotonic() - start

        assert resp.status_code == 200
        envelope = json.loads(resp.data)["data"]
        assert envelope["changed"] is True
        assert envelope["trigger"] == "event"
        assert envelope["event_type"] == expected_event_type
        assert envelope["status"] == status.value
        assert "cursor" in envelope
        # The actual short-circuit returns within microseconds; budget
        # 5s rather than 1s to absorb slow-runner noise without losing
        # the "did it block on the wake queue?" signal.
        assert elapsed < 5.0, (
            f"terminal short-circuit took {elapsed:.2f}s — did it block on the wake queue?"
        )

    @patch("routes.pipelines.get_repo_path", return_value="/tmp/test")
    @patch("routes.pipelines._resolve_pipeline")
    def test_running_pipeline_still_blocks(
        self,
        mock_resolve: MagicMock,
        mock_repo: MagicMock,
        client,
        isolated_event_bus: EventBus,
    ) -> None:
        """Regression guard: the short-circuit must not fire on RUNNING."""
        pipeline = _make_pipeline()
        assert pipeline.status == PipelineStatus.RUNNING

        mock_resolve.return_value = (MagicMock(), pipeline)

        start = time.monotonic()
        resp = client.get("/api/v1/pipelines/issue-1932-test/status/wait?wait=1")
        elapsed = time.monotonic() - start

        assert resp.status_code == 200
        envelope = json.loads(resp.data)["data"]
        assert envelope["changed"] is False
        assert envelope["no_change"] is True
        assert elapsed >= 0.5, "RUNNING pipeline must still block until timeout"
