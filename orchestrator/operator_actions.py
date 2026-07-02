"""Operator-grade contract task mutations (#3124).

Before this module, an operator facing a contract task that no live
agent was permitted to satisfy (e.g. a task reassigned to a producer
that had already CONFIRMED — see #3124) had no in-band remediation:
task ``status`` is owned by implementer/reviewer
(``egg_contracts.roles.FIELD_OWNERSHIP``), the overseer's mutations get
403, and the documented workaround was to ``kubectl exec`` into an
agent pod and impersonate its role. This module is the sanctioned
path: it applies the mutation as ``Role.HUMAN`` (the operator), audited
with the operator actor string, and is exposed through
lifecycle-secret-guarded surfaces only (the orchestrator REST route in
``routes/contracts.py`` and the HITL decision-resolution dispatch in
``routes/decisions.py``) — sandbox agents cannot reach it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_parent_path = Path(__file__).parent
if str(_parent_path) not in sys.path:
    sys.path.insert(0, str(_parent_path))

_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

import contract_store  # noqa: E402
from egg_contracts import Role, apply_mutation, load_contract, save_contract  # noqa: E402
from egg_contracts.loader import ContractNotFoundError  # noqa: E402

try:
    from egg_logging import get_logger
except ImportError:  # pragma: no cover — logging fallback
    import logging

    def get_logger(name: str, **kwargs: Any):  # type: ignore[misc]
        return logging.getLogger(name)


logger = get_logger("orchestrator.operator_actions")


class OperatorActionError(Exception):
    """Operator action failed; ``status_code`` maps to the HTTP response."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def complete_task_as_operator(
    pipeline_id: str,
    task_id: str,
    *,
    commit: str | None = None,
    reason: str = "",
    actor: str = "operator",
    issue_number: int | None = None,
) -> dict[str, Any]:
    """Mark a contract task ``complete`` as an audited operator action.

    Mirrors the agent-side ``mcp__task__complete`` handler (link the
    commit first when provided, then flip ``status``) but runs as
    ``Role.HUMAN`` so it is not subject to the implementer/reviewer
    field-ownership restriction. Both mutations go through
    ``apply_mutation`` so the contract audit log records the actor and
    reason.

    Callers MUST sit behind an operator-authenticated surface
    (lifecycle secret); this function does no authentication itself.

    Raises :class:`OperatorActionError` with an HTTP-ish status code on
    any failure (worktree/contract/task not found, mutation rejected,
    save failed).
    """
    worktree = contract_store.resolve_pipeline_worktree(pipeline_id)
    if worktree is None:
        raise OperatorActionError(
            f"No pipeline worktree found for {pipeline_id} (pipeline not "
            f"set up on this host, or already cleaned up)",
            status_code=404,
        )

    # Contracts are keyed by pipeline id in the k8s flow and by issue
    # number in older flows — same candidate order as the completeness
    # gate's ``load_live_contract``.
    identifiers: list[int | str] = [pipeline_id]
    if issue_number:
        identifiers.append(issue_number)

    last_error: str = "contract not found"
    for identifier in identifiers:
        with contract_store.lock_for(identifier):
            try:
                contract = load_contract(identifier, worktree)
            except ContractNotFoundError:
                continue
            except Exception as exc:
                last_error = str(exc)
                continue

            return _complete_task_locked(
                contract,
                worktree,
                identifier,
                task_id,
                commit=commit,
                reason=reason,
                actor=actor,
            )

    raise OperatorActionError(
        f"Contract for {pipeline_id} not loadable ({last_error})",
        status_code=404,
    )


def _complete_task_locked(
    contract: Any,
    worktree: Path,
    identifier: int | str,
    task_id: str,
    *,
    commit: str | None,
    reason: str,
    actor: str,
) -> dict[str, Any]:
    """Apply the completion mutations. Caller holds the contract lock."""
    located: tuple[int, int, Any, Any] | None = None
    for slice_idx, slice_obj in enumerate(contract.slices or []):
        for task_idx, task in enumerate(slice_obj.tasks or []):
            if task.id == task_id:
                located = (slice_idx, task_idx, slice_obj, task)
                break
        if located:
            break

    if located is None:
        raise OperatorActionError(
            f"Task {task_id!r} not found on contract {identifier}",
            status_code=404,
        )

    slice_idx, task_idx, slice_obj, task = located
    prior_status = str(task.status)

    audit_reason = "Operator task completion (#3124)"
    if reason:
        audit_reason = f"{audit_reason}: {reason}"

    # Commit evidence first, then status — same order as the agent-side
    # handler so a failure between the two leaves the safer state
    # (commit linked, task still pending) rather than a completed task
    # with no evidence.
    if commit:
        result = apply_mutation(
            contract,
            role=Role.HUMAN,
            actor=actor,
            field_path=f"phases.{slice_idx}.tasks.{task_idx}.commit",
            new_value=commit,
            reason=audit_reason,
        )
        if not result.success:
            raise OperatorActionError(
                f"Commit-link mutation rejected: {result.message}",
                status_code=400,
            )

    result = apply_mutation(
        contract,
        role=Role.HUMAN,
        actor=actor,
        field_path=f"phases.{slice_idx}.tasks.{task_idx}.status",
        new_value="complete",
        reason=audit_reason,
    )
    if not result.success:
        raise OperatorActionError(
            f"Status mutation rejected: {result.message}",
            status_code=400,
        )

    try:
        save_contract(contract, worktree)
    except Exception as exc:
        raise OperatorActionError(
            f"Failed to save contract: {exc}",
            status_code=500,
        ) from exc

    logger.info(
        "Operator marked contract task complete",
        identifier=str(identifier),
        task_id=task_id,
        slice_id=getattr(slice_obj, "id", None),
        prior_status=prior_status,
        commit=commit,
        actor=actor,
    )

    return {
        "task_id": task_id,
        "slice_id": getattr(slice_obj, "id", None),
        "prior_status": prior_status,
        "status": "complete",
        "commit": commit or getattr(task, "commit", None) or None,
        "actor": actor,
    }


