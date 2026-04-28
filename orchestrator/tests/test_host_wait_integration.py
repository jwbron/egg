"""Integration tests for the ``/status/wait`` route (issue #1932 + #2211).

Originally these tests went through the ``wait_for_status_change`` MCP
tool handler.  That tool was removed in #2211 — host-side blocking
waits now run via ``egg-orch pipeline wait-status`` (Bash CLI), which
is a thin wrapper over the same route.  The tests below now exercise
the route directly via a Flask test client; the CLI wrapper has its
own dedicated tests.

The route is the load-bearing piece — every concrete wake / cursor /
trigger / no-change concern that CAN be validated off-Docker is still
validated here:

    1. OVERSEER_ALERT on the message bus wakes the route with
       ``changed=True, trigger="message"``.
    2. DECISION_CREATED on the EventBus wakes with ``trigger="event"``.
    3. PHASE_STARTED on the EventBus wakes with ``trigger="event"``.
    4. Cursor round-trip: an already-seen event is suppressed on the
       second call; a new event during the second call's wait window
       wakes it.
    5. Timeout returns ``changed=False, no_change=True`` with the
       minimal envelope shape the dashboard depends on.

These tests run unmodified in CI via ``make test`` and do not require
Docker or live orchestrator processes.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

from events import Event, EventBus, EventType  # noqa: E402
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


def _wait(client, *, wait: int = 5, since: str | None = None) -> dict:
    """Hit ``/status/wait`` and return the response body data envelope."""
    path = f"/api/v1/pipelines/issue-1932-e2e/status/wait?wait={wait}"
    if since is not None:
        from urllib.parse import quote as _quote

        path += f"&since={_quote(since, safe='')}"
    resp = client.get(path)
    body = resp.get_json()
    assert resp.status_code == 200, f"unexpected status {resp.status_code}: {body!r}"
    # The route returns ``{success: true, data: {...envelope...}}``.
    return body.get("data") if isinstance(body, dict) and "data" in body else body


class TestStatusWaitRoute:
    """End-to-end-ish integration tests against the /status/wait route."""

    def test_overseer_alert_wakes_route(
        self,
        client,
        mock_pipeline_resolver,
        isolated_event_bus,
    ) -> None:
        """An OVERSEER_ALERT on the message bus wakes the route with
        ``changed=true, trigger="message"``.
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
            envelope = _wait(client, wait=5)

        assert envelope["changed"] is True
        assert envelope["trigger"] == "message"
        assert len(envelope["messages"]) >= 1
        assert envelope["messages"][0]["message_type"] == MessageType.OVERSEER_ALERT
        assert "cursor" in envelope
        ok, _msg_id, _evt_seq = _parse_status_wait_cursor(envelope["cursor"])
        assert ok is True

    def test_decision_created_event_wakes_route(
        self,
        client,
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

        envelope = _wait(client, wait=5)

        assert envelope["changed"] is True
        assert envelope["trigger"] == "event"
        assert envelope["event_type"] == EventType.DECISION_CREATED.value

    def test_phase_started_event_wakes_route(
        self,
        client,
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

        envelope = _wait(client, wait=5)

        assert envelope["changed"] is True
        assert envelope["trigger"] == "event"
        assert envelope["event_type"] == EventType.PHASE_STARTED.value

    def test_cursor_round_trip_suppresses_already_seen_event(
        self,
        client,
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

        This is the "race window closed by since" property.  Note:
        events that fire in the gap BETWEEN call-1's return and
        call-2's subscribe are NOT closed here — event history is not
        replayed.
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

        envelope_1 = _wait(client, wait=3)
        assert envelope_1["changed"] is True
        assert envelope_1["trigger"] == "event"
        call_1_cursor = envelope_1["cursor"]
        ok, _msg_id, call_1_seq = _parse_status_wait_cursor(call_1_cursor)
        assert ok is True
        assert call_1_seq is not None

        # --- Call 2 passes since=call_1_cursor; no new event ------
        envelope_2 = _wait(client, wait=1, since=call_1_cursor)
        assert envelope_2["changed"] is False
        assert envelope_2["no_change"] is True

        # --- Call 3 — NEW event during the wait wakes call-3 -------
        def _fire_event_2() -> None:
            time.sleep(0.1)
            isolated_event_bus.publish(
                Event(
                    event_type=EventType.DECISION_CREATED,
                    pipeline_id="issue-1932-e2e",
                )
            )

        threading.Thread(target=_fire_event_2, daemon=True).start()

        envelope_3 = _wait(client, wait=3, since=call_1_cursor)
        assert envelope_3["changed"] is True
        assert envelope_3["trigger"] == "event"
        assert envelope_3["event_type"] == EventType.DECISION_CREATED.value
        ok, _msg_id, call_3_seq = _parse_status_wait_cursor(envelope_3["cursor"])
        assert ok is True
        assert call_3_seq is not None and call_3_seq > call_1_seq

    def test_timeout_envelope_has_expected_keys(
        self,
        client,
        mock_pipeline_resolver,
        isolated_event_bus,
    ) -> None:
        """Timeout envelope contains exactly the minimal keys and no
        snapshot-shaped extras.  Pins the structural branching on
        ``no_change`` that the CLI and SKILL.md rely on.
        """
        envelope = _wait(client, wait=1)
        assert envelope["changed"] is False
        assert envelope["no_change"] is True
        assert "cursor" in envelope
        # Minimal envelope — snapshot keys MUST be absent.
        assert "running_agents" not in envelope
        assert "completed_agents" not in envelope
        assert "recent_messages" not in envelope
        assert "pipeline" not in envelope

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
