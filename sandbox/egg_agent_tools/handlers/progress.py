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
        raise HandlerError("pipeline_id required. Set EGG_PIPELINE_ID or pass 'pipeline_id'.")
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

    result = orchestrator_request(f"/api/v1/pipelines/{pid}/progress", method="POST", data=data)
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
    result = orchestrator_request(f"/api/v1/pipelines/{pid}/signal", method="POST", data=data)
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
    result = orchestrator_request(f"/api/v1/pipelines/{pid}/signal", method="POST", data=data)
    if not result.get("success"):
        raise GatewayError(result.get("message", "heartbeat failed"))
    return {"ok": True, "role": role, "signal": result}


_VALID_OVERSEER_PRIORITIES = ("low", "medium", "high")


def progress_overseer_alert(req: dict[str, Any]) -> dict[str, Any]:
    """Broadcast an OVERSEER_ALERT message to the human operator.

    Wraps ``POST /api/v1/pipelines/<pid>/messages`` with
    ``message_type=OVERSEER_ALERT`` and ``to_role="all"`` hard-coded —
    mirrors ``egg-orch overseer alert`` so the overseer-only alert
    channel is the single source of truth for anomaly escalation.
    The sdlc skill and ``get_status`` enrichment only react to
    OVERSEER_ALERT; STATUS/HANDOFF blend into normal traffic.

    Request:
        anomaly (str): required — anomaly type (free text; known types
            include stuck-phase-transition, agent-heartbeat-stall,
            agent-loop, orchestrator-consensus-silent,
            unauthorized-overseer-action, unmediated-disagreement).
        priority (str): required — one of ``low``/``medium``/``high``.
        summary (str): required — one-line description.
        detail (str): optional longer description / observed evidence.
        recommend (str): optional recommended action for the human.
        pipeline_id, role: optional overrides (role defaults to
            EGG_AGENT_ROLE or ``overseer``).

    Response:
        { ok: True, role, alert: {...} }

    State-machine effect: none. This is a write into the message bus;
    the pipeline state machine is not advanced.
    """
    pid = _require_pipeline_id(req)
    role = req.get("role") or get_agent_role() or "overseer"

    anomaly = req.get("anomaly")
    if not anomaly or not isinstance(anomaly, str):
        raise HandlerError("'anomaly' is required")
    priority = req.get("priority")
    if not priority or not isinstance(priority, str):
        raise HandlerError("'priority' is required")
    if priority not in _VALID_OVERSEER_PRIORITIES:
        raise HandlerError(
            f"'priority' must be one of {list(_VALID_OVERSEER_PRIORITIES)}; "
            f"got {priority!r}"
        )
    summary = req.get("summary")
    if not summary or not isinstance(summary, str):
        raise HandlerError("'summary' is required")

    body_parts: list[str] = [summary]
    detail = req.get("detail")
    if detail:
        if not isinstance(detail, str):
            raise HandlerError("'detail' must be a string")
        body_parts.append(f"\nDetail:\n{detail}")
    recommend = req.get("recommend")
    if recommend:
        if not isinstance(recommend, str):
            raise HandlerError("'recommend' must be a string")
        body_parts.append(f"\nRecommended action:\n{recommend}")
    body_text = "\n".join(body_parts).strip()

    data = {
        "from_role": role,
        "to_role": "all",
        "message_type": "OVERSEER_ALERT",
        "subject": f"{anomaly} [{priority}]",
        "body": body_text,
    }

    result = orchestrator_request(
        f"/api/v1/pipelines/{pid}/messages", method="POST", data=data
    )
    if not result.get("success"):
        raise GatewayError(result.get("message", "overseer alert failed"))
    alert_msg = result.get("data", {}).get("message", {})
    return {"ok": True, "role": role, "alert": alert_msg, "signal": result}


def progress_query_status(req: dict[str, Any]) -> dict[str, Any]:
    """Read pipeline status via ``GET /api/v1/pipelines/<pid>/status``.

    CLI counterpart: ``egg-orch pipeline status``. Wrapped as an MCP
    tool so the overseer role (and any observer) can read pipeline
    state without shelling out.

    Request:
        pipeline_id: optional override.
        include_raw (bool): if True, include the full raw status
            payload alongside the summary.

    Response:
        { ok: True, pipeline_id, status, current_phase, pending_decisions,
          updated_at, raw?: {...} }

    State-machine effect: none. Pure read.
    """
    pid = _require_pipeline_id(req)
    include_raw = bool(req.get("include_raw", False))
    result = orchestrator_request(f"/api/v1/pipelines/{pid}/status")
    if not result.get("success", True):
        # The orchestrator returns {success: False, ...} for missing
        # pipelines.  Surface as GatewayError so the MCP client gets a
        # structured is_error payload.
        raise GatewayError(result.get("message", "pipeline status fetch failed"))
    data = result.get("data", result) or {}

    response: dict[str, Any] = {
        "ok": True,
        "pipeline_id": pid,
        "status": data.get("status"),
        "current_phase": data.get("current_phase"),
        "pending_decisions": data.get("pending_decisions", 0),
        "updated_at": data.get("updated_at"),
    }
    if include_raw:
        response["raw"] = data
    return response
