"""
Phase transition logic for the SDLC pipeline.

This module handles phase transitions, validating that the caller has
the appropriate role to advance the pipeline from one phase to another.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

# Add shared directory to path for egg_contracts
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Try relative imports first (module mode), fall back to absolute (script mode)
try:
    from .phase_filter import PipelinePhase, get_phase_filter
except ImportError:
    from phase_filter import PipelinePhase, get_phase_filter  # type: ignore[no-redef, import-not-found]


class TransitionRole(StrEnum):
    """Roles that can perform phase transitions."""

    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    HUMAN = "human"


# Phase transition graph: defines which phases can transition to which
VALID_TRANSITIONS: dict[PipelinePhase, list[PipelinePhase]] = {
    PipelinePhase.REFINE: [PipelinePhase.PLAN],
    PipelinePhase.PLAN: [PipelinePhase.IMPLEMENT],
    PipelinePhase.IMPLEMENT: [PipelinePhase.PR],
    PipelinePhase.PR: [],  # Terminal state - no automatic transitions
}


@dataclass
class TransitionResult:
    """Result of a phase transition attempt."""

    success: bool
    message: str
    from_phase: PipelinePhase | None = None
    to_phase: PipelinePhase | None = None
    transitioned_at: datetime | None = None
    transitioned_by: str | None = None

    @classmethod
    def allowed(
        cls,
        from_phase: PipelinePhase,
        to_phase: PipelinePhase,
        transitioned_by: str,
    ) -> TransitionResult:
        """Create a successful transition result."""
        return cls(
            success=True,
            message=f"Transition from '{from_phase.value}' to '{to_phase.value}' allowed",
            from_phase=from_phase,
            to_phase=to_phase,
            transitioned_at=datetime.now(UTC),
            transitioned_by=transitioned_by,
        )

    @classmethod
    def denied(
        cls,
        message: str,
        from_phase: PipelinePhase | None = None,
        to_phase: PipelinePhase | None = None,
    ) -> TransitionResult:
        """Create a denied transition result."""
        return cls(
            success=False,
            message=message,
            from_phase=from_phase,
            to_phase=to_phase,
        )


@dataclass
class TransitionRequest:
    """A request to transition between phases."""

    from_phase: PipelinePhase
    to_phase: PipelinePhase
    role: TransitionRole
    actor: str
    reason: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TransitionRequest:
        """Create a TransitionRequest from a dictionary."""
        return cls(
            from_phase=PipelinePhase(data["from_phase"]),
            to_phase=PipelinePhase(data["to_phase"]),
            role=TransitionRole(data["role"]),
            actor=data.get("actor", "unknown"),
            reason=data.get("reason"),
        )


def validate_transition(request: TransitionRequest) -> TransitionResult:
    """Validate a phase transition request.

    Checks that:
    1. The transition is valid (from_phase can transition to to_phase)
    2. The caller's role meets the exit requirement for from_phase

    Args:
        request: The transition request to validate

    Returns:
        TransitionResult indicating whether the transition is allowed
    """
    # Check if the transition is valid
    valid_targets = VALID_TRANSITIONS.get(request.from_phase, [])
    if request.to_phase not in valid_targets:
        return TransitionResult.denied(
            message=(
                f"Invalid transition: Cannot transition from '{request.from_phase.value}' "
                f"to '{request.to_phase.value}'. "
                f"Valid targets: {[p.value for p in valid_targets] if valid_targets else 'none'}"
            ),
            from_phase=request.from_phase,
            to_phase=request.to_phase,
        )

    # Get the exit requirement for the from_phase
    phase_filter = get_phase_filter()
    exit_requires = phase_filter.get_exit_requirement(request.from_phase)

    if exit_requires is None:
        # No requirement configured, allow the transition
        return TransitionResult.allowed(
            from_phase=request.from_phase,
            to_phase=request.to_phase,
            transitioned_by=request.actor,
        )

    # Check if the role meets the exit requirement
    if not _role_can_exit(request.role, exit_requires):
        return TransitionResult.denied(
            message=(
                f"Transition denied: Role '{request.role.value}' cannot exit phase "
                f"'{request.from_phase.value}'. Required role: '{exit_requires}'"
            ),
            from_phase=request.from_phase,
            to_phase=request.to_phase,
        )

    return TransitionResult.allowed(
        from_phase=request.from_phase,
        to_phase=request.to_phase,
        transitioned_by=request.actor,
    )


def _role_can_exit(role: TransitionRole, required: str) -> bool:
    """Check if a role can satisfy an exit requirement.

    Role hierarchy:
    - human can satisfy any requirement
    - reviewer can satisfy reviewer and implementer requirements
    - implementer can only satisfy implementer requirements

    Args:
        role: The caller's role
        required: The required role to exit the phase

    Returns:
        True if the role can satisfy the requirement
    """
    # Human can do anything
    if role == TransitionRole.HUMAN:
        return True

    # Reviewer can satisfy reviewer and implementer
    if role == TransitionRole.REVIEWER:
        return required in ("reviewer", "implementer")

    # Implementer can only satisfy implementer
    if role == TransitionRole.IMPLEMENTER:
        return required == "implementer"

    return False


def get_next_phase(current: PipelinePhase) -> PipelinePhase | None:
    """Get the next phase in the pipeline.

    Args:
        current: The current phase

    Returns:
        The next phase, or None if at terminal state
    """
    valid_targets = VALID_TRANSITIONS.get(current, [])
    if valid_targets:
        return valid_targets[0]
    return None


def can_transition_to(
    from_phase: str | PipelinePhase,
    to_phase: str | PipelinePhase,
    role: str | TransitionRole,
    actor: str = "unknown",
) -> TransitionResult:
    """Check if a transition is allowed (convenience function).

    Args:
        from_phase: Current phase
        to_phase: Target phase
        role: Caller's role
        actor: Actor performing the transition

    Returns:
        TransitionResult indicating whether the transition is allowed
    """
    if isinstance(from_phase, str):
        from_phase = PipelinePhase(from_phase)
    if isinstance(to_phase, str):
        to_phase = PipelinePhase(to_phase)
    if isinstance(role, str):
        role = TransitionRole(role)

    request = TransitionRequest(
        from_phase=from_phase,
        to_phase=to_phase,
        role=role,
        actor=actor,
    )
    return validate_transition(request)


def create_audit_entry(
    result: TransitionResult,
    role: TransitionRole,
    reason: str | None = None,
) -> dict[str, Any]:
    """Create an audit log entry for a phase transition.

    Args:
        result: The transition result
        role: The role that performed the transition
        reason: Optional reason for the transition

    Returns:
        Audit entry dictionary matching the contract schema
    """
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "actor": result.transitioned_by or "unknown",
        "role": role.value,
        "action": "transition",
        "field_path": "current_phase",
        "old_value": result.from_phase.value if result.from_phase else None,
        "new_value": result.to_phase.value if result.to_phase else None,
        "reason": reason,
    }
