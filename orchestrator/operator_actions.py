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
