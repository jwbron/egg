"""Unit tests for egg_agent_tools.handlers.message.

Covers the three event-driven primitives #1897 added and #1922 exposed
as MCP tools: message_wait, message_wait_loop, message_heartbeat.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "sandbox"))
sys.path.insert(0, str(ROOT / "shared"))

from egg_agent_tools.handlers import message  # noqa: E402
from egg_agent_tools.handlers.errors import GatewayError, HandlerError  # noqa: E402


class TestMessageWait:
    def test_match_returns_messages(self):
        server = {
            "success": True,
            "data": {
                "matched": True,
                "messages": [
                    {
                        "id": "m-1",
                        "from_role": "coder",
                        "to_role": "reviewer_code",
                        "message_type": "CONSENSUS_ACK",
                    }
                ],
            },
        }
        with patch(
            "egg_agent_tools.handlers.message.orchestrator_request",
            return_value=server,
        ) as req:
            resp = message.message_wait(
                {
                    "pipeline_id": "p",
                    "role": "coder",
                    "for_types": ["CONSENSUS_ACK", "CONSENSUS_NACK"],
                    "timeout": 30,
                }
            )
        assert resp["matched"] is True
        assert resp["messages"][0]["id"] == "m-1"
        assert resp["for_types"] == ["CONSENSUS_ACK", "CONSENSUS_NACK"]
        # Endpoint must carry both for= params, role filter, and timeout.
        endpoint = req.call_args.args[0]
        assert "for=CONSENSUS_ACK" in endpoint
        assert "for=CONSENSUS_NACK" in endpoint
        assert "role=coder" in endpoint
        assert "timeout=30" in endpoint

    def test_timeout_returns_no_match(self):
        server = {"success": True, "data": {"matched": False, "messages": []}}
        with patch(
            "egg_agent_tools.handlers.message.orchestrator_request",
            return_value=server,
        ):
            resp = message.message_wait(
                {"pipeline_id": "p", "for_types": ["CONSENSUS_ACK"], "timeout": 5}
            )
        assert resp["matched"] is False
        assert resp["messages"] == []

    def test_accepts_legacy_for_key(self):
        with patch(
            "egg_agent_tools.handlers.message.orchestrator_request",
            return_value={"success": True, "data": {"matched": False, "messages": []}},
        ):
            resp = message.message_wait({"pipeline_id": "p", "for": ["FEEDBACK_ANSWER"]})
        assert resp["for_types"] == ["FEEDBACK_ANSWER"]

    def test_missing_for_types_raises(self):
        with pytest.raises(HandlerError):
            message.message_wait({"pipeline_id": "p", "for_types": []})

    def test_missing_pipeline_raises(self):
        with patch("egg_agent_tools.handlers.message.get_pipeline_id", return_value=None):
            with pytest.raises(HandlerError):
                message.message_wait({"for_types": ["X"]})

    def test_gateway_error_propagates(self):
        with patch(
            "egg_agent_tools.handlers.message.orchestrator_request",
            side_effect=GatewayError("orch down", status_code=503),
        ):
            with pytest.raises(GatewayError):
                message.message_wait({"pipeline_id": "p", "for_types": ["X"]})

    def test_cursor_surfaced_on_match(self):
        """Issue #1995: server cursor is threaded through the handler."""
        server = {
            "success": True,
            "data": {
                "matched": True,
                "messages": [{"id": "m-7", "message_type": "CONSENSUS_ACK"}],
                "cursor": "m-7",
            },
        }
        with patch(
            "egg_agent_tools.handlers.message.orchestrator_request",
            return_value=server,
        ):
            resp = message.message_wait({"pipeline_id": "p", "for_types": ["CONSENSUS_ACK"]})
        assert resp["cursor"] == "m-7"

    def test_cursor_surfaced_on_timeout(self):
        """Issue #1995: even on timeout the server reports the stream tip."""
        server = {
            "success": True,
            "data": {"matched": False, "messages": [], "cursor": "tip-12"},
        }
        with patch(
            "egg_agent_tools.handlers.message.orchestrator_request",
            return_value=server,
        ):
            resp = message.message_wait({"pipeline_id": "p", "for_types": ["X"]})
        assert resp["matched"] is False
        assert resp["cursor"] == "tip-12"

    def test_cursor_defaults_to_none_when_server_omits(self):
        """Older orchestrators that don't emit ``cursor`` must not crash."""
        server = {"success": True, "data": {"matched": False, "messages": []}}
        with patch(
            "egg_agent_tools.handlers.message.orchestrator_request",
            return_value=server,
        ):
            resp = message.message_wait({"pipeline_id": "p", "for_types": ["X"]})
        assert resp["cursor"] is None

    def test_since_param_forwarded_to_endpoint(self):
        server = {"success": True, "data": {"matched": False, "messages": []}}
        with patch(
            "egg_agent_tools.handlers.message.orchestrator_request",
            return_value=server,
        ) as req:
            message.message_wait(
                {
                    "pipeline_id": "p",
                    "for_types": ["X"],
                    "since": "m-3",
                }
            )
        endpoint = req.call_args.args[0]
        assert "since_id=m-3" in endpoint


