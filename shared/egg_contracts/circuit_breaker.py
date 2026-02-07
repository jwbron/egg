"""
Circuit breaker logic for SDLC pipeline escalation.

Tracks review cycles and triggers escalation when thresholds are exceeded:
- Per-task cycles: 3 (implement->review->kick-back counts as one cycle)
- Total pipeline cycles: 10
- Single task timeout: triggers human review

State transitions:
- CLOSED -> OPEN: Per-task or total threshold exceeded
- OPEN: Human review required
- OPEN -> CLOSED: Human provides guidance, cycle resumes
"""

from dataclasses import dataclass
from datetime import datetime, UTC
from typing import Any

from .models import (
    CircuitBreaker,
    CircuitBreakerStatus,
    Contract,
    Phase,
    Task,
    TaskStatus,
)


@dataclass
class CircuitBreakerResult:
    """Result of a circuit breaker check."""

    tripped: bool
    reason: str
    task_id: str | None = None
    phase_id: str | None = None
    cycle_count: int = 0
    max_cycles: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        result = {"tripped": self.tripped, "reason": self.reason}
        if self.task_id:
            result["task_id"] = self.task_id
        if self.phase_id:
            result["phase_id"] = self.phase_id
        result["cycle_count"] = self.cycle_count
        result["max_cycles"] = self.max_cycles
        return result


# Default thresholds
DEFAULT_TASK_MAX_CYCLES = 3
DEFAULT_TOTAL_MAX_CYCLES = 10


def check_circuit_breaker(contract: Contract) -> CircuitBreakerResult:
    """
    Check if circuit breaker should trip.

    Checks:
    1. Total pipeline cycles against max
    2. Per-task cycles against max
    3. Any task marked as escalated

    Args:
        contract: Contract to check

    Returns:
        CircuitBreakerResult indicating if breaker should trip
    """
    # Check total cycles
    if contract.circuit_breaker:
        total_cycles = contract.circuit_breaker.total_cycles
        max_total = contract.circuit_breaker.max_total_cycles
    else:
        total_cycles = 0
        max_total = DEFAULT_TOTAL_MAX_CYCLES

    if total_cycles >= max_total:
        return CircuitBreakerResult(
            tripped=True,
            reason=f"Total pipeline cycles ({total_cycles}) exceeded maximum ({max_total})",
            cycle_count=total_cycles,
            max_cycles=max_total,
        )

    # Check per-task cycles
    for phase in contract.phases:
        for task in phase.tasks:
            task_cycles = task.review_cycles
            task_max = task.max_cycles

            if task_cycles >= task_max:
                return CircuitBreakerResult(
                    tripped=True,
                    reason=f"Task {task.id} review cycles ({task_cycles}) exceeded maximum ({task_max})",
                    task_id=task.id,
                    phase_id=phase.id,
                    cycle_count=task_cycles,
                    max_cycles=task_max,
                )

            # Check if task is explicitly escalated
            if task.escalated:
                return CircuitBreakerResult(
                    tripped=True,
                    reason=f"Task {task.id} was explicitly escalated",
                    task_id=task.id,
                    phase_id=phase.id,
                    cycle_count=task_cycles,
                    max_cycles=task_max,
                )

    # No trip
    return CircuitBreakerResult(
        tripped=False,
        reason="All thresholds within limits",
        cycle_count=total_cycles,
        max_cycles=max_total,
    )


def trip_circuit_breaker(
    contract: Contract,
    reason: str,
    task_id: str | None = None,
) -> None:
    """
    Trip the circuit breaker to OPEN state.

    Args:
        contract: Contract to update
        reason: Reason for tripping
        task_id: Optional task that caused the trip
    """
    if contract.circuit_breaker is None:
        contract.circuit_breaker = CircuitBreaker()

    contract.circuit_breaker.status = CircuitBreakerStatus.OPEN
    contract.circuit_breaker.opened_at = datetime.now(UTC)
    contract.circuit_breaker.opened_reason = reason

    # Mark specific task as escalated if provided
    if task_id:
        for phase in contract.phases:
            for task in phase.tasks:
                if task.id == task_id:
                    task.escalated = True
                    break


def reset_circuit_breaker(contract: Contract) -> None:
    """
    Reset the circuit breaker to CLOSED state.

    Args:
        contract: Contract to update
    """
    if contract.circuit_breaker is None:
        contract.circuit_breaker = CircuitBreaker()

    contract.circuit_breaker.status = CircuitBreakerStatus.CLOSED
    contract.circuit_breaker.total_cycles = 0
    contract.circuit_breaker.opened_at = None
    contract.circuit_breaker.opened_reason = None

    # Reset task escalation flags
    for phase in contract.phases:
        for task in phase.tasks:
            task.escalated = False


def increment_total_cycles(contract: Contract) -> int:
    """
    Increment the total pipeline cycle count.

    Args:
        contract: Contract to update

    Returns:
        New cycle count
    """
    if contract.circuit_breaker is None:
        contract.circuit_breaker = CircuitBreaker()

    contract.circuit_breaker.total_cycles += 1
    return contract.circuit_breaker.total_cycles


def increment_task_cycles(contract: Contract, task_id: str) -> int | None:
    """
    Increment the review cycle count for a task.

    Args:
        contract: Contract to update
        task_id: Task ID to increment

    Returns:
        New cycle count or None if task not found
    """
    for phase in contract.phases:
        for task in phase.tasks:
            if task.id == task_id:
                task.review_cycles += 1
                return task.review_cycles
    return None


def get_stuck_tasks(contract: Contract) -> list[tuple[str, str, int]]:
    """
    Get tasks that are approaching or at their cycle limit.

    Args:
        contract: Contract to check

    Returns:
        List of (phase_id, task_id, cycles) for stuck tasks
    """
    stuck = []
    for phase in contract.phases:
        for task in phase.tasks:
            if task.status in (TaskStatus.INCOMPLETE, TaskStatus.IN_PROGRESS):
                if task.review_cycles >= task.max_cycles - 1:  # At or near limit
                    stuck.append((phase.id, task.id, task.review_cycles))
    return stuck


def is_circuit_open(contract: Contract) -> bool:
    """
    Check if the circuit breaker is in OPEN state.

    Args:
        contract: Contract to check

    Returns:
        True if circuit is open
    """
    if contract.circuit_breaker is None:
        return False
    return contract.circuit_breaker.status == CircuitBreakerStatus.OPEN


def get_circuit_status(contract: Contract) -> dict[str, Any]:
    """
    Get the current circuit breaker status.

    Args:
        contract: Contract to check

    Returns:
        Status dictionary
    """
    if contract.circuit_breaker is None:
        return {
            "status": "closed",
            "total_cycles": 0,
            "max_total_cycles": DEFAULT_TOTAL_MAX_CYCLES,
            "opened_at": None,
            "opened_reason": None,
        }

    cb = contract.circuit_breaker
    return {
        "status": cb.status.value,
        "total_cycles": cb.total_cycles,
        "max_total_cycles": cb.max_total_cycles,
        "opened_at": cb.opened_at.isoformat() if cb.opened_at else None,
        "opened_reason": cb.opened_reason,
    }
