"""Progress-signal handlers (progress, error, heartbeat)."""

from __future__ import annotations

from typing import Any

from egg_agent_tools.handlers._gateway import (
    get_agent_role,
    get_pipeline_id,
    orchestrator_request,
)
from egg_agent_tools.handlers.errors import GatewayError, HandlerError


def _require_pipeline_id(req: dict[str, Any]) -> str:
    pid = req.get("pipeline_id") or get_pipeline_id()
    if not pid:
        raise HandlerError(
            "pipeline_id required. Set EGG_PIPELINE_ID or pass 'pipeline_id'."
        )
    return pid


def _require_role(req: dict[str, Any]) -> str:
    role = req.get("role") or get_agent_role()
    if not role:
        raise HandlerError("role required. Set EGG_AGENT_ROLE or pass 'role'.")
    return role


def progress_emit(req: dict[str, Any]) -> dict[str, Any]:
    """Signal a progress update for the current agent.

    Request:
        percent (int): 0-100. Required.
        task (str): optional current task ID.
        message (str): optional free-form message.
        pipeline_id, role: overrides.
    """
    pid = _require_pipeline_id(req)
    role = _require_role(req)
    percent = req.get("percent")
    if percent is None:
        raise HandlerError("'percent' is required (0-100)")
    try:
        percent_int = int(percent)
    except (TypeError, ValueError) as exc:
        raise HandlerError("'percent' must be an integer") from exc
    if not 0 <= percent_int <= 100:
        raise HandlerError("'percent' must be between 0 and 100")

    data: dict[str, Any] = {
        "signal_type": "progress",
        "agent_role": role,
        "progress_percent": percent_int,
    }
    if req.get("task"):
        data["current_task"] = req["task"]
    if req.get("message"):
        data["message"] = req["message"]

    result = orchestrator_request(
        f"/api/v1/pipelines/{pid}/signal", method="POST", data=data
    )
    if not result.get("success"):
        raise GatewayError(result.get("message", "progress signal failed"))
    return {"ok": True, "role": role, "percent": percent_int, "signal": result}


def progress_signal_error(req: dict[str, Any]) -> dict[str, Any]:
    """Signal a recoverable / unrecoverable error for the current agent.

    Request:
        error (str): required error message.
        recoverable (bool): default False.
        pipeline_id, role: overrides.
    """
    pid = _require_pipeline_id(req)
    role = _require_role(req)
    error = req.get("error")
    if not error or not isinstance(error, str):
        raise HandlerError("'error' is required")
    recoverable = bool(req.get("recoverable", False))

    data = {
        "signal_type": "error",
        "agent_role": role,
        "error": error,
        "recoverable": recoverable,
    }
    result = orchestrator_request(
        f"/api/v1/pipelines/{pid}/signal", method="POST", data=data
    )
    if not result.get("success"):
        raise GatewayError(result.get("message", "error signal failed"))
    return {"ok": True, "role": role, "signal": result}


def progress_heartbeat(req: dict[str, Any]) -> dict[str, Any]:
    """Send a heartbeat signal.

    Request:
        pipeline_id, role: overrides.
    """
    pid = _require_pipeline_id(req)
    role = _require_role(req)

    data = {"signal_type": "heartbeat", "agent_role": role}
    result = orchestrator_request(
        f"/api/v1/pipelines/{pid}/signal", method="POST", data=data
    )
    if not result.get("success"):
        raise GatewayError(result.get("message", "heartbeat failed"))
    return {"ok": True, "role": role, "signal": result}