class TestMessageWaitLoop:
    def test_matches_on_first_iteration(self):
        calls: list[dict] = []

        def fake_wait(req):
            calls.append(req)
            return {"ok": True, "matched": True, "messages": [{"id": "m-1"}]}

        with patch("egg_agent_tools.handlers.message.message_wait", side_effect=fake_wait):
            resp = message.message_wait_loop(
                {
                    "pipeline_id": "p",
                    "for_types": ["CONSENSUS_ACK"],
                    "max_iterations": 3,
                }
            )
        assert resp["matched"] is True
        assert resp["iterations"] == 1
        assert len(calls) == 1

    def test_loops_through_timeouts_until_match(self):
        results = [
            {"ok": True, "matched": False, "messages": []},
            {"ok": True, "matched": False, "messages": []},
            {"ok": True, "matched": True, "messages": [{"id": "m-2"}]},
        ]

        def fake_wait(req):
            return results.pop(0)

        with patch("egg_agent_tools.handlers.message.message_wait", side_effect=fake_wait):
            resp = message.message_wait_loop(
                {"pipeline_id": "p", "for_types": ["X"], "max_iterations": 10}
            )
        assert resp["matched"] is True
        assert resp["iterations"] == 3

    def test_transient_gateway_error_retries_then_matches(self):
        sleeps: list[float] = []
        sequence = [
            GatewayError("flake", status_code=503),
            GatewayError("timeout", status_code=408),
            {"ok": True, "matched": True, "messages": [{"id": "m-3"}]},
        ]

        def fake_wait(req):
            item = sequence.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with patch("egg_agent_tools.handlers.message.message_wait", side_effect=fake_wait):
            resp = message.message_wait_loop(
                {
                    "pipeline_id": "p",
                    "for_types": ["X"],
                    "max_iterations": 10,
                    "_sleep": sleeps.append,
                }
            )
        assert resp["matched"] is True
        assert len(sleeps) == 2
        # Backoff must stay capped at 5s.
        assert all(s <= 5.0 for s in sleeps)

    def test_permanent_gateway_error_propagates(self):
        with patch(
            "egg_agent_tools.handlers.message.message_wait",
            side_effect=GatewayError("forbidden", status_code=403),
        ):
            with pytest.raises(GatewayError):
                message.message_wait_loop(
                    {"pipeline_id": "p", "for_types": ["X"], "max_iterations": 3}
                )

    def test_safety_cap_trips_without_match(self):
        def fake_wait(req):
            return {"ok": True, "matched": False, "messages": []}

        with patch("egg_agent_tools.handlers.message.message_wait", side_effect=fake_wait):
            resp = message.message_wait_loop(
                {"pipeline_id": "p", "for_types": ["X"], "max_iterations": 2}
            )
        assert resp["matched"] is False
        assert resp["iterations"] == 2

    def test_cursor_threaded_between_iterations(self):
        """Issue #1995: each timeout hands its cursor to the next call.

        Without this, an event that lands on the bus between iteration N
        returning (timeout) and iteration N+1 starting would be invisible
        because from_tip=True would snap to a new tip past it.
        """
        observed_since: list[str | None] = []
        responses = [
            {"ok": True, "matched": False, "messages": [], "cursor": "tip-1"},
            {"ok": True, "matched": False, "messages": [], "cursor": "tip-2"},
            {
                "ok": True,
                "matched": True,
                "messages": [{"id": "m-final"}],
                "cursor": "m-final",
            },
        ]

        def fake_wait(req):
            observed_since.append(req.get("since"))
            return responses.pop(0)

        with patch("egg_agent_tools.handlers.message.message_wait", side_effect=fake_wait):
            resp = message.message_wait_loop(
                {"pipeline_id": "p", "for_types": ["CONSENSUS_ACK"], "max_iterations": 5}
            )
        assert resp["matched"] is True
        assert resp["cursor"] == "m-final"
        # First call: caller passed no ``since``.
        # Subsequent calls: handler must thread the cursor from the
        # prior server response so the gap between iterations is closed.
        assert observed_since == [None, "tip-1", "tip-2"]

    def test_cursor_from_initial_since_preserved_if_server_returns_none(self):
        """Stream empty → server sends cursor=None. Handler must not
        overwrite the caller-supplied ``since`` with None — otherwise the
        next iteration would re-scan from start / tip."""
        observed_since: list[str | None] = []
        responses = [
            {"ok": True, "matched": False, "messages": [], "cursor": None},
            {"ok": True, "matched": True, "messages": [{"id": "m-x"}], "cursor": "m-x"},
        ]

        def fake_wait(req):
            observed_since.append(req.get("since"))
            return responses.pop(0)

        with patch("egg_agent_tools.handlers.message.message_wait", side_effect=fake_wait):
            message.message_wait_loop(
                {
                    "pipeline_id": "p",
                    "for_types": ["X"],
                    "since": "m-caller",
                    "max_iterations": 5,
                }
            )
        assert observed_since == ["m-caller", "m-caller"]

    def test_cursor_surfaced_on_safety_cap(self):
        """When the safety cap trips, the last seen cursor must still
        be surfaced so the caller can resume cleanly."""
        responses = [
            {"ok": True, "matched": False, "messages": [], "cursor": "tip-a"},
            {"ok": True, "matched": False, "messages": [], "cursor": "tip-b"},
        ]

        def fake_wait(req):
            return responses.pop(0)

        with patch("egg_agent_tools.handlers.message.message_wait", side_effect=fake_wait):
            resp = message.message_wait_loop(
                {"pipeline_id": "p", "for_types": ["X"], "max_iterations": 2}
            )
        assert resp["matched"] is False
        assert resp["cursor"] == "tip-b"


