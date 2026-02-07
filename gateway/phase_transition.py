"""
Phase transition logic for SDLC pipeline.

Manages transitions between pipeline phases, enforcing exit requirements:
- refine -> plan: Requires human approval
- plan -> implement: Requires human approval
- implement -> pr: Requires reviewer approval (all tasks complete)
- pr -> done: Requires human merge (external to this system)
"""

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Add shared directory to path
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str) -> logging.Logger:
        return logging.getLogger(name)


try:
    from egg_contracts import Contract, load_contract, save_contract
    from egg_contracts.models import PipelinePhase, PhaseStatus, TaskStatus
    from egg_contracts.audit import log_mutation
    from egg_contracts.roles import Role
except ImportError:
    Contract = None  # type: ignore
    load_contract = None  # type: ignore
    save_contract = None  # type: ignore
    PipelinePhase = None  # type: ignore
    Role = None  # type: ignore


logger = get_logger("gateway.phase_transition")


# Valid phase transitions
PHASE_ORDER = ["refine", "plan", "implement", "pr"]

PHASE_TRANSITIONS = {
    "refine": {"next": "plan", "exit_requires": "human"},
    "plan": {"next": "implement", "exit_requires": "human"},
    "implement": {"next": "pr", "exit_requires": "reviewer"},
    "pr": {"next": None, "exit_requires": "human"},
}


