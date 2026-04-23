"""BRC consensus handlers (propose, ack, nack, confirm, state, blocking)."""

from __future__ import annotations

import os
import subprocess
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
        raise HandlerError(
            "role required. Set EGG_AGENT_ROLE or pass 'role'."
        )
    return role


def _resolve_head_sha() -> str:
    cwd = os.environ.get("EGG_REPO_PATH") or None
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            text=True,
            cwd=cwd,
            # stdin=DEVNULL defensively avoids the subprocess inheriting
            # a non-tty parent's stdio and blocking on an interactive
            # prompt.  Covers the edge case where the handler runs
            # inside a cron / systemd-style parent.
            stdin=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise HandlerError(
            "'commit_sha' not provided and could not resolve HEAD"
        ) from exc


def brc_propose(req: dict[str, Any]) -> dict[str, Any]:
    """Send a CONSENSUS_PROPOSE signal.

    Request (all optional unless noted):
        summary (str): proposal summary (required; ≥50 chars recommended)
            unless ``raw_payload`` already carries it.
        artifacts (list[str]): artifact references.
        risk_considered (str): risk summary.
        commit_sha (str): commit SHA; defaults to ``git rev-parse HEAD``.
        files_changed (list[str])
        tests_run (list[str])
        tasks (list[str]): tasks_satisfied
        attestation (dict): attestation payload.
        changed_artifacts (list[str]): optional re-proposal delta.
        raw_payload (dict): pre-built payload dict — every key is
            forwarded verbatim to the orchestrator.  Structured
            ``req`` keys take precedence when both are supplied.
        pipeline_id, role: contract/role overrides.

    Response:
        { ok: True, signal: <orchestrator response>, phase: "..." }
    """
    pid = _require_pipeline_id(req)
    role = _require_role(req)

    raw_payload = req.get("raw_payload")
    if raw_payload and not isinstance(raw_payload, dict):
        raise HandlerError("'raw_payload' must be a dict if provided")

    summary = req.get("summary") or (
        raw_payload.get("summary") if raw_payload else None
    )
    if not summary or not isinstance(summary, str):
        raise HandlerError("'summary' is required")

    commit_sha = (
        req.get("commit_sha")
        or (raw_payload.get("commit_sha") if raw_payload else None)
        or _resolve_head_sha()
    )

    # Start from raw_payload (if any) so unknown/custom schema fields
    # are preserved verbatim; structured kwargs layer on top.
    payload: dict[str, Any] = dict(raw_payload) if raw_payload else {}
    payload.update(
        {
            "summary": summary,
            "attestation": req.get("attestation")
            or payload.get("attestation")
            or {},
            "artifacts": list(
                req.get("artifacts") or payload.get("artifacts") or []
            ),
            "risk_considered": (
                req.get("risk_considered")
                or req.get("risk")
                or payload.get("risk_considered")
                or payload.get("risk")
                or ""
            ),
            "commit_sha": commit_sha,
            "files_changed": list(
                req.get("files_changed") or payload.get("files_changed") or []
            ),
            "tests_run": list(
                req.get("tests_run") or payload.get("tests_run") or []
            ),
            "tasks_satisfied": list(
                req.get("tasks")
                or req.get("tasks_satisfied")
                or payload.get("tasks_satisfied")
                or payload.get("tasks")
                or []
            ),
        }
    )
    data: dict[str, Any] = {
        "signal_type": "consensus_propose",
        "agent_role": role,
        "payload": payload,
    }
    if req.get("changed_artifacts"):
        data["changed_artifacts"] = list(req["changed_artifacts"])

    result = orchestrator_request(
        f"/api/v1/pipelines/{pid}/signal", method="POST", data=data
    )
    if not result.get("success"):
        raise GatewayError(result.get("message", "propose failed"))

    consensus = result.get("data", {}).get("consensus", {})
    phase = consensus.get("agents", {}).get(role, {}).get("phase", "")
    return {"ok": True, "role": role, "phase": phase, "signal": result}


