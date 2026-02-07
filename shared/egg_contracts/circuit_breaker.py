"""
Circuit breaker logic for SDLC pipeline escalation.

Provides circuit breaker functionality to prevent infinite loops in the
implement->review cycle. When thresholds are exceeded, the pipeline pauses
and escalates to human intervention.

Thresholds:
- Per-task cycles: 3 (implement→review→kick-back counts as one cycle)
- Total pipeline cycles: 10
- Single task timeout: triggers human review

State transitions:
- CLOSED → OPEN: Per-task or total threshold exceeded
- OPEN: Human review required for stuck task
- OPEN → CLOSED: Human provides guidance, cycle resumes
"""

from typing import Any

from .models import (
    AuditRole,
    CircuitBreakerStatus,
    Contract,
    Phase,
    Task,
    TaskStatus,
)


class CircuitBreakerConfig:
    """Configuration for circuit breaker thresholds."""

    # Per-task threshold: how many implement→review cycles before escalation
    DEFAULT_TASK_MAX_CYCLES: int = 3

    # Total pipeline threshold: max cycles across all tasks
    DEFAULT_PIPELINE_MAX_CYCLES: int = 10

    # Default timeout for single task (in minutes) before checkpoint
    DEFAULT_TASK_TIMEOUT_MINUTES: int = 60


class CircuitBreakerResult:
    """Result of a circuit breaker check."""

    def __init__(
        self,
        should_trip: bool,
        reason: str | None = None,
        affected_tasks: list[str] | None = None,
        affected_phases: list[str] | None = None,
        recommendation: str | None = None,
    ):
        self.should_trip = should_trip
        self.reason = reason
        self.affected_tasks = affected_tasks or []
        self.affected_phases = affected_phases or []
        self.recommendation = recommendation

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "should_trip": self.should_trip,
            "reason": self.reason,
            "affected_tasks": self.affected_tasks,
            "affected_phases": self.affected_phases,
            "recommendation": self.recommendation,
        }

    def __repr__(self) -> str:
        return f"CircuitBreakerResult(should_trip={self.should_trip}, reason={self.reason!r})"


def check_task_threshold(task: Task) -> CircuitBreakerResult:
    """
    Check if a single task has exceeded its cycle threshold.

    Args:
        task: The task to check

    Returns:
        CircuitBreakerResult indicating if threshold is exceeded
    """
    if task.review_cycles >= task.max_cycles:
        return CircuitBreakerResult(
            should_trip=True,
            reason=f"Task {task.id} exceeded max cycles ({task.review_cycles}/{task.max_cycles})",
            affected_tasks=[task.id],
            recommendation=(
                f"Review task {task.id} acceptance criteria. Consider: "
                "1) Clarifying requirements, 2) Breaking into smaller tasks, "
                "3) Providing additional context or examples."
            ),
        )
    return CircuitBreakerResult(should_trip=False)


def check_phase_threshold(phase: Phase) -> CircuitBreakerResult:
    """
    Check if a phase has exceeded its cycle threshold.

    Args:
        phase: The phase to check

    Returns:
        CircuitBreakerResult indicating if threshold is exceeded
    """
    if phase.review_cycles >= phase.max_cycles:
        return CircuitBreakerResult(
            should_trip=True,
            reason=f"Phase {phase.id} exceeded max cycles ({phase.review_cycles}/{phase.max_cycles})",
            affected_phases=[phase.id],
            affected_tasks=[t.id for t in phase.tasks if t.status != TaskStatus.COMPLETE],
            recommendation=(
                f"Review phase {phase.id} scope. Consider: "
                "1) Re-evaluating task breakdown, 2) Adjusting acceptance criteria, "
                "3) Providing human guidance on blocked tasks."
            ),
        )
    return CircuitBreakerResult(should_trip=False)


