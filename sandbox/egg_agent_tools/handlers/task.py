"""Task-level handlers (complete task, add commit, update notes, mark gap)."""

from __future__ import annotations

import re
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

# Apply-phase notes-prefix projection. The APPLIER role on Jira-epic
# pipelines (issue #1557) encodes per-task lifecycle status as a
# structured prefix line in ``Task.notes`` (no typed ``mcp__task__set_status``
# MCP exists today). When ``task_update_notes`` writes notes whose first
# lines match these patterns, we project the values onto the typed
# ``Task.jira_action_status`` / ``Task.jira_key`` fields immediately
# after the notes write so downstream consumers (apply-phase
# ``reviewer_contract``, the wontdo drain's idempotency gate,
# plan-parser round-trips) see a single coherent surface instead of two
# views that can drift.
#
# Atomicity: the projection issues 1-2 additional ``_task_field_mutate``
# gateway calls after the notes write. These are NOT atomic with the
# notes write — a crash between calls leaves the typed fields trailing
# the prefix by one mutation. The notes prefix remains the
# authoritative source under that race; the next ``task_update_notes``
# call re-runs the projection, and reviewers / drain that read either
# surface still converge. (When a typed ``mcp__task__set_status`` MCP
# lands, both surfaces collapse to one and this race goes away.)
_JIRA_ACTION_STATUS_PREFIX_RE = re.compile(
    r"^jira_action_status=(pending|in_flight|applied|failed)\s*$"
)
_JIRA_KEY_PREFIX_RE = re.compile(r"^jira_key=([A-Z][A-Z0-9_]*-[0-9]+)\s*$")


def _project_notes_prefix(notes: str) -> tuple[str | None, str | None]:
    """Extract typed (jira_action_status, jira_key) from a notes prefix.

    The applier emits the prefix as the first 1-2 lines of
    ``Task.notes``. Either field may be absent; both None means the
    notes carry no prefix and the typed projection is a no-op.
    """
    status: str | None = None
    key: str | None = None
    # Inspect at most the first two non-empty lines — the prefix is
    # always at the top, before any human-readable narrative.
    for line in notes.splitlines()[:2]:
        m = _JIRA_ACTION_STATUS_PREFIX_RE.match(line)
        if m:
            status = m.group(1)
            continue
        m = _JIRA_KEY_PREFIX_RE.match(line)
        if m:
            key = m.group(1)
    return status, key


# Bounded retry on gap TOCTOU collisions.  Two concurrent ``mark_gap``
# calls may both observe ``len(existing_gaps) == N`` and race on the
# same index; the loser re-reads and retries at ``N+1``.  Three
# attempts cover the realistic contention window (two tester→coder
# handoffs firing at the same moment) and keep the handler's worst-case
# latency bounded so the MCP 60 s timeout still applies.
_GAP_RETRY_ATTEMPTS = 3


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
        if not isinstance(commit, str):
            raise HandlerError("'commit' must be a string")
        _validate_commit_sha(commit)

    repo_path = req.get("repo_path") or get_repo_path()
    identifier = _resolve_identifier(req)

    # Atomicity: the commit-link and the status transition are two
    # separate gateway mutations (the gateway's ``contract/mutate``
    # endpoint takes a single field-path per call).  We link the commit
    # FIRST so a mid-way failure leaves the task not-yet-complete with
    # the commit populated — callers can retry the same request to
    # progress.  Matches the ordering in ``phase_complete_phase``.
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
            raise GatewayError(commit_result.get("message", "commit link failed"))

    status_path = f"phases.{phase_idx}.tasks.{task_idx}.status"
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

    # Apply-phase typed-field projection (issue #1557). When the notes
    # start with a structured ``jira_action_status=<value>`` /
    # ``jira_key=<KEY>`` prefix written by the APPLIER, propagate the
    # values to the typed ``Task.jira_action_status`` / ``Task.jira_key``
    # fields so the apply-phase reviewer and the wontdo drain's
    # idempotency gate see a single coherent surface. The projection
    # runs as 1-2 follow-up ``_task_field_mutate`` calls (NOT atomic
    # with the notes write — see the module-level comment on
    # ``_project_notes_prefix`` for the race window): if a follow-up
    # raises ``GatewayError``, the notes write has already landed and
    # the prefix remains authoritative; the next ``task_update_notes``
    # re-runs the projection.
    projected_status, projected_key = _project_notes_prefix(notes)
    if projected_status is not None:
        _task_field_mutate(
            identifier=identifier,
            repo_path=repo_path,
            phase_idx=phase_idx,
            task_idx=task_idx,
            field="jira_action_status",
            value=projected_status,
            reason=f"Projected jira_action_status={projected_status} from notes prefix on {task_id}",
        )
    if projected_key is not None:
        _task_field_mutate(
            identifier=identifier,
            repo_path=repo_path,
            phase_idx=phase_idx,
            task_idx=task_idx,
            field="jira_key",
            value=projected_key,
            reason=f"Projected jira_key={projected_key} from notes prefix on {task_id}",
        )
    return {"ok": True, "task": task_id}


