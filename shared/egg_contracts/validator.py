"""
Contract mutation validator.

This module validates mutations against role permissions, ensuring that
only authorized roles can modify specific fields.
"""

from dataclasses import dataclass
from typing import Any

from .audit import create_update_entry
from .models import AuditEntry, AuditRole, Contract
from .roles import Role, can_modify, get_field_owner, normalize_path


@dataclass
class ValidationResult:
    """Result of a mutation validation."""

    valid: bool
    message: str
    field_path: str | None = None
    required_role: str | None = None


@dataclass
class MutationResult:
    """Result of applying a mutation."""

    success: bool
    message: str
    contract: Contract | None = None
    audit_entry: AuditEntry | None = None


def validate_mutation(
    role: Role,
    field_path: str,
    new_value: Any,
    contract: Contract | None = None,
) -> ValidationResult:
    """
    Validate whether a role can make a specific mutation.

    Args:
        role: The role attempting the mutation
        field_path: JSON path to the field being modified
        new_value: The new value being set
        contract: Optional contract for additional context validation

    Returns:
        ValidationResult indicating if the mutation is allowed
    """
    # Check role authorization
    if not can_modify(role, field_path):
        owner = get_field_owner(field_path)
        normalized = normalize_path(field_path)
        return ValidationResult(
            valid=False,
            message=f"Cannot modify field '{normalized}'. "
            f"Role '{role.value}' is not authorized to modify this field. "
            f"This field can only be modified by role '{owner.value}'.",
            field_path=field_path,
            required_role=owner.value,
        )

    return ValidationResult(valid=True, message="Mutation allowed")


def apply_mutation(
    contract: Contract,
    role: Role,
    actor: str,
    field_path: str,
    new_value: Any,
    reason: str | None = None,
) -> MutationResult:
    """
    Apply a mutation to the contract after validation.

    Args:
        contract: The contract to mutate
        role: The role attempting the mutation
        actor: Identifier of who is making the change
        field_path: JSON path to the field being modified
        new_value: The new value to set
        reason: Optional reason for the change

    Returns:
        MutationResult with the updated contract or error
    """
    # Validate the mutation
    validation = validate_mutation(role, field_path, new_value, contract)
    if not validation.valid:
        return MutationResult(
            success=False,
            message=validation.message,
        )

    # Get the old value and apply the mutation
    try:
        old_value = _get_value(contract, field_path)
        _set_value(contract, field_path, new_value)
    except (KeyError, IndexError, AttributeError) as e:
        return MutationResult(
            success=False,
            message=f"Failed to apply mutation: {e}",
        )

    # Create audit entry
    audit_role = AuditRole(role.value)
    audit_entry = create_update_entry(
        actor=actor,
        role=audit_role,
        field_path=field_path,
        old_value=old_value,
        new_value=new_value,
        reason=reason,
    )
    contract.audit_log.append(audit_entry)

    return MutationResult(
        success=True,
        message="Mutation applied successfully",
        contract=contract,
        audit_entry=audit_entry,
    )


def _get_value(obj: Any, path: str) -> Any:
    """
    Get a value from an object using a dot-notation path.

    Args:
        obj: The object to traverse
        path: Dot-notation path (e.g., "phases.0.tasks.1.status")

    Returns:
        The value at the path

    Raises:
        KeyError: If path doesn't exist
        IndexError: If array index is out of bounds
    """
    parts = path.split(".")
    current = obj

    for part in parts:
        if isinstance(current, list):
            idx = int(part)
            current = current[idx]
        elif hasattr(current, part):
            current = getattr(current, part)
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise KeyError(f"Cannot access '{part}' on {type(current)}")

    return current


def _set_value(obj: Any, path: str, value: Any) -> None:
    """
    Set a value on an object using a dot-notation path.

    Args:
        obj: The object to modify
        path: Dot-notation path (e.g., "phases.0.tasks.1.status")
        value: The value to set

    Raises:
        KeyError: If path doesn't exist
        IndexError: If array index is out of bounds
    """
    parts = path.split(".")
    current = obj

    # Navigate to parent of the target
    for part in parts[:-1]:
        if isinstance(current, list):
            idx = int(part)
            current = current[idx]
        elif hasattr(current, part):
            current = getattr(current, part)
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise KeyError(f"Cannot access '{part}' on {type(current)}")

    # Set the final value
    final_part = parts[-1]
    if isinstance(current, list):
        idx = int(final_part)
        current[idx] = value
    elif hasattr(current, final_part):
        setattr(current, final_part, value)
    elif isinstance(current, dict):
        current[final_part] = value
    else:
        raise KeyError(f"Cannot set '{final_part}' on {type(current)}")


def validate_task_mutation(
    role: Role,
    field: str,
    new_value: Any,
) -> ValidationResult:
    """
    Convenience function to validate a task field mutation.

    Args:
        role: The role attempting the mutation
        field: The field name (e.g., "status", "commit", "notes")
        new_value: The new value

    Returns:
        ValidationResult
    """
    # Build the full path - we use placeholder indices since we normalize anyway
    field_path = f"phases.*.tasks.*.{field}"
    return validate_mutation(role, field_path, new_value)


def validate_phase_mutation(
    role: Role,
    field: str,
    new_value: Any,
) -> ValidationResult:
    """
    Convenience function to validate a phase field mutation.

    Args:
        role: The role attempting the mutation
        field: The field name (e.g., "status")
        new_value: The new value

    Returns:
        ValidationResult
    """
    field_path = f"phases.*.{field}"
    return validate_mutation(role, field_path, new_value)