def add_task_as_operator(
    pipeline_id: str,
    slice_id: str,
    description: str,
    *,
    acceptance_criteria: str = "",
    files_affected: list[str] | None = None,
    role: str | None = None,
    reason: str = "",
    actor: str = "operator",
    issue_number: int | None = None,
) -> dict[str, Any]:
    """Append a new task to a contract slice as an audited operator action (#3428).

    The executor for HITL decision options that mandate a contract
    mutation ("add a new task/slice to wire X as a dependency"): agents
    have no task-add verb, so without this the resolution is recorded
    and nothing materializes — the reviewer that raised the question
    keeps withholding ACK and the slice re-deadlocks. Runs as
    ``Role.HUMAN`` through ``apply_mutation`` so the audit log records
    the actor and reason, mirroring :func:`complete_task_as_operator`.

    The new task id is allocated as ``task-<P>-<N>`` where ``P`` is the
    slice number and ``N`` is one past the highest existing ``task-P-M``
    in that slice, all under the contract lock (no TOCTOU window).

    Callers MUST sit behind an operator-authenticated surface
    (lifecycle secret); this function does no authentication itself.

    Raises :class:`OperatorActionError` with an HTTP-ish status code on
    any failure (worktree/contract/slice not found, mutation rejected,
    save failed).
    """
    description = (description or "").strip()
    if not description:
        raise OperatorActionError("description must be non-empty", status_code=400)

    worktree = contract_store.resolve_pipeline_worktree(pipeline_id)
    if worktree is None:
        raise OperatorActionError(
            f"No pipeline worktree found for {pipeline_id} (pipeline not "
            f"set up on this host, or already cleaned up)",
            status_code=404,
        )

    identifiers: list[int | str] = [pipeline_id]
    if issue_number:
        identifiers.append(issue_number)

    last_error: str = "contract not found"
    for identifier in identifiers:
        with contract_store.lock_for(identifier):
            try:
                contract = load_contract(identifier, worktree)
            except ContractNotFoundError:
                continue
            except Exception as exc:
                last_error = str(exc)
                continue

            return _add_task_locked(
                contract,
                worktree,
                identifier,
                slice_id,
                description,
                acceptance_criteria=acceptance_criteria,
                files_affected=files_affected or [],
                role=role,
                reason=reason,
                actor=actor,
            )

    raise OperatorActionError(
        f"Contract for {pipeline_id} not loadable ({last_error})",
        status_code=404,
    )


