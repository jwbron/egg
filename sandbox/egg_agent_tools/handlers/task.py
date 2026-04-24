"""Task-level handlers (complete task, add commit, update notes, mark gap)."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from egg_agent_tools.handlers._gateway import (
    container_id_field,
    gateway_request,
    get_agent_role,
    get_contract_identifier,
    get_repo_path,
)
from egg_agent_tools.handlers.errors import GatewayError, HandlerError

_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _resolve_identifier(req: dict[str, Any]) -> int | str:
    explicit = req.get("issue") or req.get("pipeline_id")
    if explicit:
        return explicit  # type: ignore[no-any-return]
    identifier = get_contract_identifier()
    if identifier is None:
        raise HandlerError(
            "Contract identifier required. "
            "Set EGG_ISSUE_NUMBER or EGG_PIPELINE_ID, or pass 'issue'/'pipeline_id'."
        )
    return identifier


def _parse_task_id(task_id: str) -> tuple[int, int]:
    """Parse ``task-N`` or ``task-P-T`` → (phase_idx, task_idx)."""
    lower = task_id.lower()
    stripped = lower.removeprefix("task-")
    if stripped == lower:
        raise HandlerError(f"Invalid task ID '{task_id}': expected format 'task-N' or 'task-P-T'")
    parts = stripped.split("-")
    try:
        if len(parts) == 1:
            phase_idx = 0
            task_idx = int(parts[0]) - 1
        elif len(parts) == 2:
            phase_idx = int(parts[0]) - 1
            task_idx = int(parts[1]) - 1
        else:
            raise ValueError
    except ValueError as exc:
        raise HandlerError(f"Invalid task ID '{task_id}'") from exc
    if phase_idx < 0 or task_idx < 0:
        raise HandlerError(f"Task/phase numbers must be >= 1: {task_id}")
    return phase_idx, task_idx


def _validate_commit_sha(commit: str) -> str:
    if not _COMMIT_SHA_PATTERN.match(commit):
        raise HandlerError(f"Invalid commit SHA '{commit}': expected 7-40 hexadecimal characters")
    return commit


def _task_field_mutate(
    *,
    identifier: int | str,
    repo_path: str,
    phase_idx: int,
    task_idx: int,
    field: str,
    value: Any,
    reason: str,
) -> dict[str, Any]:
    """Mutate a single ``phases.<p>.tasks.<t>.<field>`` entry via the gateway.

    Shared helper for ``add_commit`` / ``update_notes`` / ``mark_gap`` so
    the three handlers stay focused on their field-specific concerns
    (shape validation, reason text) and avoid duplicating the gateway
    dispatch shape.
    """
    field_path = f"phases.{phase_idx}.tasks.{task_idx}.{field}"
    result = gateway_request(
        "/api/v1/contract/mutate",
        method="POST",
        data={
            "identifier": identifier,
            "repo_path": repo_path,
            "field_path": field_path,
            "new_value": value,
            "actor": "egg",
            "reason": reason,
            **container_id_field(),
        },
    )
    if not result.get("success"):
        raise GatewayError(result.get("message", f"{field} mutate failed"))
    return result


def task_complete(req: dict[str, Any]) -> dict[str, Any]:
    """Mark a task complete, optionally linking a commit.

    Request:
        task (str): required, e.g. ``task-1-2``.
        commit (str): optional git commit SHA.
        repo_path, pipeline_id, issue: optional overrides.

    Response:
        { ok: True, task: task_id, commit: sha|None }
    """
    task_id = req.get("task")
    if not task_id or not isinstance(task_id, str):
        raise HandlerError("'task' is required")
    phase_idx, task_idx = _parse_task_id(task_id)

    commit = req.get("commit")
    if commit is not None:
        _validate_commit_sha(commit)

    repo_path = req.get("repo_path") or get_repo_path()
    identifier = _resolve_identifier(req)

    status_path = f"phases.{phase_idx}.tasks.{task_idx}.status"
    # On failure here, gateway_request raises GatewayError; the CLI
    # shim prepends "Error setting status: " for legacy parity.
    result = gateway_request(
        "/api/v1/contract/mutate",
        method="POST",
        data={
            "identifier": identifier,
            "repo_path": repo_path,
            "field_path": status_path,
            "new_value": "complete",
            "actor": "egg",
            "reason": f"Marked {task_id} as complete",
            **container_id_field(),
        },
    )
    if not result.get("success"):
        raise GatewayError(result.get("message", "status mutate failed"))

    if commit:
        commit_path = f"phases.{phase_idx}.tasks.{task_idx}.commit"
        commit_result = gateway_request(
            "/api/v1/contract/mutate",
            method="POST",
            data={
                "identifier": identifier,
                "repo_path": repo_path,
                "field_path": commit_path,
                "new_value": commit,
                "actor": "egg",
                "reason": f"Linked commit {commit[:7]} to {task_id}",
                **container_id_field(),
            },
        )
        if not commit_result.get("success"):
            raise GatewayError(
                "Task marked complete but failed to link commit: "
                + commit_result.get("message", "unknown error"),
            )

    return {"ok": True, "task": task_id, "commit": commit}


def task_add_commit(req: dict[str, Any]) -> dict[str, Any]:
    """Link a git commit SHA to an existing task.

    Request:
        task (str): required, e.g. ``task-1-2``.
        commit (str): required git commit SHA (7-40 hex characters).
        repo_path, pipeline_id, issue: optional overrides.

    Response:
        { ok: True, task: task_id, commit: sha }

    State-machine effect: links the commit SHA to the task. Does NOT
    mark the task complete — call ``task_complete`` separately once
    all work on the task is done.
    """
    task_id = req.get("task")
    if not task_id or not isinstance(task_id, str):
        raise HandlerError("'task' is required")
    commit = req.get("commit")
    if not commit or not isinstance(commit, str):
        raise HandlerError("'commit' is required")
    _validate_commit_sha(commit)
    phase_idx, task_idx = _parse_task_id(task_id)

    repo_path = req.get("repo_path") or get_repo_path()
    identifier = _resolve_identifier(req)

    _task_field_mutate(
        identifier=identifier,
        repo_path=repo_path,
        phase_idx=phase_idx,
        task_idx=task_idx,
        field="commit",
        value=commit,
        reason=f"Linked commit {commit[:7]} to {task_id}",
    )
    return {"ok": True, "task": task_id, "commit": commit}


def task_update_notes(req: dict[str, Any]) -> dict[str, Any]:
    """Append/replace implementation notes on a task.

    Request:
        task (str): required, e.g. ``task-1-2``.
        notes (str): required implementation-notes string.
        repo_path, pipeline_id, issue: optional overrides.

    Response:
        { ok: True, task: task_id }

    State-machine effect: replaces the task's ``notes`` field. Does
    NOT mark the task complete.
    """
    task_id = req.get("task")
    if not task_id or not isinstance(task_id, str):
        raise HandlerError("'task' is required")
    notes = req.get("notes")
    if notes is None or not isinstance(notes, str):
        raise HandlerError("'notes' is required")
    phase_idx, task_idx = _parse_task_id(task_id)

    repo_path = req.get("repo_path") or get_repo_path()
    identifier = _resolve_identifier(req)

    _task_field_mutate(
        identifier=identifier,
        repo_path=repo_path,
        phase_idx=phase_idx,
        task_idx=task_idx,
        field="notes",
        value=notes,
        reason=f"Updated notes for {task_id}",
    )
    return {"ok": True, "task": task_id}


def task_mark_gap(req: dict[str, Any]) -> dict[str, Any]:
    """Append a tester→coder coverage-gap record to a task.

    No CLI counterpart — this is a net-new capability introduced in
    iteration 2 (no-CLI, decision-4). Persistence routes through the
    existing gateway contract-mutate endpoint onto the new
    ``phases.<p>.tasks.<t>.gaps[<n>]`` field on the Task model.

    Request:
        task (str): required, e.g. ``task-1-2``.
        description (str): required — what the tester thinks is
            uncovered.
        to_role (str): optional target role (defaults to ``"coder"``).
        from_role (str): optional sender override (defaults to
            ``EGG_AGENT_ROLE``).
        gap_id (str): optional explicit gap id; the handler generates a
            ``gap-<short-uuid>`` slug when omitted.
        repo_path, pipeline_id, issue: optional overrides.

    Response:
        { ok: True, task: task_id, gap_id: "gap-..." }
    """
    task_id = req.get("task")
    if not task_id or not isinstance(task_id, str):
        raise HandlerError("'task' is required")
    description = req.get("description")
    if not description or not isinstance(description, str):
        raise HandlerError("'description' is required")
    phase_idx, task_idx = _parse_task_id(task_id)

    to_role = req.get("to_role") or "coder"
    if not isinstance(to_role, str) or not to_role:
        raise HandlerError("'to_role' must be a non-empty string")
    from_role = req.get("from_role") or get_agent_role()
    if not from_role:
        raise HandlerError(
            "Sender role required. Set EGG_AGENT_ROLE or pass 'from_role'."
        )

    gap_id = req.get("gap_id") or f"gap-{uuid.uuid4().hex[:8]}"
    if not isinstance(gap_id, str):
        raise HandlerError("'gap_id' must be a string if provided")

    repo_path = req.get("repo_path") or get_repo_path()
    identifier = _resolve_identifier(req)

    # Fetch to find where to append into gaps[].  We use gateway read
    # rather than Python-side merge so the tool works even when the
    # handler doesn't have the contract in-process.
    params: dict[str, str] = {}
    if repo_path:
        params["repo_path"] = repo_path
    from egg_agent_tools.handlers._gateway import get_container_id

    cid = get_container_id()
    if cid:
        params["container_id"] = cid
    read_result = gateway_request(
        f"/api/v1/contract/{identifier}", params=params or None
    )
    if not read_result.get("success"):
        raise GatewayError(read_result.get("message", "contract fetch failed"))
    contract = read_result.get("data", {}) or {}
    phases = contract.get("phases") or []
    if phase_idx >= len(phases):
        raise HandlerError(
            f"Phase index {phase_idx + 1} out of range for contract "
            f"(has {len(phases)} phase(s))"
        )
    tasks = phases[phase_idx].get("tasks") or []
    if task_idx >= len(tasks):
        raise HandlerError(
            f"Task index {task_idx + 1} out of range for phase {phase_idx + 1} "
            f"(has {len(tasks)} task(s))"
        )
    existing_gaps = list(tasks[task_idx].get("gaps") or [])
    next_gap_idx = len(existing_gaps)

    gap_record = {
        "id": gap_id,
        "from_role": from_role,
        "to_role": to_role,
        "description": description,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "resolved": False,
    }

    field_path = f"phases.{phase_idx}.tasks.{task_idx}.gaps.{next_gap_idx}"
    result = gateway_request(
        "/api/v1/contract/mutate",
        method="POST",
        data={
            "identifier": identifier,
            "repo_path": repo_path,
            "field_path": field_path,
            "new_value": gap_record,
            "actor": "egg",
            "reason": (
                f"Recorded gap {gap_id} on {task_id} "
                f"(from {from_role} to {to_role})"
            ),
            **container_id_field(),
        },
    )
    if not result.get("success"):
        raise GatewayError(result.get("message", "gap mutate failed"))

    return {"ok": True, "task": task_id, "gap_id": gap_id, "gap": gap_record}
