"""Contract-task completeness checks for the BRC consensus gate (#3114).

Pipeline ``pipeline-2d9cc50d`` closed a slice whose contract recorded
0/10 tasks complete: nothing on the consensus path ever read the
contract's task rows, the contract reviewer's single review edge
(``reviewer_contract -> coder``) meant the documenter's openly declared
deferred work was visible to no blocking reviewer, and the attestation
channel that *would* have carried ``tasks_verified`` cannot be populated
from the sandbox BRC tools at all.

This module supplies the pure checks behind the structural fix: the
contract reviewer (the "enforcer", see
``egg_contracts.agent_roles.CONTRACT_ENFORCER_ROLES``) is the role that
holds a slice's consensus open until the contract is delivered. The
orchestrator's signal routes use these helpers to reject:

* an enforcer ACK of a producer whose owned task rows in the active
  slice are not ``complete`` (the enforcer NACKs instead, citing the
  rows — pressure lands on the producer that owns the gap);
* an enforcer CONFIRM while *any* task row in the active slice is
  incomplete (covers role-less rows and no-op-proposal bypasses);
* a ``no_changes_needed`` proposal from a producer that owns incomplete
  rows in the active slice (a no-op proposal is vacuously fully-acked,
  so without this it would bypass the per-producer ACK gate entirely).

All checks are scoped to the implement phase by the callers — plan and
refine consensus run against contracts whose task rows are *expected*
to be pending, and the apply phase (#1557) tracks per-task lifecycle in
``jira_action_status`` instead.

#3125 extends the module with the slice-close evidence-reachability
helpers (``evidence_commits`` / ``evidence_gate_enabled``): commit SHAs
cited by task records are only an integrity contract if the close path
verifies they actually reached the integration branch.

Failure posture: the gate degrades gracefully (returns ``None`` /
skips) when the contract cannot be loaded or the slice id does not
resolve — an orchestrator-side infrastructure failure must not deadlock
consensus (same posture as the propose-time validators, #3081). The
``EGG_CONTRACT_ACK_GATE`` env var is an operator kill switch.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from egg_contracts.loader import (
    ContractNotFoundError,
    ContractValidationError,
    load_contract,
)
from egg_contracts.models import Contract, TaskStatus
from egg_logging import get_logger

logger = get_logger("orchestrator.contract_completeness")

# Operator kill switch for the completeness gate. Default on; set to
# "off" (or 0/false/no) to disable enforcement without a redeploy.
GATE_ENV_VAR = "EGG_CONTRACT_ACK_GATE"

# Operator kill switch for the slice-close evidence-reachability gate
# (#3125). Separate from the ACK/CONFIRM gate so each can be toggled
# independently during an incident.
EVIDENCE_GATE_ENV_VAR = "EGG_EVIDENCE_REACHABILITY_GATE"

_DISABLED_VALUES = frozenset({"off", "0", "false", "no"})


def gate_enabled() -> bool:
    """Return True unless the operator kill switch disables the gate."""
    return os.environ.get(GATE_ENV_VAR, "on").strip().lower() not in _DISABLED_VALUES


def evidence_gate_enabled() -> bool:
    """Return True unless the evidence-reachability kill switch is set."""
    return os.environ.get(EVIDENCE_GATE_ENV_VAR, "on").strip().lower() not in _DISABLED_VALUES


def load_live_contract(
    worktree: Path,
    identifiers: Sequence[int | str],
) -> Contract | None:
    """Load the live (shared-worktree) contract, trying identifiers in order.

    Contracts are keyed by pipeline id in the k8s flow and by issue
    number in older flows; callers pass both candidates when available.
    Returns ``None`` when no candidate resolves or the file fails
    validation — the gate degrades gracefully rather than blocking
    consensus on an orchestrator-side read failure.
    """
    for identifier in identifiers:
        if identifier is None or identifier == "":
            continue
        try:
            return load_contract(identifier, worktree)
        except ContractNotFoundError:
            continue
        except ContractValidationError as exc:
            logger.warning(
                "Contract completeness gate: contract failed validation; gate skipped",
                identifier=str(identifier),
                error=str(exc),
            )
            return None
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "Contract completeness gate: contract load failed; gate skipped",
                identifier=str(identifier),
                error=str(exc),
            )
            return None
    return None


def _slices_in_scope(contract: Contract, slice_id: str | None) -> list[Any] | None:
    """Resolve the slice scope for a check.

    ``slice_id`` given → the matching slice only; ``None`` when no slice
    matches (caller logs and skips the gate — a tracker/contract drift
    must not deadlock consensus). ``slice_id=None`` → all slices (the
    non-sliced implement consensus covers the whole contract).
    """
    slices = list(contract.slices or [])
    if slice_id is None:
        return slices
    matched = [s for s in slices if getattr(s, "id", None) == slice_id]
    return matched or None


def incomplete_tasks(
    contract: Contract,
    slice_id: str | None = None,
    role: str | None = None,
) -> list[dict[str, Any]] | None:
    """List the non-``complete`` task rows in scope.

    Args:
        contract: The live contract.
        slice_id: Scope to one slice (``None`` → all slices).
        role: Scope to rows owned by this producer role. ``None`` →
            all rows including role-less ones (the CONFIRM-time check).

    Returns:
        One dict per incomplete row (``id`` / ``role`` / ``status`` /
        ``commit``), empty list when the scope is fully complete, or
        ``None`` when ``slice_id`` was given but no such slice exists
        (caller skips the gate).
    """
    slices = _slices_in_scope(contract, slice_id)
    if slices is None:
        return None

    rows: list[dict[str, Any]] = []
    for sl in slices:
        for task in sl.tasks or []:
            if role is not None and task.role != role:
                continue
            if task.status == TaskStatus.COMPLETE:
                continue
            rows.append(
                {
                    "id": task.id,
                    "role": task.role,
                    "status": str(task.status),
                    "commit": task.commit,
                }
            )
    return rows


def task_ids_for_role(
    contract: Contract,
    slice_id: str | None,
    role: str,
) -> set[str] | None:
    """Task ids owned by ``role`` in scope (``None`` — slice not found)."""
    slices = _slices_in_scope(contract, slice_id)
    if slices is None:
        return None
    return {task.id for sl in slices for task in sl.tasks or [] if task.role == role}


def all_task_ids(contract: Contract, slice_id: str | None) -> set[str] | None:
    """All task ids in scope (``None`` — slice not found)."""
    slices = _slices_in_scope(contract, slice_id)
    if slices is None:
        return None
    return {task.id for sl in slices for task in sl.tasks or []}


def format_incomplete_rows(rows: list[dict[str, Any]]) -> str:
    """One-line-per-row summary for rejection messages."""
    return "; ".join(
        f"{r['id']} (role={r['role'] or 'unassigned'}, status={r['status']})" for r in rows
    )


def evidence_commits(
    contract: Contract,
    slice_id: str | None = None,
) -> list[dict[str, Any]] | None:
    """List the task rows in scope that cite a commit SHA as evidence.

    The slice close-merge gate (#3125) checks every cited SHA for
    reachability from the integration branch tip before the slice may
    close — a task record pointing at a commit the slice PR does not
    contain means the prescribed ``complete-task --commit`` unblock
    flow silently dropped a deliverable.

    Rows are included regardless of ``status``: a row can carry a
    commit while still pending (the completion CLI links the commit
    before flipping status), and a cited-but-unreachable commit is a
    gap worth failing on either way.

    **Role-less rows are excluded (#3339).** The gate exists to protect
    the producer-scoped #3124 flow: a *confirmed producer* records a
    post-confirmation commit that lives only on its local worktree
    branch, so the deliverable would be lost on close. That producer
    always owns a role. A task with ``role=None`` (planner left it
    unassigned) has no producer obligated to push its commit through
    ``consensus_push``, so a SHA it cites is bookkeeping — not a gated
    deliverable. Yet a stray ``add-commit`` / ``complete-task --commit``
    on such a row (and ``_merge_preserved_slice_runtime`` re-attaching
    it across a ``restart_phase`` re-fork) made the gate fire on an
    orphan commit ``role=unassigned`` for a slice that had *already
    reached full BRC consensus* — every reviewer approved the diff that
    is on the integration branch — and that single bookkeeping mismatch
    failed the slice and cascaded the whole phase. Scoping the gate to
    role-bound rows keeps its full protection for real producer commits
    while no longer nuking a consensus-reached slice on an unassigned
    orphan.

    Returns one dict per role-bound row (``id`` / ``role`` /
    ``commit``), empty list when no such row cites a commit, or ``None``
    when ``slice_id`` was given but no such slice exists (caller skips
    the gate).

    Row ordering is intentional: slice-declaration order outermost,
    task-declaration order within each slice. Callers (the close-merge
    gate, the failure-string formatter) rely on this for deterministic
    operator-facing output.
    """
    slices = _slices_in_scope(contract, slice_id)
    if slices is None:
        return None
    return [
        {"id": task.id, "role": task.role, "commit": task.commit}
        for sl in slices
        for task in sl.tasks or []
        if task.commit and task.role
    ]


def format_evidence_rows(rows: list[dict[str, Any]]) -> str:
    """One-line-per-row summary for evidence-reachability messages."""
    return "; ".join(
        f"{r['id']} (role={r['role'] or 'unassigned'}, commit={r['commit']})" for r in rows
    )