def check_pipeline_threshold(contract: Contract) -> CircuitBreakerResult:
    """
    Check if the total pipeline cycles have exceeded threshold.

    Args:
        contract: The contract to check

    Returns:
        CircuitBreakerResult indicating if threshold is exceeded
    """
    cb = contract.circuit_breaker
    if cb.total_cycles >= cb.max_total_cycles:
        # Collect all incomplete tasks
        incomplete_tasks = []
        for phase in contract.phases:
            for task in phase.tasks:
                if task.status not in (TaskStatus.COMPLETE,):
                    incomplete_tasks.append(task.id)

        return CircuitBreakerResult(
            should_trip=True,
            reason=f"Pipeline exceeded max total cycles ({cb.total_cycles}/{cb.max_total_cycles})",
            affected_tasks=incomplete_tasks,
            recommendation=(
                "The pipeline has exceeded its maximum cycle limit. Human review required. "
                "Consider: 1) Reviewing all incomplete tasks, 2) Providing guidance on stuck areas, "
                "3) Adjusting the implementation plan."
            ),
        )
    return CircuitBreakerResult(should_trip=False)


def check_all_thresholds(contract: Contract) -> CircuitBreakerResult:
    """
    Check all circuit breaker thresholds.

    Checks in order of specificity: task → phase → pipeline.

    Args:
        contract: The contract to check

    Returns:
        CircuitBreakerResult with combined information if any threshold exceeded
    """
    # Check if circuit breaker is already open
    if contract.circuit_breaker.status == CircuitBreakerStatus.OPEN:
        return CircuitBreakerResult(
            should_trip=True,
            reason="Circuit breaker is already OPEN - awaiting human intervention",
            recommendation="Provide guidance via HITL checkboxes or issue comments to resume.",
        )

    # Check individual tasks
    for phase in contract.phases:
        for task in phase.tasks:
            result = check_task_threshold(task)
            if result.should_trip:
                return result

    # Check phases
    for phase in contract.phases:
        result = check_phase_threshold(phase)
        if result.should_trip:
            return result

    # Check total pipeline
    return check_pipeline_threshold(contract)


def should_escalate(contract: Contract) -> tuple[bool, str | None]:
    """
    Simplified check for whether escalation is needed.

    Args:
        contract: The contract to check

    Returns:
        Tuple of (should_escalate, reason)
    """
    result = check_all_thresholds(contract)
    return result.should_trip, result.reason


def open_circuit_breaker(
    contract: Contract,
    reason: str,
    actor: str = "system",
) -> Contract:
    """
    Open the circuit breaker, blocking further automatic cycles.

    Args:
        contract: The contract to modify
        reason: Reason for opening the circuit breaker
        actor: Who triggered the opening

    Returns:
        Modified contract with OPEN circuit breaker
    """
    from .audit import create_audit_entry
    from .models import AuditAction

    contract.circuit_breaker.status = CircuitBreakerStatus.OPEN

    # Add audit entry
    entry = create_audit_entry(
        actor=actor,
        role=AuditRole.SYSTEM,
        action=AuditAction.TRANSITION,
        field_path="circuit_breaker.status",
        old_value="closed",
        new_value="open",
        reason=reason,
    )
    contract.audit_log.append(entry)

    return contract


def close_circuit_breaker(
    contract: Contract,
    actor: str,
    role: AuditRole = AuditRole.HUMAN,
    reason: str = "Human provided guidance",
) -> Contract:
    """
    Close the circuit breaker, allowing cycles to resume.

    Args:
        contract: The contract to modify
        actor: Who is closing the circuit breaker
        role: Role of the actor (typically HUMAN)
        reason: Reason for closing

    Returns:
        Modified contract with CLOSED circuit breaker
    """
    from .audit import create_audit_entry
    from .models import AuditAction

    contract.circuit_breaker.status = CircuitBreakerStatus.CLOSED

    # Add audit entry
    entry = create_audit_entry(
        actor=actor,
        role=role,
        action=AuditAction.TRANSITION,
        field_path="circuit_breaker.status",
        old_value="open",
        new_value="closed",
        reason=reason,
    )
    contract.audit_log.append(entry)

    return contract


def increment_task_cycle(
    contract: Contract,
    phase_id: str,
    task_id: str,
    actor: str = "system",
) -> tuple[Contract, CircuitBreakerResult]:
    """
    Increment the review cycle count for a task.

    Args:
        contract: The contract to modify
        phase_id: ID of the phase containing the task
        task_id: ID of the task to increment
        actor: Who triggered the increment

    Returns:
        Tuple of (modified contract, circuit breaker check result)
    """
    from .audit import create_update_entry

    task = contract.get_task(phase_id, task_id)
    if task is None:
        raise ValueError(f"Task {task_id} not found in phase {phase_id}")

    old_cycles = task.review_cycles
    task.review_cycles += 1

    # Add audit entry
    entry = create_update_entry(
        actor=actor,
        role=AuditRole.SYSTEM,
        field_path=f"phases.{phase_id}.tasks.{task_id}.review_cycles",
        old_value=old_cycles,
        new_value=task.review_cycles,
    )
    contract.audit_log.append(entry)

    # Check if this triggers the circuit breaker
    result = check_task_threshold(task)
    if result.should_trip:
        task.escalated = True
        contract = open_circuit_breaker(contract, result.reason or "Task threshold exceeded", actor)

    return contract, result


