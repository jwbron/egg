"""
Contract mutation validator.

Validates that mutations are allowed based on:
1. Role-based field access rules
2. Schema validation
3. Business logic constraints
"""

from dataclasses import dataclass, field
from typing import Any

from .models import Contract
from .roles import Role, can_modify, get_field_owner, FieldAccess


class ValidationError(Exception):
    """Raised when a mutation is not allowed."""

    def __init__(self, message: str, field_path: str, role: str, owner: str):
        super().__init__(message)
        self.message = message
        self.field_path = field_path
        self.role = role
        self.owner = owner

    def to_dict(self) -> dict[str, str]:
        """Convert to dictionary for API response."""
        return {
            "error": self.message,
            "field_path": self.field_path,
            "role": self.role,
            "owner": self.owner,
        }


@dataclass
class MutationResult:
    """Result of a mutation validation."""

    allowed: bool
    field_path: str
    role: str
    owner: str
    message: str
    old_value: Any = None
    new_value: Any = None


@dataclass
class ValidationReport:
    """Report of all mutation validations."""

    valid: bool
    mutations: list[MutationResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add_mutation(self, result: MutationResult) -> None:
        """Add a mutation result."""
        self.mutations.append(result)
        if not result.allowed:
            self.valid = False
            self.errors.append(result.message)


class ContractValidator:
    """
    Validates contract mutations against role-based access rules.

    Usage:
        validator = ContractValidator(Role.IMPLEMENTER)
        result = validator.validate_mutation("phases.0.tasks.0.commit", "abc123")
        if not result.allowed:
            raise ValidationError(result.message, ...)
    """

    def __init__(self, role: Role):
        """
        Initialize validator with a role.

        Args:
            role: The role attempting mutations
        """
        self.role = role

    def validate_mutation(
        self,
        field_path: str,
        new_value: Any,
        old_value: Any = None,
    ) -> MutationResult:
        """
        Validate a single field mutation.

        Args:
            field_path: JSON path to the field being modified
            new_value: New value to set
            old_value: Current value (optional, for audit)

        Returns:
            MutationResult indicating if mutation is allowed
        """
        owner = get_field_owner(field_path)
        allowed = can_modify(self.role, field_path)

        if allowed:
            message = f"Mutation allowed: {field_path}"
        else:
            message = (
                f"Cannot modify '{field_path}'. "
                f"Role '{self.role.value}' is not authorized to modify this field. "
                f"This field can only be modified by role '{owner.value}'."
            )

        return MutationResult(
            allowed=allowed,
            field_path=field_path,
            role=self.role.value,
            owner=owner.value,
            message=message,
            old_value=old_value,
            new_value=new_value,
        )

    def validate_mutations(
        self,
        mutations: list[tuple[str, Any, Any]],
    ) -> ValidationReport:
        """
        Validate multiple mutations.

        Args:
            mutations: List of (field_path, new_value, old_value) tuples

        Returns:
            ValidationReport with all results
        """
        report = ValidationReport(valid=True)
        for field_path, new_value, old_value in mutations:
            result = self.validate_mutation(field_path, new_value, old_value)
            report.add_mutation(result)
        return report

    def check_task_commit(self, task_id: str, commit_sha: str) -> MutationResult:
        """
        Validate adding a commit to a task.

        This is a convenience method for the common operation.

        Args:
            task_id: Task ID (e.g., "task-1")
            commit_sha: Git commit SHA

        Returns:
            MutationResult
        """
        # We don't know the phase index, so use a generic path
        # The actual path validation happens in the gateway
        field_path = f"phases.*.tasks.*.commit"
        return self.validate_mutation(field_path, commit_sha)

    def check_task_status(self, task_id: str, status: str) -> MutationResult:
        """
        Validate changing a task status.

        Args:
            task_id: Task ID
            status: New status

        Returns:
            MutationResult
        """
        field_path = f"phases.*.tasks.*.status"
        return self.validate_mutation(field_path, status)


def validate_contract_mutation(
    role: Role,
    field_path: str,
    new_value: Any,
    old_value: Any = None,
) -> MutationResult:
    """
    Convenience function to validate a single mutation.

    Args:
        role: Role attempting the mutation
        field_path: JSON path to the field
        new_value: New value
        old_value: Old value (optional)

    Returns:
        MutationResult
    """
    validator = ContractValidator(role)
    return validator.validate_mutation(field_path, new_value, old_value)


def raise_if_not_allowed(
    role: Role,
    field_path: str,
    new_value: Any = None,
) -> None:
    """
    Raise ValidationError if mutation is not allowed.

    Args:
        role: Role attempting the mutation
        field_path: JSON path to the field
        new_value: New value (optional)

    Raises:
        ValidationError: If mutation is not allowed
    """
    result = validate_contract_mutation(role, field_path, new_value)
    if not result.allowed:
        raise ValidationError(
            result.message,
            result.field_path,
            result.role,
            result.owner,
        )