def _add_task_locked(
    contract: Any,
    worktree: Path,
    identifier: int | str,
    slice_id: str,
    description: str,
    *,
    acceptance_criteria: str,
    files_affected: list[str],
    role: str | None,
    reason: str,
    actor: str,
) -> dict[str, Any]:
    """Apply the task-add mutation. Caller holds the contract lock."""
    import re

    from egg_contracts import Task

    slice_num_match = re.match(r"^(?:slice|phase)-([0-9]+)$", slice_id or "")
    if slice_num_match is None:
        raise OperatorActionError(
            f"slice_id must match 'slice-<N>' (got {slice_id!r})",
            status_code=400,
        )
    slice_num = slice_num_match.group(1)

    located: tuple[int, Any] | None = None
    for slice_idx, slice_obj in enumerate(contract.slices or []):
        obj_match = re.match(r"^(?:slice|phase)-([0-9]+)$", getattr(slice_obj, "id", "") or "")
        # Match on the slice number so canonical ``slice-N`` payloads find
        # legacy ``phase-N`` contracts and vice versa.
        if obj_match and obj_match.group(1) == slice_num:
            located = (slice_idx, slice_obj)
            break

    if located is None:
        raise OperatorActionError(
            f"Slice {slice_id!r} not found on contract {identifier}",
            status_code=404,
        )
    slice_idx, slice_obj = located

    tasks = list(slice_obj.tasks or [])
    max_n = 0
    for task in tasks:
        task_match = re.match(rf"^task-{slice_num}-([0-9]+)$", getattr(task, "id", "") or "")
        if task_match:
            max_n = max(max_n, int(task_match.group(1)))
    task_id = f"task-{slice_num}-{max_n + 1}"

    # Build a validated ``Task`` model (not a raw dict): ``_set_value``
    # appends the value as-is, and downstream consumers
    # (``Slice.tasks_all_complete``, the slice scheduler) attribute-access
    # the entries.
    try:
        new_task = Task(
            id=task_id,
            description=description,
            acceptance_criteria=acceptance_criteria,
            files_affected=files_affected,
            role=role,
        )
    except Exception as exc:
        raise OperatorActionError(
            f"Invalid task payload: {exc}",
            status_code=400,
        ) from exc

    audit_reason = "Operator task add (#3428)"
    if reason:
        audit_reason = f"{audit_reason}: {reason}"

    result = apply_mutation(
        contract,
        role=Role.HUMAN,
        actor=actor,
        field_path=f"phases.{slice_idx}.tasks.{len(tasks)}",
        new_value=new_task,
        reason=audit_reason,
    )
    if not result.success:
        raise OperatorActionError(
            f"Task-add mutation rejected: {result.message}",
            status_code=400,
        )

    try:
        save_contract(contract, worktree)
    except Exception as exc:
        raise OperatorActionError(
            f"Failed to save contract: {exc}",
            status_code=500,
        ) from exc

    logger.info(
        "Operator appended contract task",
        identifier=str(identifier),
        task_id=task_id,
        slice_id=getattr(slice_obj, "id", None),
        role=role,
        actor=actor,
    )

    return {
        "task_id": task_id,
        "slice_id": getattr(slice_obj, "id", None),
        "description": description,
        "role": role,
        "status": "pending",
        "actor": actor,
    }


def rewrite_task_description_as_operator(
    pipeline_id: str,
    new_task_description: str,
    *,
    reason: str = "",
    actor: str = "operator",
    issue_number: int | None = None,
) -> dict[str, Any]:
    """Rewrite the pipeline's seed (``contract.task_description``) as an operator action.

    The first-principles redirect accept-path calls this when an operator
    adopts a redirect: the seed is rewritten to the proposed direction so the
    re-run refine phase analyzes against it. Runs as ``Role.HUMAN`` — the seed
    is otherwise immutable by agents (it is the operator-owned identity anchor),
    which is exactly why a redirect must be a human's call. The mutation goes
    through ``apply_mutation`` so the audit log records the actor and reason.

    Returns ``{"worktree", "identifier", "prior", "new"}``. The caller is
    responsible for durably committing+pushing the worktree to the work branch
    so a subsequent refine restart's re-fork sees the rewrite. Raises
    :class:`OperatorActionError` on failure.
    """
    new_seed = (new_task_description or "").strip()
    if not new_seed:
        raise OperatorActionError("new_task_description must be non-empty", status_code=400)

    worktree = contract_store.resolve_pipeline_worktree(pipeline_id)
    if worktree is None:
        raise OperatorActionError(
            f"No pipeline worktree found for {pipeline_id} (pipeline not "
            f"set up on this host, or already cleaned up)",
            status_code=404,
        )

    identifiers: list[int | str] = [pipeline_id]
    if issue_number:
        identifiers.append(issue_number)

    audit_reason = "Operator first-principles redirect (seed rewrite)"
    if reason:
        audit_reason = f"{audit_reason}: {reason}"

    last_error = "contract not found"
    for identifier in identifiers:
        with contract_store.lock_for(identifier):
            try:
                contract = load_contract(identifier, worktree)
            except ContractNotFoundError:
                continue
            except Exception as exc:
                last_error = str(exc)
                continue

            prior = getattr(contract, "task_description", None)
            result = apply_mutation(
                contract,
                role=Role.HUMAN,
                actor=actor,
                field_path="task_description",
                new_value=new_seed,
                reason=audit_reason,
            )
            if not result.success:
                raise OperatorActionError(
                    f"task_description mutation rejected: {result.message}",
                    status_code=400,
                )

            try:
                save_contract(contract, worktree)
            except Exception as exc:
                raise OperatorActionError(
                    f"Failed to save contract: {exc}",
                    status_code=500,
                ) from exc

            logger.info(
                "Operator rewrote seed for first-principles redirect",
                identifier=str(identifier),
                pipeline_id=pipeline_id,
                actor=actor,
            )
            return {
                "worktree": str(worktree),
                "identifier": identifier,
                "prior": prior,
                "new": new_seed,
            }

    raise OperatorActionError(
        f"Contract for {pipeline_id} not loadable ({last_error})",
        status_code=404,
    )
