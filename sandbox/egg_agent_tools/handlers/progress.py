"""Progress-signal handlers (progress, error, heartbeat)."""

from __future__ import annotations

import re
from typing import Any

from egg_agent_tools.handlers._gateway import (
    get_agent_role,
    get_pipeline_id,
    get_slice_id,
    orchestrator_request,
)
from egg_agent_tools.handlers.errors import GatewayError, HandlerError

_PIPELINE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
_SLICE_ID_PATTERN = re.compile(r"^slice-[0-9]+$")


def _maybe_attach_slice_id(req: dict[str, Any], data: dict[str, Any]) -> None:
    """Forward ``slice_id`` from the request or env onto the signal body.

    Mirrors ``brc._maybe_attach_slice_id`` so the orchestrator-side
    ``handle_error_signal`` can scope its "agent already COMPLETE,
    suppress error" check by ``(role, slice_id)`` rather than role
    alone — without this, slice-2 coder finishing would silently
    swallow slice-3 coder's error (#2422).
    """
    slice_id = req.get("slice_id") or get_slice_id()
    if not slice_id:
        return
    if not isinstance(slice_id, str) or not _SLICE_ID_PATTERN.fullmatch(slice_id):
        raise HandlerError(f"Invalid slice_id {slice_id!r}: must match 'slice-<N>'")
    data["slice_id"] = slice_id


def _require_pipeline_id(req: dict[str, Any]) -> str:
    pid = req.get("pipeline_id") or get_pipeline_id()
    if not pid:
        raise HandlerError("pipeline_id required. Set EGG_PIPELINE_ID or pass 'pipeline_id'.")
    if not _PIPELINE_ID_PATTERN.match(pid):
        raise HandlerError(f"Invalid pipeline_id {pid!r}: must match [a-zA-Z0-9_-]+")
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
    event = (result.get("data") or {}).get("event", {})
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

    data: dict[str, Any] = {
        "signal_type": "error",
        "agent_role": role,
        "error": error,
        "recoverable": recoverable,
    }
    _maybe_attach_slice_id(req, data)
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


# Body-size cap for ``recommendation_payload`` (issue #1962). 50 KB is the
# same hard limit the gateway enforces on issue bodies; payloads larger
# than this never need to round-trip through the message bus.
_MAX_RECOMMENDATION_PAYLOAD_BYTES = 50_000

# Schema-version semantics for OVERSEER_ALERT.metadata (issue #1962):
# v1 = pre-#1962 alerts (no `recommendation` / `recommendation_payload`);
# v2 = adds the two fields. /sdlc parsing reads the version (defaulting
# to 1 if absent) and falls back gracefully on lower versions per
# risk_analyst R-COMPAT-06.
_OVERSEER_ALERT_SCHEMA_VERSION = 2