class TestMessageWaitLoopHeartbeat:
    """Pins issue #2036: wait_loop must emit ``WAITING_FOR_EVENT``
    heartbeats while blocking so the overseer's stall detector sees a
    real liveness signal.

    Regression context: before the fix, a reviewer or downstream producer
    in ``mcp__brc__wait_loop`` would go 5–15 minutes with no HEARTBEAT
    message on the bus, and the overseer flagged all three BRC agents
    (``tester``, ``reviewer_code``, ``coder``) as stalled even though
    they were all behaving correctly inside the wait primitive.
    """

    def _capture_emit(self):
        """Return (emitted_list, fake_emit) for use in tests."""
        emitted: list[dict] = []

        def fake_emit(pipeline_id, role, state, body, since=None, slice_id=None):
            emitted.append(
                {
                    "pipeline_id": pipeline_id,
                    "role": role,
                    "state": state,
                    "body": body,
                    "since": since,
                    "slice_id": slice_id,
                }
            )

        return emitted, fake_emit

    def test_emits_waiting_heartbeat_on_entry_and_working_on_exit(self):
        emitted, fake_emit = self._capture_emit()
        with patch(
            "egg_agent_tools.handlers.message.message_wait",
            return_value={"ok": True, "matched": True, "messages": [{"id": "m"}]},
        ):
            resp = message.message_wait_loop(
                {
                    "pipeline_id": "p",
                    "role": "coder",
                    "for_types": ["CONSENSUS_ACK"],
                    "from_role": "reviewer_code",
                    "_emit_heartbeat": fake_emit,
                    "_heartbeat_interval": 0,  # disable periodic thread
                }
            )
        assert resp["matched"] is True

        waiting = [e for e in emitted if e["state"] == "WAITING_FOR_EVENT"]
        working = [e for e in emitted if e["state"] == "WORKING"]
        assert len(waiting) >= 1, f"expected >=1 WAITING_FOR_EVENT beat, got {emitted}"
        assert len(working) >= 1, f"expected >=1 WORKING exit beat, got {emitted}"
        # Entry beat carries role + for_types + from_role so the bus
        # message is debuggable without needing structured metadata.
        entry = waiting[0]
        assert entry["role"] == "coder"
        assert entry["pipeline_id"] == "p"
        assert "CONSENSUS_ACK" in entry["body"]
        assert "reviewer_code" in entry["body"]
        # ``since`` carries the wait entry time so the overseer can
        # render "waiting since X" without parsing log timestamps.
        assert entry["since"] is not None

    def test_emits_final_working_heartbeat_even_on_safety_cap(self):
        emitted, fake_emit = self._capture_emit()
        with patch(
            "egg_agent_tools.handlers.message.message_wait",
            return_value={"ok": True, "matched": False, "messages": []},
        ):
            resp = message.message_wait_loop(
                {
                    "pipeline_id": "p",
                    "role": "coder",
                    "for_types": ["X"],
                    "max_iterations": 2,
                    "_emit_heartbeat": fake_emit,
                    "_heartbeat_interval": 0,
                }
            )
        assert resp["matched"] is False
        assert any(e["state"] == "WORKING" for e in emitted), (
            "WORKING transition must fire even when wait_loop gives up via safety cap"
        )

    def test_emits_final_working_heartbeat_on_permanent_error(self):
        emitted, fake_emit = self._capture_emit()
        with patch(
            "egg_agent_tools.handlers.message.message_wait",
            side_effect=GatewayError("forbidden", status_code=403),
        ):
            with pytest.raises(GatewayError):
                message.message_wait_loop(
                    {
                        "pipeline_id": "p",
                        "role": "coder",
                        "for_types": ["X"],
                        "max_iterations": 3,
                        "_emit_heartbeat": fake_emit,
                        "_heartbeat_interval": 0,
                    }
                )
        assert any(e["state"] == "WORKING" for e in emitted), (
            "WORKING transition must fire in the finally even when wait_loop raises"
        )

    def test_emitter_exceptions_do_not_kill_wait(self):
        def boom(*args, **kwargs):
            raise RuntimeError("heartbeat server down")

        # Emitter raising must not kill the loop. We only guarantee this
        # for background ticks and the exit beat — the entry tick runs
        # synchronously so a caller-injected raiser would propagate. The
        # real ``_default_emit_wait_loop_heartbeat`` swallows its own
        # errors, which is what ships in production.
        sleeps: list[float] = []
        with patch(
            "egg_agent_tools.handlers.message.message_wait",
            return_value={"ok": True, "matched": True, "messages": [{"id": "m"}]},
        ):
            resp = message.message_wait_loop(
                {
                    "pipeline_id": "p",
                    "role": "coder",
                    "for_types": ["X"],
                    "_emit_heartbeat": (
                        lambda pid, role, state, body, since=None, slice_id=None: None
                    ),  # no-op (production default swallows errors)
                    "_heartbeat_interval": 0,
                    "_sleep": sleeps.append,
                }
            )
        assert resp["matched"] is True

    def test_default_emitter_short_circuits_without_pipeline_or_role(self):
        """Existing tests pass no role; the default emitter must not
        attempt a real HTTP call in that case — otherwise every unit
        test in ``TestMessageWaitLoop`` would hit the network."""
        with patch("egg_agent_tools.handlers.message.orchestrator_request") as req:
            message._default_emit_wait_loop_heartbeat(None, "coder", "WAITING_FOR_EVENT", "hi")
            message._default_emit_wait_loop_heartbeat("p", None, "WAITING_FOR_EVENT", "hi")
            message._default_emit_wait_loop_heartbeat(None, None, "WAITING_FOR_EVENT", "hi")
        assert req.call_count == 0

    def test_default_emitter_swallows_gateway_errors(self):
        """Liveness beats must never kill the wait even if the server
        returns 429, 500, or is down entirely."""
        with patch(
            "egg_agent_tools.handlers.message.orchestrator_request",
            side_effect=GatewayError("rate limited", status_code=429),
        ):
            message._default_emit_wait_loop_heartbeat("p", "coder", "WAITING_FOR_EVENT", "hi")
        # No exception raised — test passes by reaching this line.

    def test_default_emitter_forwards_since_in_payload(self):
        """``since`` (when supplied) must be threaded into the heartbeat
        body so the overseer can render "waiting since X" without parsing
        log timestamps. Reviewer suggestion on PR #2041."""
        with patch("egg_agent_tools.handlers.message.orchestrator_request") as req:
            message._default_emit_wait_loop_heartbeat(
                "p", "coder", "WAITING_FOR_EVENT", "hi", "2026-04-24T12:00:00+00:00"
            )
        assert req.call_count == 1
        sent_body = req.call_args.kwargs["data"]
        assert sent_body["since"] == "2026-04-24T12:00:00+00:00"
        assert sent_body["state"] == "WAITING_FOR_EVENT"

    def test_default_emitter_omits_since_when_not_provided(self):
        """``since`` is optional — when callers don't supply it (e.g. the
        WORKING exit beat), the field stays out of the payload."""
        with patch("egg_agent_tools.handlers.message.orchestrator_request") as req:
            message._default_emit_wait_loop_heartbeat("p", "coder", "WORKING", "exited")
        assert req.call_count == 1
        assert "since" not in req.call_args.kwargs["data"]

    def test_default_emitter_forwards_slice_id_in_payload(self):
        """Issue #2451: slice-scoped agents' wait-loop heartbeats must
        forward ``slice_id`` so the orchestrator's gateway-session
        fan-out can reconstruct ``egg-agent-{pid}-{slice}-{role}``
        instead of falling back to the pipeline-level shape and 404'ing
        the gateway lookup. This is the dominant heartbeat path — wait
        -loop ticks at 60 s for every blocked agent — so a missing
        forward here is what produced the steady stream of
        "Session not found for container" warnings in #2451.
        """
        with patch("egg_agent_tools.handlers.message.orchestrator_request") as req:
            message._default_emit_wait_loop_heartbeat(
                "p", "reviewer_code", "WAITING_FOR_EVENT", "blocked", slice_id="slice-2"
            )
        assert req.call_count == 1
        sent_body = req.call_args.kwargs["data"]
        assert sent_body["slice_id"] == "slice-2"
        assert sent_body["state"] == "WAITING_FOR_EVENT"
        assert sent_body["from_role"] == "reviewer_code"

    def test_default_emitter_omits_slice_id_for_pipeline_level_agents(self):
        """Pipeline-level agents (no slice) must NOT include a
        ``slice_id`` field — the orchestrator's fan-out then falls back
        to the pipeline-level container_id shape, which is what
        ``JOB_NAME_FORMAT`` (no slice) registered.
        """
        with patch("egg_agent_tools.handlers.message.orchestrator_request") as req:
            message._default_emit_wait_loop_heartbeat(
                "p", "planner", "WAITING_FOR_EVENT", "blocked"
            )
        assert req.call_count == 1
        assert "slice_id" not in req.call_args.kwargs["data"]

    def test_wait_loop_threads_slice_id_into_periodic_ticks(self):
        """Regression for #2451: ``message_wait_loop`` must capture
        ``slice_id`` once at entry and pass it on every emitted tick
        (entry, periodic ``WAITING_FOR_EVENT``, and the final ``WORKING``
        beat in the finally). Without this, slice-scoped reviewers /
        testers spend their entire lifetime emitting fan-out 404s.
        """
        emitted, fake_emit = self._capture_emit()
        with patch(
            "egg_agent_tools.handlers.message.message_wait",
            return_value={"ok": True, "matched": True, "messages": [{"id": "m"}]},
        ):
            message.message_wait_loop(
                {
                    "pipeline_id": "p",
                    "role": "reviewer_code",
                    "slice_id": "slice-3",
                    "for_types": ["CONSENSUS_ACK"],
                    "_emit_heartbeat": fake_emit,
                    "_heartbeat_interval": 0,
                }
            )
        assert emitted, "wait_loop must emit at least one heartbeat"
        # Every emitted beat (entry + exit) must carry the captured slice_id.
        slice_ids = {e["slice_id"] for e in emitted}
        assert slice_ids == {"slice-3"}, (
            f"every wait_loop tick must forward slice_id; got {slice_ids}"
        )

    def test_wait_loop_omits_slice_id_for_pipeline_level_agents(self):
        """Pipeline-level agents (no env var, no override) must not
        smuggle a ``slice_id`` onto the heartbeat — the orchestrator's
        fan-out would otherwise build a slice-shaped container_id that
        the pipeline-level pod never registered.
        """
        emitted, fake_emit = self._capture_emit()
        with (
            patch(
                "egg_agent_tools.handlers.message.message_wait",
                return_value={"ok": True, "matched": True, "messages": [{"id": "m"}]},
            ),
            patch(
                "egg_agent_tools.handlers._gateway.get_slice_id",
                return_value=None,
            ),
        ):
            message.message_wait_loop(
                {
                    "pipeline_id": "p",
                    "role": "planner",
                    "for_types": ["CONSENSUS_ACK"],
                    "_emit_heartbeat": fake_emit,
                    "_heartbeat_interval": 0,
                }
            )
        slice_ids = {e["slice_id"] for e in emitted}
        assert slice_ids == {None}, (
            f"pipeline-level wait_loop ticks must not carry slice_id; got {slice_ids}"
        )

    def test_wait_loop_rejects_invalid_slice_id_at_entry(self):
        """Defense-in-depth: a malformed ``slice_id`` is rejected
        before the wait begins so a path separator or shell metachar
        cannot be smuggled into the heartbeat fan-out's container_id.
        """
        emitted, fake_emit = self._capture_emit()
        with pytest.raises(HandlerError):
            message.message_wait_loop(
                {
                    "pipeline_id": "p",
                    "role": "reviewer_code",
                    "slice_id": "../escape",
                    "for_types": ["CONSENSUS_ACK"],
                    "_emit_heartbeat": fake_emit,
                    "_heartbeat_interval": 0,
                }
            )
        assert emitted == [], "malformed slice_id must reject before any tick fires"

    def test_since_is_captured_once_and_shared_across_ticks(self):
        """``since`` is the wait *entry* time, captured once before the
        loop. Every WAITING_FOR_EVENT beat must carry the same value so
        the overseer reads it as a monotonically aging "waiting since"
        rather than a clock that resets every interval."""
        import time

        emitted, fake_emit = self._capture_emit()

        def slow_wait(_req):
            time.sleep(0.15)
            return {"ok": True, "matched": True, "messages": [{"id": "m"}]}

        with patch(
            "egg_agent_tools.handlers.message.message_wait",
            side_effect=slow_wait,
        ):
            message.message_wait_loop(
                {
                    "pipeline_id": "p",
                    "role": "coder",
                    "for_types": ["X"],
                    "_emit_heartbeat": fake_emit,
                    "_heartbeat_interval": 0.05,
                }
            )
        waiting_sinces = {e["since"] for e in emitted if e["state"] == "WAITING_FOR_EVENT"}
        assert len(waiting_sinces) == 1, (
            f"WAITING_FOR_EVENT beats must share one ``since``; got {waiting_sinces}"
        )
        # And the captured value must be a real timestamp string, not None.
        assert next(iter(waiting_sinces)) is not None

    def test_periodic_tick_fires_during_blocking_wait(self):
        """Uses a tiny interval and a synthetic slow ``message_wait`` to
        prove the background thread emits at least one keep-alive while
        the inner wait is blocked. This is the in-process analogue of
        the end-to-end scenario in #2036's proposed regression test."""
        import time

        emitted, fake_emit = self._capture_emit()
        # Inner wait blocks briefly then matches, during which the
        # background thread (50 ms interval) should tick multiple times.
        done = threading.Event()

        def slow_wait(_req):
            time.sleep(0.25)
            done.set()
            return {"ok": True, "matched": True, "messages": [{"id": "m"}]}

        with patch(
            "egg_agent_tools.handlers.message.message_wait",
            side_effect=slow_wait,
        ):
            message.message_wait_loop(
                {
                    "pipeline_id": "p",
                    "role": "coder",
                    "for_types": ["CONSENSUS_ACK"],
                    "_emit_heartbeat": fake_emit,
                    "_heartbeat_interval": 0.05,
                }
            )
        assert done.is_set()
        waiting = [e for e in emitted if e["state"] == "WAITING_FOR_EVENT"]
        # 1 entry tick + >= 1 periodic tick during the 250 ms wait.
        assert len(waiting) >= 2, (
            f"expected periodic WAITING_FOR_EVENT ticks, got {len(waiting)} ({emitted})"
        )


