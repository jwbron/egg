"""Task-level handlers (complete task, add commit)."""

from __future__ import annotations

import re
from typing import Any

from egg_agent_tools.handlers._gateway import (
    container_id_field,
    gateway_request,
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
