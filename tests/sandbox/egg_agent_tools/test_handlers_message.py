"""Unit tests for egg_agent_tools.handlers.message.

Covers the three event-driven primitives #1897 added and #1922 exposed
as MCP tools: message_wait, message_wait_loop, message_heartbeat.
"""

from __future__ import annotations

import sys
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
