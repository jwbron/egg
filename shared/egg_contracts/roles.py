"""
Role definitions and field ownership mapping for contract mutations.

This module defines the roles that can interact with contracts and
maps which fields each role is authorized to modify.
"""

from enum import StrEnum


class Role(StrEnum):
    """Roles that can interact with contracts.

    Uses StrEnum for consistency with AuditRole in models.py, making
    conversions and comparisons simpler.
    """

    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    HUMAN = "human"
    SYSTEM = "system"


# Field ownership mapping: maps JSON paths to the role that owns them
# Paths use dot notation (e.g., "phases.*.tasks.*.status")
# Wildcard (*) matches any array index
FIELD_OWNERSHIP: dict[str, Role] = {
    # Task fields owned by implementer
    "phases.*.tasks.*.commit": Role.IMPLEMENTER,
    "phases.*.tasks.*.notes": Role.IMPLEMENTER,
    "phases.*.tasks.*.files_affected": Role.IMPLEMENTER,
    "phases.*.tasks.*.files_affected.*": Role.IMPLEMENTER,
    # Task fields owned by reviewer
    "phases.*.tasks.*.status": Role.REVIEWER,
    # Phase fields owned by reviewer
    "phases.*.status": Role.REVIEWER,
    "phases.*.review_feedback": Role.REVIEWER,
    "phases.*.review_feedback.*": Role.REVIEWER,
    # Acceptance criteria owned by reviewer
    "acceptance_criteria.*.verified": Role.REVIEWER,
    # Pipeline phase transitions owned by reviewer (allows implement→pr advancement)
    "current_phase": Role.REVIEWER,
    # Decisions: implementer can CREATE new decisions, but only human can RESOLVE them
    "decisions.*": Role.IMPLEMENTER,
    "decisions.*.resolved": Role.HUMAN,
    "decisions.*.resolution": Role.HUMAN,
    "decisions.*.resolved_by": Role.HUMAN,
    "decisions.*.resolved_at": Role.HUMAN,
}

# Fields that any role can read but only the owner can write
# All other fields default to SYSTEM ownership (created at contract init)
DEFAULT_OWNER = Role.SYSTEM


def normalize_path(path: str) -> str:
    """
    Normalize a JSON path by replacing numeric indices with wildcards.

    Args:
        path: JSON path like "phases.0.tasks.1.status"

    Returns:
        Normalized path like "phases.*.tasks.*.status"
    """
    parts = path.split(".")
    normalized = []
    for part in parts:
        if part.isdigit():
            normalized.append("*")
        else:
            normalized.append(part)
    return ".".join(normalized)


def get_field_owner(path: str) -> Role:
    """
    Get the role that owns a specific field path.

    Args:
        path: JSON path to the field (e.g., "phases.0.tasks.1.status")

    Returns:
        The role that owns this field
    """
    normalized = normalize_path(path)

    # Try exact match first
    if normalized in FIELD_OWNERSHIP:
        return FIELD_OWNERSHIP[normalized]

    # Try prefix match for nested paths (e.g., review_feedback.* matches review_feedback.0.feedback)
    for pattern, owner in FIELD_OWNERSHIP.items():
        if pattern.endswith(".*"):
            prefix = pattern[:-2]
            if normalized.startswith(prefix + "."):
                return owner

    return DEFAULT_OWNER


def can_modify(role: Role, path: str) -> bool:
    """
    Check if a role can modify a specific field.

    Args:
        role: The role attempting the modification
        path: JSON path to the field

    Returns:
        True if the role can modify the field
    """
    # Human can modify everything
    if role == Role.HUMAN:
        return True

    # System can create/initialize contracts but not modify owned fields
    if role == Role.SYSTEM:
        owner = get_field_owner(path)
        # System can only modify fields it owns
        return owner == Role.SYSTEM

    # Check if the role owns this field
    owner = get_field_owner(path)
    return owner == role


def get_role_permissions(role: Role) -> dict[str, list[str]]:
    """
    Get a summary of what fields a role can modify.

    Args:
        role: The role to get permissions for

    Returns:
        Dictionary with 'can_modify' and 'cannot_modify' lists
    """
    if role == Role.HUMAN:
        return {
            "can_modify": ["*"],
            "cannot_modify": [],
        }

    can_mod = []
    cannot_mod = []

    for path, owner in FIELD_OWNERSHIP.items():
        if owner == role:
            can_mod.append(path)
        else:
            cannot_mod.append(path)

    return {
        "can_modify": can_mod,
        "cannot_modify": cannot_mod,
    }
