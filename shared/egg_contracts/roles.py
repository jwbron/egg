"""
Role-based field access control for SDLC contracts.

Enforces which roles can modify which fields:
- Implementer: Can modify commit, notes
- Reviewer: Can modify status, verified, review_feedback
- Human: Can modify all fields including decisions
"""

from enum import StrEnum
from typing import Literal


class Role(StrEnum):
    """Agent/actor roles."""

    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    HUMAN = "human"


class FieldAccess(StrEnum):
    """Field access levels."""

    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    HUMAN = "human"
    ANY = "any"  # Any role can modify


# Field ownership mapping
# Maps JSON paths (using dot notation with wildcards) to the role that owns them
FIELD_OWNERSHIP: dict[str, FieldAccess] = {
    # Issue metadata - read-only after creation
    "issue.*": FieldAccess.HUMAN,
    # Pipeline phase - only human can transition
    "currentPhase": FieldAccess.HUMAN,
    # Branch - set at creation
    "branch": FieldAccess.HUMAN,
    # Acceptance criteria verification - reviewer only
    "acceptance_criteria.*.verified": FieldAccess.REVIEWER,
    # Phase status - reviewer controls
    "phases.*.status": FieldAccess.REVIEWER,
    "phases.*.review_cycles": FieldAccess.ANY,  # Incremented by system
    "phases.*.escalated": FieldAccess.ANY,  # Set by circuit breaker
    "phases.*.escalation_reason": FieldAccess.ANY,
    "phases.*.review_feedback": FieldAccess.REVIEWER,
    # Task status - reviewer controls
    "phases.*.tasks.*.status": FieldAccess.REVIEWER,
    "phases.*.tasks.*.feedback": FieldAccess.REVIEWER,
    # Task implementation - implementer controls
    "phases.*.tasks.*.commit": FieldAccess.IMPLEMENTER,
    "phases.*.tasks.*.notes": FieldAccess.IMPLEMENTER,
    # Task escalation - system controlled
    "phases.*.tasks.*.review_cycles": FieldAccess.ANY,
    "phases.*.tasks.*.escalated": FieldAccess.ANY,
    # Decisions - human only for resolution
    "decisions.*.resolved": FieldAccess.HUMAN,
    "decisions.*.resolution": FieldAccess.HUMAN,
    "decisions.*.resolved_by": FieldAccess.HUMAN,
    "decisions.*.resolved_at": FieldAccess.HUMAN,
    "decisions.*.debounce_until": FieldAccess.ANY,  # System managed
    # Decision creation - any role can create decisions
    "decisions": FieldAccess.ANY,
    # Circuit breaker - system controlled
    "circuit_breaker.*": FieldAccess.ANY,
    # Audit log - append only, any role
    "audit_log": FieldAccess.ANY,
}


def normalize_path(path: str) -> str:
    """
    Normalize a JSON path for matching.

    Converts specific indices to wildcards for matching against patterns.
    e.g., "phases.0.tasks.1.status" -> "phases.*.tasks.*.status"
    """
    parts = path.split(".")
    normalized = []
    for part in parts:
        if part.isdigit():
            normalized.append("*")
        else:
            normalized.append(part)
    return ".".join(normalized)


def get_field_owner(path: str) -> FieldAccess:
    """
    Get the role that owns a field.

    Args:
        path: JSON path to the field (e.g., "phases.0.tasks.1.status")

    Returns:
        The FieldAccess level for that field
    """
    normalized = normalize_path(path)

    # Try exact match first
    if normalized in FIELD_OWNERSHIP:
        return FIELD_OWNERSHIP[normalized]

    # Try progressively more general patterns
    parts = normalized.split(".")
    while parts:
        # Try with wildcard at end
        pattern = ".".join(parts) + ".*"
        if pattern in FIELD_OWNERSHIP:
            return FIELD_OWNERSHIP[pattern]

        # Try without last part
        parts.pop()
        pattern = ".".join(parts)
        if pattern in FIELD_OWNERSHIP:
            return FIELD_OWNERSHIP[pattern]

    # Default to human-only for unknown fields
    return FieldAccess.HUMAN


def can_modify(role: Role, path: str) -> bool:
    """
    Check if a role can modify a field.

    Args:
        role: The role attempting the modification
        path: JSON path to the field

    Returns:
        True if the role can modify the field
    """
    owner = get_field_owner(path)

    # Human can modify everything
    if role == Role.HUMAN:
        return True

    # ANY means any role can modify
    if owner == FieldAccess.ANY:
        return True

    # Check role matches owner
    if owner == FieldAccess.IMPLEMENTER and role == Role.IMPLEMENTER:
        return True
    if owner == FieldAccess.REVIEWER and role == Role.REVIEWER:
        return True

    return False


def get_allowed_fields(role: Role) -> list[str]:
    """
    Get list of field patterns a role can modify.

    Args:
        role: The role to check

    Returns:
        List of field path patterns the role can modify
    """
    allowed = []
    for pattern, access in FIELD_OWNERSHIP.items():
        if access == FieldAccess.ANY:
            allowed.append(pattern)
        elif access == FieldAccess.HUMAN and role == Role.HUMAN:
            allowed.append(pattern)
        elif access == FieldAccess.IMPLEMENTER and role in (Role.IMPLEMENTER, Role.HUMAN):
            allowed.append(pattern)
        elif access == FieldAccess.REVIEWER and role in (Role.REVIEWER, Role.HUMAN):
            allowed.append(pattern)
    return sorted(allowed)


RoleType = Literal["implementer", "reviewer", "human"]