_VALID_RECOMMENDATIONS = ("file_issue",)


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
            ``unmediated-disagreement`` is for observers (overseer /
            mediator) flagging that no one is adjudicating a
            disagreement; producers blocked by reviewer NACKs that
            name an operator-decidable scope question should call
            ``mcp__sdlc__register_open_question`` instead so the
            decision lands in ``pending_decisions`` (HITL gate)
            rather than as an informational alert.
        priority (str): required — one of ``low``/``medium``/``high``.
        summary (str): required — one-line description.
        detail (str): optional longer description / observed evidence.
        recommend (str): optional recommended action for the human.
        recommendation (str): optional structured advisor recommendation
            (issue #1962). Currently the only legal value is
            ``"file_issue"``; the human gates the actual filing via the
            existing HITL flow. Carried in ``metadata.recommendation``
            so legacy consumers see no schema change.
        recommendation_payload (dict): optional opaque blob carrying the
            advisor's composed ``issue_title`` / ``issue_body`` /
            ``priority`` / ``anomaly_signature``. Bounded at 50 KB.
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
            f"'priority' must be one of {list(_VALID_OVERSEER_PRIORITIES)}; got {priority!r}"
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

    # Issue #1962: optional structured recommendation. The fields land
    # as first-class optional fields on the OVERSEER_ALERT message
    # envelope (orchestrator/message_store.py:Message) so the
    # backwards-compat regression test in TASK-7-1 can distinguish
    # "no field" from "metadata key missing". Pre-#1962 callers omit
    # both fields and the message round-trips with schema_version=1
    # (the pre-#1962 default); when either field is set the message
    # records schema_version=2 so /sdlc parsers can branch on the
    # version per risk_analyst R-COMPAT-06.
    data: dict[str, Any] = {
        "from_role": role,
        "to_role": "all",
        "message_type": "OVERSEER_ALERT",
        "subject": f"{anomaly} [{priority}]",
        "body": body_text,
    }
    recommendation = req.get("recommendation")
    if recommendation is not None:
        if not isinstance(recommendation, str):
            raise HandlerError("'recommendation' must be a string")
        if recommendation not in _VALID_RECOMMENDATIONS:
            raise HandlerError(
                f"'recommendation' must be one of {list(_VALID_RECOMMENDATIONS)}; "
                f"got {recommendation!r}"
            )
        data["recommendation"] = recommendation
        data["schema_version"] = _OVERSEER_ALERT_SCHEMA_VERSION
    recommendation_payload = req.get("recommendation_payload")
    if recommendation_payload is not None:
        if not isinstance(recommendation_payload, dict):
            raise HandlerError("'recommendation_payload' must be a dict")
        # Cheap size cap; full content scan happens at the gateway.
        import json as _json

        encoded = _json.dumps(recommendation_payload).encode("utf-8")
        if len(encoded) > _MAX_RECOMMENDATION_PAYLOAD_BYTES:
            raise HandlerError(
                f"'recommendation_payload' exceeds {_MAX_RECOMMENDATION_PAYLOAD_BYTES} bytes"
            )
        data["recommendation_payload"] = recommendation_payload
        data["schema_version"] = _OVERSEER_ALERT_SCHEMA_VERSION

    result = orchestrator_request(f"/api/v1/pipelines/{pid}/messages", method="POST", data=data)
    if not result.get("success"):
        raise GatewayError(result.get("message", "overseer alert failed"))
    alert_msg = (result.get("data") or {}).get("message", {})
    return {"ok": True, "role": role, "alert": alert_msg, "signal": result}


def progress_query_status(req: dict[str, Any]) -> dict[str, Any]:
    """Read pipeline status via ``GET /api/v1/pipelines/<pid>/status``.

    CLI counterpart: ``egg-orch pipeline status``. Wrapped as an MCP
    tool so the overseer role (and any observer) can read pipeline
    state without shelling out.

    Security: the caller may supply ``pipeline_id`` only if it
    matches ``EGG_PIPELINE_ID`` exactly. A disagreeing override is
    rejected with ``HandlerError`` (risk_analyst R2 + reviewer_code
    NACK #2 — cross-pipeline-read hardening). When no env pipeline id
    is set (e.g. operator shell use), the caller-supplied value is
    accepted as a fallback.

    Request:
        pipeline_id: optional; must match EGG_PIPELINE_ID when that
            env var is set.
        include_raw (bool): if True, include the full raw status
            payload alongside the summary.

    Response:
        { ok: True, pipeline_id, status, current_phase, pending_decisions,
          updated_at, raw?: {...} }

    State-machine effect: none. Pure read.
    """
    env_pid = get_pipeline_id()
    caller_pid = req.get("pipeline_id")
    if caller_pid and env_pid and caller_pid != env_pid:
        raise HandlerError(
            "Caller-supplied pipeline_id must match EGG_PIPELINE_ID; "
            f"got {caller_pid!r} (env={env_pid!r})."
        )
    pid = env_pid or caller_pid
    if not pid:
        raise HandlerError("pipeline_id required. Set EGG_PIPELINE_ID or pass 'pipeline_id'.")
    if not _PIPELINE_ID_PATTERN.match(pid):
        raise HandlerError(f"Invalid pipeline_id {pid!r}: must match [a-zA-Z0-9_-]+")
    include_raw = bool(req.get("include_raw", False))
    result = orchestrator_request(f"/api/v1/pipelines/{pid}/status")
    if not result.get("success"):
        # The orchestrator returns {success: False, ...} for missing
        # pipelines.  Surface as GatewayError so the MCP client gets a
        # structured is_error payload.  Consistent with every other
        # handler — defaults to None/falsy when the key is absent.
        raise GatewayError(result.get("message", "pipeline status fetch failed"))
    data = result.get("data") or {}

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
