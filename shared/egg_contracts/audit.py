"""
Audit logging for contract modifications.

All contract mutations are logged to the audit_log field for traceability.
"""

from datetime import UTC, datetime
from typing import Any

from .models import AuditAction, AuditEntry, Contract
from .roles import Role


def create_audit_entry(
    actor: str,
    role: Role | str,
    action: AuditAction | str,
    field_path: str,
    new_value: Any = None,
    old_value: Any = None,
    reason: str | None = None,
) -> AuditEntry:
    """
    Create an audit log entry.

    Args:
        actor: Who performed the action (username or role name)
        role: Role of the actor
        action: Type of action (create, update, delete, blocked)
        field_path: JSON path of the modified field
        new_value: New value (for create/update)
        old_value: Previous value (for update/delete)
        reason: Reason for blocked operations

    Returns:
        AuditEntry ready to be appended to contract.audit_log
    """
    if isinstance(action, str):
        action = AuditAction(action)
    if isinstance(role, Role):
        role = role.value

    return AuditEntry(
        timestamp=datetime.now(UTC),
        actor=actor,
        role=role,
        action=action,
        field_path=field_path,
        old_value=old_value,
        new_value=new_value,
        reason=reason,
    )


def log_mutation(
    contract: Contract,
    actor: str,
    role: Role | str,
    field_path: str,
    new_value: Any,
    old_value: Any = None,
) -> None:
    """
    Log a successful mutation to the contract audit log.

    Args:
        contract: Contract to update
        actor: Who performed the action
        role: Role of the actor
        field_path: JSON path of the modified field
        new_value: New value
        old_value: Previous value
    """
    entry = create_audit_entry(
        actor=actor,
        role=role,
        action=AuditAction.UPDATE if old_value is not None else AuditAction.CREATE,
        field_path=field_path,
        new_value=new_value,
        old_value=old_value,
    )
    if contract.audit_log is None:
        contract.audit_log = []
    contract.audit_log.append(entry)


def log_blocked_operation(
    contract: Contract,
    actor: str,
    role: Role | str,
    field_path: str,
    attempted_value: Any,
    reason: str,
) -> None:
    """
    Log a blocked mutation attempt to the contract audit log.

    Args:
        contract: Contract to update
        actor: Who attempted the action
        role: Role of the actor
        field_path: JSON path of the attempted modification
        attempted_value: Value that was attempted
        reason: Why the operation was blocked
    """
    entry = create_audit_entry(
        actor=actor,
        role=role,
        action=AuditAction.BLOCKED,
        field_path=field_path,
        new_value=attempted_value,
        reason=reason,
    )
    if contract.audit_log is None:
        contract.audit_log = []
    contract.audit_log.append(entry)


def get_field_history(
    contract: Contract,
    field_path: str,
) -> list[AuditEntry]:
    """
    Get the modification history for a specific field.

    Args:
        contract: Contract to search
        field_path: JSON path of the field

    Returns:
        List of audit entries for this field, oldest first
    """
    if not contract.audit_log:
        return []
    return [entry for entry in contract.audit_log if entry.field_path == field_path]


def get_actor_history(
    contract: Contract,
    actor: str,
) -> list[AuditEntry]:
    """
    Get all actions by a specific actor.

    Args:
        contract: Contract to search
        actor: Actor to filter by

    Returns:
        List of audit entries by this actor, oldest first
    """
    if not contract.audit_log:
        return []
    return [entry for entry in contract.audit_log if entry.actor == actor]


def get_blocked_operations(contract: Contract) -> list[AuditEntry]:
    """
    Get all blocked operation attempts.

    Args:
        contract: Contract to search

    Returns:
        List of blocked operation audit entries
    """
    if not contract.audit_log:
        return []
    return [entry for entry in contract.audit_log if entry.action == AuditAction.BLOCKED]