@dataclass
class TransitionResult:
    """Result of a phase transition attempt."""

    success: bool
    message: str
    from_phase: str | None = None
    to_phase: str | None = None
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API response."""
        result = {"success": self.success, "message": self.message}
        if self.from_phase:
            result["from_phase"] = self.from_phase
        if self.to_phase:
            result["to_phase"] = self.to_phase
        if self.details:
            result["details"] = self.details
        return result


def can_transition(current_phase: str, target_phase: str) -> bool:
    """
    Check if a phase transition is valid.

    Args:
        current_phase: Current phase name
        target_phase: Desired phase name

    Returns:
        True if transition is valid
    """
    config = PHASE_TRANSITIONS.get(current_phase)
    if not config:
        return False
    return config.get("next") == target_phase


def get_exit_requirement(phase: str) -> str | None:
    """
    Get the exit requirement for a phase.

    Args:
        phase: Phase name

    Returns:
        Exit requirement role or None
    """
    config = PHASE_TRANSITIONS.get(phase)
    if config:
        return config.get("exit_requires")
    return None


def check_implementation_complete(contract: Contract) -> tuple[bool, list[str]]:
    """
    Check if all implementation phase tasks are complete.

    Args:
        contract: Contract to check

    Returns:
        Tuple of (all_complete, incomplete_task_ids)
    """
    incomplete = []
    for phase in contract.phases:
        for task in phase.tasks:
            if task.status not in (TaskStatus.COMPLETE, TaskStatus.FAILED):
                incomplete.append(task.id)
    return len(incomplete) == 0, incomplete


def validate_transition(
    contract: Contract,
    target_phase: str,
    actor_role: str,
) -> TransitionResult:
    """
    Validate a phase transition.

    Args:
        contract: Current contract state
        target_phase: Desired phase
        actor_role: Role of the actor attempting transition

    Returns:
        TransitionResult indicating if transition is valid
    """
    current_phase = contract.currentPhase.value if contract.currentPhase else "refine"

    # Check if target phase is valid
    try:
        PipelinePhase(target_phase)
    except ValueError:
        return TransitionResult(
            success=False,
            message=f"Invalid target phase: {target_phase}",
            from_phase=current_phase,
        )

    # Check if transition is valid
    if not can_transition(current_phase, target_phase):
        next_phase = PHASE_TRANSITIONS.get(current_phase, {}).get("next")
        return TransitionResult(
            success=False,
            message=f"Cannot transition from {current_phase} to {target_phase}",
            from_phase=current_phase,
            to_phase=target_phase,
            details={
                "valid_next_phase": next_phase,
                "hint": f"Must transition to {next_phase} first" if next_phase else "No further transitions",
            },
        )

    # Check exit requirements
    exit_req = get_exit_requirement(current_phase)
    if exit_req:
        if exit_req == "human" and actor_role != "human":
            return TransitionResult(
                success=False,
                message=f"Phase {current_phase} requires human approval to exit",
                from_phase=current_phase,
                to_phase=target_phase,
                details={"required_role": "human", "actor_role": actor_role},
            )
        if exit_req == "reviewer" and actor_role not in ("reviewer", "human"):
            return TransitionResult(
                success=False,
                message=f"Phase {current_phase} requires reviewer approval to exit",
                from_phase=current_phase,
                to_phase=target_phase,
                details={"required_role": "reviewer", "actor_role": actor_role},
            )

    # Special check for implement -> pr transition
    if current_phase == "implement" and target_phase == "pr":
        all_complete, incomplete = check_implementation_complete(contract)
        if not all_complete:
            return TransitionResult(
                success=False,
                message="Cannot create PR until all tasks are complete",
                from_phase=current_phase,
                to_phase=target_phase,
                details={
                    "incomplete_tasks": incomplete,
                    "hint": "Reviewer must mark all tasks as complete",
                },
            )

    return TransitionResult(
        success=True,
        message=f"Transition from {current_phase} to {target_phase} is valid",
        from_phase=current_phase,
        to_phase=target_phase,
    )


def execute_transition(
    repo_root: Path | str,
    issue_number: int,
    target_phase: str,
    actor: str,
    actor_role: str,
) -> TransitionResult:
    """
    Execute a phase transition.

    Args:
        repo_root: Path to repository root
        issue_number: GitHub issue number
        target_phase: Desired phase
        actor: Who is performing the transition
        actor_role: Role of the actor

    Returns:
        TransitionResult
    """
    if load_contract is None or save_contract is None:
        return TransitionResult(
            success=False,
            message="Contract system not available",
        )

    contract = load_contract(repo_root, issue_number)
    if not contract:
        return TransitionResult(
            success=False,
            message=f"Contract not found for issue #{issue_number}",
        )

    # Validate the transition
    validation = validate_transition(contract, target_phase, actor_role)
    if not validation.success:
        return validation

    # Execute the transition
    from_phase = contract.currentPhase.value if contract.currentPhase else "refine"
    contract.currentPhase = PipelinePhase(target_phase)

    # Log the transition
    if log_mutation is not None:
        try:
            role = Role(actor_role) if Role else actor_role
            log_mutation(
                contract,
                actor=actor,
                role=role,
                field_path="currentPhase",
                new_value=target_phase,
                old_value=from_phase,
            )
        except Exception as e:
            logger.warning("Failed to log phase transition", error=str(e))

    # Save the updated contract
    save_contract(contract, repo_root)

    logger.info(
        "Phase transition executed",
        issue=issue_number,
        from_phase=from_phase,
        to_phase=target_phase,
        actor=actor,
        role=actor_role,
    )

    return TransitionResult(
        success=True,
        message=f"Transitioned from {from_phase} to {target_phase}",
        from_phase=from_phase,
        to_phase=target_phase,
    )


def get_current_phase(
    repo_root: Path | str,
    issue_number: int,
) -> str | None:
    """
    Get the current phase for an issue.

    Args:
        repo_root: Path to repository root
        issue_number: GitHub issue number

    Returns:
        Current phase name or None if contract not found
    """
    if load_contract is None:
        return None

    contract = load_contract(repo_root, issue_number)
    if not contract:
        return None

    return contract.currentPhase.value if contract.currentPhase else "refine"


def get_next_phase(current_phase: str) -> str | None:
    """
    Get the next phase in the pipeline.

    Args:
        current_phase: Current phase name

    Returns:
        Next phase name or None if at end
    """
    config = PHASE_TRANSITIONS.get(current_phase)
    if config:
        return config.get("next")
    return None