def increment_phase_cycle(
    contract: Contract,
    phase_id: str,
    actor: str = "system",
) -> tuple[Contract, CircuitBreakerResult]:
    """
    Increment the review cycle count for a phase.

    Args:
        contract: The contract to modify
        phase_id: ID of the phase to increment
        actor: Who triggered the increment

    Returns:
        Tuple of (modified contract, circuit breaker check result)
    """
    from .audit import create_update_entry

    phase = contract.get_phase(phase_id)
    if phase is None:
        raise ValueError(f"Phase {phase_id} not found")

    old_cycles = phase.review_cycles
    phase.review_cycles += 1

    # Add audit entry
    entry = create_update_entry(
        actor=actor,
        role=AuditRole.SYSTEM,
        field_path=f"phases.{phase_id}.review_cycles",
        old_value=old_cycles,
        new_value=phase.review_cycles,
    )
    contract.audit_log.append(entry)

    # Check if this triggers the circuit breaker
    result = check_phase_threshold(phase)
    if result.should_trip:
        phase.escalated = True
        phase.escalation_reason = result.reason
        contract = open_circuit_breaker(contract, result.reason or "Phase threshold exceeded", actor)

    return contract, result


def increment_pipeline_cycle(
    contract: Contract,
    actor: str = "system",
) -> tuple[Contract, CircuitBreakerResult]:
    """
    Increment the total pipeline cycle count.

    Args:
        contract: The contract to modify
        actor: Who triggered the increment

    Returns:
        Tuple of (modified contract, circuit breaker check result)
    """
    from .audit import create_update_entry

    old_cycles = contract.circuit_breaker.total_cycles
    contract.circuit_breaker.total_cycles += 1

    # Add audit entry
    entry = create_update_entry(
        actor=actor,
        role=AuditRole.SYSTEM,
        field_path="circuit_breaker.total_cycles",
        old_value=old_cycles,
        new_value=contract.circuit_breaker.total_cycles,
    )
    contract.audit_log.append(entry)

    # Check if this triggers the circuit breaker
    result = check_pipeline_threshold(contract)
    if result.should_trip:
        contract = open_circuit_breaker(contract, result.reason or "Pipeline threshold exceeded", actor)

    return contract, result


def get_escalation_summary(contract: Contract) -> dict[str, Any]:
    """
    Generate a summary of escalation state for human review.

    Args:
        contract: The contract to summarize

    Returns:
        Dictionary with escalation information
    """
    escalated_tasks = []
    incomplete_tasks = []
    stuck_phases = []

    for phase in contract.phases:
        if phase.escalated:
            stuck_phases.append({
                "id": phase.id,
                "name": phase.name,
                "cycles": phase.review_cycles,
                "reason": phase.escalation_reason,
            })

        for task in phase.tasks:
            if task.escalated:
                escalated_tasks.append({
                    "id": task.id,
                    "phase": phase.id,
                    "description": task.description,
                    "cycles": task.review_cycles,
                    "status": task.status,
                })
            elif task.status not in (TaskStatus.COMPLETE,):
                incomplete_tasks.append({
                    "id": task.id,
                    "phase": phase.id,
                    "description": task.description,
                    "cycles": task.review_cycles,
                    "status": task.status,
                })

    return {
        "circuit_breaker_status": contract.circuit_breaker.status,
        "total_cycles": contract.circuit_breaker.total_cycles,
        "max_cycles": contract.circuit_breaker.max_total_cycles,
        "escalated_tasks": escalated_tasks,
        "incomplete_tasks": incomplete_tasks,
        "stuck_phases": stuck_phases,
        "requires_intervention": contract.circuit_breaker.status == CircuitBreakerStatus.OPEN,
    }
