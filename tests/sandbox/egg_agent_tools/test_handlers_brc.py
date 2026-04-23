"""Unit tests for egg_agent_tools.handlers.brc.

Covers brc_propose/ack/nack/confirm/get_state/list_blocking.  Tests patch
:func:`orchestrator_request` so no HTTP traffic occurs.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "sandbox"))
sys.path.insert(0, str(ROOT / "shared"))

from egg_agent_tools.handlers import brc  # noqa: E402
from egg_agent_tools.handlers.errors import GatewayError, HandlerError  # noqa: E402


def _ok_response(**extra):
    data = {"consensus": {"agents": {"coder": {"phase": "implement"}}}}
    data.update(extra)
    return {"success": True, "data": data}


class TestBrcPropose:
    def test_happy_path(self):
        with (
            patch(
                "egg_agent_tools.handlers.brc.orchestrator_request",
                return_value=_ok_response(),
            ) as req,
            patch("egg_agent_tools.handlers.brc._resolve_head_sha", return_value="abc1234"),
        ):
            resp = brc.brc_propose(
                {
                    "pipeline_id": "pipe-1",
                    "role": "coder",
                    "summary": "x" * 60,
                    "artifacts": ["f.py"],
                    "tasks": ["task-1-1"],
                }
            )
        assert resp["ok"] is True
        assert resp["role"] == "coder"
        assert resp["phase"] == "implement"
        assert req.call_count == 1
        data = req.call_args.kwargs["data"]
        assert data["signal_type"] == "consensus_propose"
        assert data["payload"]["summary"] == "x" * 60
        assert data["payload"]["tasks_satisfied"] == ["task-1-1"]
        assert data["payload"]["commit_sha"] == "abc1234"

    def test_missing_summary(self):
        with pytest.raises(HandlerError):
            brc.brc_propose({"pipeline_id": "p", "role": "coder"})

    def test_missing_pipeline_id(self):
        with (
            patch.dict("os.environ", {}, clear=False),
            patch("egg_agent_tools.handlers.brc.get_pipeline_id", return_value=None),
        ):
            with pytest.raises(HandlerError):
                brc.brc_propose({"role": "coder", "summary": "x" * 60})

    def test_missing_role(self):
        with patch("egg_agent_tools.handlers.brc.get_agent_role", return_value=None):
            with pytest.raises(HandlerError):
                brc.brc_propose({"pipeline_id": "p", "summary": "x" * 60})

    def test_gateway_500_raises_gateway_error(self):
        def boom(*a, **kw):
            raise GatewayError("upstream down", status_code=500)

        with (
            patch("egg_agent_tools.handlers.brc.orchestrator_request", side_effect=boom),
            patch("egg_agent_tools.handlers.brc._resolve_head_sha", return_value="a" * 40),
        ):
            with pytest.raises(GatewayError):
                brc.brc_propose({"pipeline_id": "p", "role": "coder", "summary": "x" * 60})

    def test_unsuccessful_response_raises(self):
        with (
            patch(
                "egg_agent_tools.handlers.brc.orchestrator_request",
                return_value={"success": False, "message": "nope"},
            ),
            patch("egg_agent_tools.handlers.brc._resolve_head_sha", return_value="a" * 40),
        ):
            with pytest.raises(GatewayError):
                brc.brc_propose({"pipeline_id": "p", "role": "coder", "summary": "x" * 60})


class TestBrcAck:
    def test_happy_path(self):
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value=_ok_response(),
        ) as req:
            resp = brc.brc_ack(
                {
                    "pipeline_id": "p",
                    "role": "reviewer_code",
                    "producer_role": "coder",
                    "reason": "x" * 60,
                    "files_reviewed": ["a.py"],
                }
            )
        assert resp["ok"] is True
        assert resp["producer_role"] == "coder"
        data = req.call_args.kwargs["data"]
        assert data["signal_type"] == "consensus_ack"
        assert data["payload"]["reason"] == "x" * 60
        assert data["payload"]["artifact_references"] == ["a.py"]

    def test_missing_producer_role(self):
        with pytest.raises(HandlerError):
            brc.brc_ack({"pipeline_id": "p", "role": "r", "reason": "y"})

    def test_missing_reason(self):
        with pytest.raises(HandlerError):
            brc.brc_ack({"pipeline_id": "p", "role": "r", "producer_role": "coder"})

    def test_gateway_error(self):
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            side_effect=GatewayError("fail", status_code=500),
        ):
            with pytest.raises(GatewayError):
                brc.brc_ack(
                    {
                        "pipeline_id": "p",
                        "role": "r",
                        "producer_role": "coder",
                        "reason": "y",
                    }
                )


class TestBrcNack:
    def test_happy_path(self):
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value=_ok_response(),
        ) as req:
            resp = brc.brc_nack(
                {
                    "pipeline_id": "p",
                    "role": "reviewer_code",
                    "producer_role": "coder",
                    "reason": "blocking",
                }
            )
        assert resp["ok"] is True
        data = req.call_args.kwargs["data"]
        assert data["signal_type"] == "consensus_nack"

    def test_missing_reason(self):
        with pytest.raises(HandlerError):
            brc.brc_nack({"pipeline_id": "p", "role": "r", "producer_role": "coder"})

    def test_gateway_error(self):
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            side_effect=GatewayError("fail", status_code=500),
        ):
            with pytest.raises(GatewayError):
                brc.brc_nack(
                    {
                        "pipeline_id": "p",
                        "role": "r",
                        "producer_role": "coder",
                        "reason": "why",
                    }
                )


class TestBrcConfirm:
    def test_happy_confirmed(self):
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={
                "success": True,
                "data": {"status": "confirmed", "consensus_reached": True},
            },
        ):
            resp = brc.brc_confirm({"pipeline_id": "p", "role": "coder"})
        assert resp["status"] == "confirmed"
        assert resp["consensus_reached"] is True

    def test_pending_acks(self):
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={
                "success": True,
                "data": {"status": "pending_acks", "consensus_reached": False},
            },
        ):
            resp = brc.brc_confirm({"pipeline_id": "p", "role": "coder"})
        assert resp["status"] == "pending_acks"
        assert resp["consensus_reached"] is False

    def test_gateway_error(self):
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            side_effect=GatewayError("fail", status_code=500),
        ):
            with pytest.raises(GatewayError):
                brc.brc_confirm({"pipeline_id": "p", "role": "r"})


class TestBrcGetState:
    def test_default_shape(self):
        payload = {
            "data": {
                "concurrent": {
                    "consensus": {
                        "is_complete": False,
                        "blocking_agents": ["coder"],
                        "agents": {},
                    }
                }
            }
        }
        with patch("egg_agent_tools.handlers.brc.orchestrator_request", return_value=payload):
            resp = brc.brc_get_state({"pipeline_id": "p"})
        assert resp["ok"] is True
        assert resp["is_complete"] is False
        assert resp["blocking_agents"] == ["coder"]
        assert "raw" not in resp

    def test_verbose_includes_raw(self):
        payload = {
            "data": {"concurrent": {"consensus": {"is_complete": True, "blocking_agents": []}}}
        }
        with patch("egg_agent_tools.handlers.brc.orchestrator_request", return_value=payload):
            resp = brc.brc_get_state({"pipeline_id": "p", "verbose": True})
        assert resp["raw"] == payload["data"]


class TestBrcListBlocking:
    def test_returns_blocking_list(self):
        payload = {"data": {"concurrent": {"consensus": {"blocking_agents": ["coder", "tester"]}}}}
        with patch("egg_agent_tools.handlers.brc.orchestrator_request", return_value=payload):
            resp = brc.brc_list_blocking({"pipeline_id": "p"})
        assert resp["blocking_agents"] == ["coder", "tester"]

    def test_empty_when_missing(self):
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            return_value={"data": {}},
        ):
            resp = brc.brc_list_blocking({"pipeline_id": "p"})
        assert resp["blocking_agents"] == []

    def test_gateway_error_propagates(self):
        with patch(
            "egg_agent_tools.handlers.brc.orchestrator_request",
            side_effect=GatewayError("server error", status_code=500),
        ):
            with pytest.raises(GatewayError):
                brc.brc_list_blocking({"pipeline_id": "p"})