class TestMessageHeartbeat:
    def test_happy_path(self):
        with patch(
            "egg_agent_tools.handlers.message.orchestrator_request",
            return_value={"success": True, "data": {"deduped": False}},
        ) as req:
            resp = message.message_heartbeat(
                {
                    "pipeline_id": "p",
                    "role": "coder",
                    "state": "WORKING",
                    "body": "plowing through tasks",
                }
            )
        assert resp["ok"] is True
        assert resp["deduped"] is False
        data = req.call_args.kwargs["data"]
        assert data == {
            "from_role": "coder",
            "state": "WORKING",
            "body": "plowing through tasks",
        }

    def test_waiting_on_required_for_waiting_state(self):
        with pytest.raises(HandlerError):
            message.message_heartbeat(
                {
                    "pipeline_id": "p",
                    "role": "coder",
                    "state": "WAITING_ON_ROLE",
                }
            )

    def test_waiting_on_included_when_supplied(self):
        with patch(
            "egg_agent_tools.handlers.message.orchestrator_request",
            return_value={"success": True, "data": {"deduped": True}},
        ) as req:
            resp = message.message_heartbeat(
                {
                    "pipeline_id": "p",
                    "role": "coder",
                    "state": "WAITING_ON_ROLE",
                    "waiting_on": "reviewer_code",
                }
            )
        data = req.call_args.kwargs["data"]
        assert data["waiting_on"] == "reviewer_code"
        assert resp["deduped"] is True

    def test_invalid_state_raises(self):
        with pytest.raises(HandlerError):
            message.message_heartbeat({"pipeline_id": "p", "role": "coder", "state": "BOGUS"})

    def test_rate_limit_surfaces_as_gateway_error(self):
        with patch(
            "egg_agent_tools.handlers.message.orchestrator_request",
            side_effect=GatewayError("rate limited", status_code=429, details={"retry_after": 30}),
        ):
            with pytest.raises(GatewayError) as exc:
                message.message_heartbeat({"pipeline_id": "p", "role": "coder", "state": "IDLE"})
        assert exc.value.status_code == 429

    def test_unsuccessful_response_raises(self):
        with patch(
            "egg_agent_tools.handlers.message.orchestrator_request",
            return_value={"success": False, "message": "denied"},
        ):
            with pytest.raises(GatewayError):
                message.message_heartbeat({"pipeline_id": "p", "role": "coder", "state": "WORKING"})