def _next_gap_id(existing_gaps: list[dict[str, Any]]) -> str:
    """Derive the next ``gap-<N>`` id from the existing-gaps list.

    Matches the plan (TASK-4-2) spec: N = max existing numeric suffix
    + 1, starting at 1 for an empty list.  Non-matching id strings are
    ignored (defensive against old records).
    """
    max_num = 0
    for g in existing_gaps:
        gid = g.get("id", "") if isinstance(g, dict) else ""
        if not isinstance(gid, str):
            continue
        m = re.match(r"^gap-([0-9]+)$", gid)
        if m:
            try:
                n = int(m.group(1))
            except ValueError:
                continue
            if n > max_num:
                max_num = n
    return f"gap-{max_num + 1}"


def task_mark_gap(req: dict[str, Any]) -> dict[str, Any]:
    """Append a tester→coder coverage-gap record to a task.

    Role constraint: **tester role writes; coder role reads.**  This
    is a net-new capability introduced in iteration 2 (no-CLI,
    decision-4) for structured coverage-gap handoff.  Persistence
    routes through the existing gateway contract-mutate endpoint onto
    the new ``phases.<p>.tasks.<t>.gaps[<n>]`` field on the Task model.
    No CLI counterpart — operators interact via the contract JSON
    directly.

    TOCTOU hardening: two concurrent ``mark_gap`` calls on the same
    task may observe the same ``len(gaps)`` and both race on that
    index.  The handler retries up to ``_GAP_RETRY_ATTEMPTS`` times,
    re-reading the task and regenerating the ``gap-<N>`` id + field
    path on each attempt; the loser's write lands at ``N+1`` (reviewer
    NACK #5).

    Request:
        task (str): required, e.g. ``task-1-2``.
        description (str): required — what the tester thinks is
            uncovered.
        to_role (str): optional target role (defaults to ``"coder"``).
        from_role (str): optional sender override (defaults to
            ``EGG_AGENT_ROLE``).
        repo_path, pipeline_id, issue: optional overrides.

    Response:
        { ok: True, task: task_id, gap_id: "gap-<N>", gap: {...} }
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
        raise HandlerError("Sender role required. Set EGG_AGENT_ROLE or pass 'from_role'.")

    repo_path = req.get("repo_path") or get_repo_path()
    identifier = _resolve_identifier(req)

    from egg_agent_tools.handlers._gateway import get_container_id

    params: dict[str, str] = {}
    if repo_path:
        params["repo_path"] = repo_path
    cid = get_container_id()
    if cid:
        params["container_id"] = cid

    last_error: GatewayError | None = None
    for attempt in range(1, _GAP_RETRY_ATTEMPTS + 1):
        # Re-read the contract on every attempt so a concurrent writer
        # that already landed a gap at our chosen index forces us to
        # recompute the next free slot + id.
        read_result = gateway_request(f"/api/v1/contract/{identifier}", params=params or None)
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
        gap_id = _next_gap_id(existing_gaps)

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
                "reason": (f"Recorded gap {gap_id} on {task_id} (from {from_role} to {to_role})"),
                **container_id_field(),
            },
        )
        if result.get("success"):
            return {"ok": True, "task": task_id, "gap_id": gap_id, "gap": gap_record}

        message = result.get("message", "gap mutate failed")
        last_error = GatewayError(message)
        # Any failure that smells like a TOCTOU collision ("index out
        # of range" from _set_value's append guard, or a "path already
        # exists" style error if the gateway ever switches to strict
        # set-only writes) triggers a retry.  Other errors bail
        # immediately — retrying would mask them.
        retryable = (
            "index" in message.lower()
            or "out of range" in message.lower()
            or "already exists" in message.lower()
            or "conflict" in message.lower()
        )
        if not retryable or attempt == _GAP_RETRY_ATTEMPTS:
            break

    if last_error is None:
        raise HandlerError("mark_gap failed: no attempts were made (internal error)")
    raise last_error
