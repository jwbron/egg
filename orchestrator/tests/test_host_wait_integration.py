"""End-to-end-ish integration test for the host-side wait flow (issue #1932, TASK-4-5).

The full ``integration_tests/test_host_wait_end_to_end.py`` in the plan
targets a running orchestrator with Docker + network + auth.  That
target is heavy and sandbox-unfriendly, so this lighter sibling
exercises the same code paths without the runtime dependencies:

    MCP tool handler  →  monkey-patched _make_request  →  Flask test client
                      ↘                                      ↘
                       _build_status_snapshot                 Flask route
                       (real path)                            /status/wait
                                                              (real path)
                                                              ↓
                                                             EventBus
                                                             message_store

So every concrete tool-level concern that CAN be validated off-Docker is
validated here:

    1. OVERSEER_ALERT on the message bus wakes ``wait_for_status_change``
       with ``changed=True, trigger="message"`` and the returned envelope
       merges the snapshot + route-sourced keys.
    2. DECISION_CREATED on the EventBus wakes with ``trigger="event"``.
    3. PHASE_STARTED on the EventBus wakes with ``trigger="event"``.
    4. A timeout returns ``changed=False, no_change=True`` and the cursor
       it returns — when passed back as ``since`` on a second call —
       skips the event that fired between the two calls ONLY when the
       event sequence is at-or-below the cursor; events AFTER the cursor
       do wake the second call.  This pins the R2 race-window closure.

These tests run unmodified in CI via ``make test`` and do not require
Docker or live orchestrator processes.  The plan's full
``integration_tests/test_host_wait_end_to_end.py`` would re-run the same
scenarios against a live stack — out of scope for the sandbox.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest
from flask import Flask

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

from egg_config.constants import TEST_GATEWAY_PORT  # noqa: E402
from events import Event, EventBus, EventType  # noqa: E402
from mcp_tools import PipelineToolHandler  # noqa: E402
from message_store import (  # noqa: E402
    Message,
    MessageStore,
    MessageType,
    reset_message_store,
)
from models import (  # noqa: E402
    Pipeline,
    PipelineConfig,
    PipelinePhase,
    PipelineStatus,
)
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


@pytest.fixture(autouse=True)
def _reset_store():
    reset_message_store()
    yield
    reset_message_store()


@pytest.fixture
def isolated_event_bus():
    bus = EventBus(async_delivery=False)
    with patch("events.get_event_bus", return_value=bus):
        yield bus


@pytest.fixture
def mock_pipeline_resolver():
    """Install a mock ``_resolve_pipeline`` that returns a fake pipeline."""

    def _pipeline() -> Pipeline:
        config = PipelineConfig(concurrent_execution=True, max_concurrent_agents=4)
        return Pipeline(
            id="issue-1932-e2e",
            issue_number=1932,
            repo="owner/repo",
            branch="egg/issue-1932-e2e",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
            config=config,
        )

    with (
        patch("routes.pipelines.get_repo_path", return_value="/tmp/test"),
        patch("routes.pipelines._resolve_pipeline") as mock_resolve,
    ):
        mock_resolve.return_value = (MagicMock(), _pipeline())
        yield mock_resolve


@pytest.fixture
def wired_handler(client):
    """Build a ``PipelineToolHandler`` whose ``_make_request`` pokes the
    Flask test client instead of the real network.

    This is the whole point of the integration test — we bypass only the
    network layer; every other code path (handler dispatch, snapshot
    enrichment, route cursor parsing, EventBus subscription, message
    store wait) runs in-process.
    """
    handler = PipelineToolHandler(
        orchestrator_url="http://localhost:9849",
        gateway_url=f"http://test-gateway:{TEST_GATEWAY_PORT}",
    )

    def _fake_make_request(
        endpoint: str,
        method: str = "GET",
        data: dict | None = None,
        timeout: int = 30,
    ) -> dict:
        # Route through Flask test client
        parsed = urlparse(endpoint)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        if method == "GET":
            resp = client.get(path)
        else:
            resp = client.open(
                path,
                method=method,
                json=data if data is not None else {},
            )
        return json.loads(resp.data.decode())

    # Short-circuit snapshot enrichment's second /pipelines/{id} GET
    # and /messages GET — they route through the Flask client above.
    # But we need to return valid data shaped like a pipeline snapshot,
    # so we patch the underlying pipeline-data endpoints to produce
    # deterministic responses.
    handler._make_request = _fake_make_request  # type: ignore[method-assign]
    return handler


class TestHostWaitEndToEnd:
    """End-to-end-ish integration tests (TASK-4-5)."""

    def test_overseer_alert_wakes_handler(
        self,
        wired_handler,
        mock_pipeline_resolver,
        isolated_event_bus,
    ) -> None:
        """An OVERSEER_ALERT on the message bus wakes the handler and
        the merged envelope carries ``changed=true, trigger="message"``.
        """
        store = MessageStore()

        def _fire() -> None:
            time.sleep(0.1)
            store.add_message(
                Message(
                    pipeline_id="issue-1932-e2e",
                    from_role="overseer",
                    to_role="all",
                    message_type=MessageType.OVERSEER_ALERT,
                    subject="stall detected",
                )
            )

        threading.Thread(target=_fire, daemon=True).start()

        with patch("routes.pipelines._get_message_store", return_value=lambda: store):
            result = wired_handler.handle_tool_call(
                "wait_for_status_change",
                {"task_id": "issue-1932-e2e", "wait": 5},
            )

        assert result["changed"] is True
        assert result["trigger"] == "message"
        assert len(result["messages"]) >= 1
        assert result["messages"][0]["message_type"] == MessageType.OVERSEER_ALERT
        # cursor must be present so host can pass it back on the next call
        assert "cursor" in result
        ok, _msg_id, evt_seq = _parse_status_wait_cursor(result["cursor"])
        assert ok is True

    def test_decision_created_event_wakes_handler(
        self,
        wired_handler,
        mock_pipeline_resolver,
        isolated_event_bus,
    ) -> None:
        """A DECISION_CREATED event wakes with trigger='event'."""

        def _fire() -> None:
            time.sleep(0.1)
            isolated_event_bus.publish(
                Event(
                    event_type=EventType.DECISION_CREATED,
                    pipeline_id="issue-1932-e2e",
                )
            )

        threading.Thread(target=_fire, daemon=True).start()

        result = wired_handler.handle_tool_call(
            "wait_for_status_change",
            {"task_id": "issue-1932-e2e", "wait": 5},
        )

        assert result["changed"] is True
        assert result["trigger"] == "event"
        assert result["event_type"] == EventType.DECISION_CREATED.value

    def test_phase_started_event_wakes_handler(
        self,
        wired_handler,
        mock_pipeline_resolver,
        isolated_event_bus,
    ) -> None:
        """A PHASE_STARTED event wakes with trigger='event'."""

        def _fire() -> None:
            time.sleep(0.1)
            isolated_event_bus.publish(
                Event(
                    event_type=EventType.PHASE_STARTED,
                    pipeline_id="issue-1932-e2e",
                )
            )

        threading.Thread(target=_fire, daemon=True).start()

        result = wired_handler.handle_tool_call(
            "wait_for_status_change",
            {"task_id": "issue-1932-e2e", "wait": 5},
        )

        assert result["changed"] is True
        assert result["trigger"] == "event"
        assert result["event_type"] == EventType.PHASE_STARTED.value

    def test_cursor_round_trip_suppresses_already_seen_event(
        self,
        wired_handler,
        mock_pipeline_resolver,
        isolated_event_bus,
    ) -> None:
        """Two-call cursor round-trip — already-seen events are suppressed.

        The cursor's correctness contract is:

            1. Call-1 wakes on event X (``trigger='event'``).  The
               returned cursor reflects X's sequence.
            2. Call-2 with ``since=<call-1-cursor>`` must NOT re-wake
               on X.  The ``_on_event`` filter ``event.sequence >
               event_since_seq`` is what enforces this.
            3. A NEW event Y published DURING call-2's wait window
               does wake call-2 (its sequence is strictly greater
               than the cursor).

        This is the "race window closed by since" property called
        out in plan TASK-4-5.  Note: events that fire in the gap
        BETWEEN call-1's return and call-2's subscribe are NOT
        closed here — event history is not replayed.  The sandbox
        mitigation for that window is the immediate-loop-re-entry
        contract documented in SKILL.md.
        """

        # --- Call 1 — wake on a published event --------------------
        def _fire_event_1() -> None:
            time.sleep(0.1)
            isolated_event_bus.publish(
                Event(
                    event_type=EventType.PHASE_STARTED,
                    pipeline_id="issue-1932-e2e",
                )
            )

        threading.Thread(target=_fire_event_1, daemon=True).start()

        result_1 = wired_handler.handle_tool_call(
            "wait_for_status_change",
            {"task_id": "issue-1932-e2e", "wait": 3},
        )
        assert result_1["changed"] is True
        assert result_1["trigger"] == "event"
        call_1_cursor = result_1["cursor"]
        ok, _msg_id, call_1_seq = _parse_status_wait_cursor(call_1_cursor)
        assert ok is True
        assert call_1_seq is not None

        # --- Call 2 passes since=call_1_cursor; no new event ------
        # Must NOT re-wake on the same event.  If it did, the skill
        # would loop forever on a single event.
        result_2 = wired_handler.handle_tool_call(
            "wait_for_status_change",
            {
                "task_id": "issue-1932-e2e",
                "wait": 1,
                "since": call_1_cursor,
            },
        )
        assert result_2["changed"] is False
        assert result_2["no_change"] is True

        # --- Call 3 — NEW event during the wait wakes call-3 -------
        # Pins the forward direction: new events are seen when their
        # sequence is strictly greater than the cursor.
        def _fire_event_2() -> None:
            time.sleep(0.1)
            isolated_event_bus.publish(
                Event(
                    event_type=EventType.DECISION_CREATED,
                    pipeline_id="issue-1932-e2e",
                )
            )

        threading.Thread(target=_fire_event_2, daemon=True).start()

        result_3 = wired_handler.handle_tool_call(
            "wait_for_status_change",
            {
                "task_id": "issue-1932-e2e",
                "wait": 3,
                "since": call_1_cursor,
            },
        )
        assert result_3["changed"] is True
        assert result_3["trigger"] == "event"
        assert result_3["event_type"] == EventType.DECISION_CREATED.value
        ok, _msg_id, call_3_seq = _parse_status_wait_cursor(result_3["cursor"])
        assert ok is True
        assert call_3_seq is not None and call_3_seq > call_1_seq

    def test_timeout_envelope_has_expected_keys(
        self,
        wired_handler,
        mock_pipeline_resolver,
        isolated_event_bus,
    ) -> None:
        """Timeout envelope contains exactly the minimal keys and no
        snapshot-shaped extras.  Pins the 'structural branching on
        no_change' contract that SKILL.md relies on.
        """
        result = wired_handler.handle_tool_call(
            "wait_for_status_change",
            {"task_id": "issue-1932-e2e", "wait": 1},
        )
        assert result["changed"] is False
        assert result["no_change"] is True
        assert "cursor" in result
        # Minimal envelope — snapshot keys MUST be absent.
        assert "running_agents" not in result
        assert "completed_agents" not in result
        assert "recent_messages" not in result
        assert "pipeline" not in result

    def test_cursor_builder_parser_roundtrip_for_wait_path(
        self,
    ) -> None:
        """Builder + parser are symmetrical for the shapes the wait
        route emits.  Covers the corners the route's happy-path tests
        do not exercise.
        """
        for msg_id, evt_seq in [
            ("1738012734-0", 42),
            (None, 0),
            ("abc", 1_000_000),
            (None, 999),
        ]:
            cursor = _build_status_wait_cursor(msg_id, evt_seq)
            ok, got_msg, got_seq = _parse_status_wait_cursor(cursor)
            assert ok is True
            assert got_msg == msg_id
            assert got_seq == evt_seq
