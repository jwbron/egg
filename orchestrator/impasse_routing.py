"""Orchestrator-side impasse detection and routing (#2529).

Reads :class:`AgentOutput.impasse` from each producer's per-pipeline
agent-output file after a slice's BRC cycle exits, and decides whether
to:

- **Delegate** — flip the contract task's ``role`` to the agent's
  ``suggested_role`` and bump ``delegation_attempts``. The slice's
  next BRC cycle picks up the new role from the contract.
- **Escalate** — create a HITL decision describing the impasse so the
  human can decide between cancelling, re-planning, or manually
  resolving the underlying blocker.

Auto-delegation only fires for ``WRONG_ROLE`` impasses with a single
eligible alternative producer role and a fresh task
(``delegation_attempts == 0``). Everything else escalates: a second
impasse on the same task, a non-WRONG_ROLE category, an unknown
``suggested_role``, and self-delegation attempts are all routed to
HITL.

The helper is import-only — wiring into the slice loop is done by
``orchestrator/routes/pipelines.py``. Keeping the routing logic here
makes it unit-testable in isolation from the 16k-line pipelines
module.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from egg_contracts.agent_roles import AgentRole as ContractAgentRole
from egg_contracts.decisions import next_cq_id
from egg_contracts.impasse import Impasse, ImpasseCategory
from egg_contracts.loader import load_contract, save_contract
from egg_contracts.models import (
    Contract,
    Decision,
    DecisionOption,
    DecisionType,
    Slice,
    Task,
)
from egg_contracts.orchestrator import load_agent_output
from egg_contracts.roles import Role
from egg_contracts.validator import apply_mutation

try:
    from egg_logging import get_logger
except ImportError:  # pragma: no cover - host-side fallback
    import logging

    def get_logger(name: str, **kwargs):  # type: ignore[misc]
        return logging.getLogger(name)


if TYPE_CHECKING:
    pass


logger = get_logger("orchestrator.impasse_routing")


# Producer roles eligible for delegation. Cross-phase roles
# (overseer/autofixer/conflict_resolver/inspector) are not valid
# delegation targets — auto-delegation rewires a producer task within
# the implement phase, not across phases.
_DELEGATION_ELIGIBLE_ROLES = {"coder", "tester", "documenter"}

# Bumped by orchestrator each delegation; second hit (>= 1) escalates.
DELEGATION_LIMIT = 1


class ImpasseAction(StrEnum):
    """What the orchestrator decided to do with an impasse."""

    DELEGATE = "delegate"
    """Mutated ``task.role`` to ``suggested_role`` and bumped
    ``delegation_attempts``. The slice loop should re-run the BRC
    cycle so the new role spawns and proposes."""

    ESCALATE = "escalate"
    """Created (or skipped — see ``hitl_decision_id``) a HITL decision.
    The slice should not auto-retry; the human gates the next move."""


@dataclass
class RoutingDecision:
    """Outcome of routing a single impasse."""

    action: ImpasseAction
    impasse: Impasse
    role: str
    """The role that *reported* the impasse (the impassed producer)."""
    task_id: str | None
    new_role: str | None
    """For ``DELEGATE``: the role we flipped to. ``None`` for
    ``ESCALATE``."""
    reason: str
    """Operator-readable summary of why this action was chosen."""
    hitl_decision_id: str | None = None
    """For ``ESCALATE``: the ID of the decision created on the
    contract. ``None`` if creation was skipped or failed."""


def collect_impasses(
    repo_path: Path,
    pipeline_id: str | int,
    roles: list[ContractAgentRole],
) -> list[tuple[ContractAgentRole, Impasse]]:
    """Read each role's agent-output file and collect any impasses.

    Returns the list in spawn order (= ``roles`` argument order) so the
    caller can route them deterministically rather than relying on
    filesystem mtime.
    """
    impasses: list[tuple[ContractAgentRole, Impasse]] = []
    for role in roles:
        try:
            raw = load_agent_output(repo_path, role, identifier=pipeline_id)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "Failed to read agent output during impasse scan",
                role=role.value,
                pipeline_id=str(pipeline_id),
                error=str(exc),
            )
            continue
        impasse_raw = raw.get("impasse") if isinstance(raw, dict) else None
        if not isinstance(impasse_raw, dict):
            continue
        try:
            impasses.append((role, Impasse.from_dict(impasse_raw)))
        except Exception as exc:
            logger.warning(
                "Discarding malformed impasse payload",
                role=role.value,
                pipeline_id=str(pipeline_id),
                error=str(exc),
            )
            continue
    return impasses


def _find_task(
    contract: Contract,
    slice_id: str | None,
    impasse: Impasse,
    role: str,
) -> tuple[Slice, Task] | None:
    """Resolve which slice + task the impasse applies to.

    Resolution order:

    1. ``impasse.task_id`` — exact match, scoped to ``slice_id`` when
       provided.
    2. The single task in the active slice whose ``role`` matches the
       impassed role. When the slice has multiple matches (or none),
       returns ``None`` and the caller escalates.

    Pipeline-level (non-sliced) phases pass ``slice_id=None`` and
    search across all slices.
    """
    candidate_slices: list[Slice]
    if slice_id is None:
        candidate_slices = list(contract.slices)
    else:
        candidate_slices = [s for s in contract.slices if s.id == slice_id]

    if impasse.task_id:
        for slice_obj in candidate_slices:
            for task in slice_obj.tasks:
                if task.id == impasse.task_id:
                    return slice_obj, task
        return None

    for slice_obj in candidate_slices:
        matches = [t for t in slice_obj.tasks if (t.role or "coder") == role]
        if len(matches) == 1:
            return slice_obj, matches[0]
    return None


def _is_eligible_delegation(
    impasse: Impasse,
    impassed_role: str,
    task: Task,
    *,
    force_escalate: bool = False,
) -> tuple[bool, str]:
    """Decide whether this impasse qualifies for auto-delegation.

    Returns ``(eligible, reason)``. ``reason`` is a short human-readable
    string used in the structured log + the HITL decision body.

    ``force_escalate`` is set by the slice-loop wrapper on its terminal
    iteration: a delegation that lands there can never re-run the BRC
    cycle, so the safer behaviour is to escalate to HITL instead of
    silently mutating the contract and exiting (review feedback #2 on
    PR #2553).
    """
    if force_escalate:
        return False, (
            "delegation skipped on terminal slice iteration; no further "
            "BRC cycle can execute the new role assignment"
        )
    if impasse.category != ImpasseCategory.WRONG_ROLE:
        return False, (
            f"category={impasse.category.value} is not auto-delegateable "
            "(only wrong_role triggers role-flip)"
        )
    if not impasse.suggested_role:
        return False, "no suggested_role on the impasse"
    if impasse.suggested_role == impassed_role:
        return False, "suggested_role equals the impassed role (self-delegation)"
    if impasse.suggested_role not in _DELEGATION_ELIGIBLE_ROLES:
        return False, (
            f"suggested_role={impasse.suggested_role!r} is not in the "
            "producer trio (coder/tester/documenter)"
        )
    if task.delegation_attempts >= DELEGATION_LIMIT:
        return False, (
            f"task.delegation_attempts={task.delegation_attempts} already "
            f"at limit {DELEGATION_LIMIT}; second impasse on same task"
        )
    return True, "wrong_role with single eligible alternative role"


def _build_hitl_decision(
    contract: Contract,
    slice_obj: Slice | None,
    task: Task | None,
    impasse: Impasse,
    impassed_role: str,
    reason_for_escalation: str,
) -> tuple[str, Decision]:
    """Construct the decision payload for an impasse HITL escalation.

    Returns ``(field_path, decision)`` ready to feed into
    :func:`apply_mutation`.
    """
    existing_decisions = contract.decisions or []
    next_idx = len(existing_decisions)
    # Orchestrator-side HITL escalations write to the same ``cq-N``
    # namespace as agent-registered ``register_open_question`` calls so
    # neither path collides with the pipeline-side ``decision-N``
    # allocator. See ``shared/egg_contracts/decisions.py`` (#2616).
    decision_id = next_cq_id(existing_decisions)

    task_id = task.id if task else (impasse.task_id or "<unresolved>")
    slice_id = slice_obj.id if slice_obj else "<unresolved>"

    question_lines = [
        f"Producer ``{impassed_role}`` reported an impasse on "
        f"``{task_id}`` ({slice_id}, category=``{impasse.category.value}``).",
        "",
        f"**Agent reason**: {impasse.reason}",
    ]
    if impasse.blocked_files:
        joined = ", ".join(f"``{p}``" for p in impasse.blocked_files)
        question_lines.append(f"**Blocked files**: {joined}")
    if impasse.suggested_role:
        question_lines.append(f"**Agent's suggested role**: ``{impasse.suggested_role}``")
    question_lines.append(f"**Why auto-delegation didn't fire**: {reason_for_escalation}")

    options: list[DecisionOption] = []
    if impasse.suggested_role and impasse.suggested_role in _DELEGATION_ELIGIBLE_ROLES:
        options.append(
            DecisionOption(
                id="opt-1",
                label=(
                    f"Delegate to ``{impasse.suggested_role}`` (acknowledges "
                    "second impasse / overrides safety gate)"
                ),
            )
        )
    options.append(
        DecisionOption(
            id=f"opt-{len(options) + 1}",
            label="Cancel the slice and re-plan",
        )
    )
    options.append(
        DecisionOption(
            id=f"opt-{len(options) + 1}",
            label="Resolve the underlying blocker manually, then resume",
        )
    )
    options.append(
        DecisionOption(
            id=f"opt-{len(options) + 1}",
            label="Other (explain in reply)",
        )
    )

    decision = Decision(
        id=decision_id,
        question="\n".join(question_lines),
        type=DecisionType.HITL,
        phase=contract.current_phase,
        options=options,
    )

    field_path = f"decisions.{next_idx}"
    return field_path, decision


def route_impasses(
    repo_path: Path,
    pipeline_id: str | int,
    contract_identifier: str | int,
    impasses: list[tuple[ContractAgentRole, Impasse]],
    slice_id: str | None,
    actor: str = "orchestrator-impasse-router",
    *,
    force_escalate: bool = False,
) -> list[RoutingDecision]:
    """Apply the routing policy to every impasse, mutating the contract.

    Loads the contract, walks each ``(role, impasse)`` pair, and for
    each one either:

    - flips ``task.role`` + bumps ``task.delegation_attempts`` and
      returns a ``DELEGATE`` decision, or
    - appends a HITL decision and returns an ``ESCALATE`` decision.

    All mutations go through :func:`apply_mutation` (so the audit log
    captures them) under the ``SYSTEM`` role — only SYSTEM owns
    ``phases.*.tasks.*.role`` and ``phases.*.tasks.*.delegation_attempts``
    per ``shared/egg_contracts/roles.py``.

    Saves the contract once at the end if any mutation was applied.

    ``force_escalate`` (callers: terminal slice-loop iteration) forces
    every impasse to take the escalate path even when it would
    otherwise qualify for auto-delegation. The slice loop sets this on
    its last iteration because a delegation made there can never
    re-run a BRC cycle, so the role flip would silently dangle.
    """
    if not impasses:
        return []

    contract = load_contract(contract_identifier, repo_path)
    decisions: list[RoutingDecision] = []
    mutated = False

    for role, impasse in impasses:
        impassed_role = role.value
        located = _find_task(contract, slice_id, impasse, impassed_role)
        if located is None:
            decision = _record_escalate(
                contract,
                None,
                None,
                impasse,
                impassed_role,
                actor,
                "could not resolve task_id from the contract",
            )
            decisions.append(decision)
            mutated = True
            continue

        slice_obj, task = located
        eligible, why = _is_eligible_delegation(
            impasse, impassed_role, task, force_escalate=force_escalate
        )

        if eligible:
            decision = _record_delegate(
                contract,
                slice_obj,
                task,
                impasse,
                impassed_role,
                actor,
                why,
            )
        else:
            decision = _record_escalate(
                contract,
                slice_obj,
                task,
                impasse,
                impassed_role,
                actor,
                why,
            )
        decisions.append(decision)
        mutated = True

    if mutated:
        try:
            save_contract(contract, repo_path)
        except Exception as exc:  # pragma: no cover - defensive
            logger.error(
                "Failed to persist contract after impasse routing",
                pipeline_id=str(pipeline_id),
                error=str(exc),
            )

    return decisions


def _record_delegate(
    contract: Contract,
    slice_obj: Slice,
    task: Task,
    impasse: Impasse,
    impassed_role: str,
    actor: str,
    reason: str,
) -> RoutingDecision:
    slice_idx = next(
        (i for i, s in enumerate(contract.slices) if s.id == slice_obj.id),
        None,
    )
    task_idx = next((i for i, t in enumerate(slice_obj.tasks) if t.id == task.id), None)
    if slice_idx is None or task_idx is None:  # pragma: no cover - defensive
        return _record_escalate(
            contract,
            slice_obj,
            task,
            impasse,
            impassed_role,
            actor,
            "could not locate task indices for delegation mutation",
        )

    role_path = f"phases.{slice_idx}.tasks.{task_idx}.role"
    # The impasse schema caps ``reason`` at 2000 chars; the audit log
    # can hold the full payload, and post-mortem debugging benefits
    # from the unredacted agent reasoning. Don't truncate.
    role_result = apply_mutation(
        contract,
        role=Role.SYSTEM,
        actor=actor,
        field_path=role_path,
        new_value=impasse.suggested_role,
        reason=(
            f"Impasse-driven delegation: {impassed_role} → "
            f"{impasse.suggested_role}. Agent reason: {impasse.reason}"
        ),
    )
    if not role_result.success:
        return _record_escalate(
            contract,
            slice_obj,
            task,
            impasse,
            impassed_role,
            actor,
            f"role mutation failed: {role_result.message}",
        )

    counter_path = f"phases.{slice_idx}.tasks.{task_idx}.delegation_attempts"
    counter_result = apply_mutation(
        contract,
        role=Role.SYSTEM,
        actor=actor,
        field_path=counter_path,
        new_value=task.delegation_attempts + 1,
        reason="Impasse-driven delegation counter bump",
    )
    if not counter_result.success:  # pragma: no cover - schema-guarded
        logger.warning(
            "Failed to bump delegation_attempts after role flip",
            slice_id=slice_obj.id,
            task_id=task.id,
            error=counter_result.message,
        )

    logger.info(
        "Impasse delegated",
        slice_id=slice_obj.id,
        task_id=task.id,
        from_role=impassed_role,
        to_role=impasse.suggested_role,
        reason=reason,
        agent_reason=impasse.reason[:200],
    )

    return RoutingDecision(
        action=ImpasseAction.DELEGATE,
        impasse=impasse,
        role=impassed_role,
        task_id=task.id,
        new_role=impasse.suggested_role,
        reason=reason,
    )


def _record_escalate(
    contract: Contract,
    slice_obj: Slice | None,
    task: Task | None,
    impasse: Impasse,
    impassed_role: str,
    actor: str,
    reason: str,
) -> RoutingDecision:
    field_path, decision = _build_hitl_decision(
        contract, slice_obj, task, impasse, impassed_role, reason
    )
    # decisions.* is owned by IMPLEMENTER per FIELD_OWNERSHIP — mirror
    # the existing ``register_open_question`` MCP tool's role choice.
    # The audit log records the orchestrator-side actor so it stays
    # distinguishable from agent-emitted decisions.
    result = apply_mutation(
        contract,
        role=Role.IMPLEMENTER,
        actor=actor,
        field_path=field_path,
        new_value=decision,
        reason=f"Impasse-driven HITL escalation ({impasse.category.value})",
    )
    decision_id = decision.id if result.success else None
    if not result.success:
        logger.error(
            "Failed to create HITL decision for impasse",
            slice_id=slice_obj.id if slice_obj else None,
            task_id=task.id if task else impasse.task_id,
            error=result.message,
        )
    else:
        logger.info(
            "Impasse escalated to HITL",
            slice_id=slice_obj.id if slice_obj else None,
            task_id=task.id if task else impasse.task_id,
            impassed_role=impassed_role,
            category=impasse.category.value,
            decision_id=decision_id,
            reason=reason,
        )

    return RoutingDecision(
        action=ImpasseAction.ESCALATE,
        impasse=impasse,
        role=impassed_role,
        task_id=task.id if task else impasse.task_id,
        new_role=None,
        reason=reason,
        hitl_decision_id=decision_id,
    )


__all__ = [
    "DELEGATION_LIMIT",
    "ImpasseAction",
    "RoutingDecision",
    "collect_impasses",
    "route_impasses",
]
