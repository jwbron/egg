"""Unit tests for egg_agent_tools.handlers.progress.

Covers progress_emit, progress_signal_error, progress_heartbeat.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "sandbox"))
sys.path.insert(0, str(ROOT / "shared"))

from egg_agent_tools.handlers import progress  # noqa: E402
from egg_agent_tools.handlers.errors import GatewayError, HandlerError  # noqa: E402


class TestProgressEmit:
    """progress_emit hits the structured-event endpoint (step/state)."""

    def test_happy_path(self):
        with patch(
            "egg_agent_tools.handlers.progress.orchestrator_request",
            return_value={"success": True, "data": {"event": {"id": "ev-1"}}},
        ) as req:
            resp = progress.progress_emit(
                {
                    "pipeline_id": "p",
                    "role": "coder",
                    "step": "refactor handlers",
                    "state": "working",
                    "detail": "halfway",
                }
            )
        assert resp["ok"] is True
        assert resp["event_id"] == "ev-1"
        data = req.call_args.kwargs["data"]
        assert data["step"] == "refactor handlers"
        assert data["state"] == "working"
        assert data["detail"] == "halfway"

    def test_missing_step(self):
        with pytest.raises(HandlerError):
            progress.progress_emit({"pipeline_id": "p", "role": "coder", "state": "working"})

    def test_missing_state(self):
        with pytest.raises(HandlerError):
            progress.progress_emit({"pipeline_id": "p", "role": "coder", "step": "x"})

    def test_gateway_error_propagates(self):
        with patch(
            "egg_agent_tools.handlers.progress.orchestrator_request",
            side_effect=GatewayError("orch down", status_code=503),
        ):
            with pytest.raises(GatewayError):
                progress.progress_emit(
                    {
                        "pipeline_id": "p",
                        "role": "coder",
                        "step": "x",
                        "state": "working",
                    }
                )

    def test_unsuccessful_response_raises(self):
        with patch(
            "egg_agent_tools.handlers.progress.orchestrator_request",
            return_value={"success": False, "message": "denied"},
        ):
            with pytest.raises(GatewayError):
                progress.progress_emit(
                    {
                        "pipeline_id": "p",
                        "role": "coder",
                        "step": "x",
                        "state": "working",
                    }
                )


class TestProgressSignalError:
    def test_happy_path(self):
        with patch(
            "egg_agent_tools.handlers.progress.orchestrator_request",
            return_value={"success": True, "data": {}},
        ) as req:
            resp = progress.progress_signal_error(
                {
                    "pipeline_id": "p",
                    "role": "coder",
                    "error": "oh no",
                    "recoverable": True,
                }
            )
        assert resp["ok"] is True
        data = req.call_args.kwargs["data"]
        assert data["signal_type"] == "error"
        assert data["error"] == "oh no"
        assert data["recoverable"] is True

    def test_missing_error(self):
        with pytest.raises(HandlerError):
            progress.progress_signal_error({"pipeline_id": "p", "role": "r"})

    def test_gateway_error(self):
        with patch(
            "egg_agent_tools.handlers.progress.orchestrator_request",
            side_effect=GatewayError("fail", status_code=500),
        ):
            with pytest.raises(GatewayError):
                progress.progress_signal_error({"pipeline_id": "p", "role": "r", "error": "x"})


class TestProgressHeartbeat:
    def test_happy_path(self):
        with patch(
            "egg_agent_tools.handlers.progress.orchestrator_request",
            return_value={"success": True, "data": {}},
        ) as req:
            resp = progress.progress_heartbeat({"pipeline_id": "p", "role": "coder"})
        assert resp["ok"] is True
        data = req.call_args.kwargs["data"]
        assert data["signal_type"] == "heartbeat"

    def test_gateway_error(self):
        with patch(
            "egg_agent_tools.handlers.progress.orchestrator_request",
            side_effect=GatewayError("fail", status_code=500),
        ):
            with pytest.raises(GatewayError):
                progress.progress_heartbeat({"pipeline_id": "p", "role": "r"})
