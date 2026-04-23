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
    """Emit a structured progress event (orch_cli cmd_progress_emit).

    Request:
        step (str): required — name of the current step.
        state (str): required — ``working``/``blocked``/``complete``
            (handler does not enforce the enum; orchestrator does).
        detail (str): optional free-form detail.
        blocker (str): optional blocker identifier.
        pipeline_id, role: overrides.

    Note: this wraps the structured-event endpoint
    (``POST /api/v1/pipelines/<pid>/progress``), not the legacy
    signal-progress endpoint.  The tool name ``mcp__progress__emit``
    matches the structured-event semantic.  For the percent-based
    progress signal, use a direct CLI call to
    ``egg-orch signal progress``.
    """
    pid = _require_pipeline_id(req)
    role = _require_role(req)
    step = req.get("step")
    state = req.get("state")
    if not step or not isinstance(step, str):
        raise HandlerError("'step' is required")
    if not state or not isinstance(state, str):
        raise HandlerError("'state' is required")

    data: dict[str, Any] = {
        "agent_role": role,
        "step": step,
        "state": state,
    }
    if req.get("detail"):
        data["detail"] = req["detail"]
    if req.get("blocker"):
        data["blocker"] = req["blocker"]

    result = orchestrator_request(
        f"/api/v1/pipelines/{pid}/progress", method="POST", data=data
    )
    if not result.get("success"):
        raise GatewayError(result.get("message", "progress emit failed"))
    event = result.get("data", {}).get("event", {})
    return {
        "ok": True,
        "role": role,
        "step": step,
        "state": state,
        "event_id": event.get("id"),
        "signal": result,
    }


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
