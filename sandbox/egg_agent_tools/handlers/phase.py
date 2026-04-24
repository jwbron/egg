"""Phase-context handlers (get_context, get_assigned_tasks)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from egg_agent_tools.handlers._gateway import (
    container_id_field,
    gateway_request,
    get_agent_role,
    get_container_id,
    get_contract_identifier,
    get_phase,
    get_pipeline_id,
    get_repo_path,
)
from egg_agent_tools.handlers.errors import GatewayError, HandlerError

_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _validate_commit_sha(commit: str) -> str:
    if not _COMMIT_SHA_PATTERN.match(commit):
        raise HandlerError(f"Invalid commit SHA '{commit}': expected 7-40 hexadecimal characters")
    return commit


def _parse_phase_id(phase_id: str) -> int:
    """Parse ``phase-N`` → phase_idx (0-based)."""
    if not isinstance(phase_id, str):
        raise HandlerError(f"Invalid phase ID {phase_id!r}: must be a string")
    lower = phase_id.lower()
    stripped = lower.removeprefix("phase-")
    if stripped == lower:
        raise HandlerError(f"Invalid phase ID '{phase_id}': expected format 'phase-N'")
    try:
        phase_num = int(stripped)
    except ValueError as exc:
        raise HandlerError(f"Invalid phase ID '{phase_id}': expected format 'phase-N'") from exc
    if phase_num < 1:
        raise HandlerError(f"Phase number must be >= 1: {phase_id}")
    return phase_num - 1


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


def _fetch_contract(identifier: int | str, repo_path: str | None) -> dict[str, Any]:
    params: dict[str, str] = {}
    if repo_path:
        params["repo_path"] = repo_path
    cid = get_container_id()
    if cid:
        params["container_id"] = cid
    result = gateway_request(f"/api/v1/contract/{identifier}", params=params or None)
    if not result.get("success"):
        raise GatewayError(result.get("message", "contract fetch failed"))
    return result.get("data", {})  # type: ignore[no-any-return]


def _tasks_for_role(contract: dict[str, Any], role: str | None) -> list[dict[str, Any]]:
    """Return every task (flat list) assigned to ``role``.

    If ``role`` is None, returns all tasks.  Tasks without an explicit
    ``role`` field are treated as assigned to the default producer (coder)
    for backward compatibility.
    """
    tasks: list[dict[str, Any]] = []
    for phase in contract.get("phases") or []:
        for t in phase.get("tasks") or []:
            task_role = t.get("role")
            if role is None or task_role == role or (task_role is None and role == "coder"):
                tasks.append(
                    {
                        "id": t.get("id"),
                        "description": t.get("description"),
                        "status": t.get("status"),
                        "phase_id": phase.get("id"),
                        "phase_name": phase.get("name"),
                        "acceptance": t.get("acceptance"),
                        "files": t.get("files") or [],
                        "role": task_role,
                        "commit": t.get("commit"),
                    }
                )
    return tasks


def _find_artifact_paths(pipeline_id: str | None, phase: str | None) -> list[str]:
    """Collect prior-phase artifacts from ``.egg-state/`` when present.

    Looks for ``.egg-state/drafts/<N>-*.md`` and
    ``.egg-state/agent-outputs/<N>-*-output.json`` under the repo root.
    Missing directories are ignored — the list is informational.
    """
    repo_root = Path(os.environ.get("EGG_REPO_PATH", ".")).resolve()
    state = repo_root / ".egg-state"
    if not state.exists():
        return []

    prefix = ""
    if pipeline_id:
        # Pipeline IDs sometimes look like "issue-1765"; artifact files
        # use the bare issue number.
        digits = "".join(ch for ch in pipeline_id if ch.isdigit())
        prefix = digits or pipeline_id

    paths: list[str] = []
    for sub in ("drafts", "agent-outputs", "contracts"):
        d = state / sub
        if not d.exists():
            continue
        for p in sorted(d.iterdir()):
            if prefix and not p.name.startswith(prefix):
                continue
            if phase and phase not in p.name:
                # still include — caller might want cross-phase artefacts
                pass
            try:
                paths.append(str(p.relative_to(repo_root)))
            except ValueError:
                pass  # skip paths that escape repo_root
    return paths


def phase_get_context(req: dict[str, Any]) -> dict[str, Any]:
    """Bundle the phase-context the agent would otherwise dig for.

    Request:
        pipeline_id, phase, role, repo_path, issue: optional overrides.
        include_artifacts (bool): default True; include prior-phase
            artifact paths.

    Response includes pipeline/phase/role, a filtered task list, and a
    list of referenced artifact paths (best-effort).

    No CLI counterpart — this aggregates the contract read, env-vars,
    and best-effort artifact scan into a single ergonomic agent view
    (decision-13).
    """
    role = req.get("role") or get_agent_role()
    phase = req.get("phase") or get_phase()
    pipeline_id = req.get("pipeline_id") or get_pipeline_id()
    repo_path = req.get("repo_path") or get_repo_path()
    include_artifacts = bool(req.get("include_artifacts", True))

    tasks: list[dict[str, Any]] = []
    contract_present = False
    contract: dict[str, Any] = {}
    try:
        identifier = _resolve_identifier(req)
    except HandlerError:
        # No identifier in env/args — still return environment context
        # rather than failing the whole tool.  This is the "fallback"
        # branch; we do NOT catch gateway failures here because those
        # indicate infrastructure problems the agent needs to see.
        identifier = None
    if identifier is not None:
        contract = _fetch_contract(identifier, repo_path)
        contract_present = True
        tasks = _tasks_for_role(contract, role)

    artifacts: list[str] = []
    if include_artifacts:
        artifacts = _find_artifact_paths(pipeline_id, phase)

    return {
        "ok": True,
        "pipeline_id": pipeline_id,
        "phase": phase,
        "role": role,
        "contract_present": contract_present,
        "current_contract_phase": contract.get("current_phase") if contract else None,
        "tasks": tasks,
        "artifacts": artifacts,
        "repo_path": repo_path,
    }


def phase_get_assigned_tasks(req: dict[str, Any]) -> dict[str, Any]:
    """Return only the tasks assigned to the caller's role.

    Request:
        role (str): override (defaults to EGG_AGENT_ROLE).
        status (str): optional filter (pending/in-progress/complete).
        pipeline_id, issue, repo_path: overrides.

    No CLI counterpart — this is the role-filtered view of the task
    list; the raw task set is in `egg-contract show --json` but the
    role-projection is agent-specific (decision-13).
    """
    role = req.get("role") or get_agent_role()
    status_filter = req.get("status")
    repo_path = req.get("repo_path") or get_repo_path()
    identifier = _resolve_identifier(req)
    contract = _fetch_contract(identifier, repo_path)
    tasks = _tasks_for_role(contract, role)
    if status_filter:
        tasks = [t for t in tasks if t.get("status") == status_filter]
    return {
        "ok": True,
        "role": role,
        "tasks": tasks,
        "count": len(tasks),
    }


def phase_complete_phase(req: dict[str, Any]) -> dict[str, Any]:
    """Mark a phase as complete, optionally linking a commit SHA.

    Request:
        phase (str): required — e.g. ``phase-1``.
        commit (str): optional git commit SHA to link to the phase.
        repo_path, pipeline_id, issue: optional overrides.

    Response:
        { ok: True, phase: phase_id, commit: sha|None }

    State-machine effect: transitions ``phases.<p>.status`` to
    ``"complete"``; orchestrator's downstream phase_complete signal
    fires to advance the pipeline once all phases have been completed.
    Does NOT advance the overall pipeline phase on its own.

    Atomicity: the commit-link and the status transition are two
    separate gateway mutations (the gateway's ``contract/mutate``
    endpoint takes a single field-path per call).  We link the commit
    FIRST so a mid-way failure leaves the phase not-yet-complete with
    the commit populated — callers can retry the same request to
    progress.  If the status step fails, the raise preserves the
    legacy "Error setting status:" stderr surface from the CLI shim
    (see reviewer NACK #6).  A successful return guarantees both
    mutations landed.
    """
    phase_id = req.get("phase")
    if not phase_id or not isinstance(phase_id, str):
        raise HandlerError("'phase' is required")
    phase_idx = _parse_phase_id(phase_id)

    commit = req.get("commit")
    if commit is not None:
        if not isinstance(commit, str):
            raise HandlerError("'commit' must be a string if provided")
        _validate_commit_sha(commit)

    repo_path = req.get("repo_path") or get_repo_path()
    identifier = _resolve_identifier(req)

    # Step 1 (iff commit provided): link the commit.  This is
    # idempotent — re-running with the same SHA is a no-op on the
    # gateway's side.
    if commit:
        commit_path = f"phases.{phase_idx}.commit"
        commit_result = gateway_request(
            "/api/v1/contract/mutate",
            method="POST",
            data={
                "identifier": identifier,
                "repo_path": repo_path,
                "field_path": commit_path,
                "new_value": commit,
                "actor": "egg",
                "reason": f"Linked commit {commit[:7]} to {phase_id}",
                **container_id_field(),
            },
        )
        if not commit_result.get("success"):
            raise GatewayError(commit_result.get("message", "phase commit link failed"))

    # Step 2: flip status to complete.  On failure the caller sees a
    # vanilla GatewayError ("Error setting status: …") and can retry.
    status_path = f"phases.{phase_idx}.status"
    result = gateway_request(
        "/api/v1/contract/mutate",
        method="POST",
        data={
            "identifier": identifier,
            "repo_path": repo_path,
            "field_path": status_path,
            "new_value": "complete",
            "actor": "egg",
            "reason": f"Marked {phase_id} as complete",
            **container_id_field(),
        },
    )
    if not result.get("success"):
        raise GatewayError(result.get("message", "phase status mutate failed"))

    return {"ok": True, "phase": phase_id, "commit": commit}