def brc_ack(req: dict[str, Any]) -> dict[str, Any]:
    """Send a CONSENSUS_ACK signal for a producer.

    Request:
        producer_role (str): required.
        reason (str): required.
        files_reviewed (list[str]): optional list of artifact references.
        pipeline_id, role: overrides.
    """
    pid = _require_pipeline_id(req)
    role = _require_role(req)
    producer_role = req.get("producer_role")
    if not producer_role:
        raise HandlerError("'producer_role' is required")
    reason = req.get("reason")
    if not reason:
        raise HandlerError("'reason' is required")

    data = {
        "signal_type": "consensus_ack",
        "agent_role": role,
        "producer_role": producer_role,
        "payload": {
            "artifact_references": list(req.get("files_reviewed") or []),
            "reason": reason,
        },
    }
    result = orchestrator_request(
        f"/api/v1/pipelines/{pid}/signal", method="POST", data=data
    )
    if not result.get("success"):
        raise GatewayError(result.get("message", "ack failed"))
    return {"ok": True, "role": role, "producer_role": producer_role, "signal": result}


def brc_nack(req: dict[str, Any]) -> dict[str, Any]:
    """Send a CONSENSUS_NACK signal for a producer.

    Request:
        producer_role (str): required.
        reason (str): required (describes why the proposal is blocked).
        files_reviewed (list[str]): optional list of artifact references.
        pipeline_id, role: overrides.
    """
    pid = _require_pipeline_id(req)
    role = _require_role(req)
    producer_role = req.get("producer_role")
    if not producer_role:
        raise HandlerError("'producer_role' is required")
    reason = req.get("reason")
    if not reason:
        raise HandlerError("'reason' is required")

    data = {
        "signal_type": "consensus_nack",
        "agent_role": role,
        "producer_role": producer_role,
        "payload": {
            "reason": reason,
            "artifact_references": list(req.get("files_reviewed") or []),
        },
    }
    result = orchestrator_request(
        f"/api/v1/pipelines/{pid}/signal", method="POST", data=data
    )
    if not result.get("success"):
        raise GatewayError(result.get("message", "nack failed"))
    return {"ok": True, "role": role, "producer_role": producer_role, "signal": result}


def brc_confirm(req: dict[str, Any]) -> dict[str, Any]:
    """Send CONSENSUS_CONFIRMED after all reviewers have ACKed.

    Request:
        pipeline_id, role: overrides.

    Response carries:
        status: "confirmed"|"pending_acks"
        consensus_reached: bool (only for status=="confirmed")
    """
    pid = _require_pipeline_id(req)
    role = _require_role(req)

    data = {
        "signal_type": "consensus_confirmed",
        "agent_role": role,
    }
    result = orchestrator_request(
        f"/api/v1/pipelines/{pid}/signal", method="POST", data=data
    )
    if not result.get("success"):
        raise GatewayError(result.get("message", "confirm failed"))
    body = result.get("data", {})
    pending = body.get("status") == "pending_acks"
    return {
        "ok": True,
        "role": role,
        "status": "pending_acks" if pending else "confirmed",
        "consensus_reached": bool(body.get("consensus_reached", False)),
        "message": result.get("message"),
        "signal": result,
    }


def brc_get_state(req: dict[str, Any]) -> dict[str, Any]:
    """Fetch the current BRC consensus state for the pipeline.

    Request:
        pipeline_id: override.
        verbose (bool): include the full orchestrator status payload.

    Response:
        { ok: True, consensus: {...}, verbose: bool }
    """
    pid = _require_pipeline_id(req)
    verbose = bool(req.get("verbose", False))
    result = orchestrator_request(f"/api/v1/pipelines/{pid}/status")
    data = result.get("data", {})
    consensus = data.get("concurrent", {}).get("consensus", {})

    response: dict[str, Any] = {
        "ok": True,
        "consensus": consensus,
        "is_complete": bool(consensus.get("is_complete", False)),
        "blocking_agents": list(consensus.get("blocking_agents", []) or []),
    }
    if verbose:
        response["raw"] = data
    return response


def brc_list_blocking(req: dict[str, Any]) -> dict[str, Any]:
    """Return the list of agent roles currently blocking consensus.

    Request:
        pipeline_id: override.
    """
    pid = _require_pipeline_id(req)
    result = orchestrator_request(f"/api/v1/pipelines/{pid}/status")
    consensus = (
        result.get("data", {}).get("concurrent", {}).get("consensus", {})
    )
    blocking = list(consensus.get("blocking_agents", []) or [])
    return {"ok": True, "blocking_agents": blocking}
