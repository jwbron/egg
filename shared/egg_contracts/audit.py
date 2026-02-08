"""
Audit log management for contract modifications.

This module provides functions for creating and managing audit log entries
that track all modifications to contracts.
"""

from datetime import UTC, datetime
from typing import Any

from .models import AuditAction, AuditEntry, AuditRole


def create_audit_entry(
    actor: str,
    role: AuditRole,
    action: AuditAction,
    field_path: str,
    old_value: Any = None,
    new_value: Any = None,
    reason: str | None = None,
) -> AuditEntry:
    """
    Create a new audit log entry.

    Args:
        actor: Identifier of who performed the action
        role: Role of the actor
        action: Type of action performed
        field_path: JSON path of the modified field
        old_value: Previous value (if applicable)
        new_value: New value
        reason: Optional reason for the change

    Returns:
        A new AuditEntry
    """
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


def create_update_entry(
    actor: str,
    role: AuditRole,
    field_path: str,
    old_value: Any,
    new_value: Any,
    reason: str | None = None,
) -> AuditEntry:
    """
    Create an audit entry for an update operation.

    Args:
        actor: Identifier of who performed the action
        role: Role of the actor
        field_path: JSON path of the modified field
        old_value: Previous value
        new_value: New value
        reason: Optional reason for the change

    Returns:
        A new AuditEntry for an update action
    """
    return create_audit_entry(
        actor=actor,
        role=role,
        action=AuditAction.UPDATE,
        field_path=field_path,
        old_value=old_value,
        new_value=new_value,
        reason=reason,
    )


def create_transition_entry(
    actor: str,
    role: AuditRole,
    from_phase: str,
    to_phase: str,
    reason: str | None = None,
) -> AuditEntry:
    """
    Create an audit entry for a phase transition.

    Args:
        actor: Identifier of who performed the action
        role: Role of the actor
        from_phase: Previous phase
        to_phase: New phase
        reason: Optional reason for the transition

    Returns:
        A new AuditEntry for a transition action
    """
    return create_audit_entry(
        actor=actor,
        role=role,
        action=AuditAction.TRANSITION,
        field_path="current_phase",
        old_value=from_phase,
        new_value=to_phase,
        reason=reason,
    )


def format_audit_log(entries: list[AuditEntry], limit: int | None = None) -> str:
    """
    Format audit log entries as human-readable text.

    Args:
        entries: List of audit entries to format
        limit: Optional limit on number of entries to show

    Returns:
        Formatted string representation of the audit log
    """
    if limit:
        entries = entries[-limit:]

    lines = ["Audit Log:"]
    for entry in entries:
        timestamp = entry.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        line = f"  [{timestamp}] {entry.role.value}:{entry.actor} {entry.action.value} {entry.field_path}"
        if entry.old_value is not None and entry.new_value is not None:
            line += f" ({entry.old_value} -> {entry.new_value})"
        elif entry.new_value is not None:
            line += f" = {entry.new_value}"
        if entry.reason:
            line += f" - {entry.reason}"
        lines.append(line)

    return "\n".join(lines)
