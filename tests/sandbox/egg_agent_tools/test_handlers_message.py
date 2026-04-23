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
