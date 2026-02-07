"""
Phase-based operation filtering for the SDLC pipeline.

This module enforces phase-specific operation restrictions. Each pipeline phase
(refine, plan, implement, pr) has a defined set of permitted and blocked operations.
The gateway uses this module to filter operations against the current phase.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class OperationType(StrEnum):
    """Types of operations that can be filtered."""

    GIT = "git"
    GH = "gh"
    EGG_CONTRACT = "egg-contract"


class PipelinePhase(StrEnum):
    """Pipeline phases."""

    REFINE = "refine"
    PLAN = "plan"
    IMPLEMENT = "implement"
    PR = "pr"


@dataclass
class Operation:
    """An operation that can be allowed or blocked."""

    type: OperationType
    pattern: str
    description: str = ""

    def matches(self, command: str) -> bool:
        """Check if this operation pattern matches the given command.

        Args:
            command: The command to match (e.g., "push origin main")

        Returns:
            True if the pattern matches the command
        """
        # Use fnmatch for wildcard matching
        return fnmatch.fnmatch(command, self.pattern)


@dataclass
class PhasePermissions:
    """Permission set for a single phase."""

    allowed_operations: list[Operation]
    blocked_operations: list[Operation]
    exit_requires: str  # "human", "reviewer", or "implementer"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PhasePermissions:
        """Create PhasePermissions from a dictionary."""
        allowed = [
            Operation(
                type=OperationType(op["type"]),
                pattern=op["pattern"],
                description=op.get("description", ""),
            )
            for op in data.get("allowed_operations", [])
        ]
        blocked = [
            Operation(
                type=OperationType(op["type"]),
                pattern=op["pattern"],
                description=op.get("description", ""),
            )
            for op in data.get("blocked_operations", [])
        ]
        return cls(
            allowed_operations=allowed,
            blocked_operations=blocked,
            exit_requires=data.get("exit_requires", "human"),
        )


@dataclass
class FilterResult:
    """Result of an operation filter check."""

    allowed: bool
    message: str
    operation_type: OperationType | None = None
    phase: PipelinePhase | None = None
    blocked_reason: str | None = None

    @classmethod
    def allow(cls, message: str = "Operation allowed") -> FilterResult:
        """Create an allowed result."""
        return cls(allowed=True, message=message)

    @classmethod
    def block(
        cls,
        message: str,
        operation_type: OperationType,
        phase: PipelinePhase,
        blocked_reason: str,
    ) -> FilterResult:
        """Create a blocked result."""
        return cls(
            allowed=False,
            message=message,
            operation_type=operation_type,
            phase=phase,
            blocked_reason=blocked_reason,
        )


class PhaseFilter:
    """Filter operations based on the current pipeline phase."""

    def __init__(self, permissions_path: Path | None = None):
        """Initialize the phase filter.

        Args:
            permissions_path: Path to the phase-permissions.json file.
                            If None, uses the default path.
        """
        self._permissions: dict[PipelinePhase, PhasePermissions] = {}
        self._permissions_path = permissions_path
        self._loaded = False

    def _get_default_permissions_path(self) -> Path:
        """Get the default path to phase-permissions.json."""
        # In container: /app/.egg/phase-permissions.json
        # On host: relative to this file
        container_path = Path("/app/.egg/phase-permissions.json")
        if container_path.exists():
            return container_path

        # Try relative to this file
        relative_path = Path(__file__).parent.parent / ".egg" / "phase-permissions.json"
        if relative_path.exists():
            return relative_path

        # Fall back to current directory
        return Path(".egg/phase-permissions.json")

    def _load_permissions(self) -> None:
        """Load permissions from the JSON file."""
        if self._loaded:
            return

        path = self._permissions_path or self._get_default_permissions_path()

        if not path.exists():
            # Use default permissions if file doesn't exist
            self._permissions = self._get_default_permissions()
            self._loaded = True
            return

        with path.open() as f:
            data = json.load(f)

        phases_data = data.get("phases", {})
        for phase_name, phase_data in phases_data.items():
            try:
                phase = PipelinePhase(phase_name)
                self._permissions[phase] = PhasePermissions.from_dict(phase_data)
            except ValueError:
                # Skip unknown phases
                pass

        self._loaded = True

    def _get_default_permissions(self) -> dict[PipelinePhase, PhasePermissions]:
        """Get default permissions when no file is available."""
        return {
            PipelinePhase.REFINE: PhasePermissions(
                allowed_operations=[
                    Operation(OperationType.GH, "issue comment *", "Comment on issues"),
                    Operation(OperationType.GH, "issue edit *", "Edit issues"),
                    Operation(
                        OperationType.EGG_CONTRACT, "add-decision *", "Create HITL decisions"
                    ),
                    Operation(OperationType.EGG_CONTRACT, "show *", "View contract state"),
                ],
                blocked_operations=[
                    Operation(OperationType.GIT, "push *", "Cannot push during refine"),
                    Operation(OperationType.GH, "pr create*", "Cannot create PRs during refine"),
                ],
                exit_requires="human",
            ),
            PipelinePhase.PLAN: PhasePermissions(
                allowed_operations=[
                    Operation(OperationType.GH, "issue comment *", "Comment on issues"),
                    Operation(OperationType.GH, "issue edit *", "Edit issues"),
                    Operation(
                        OperationType.EGG_CONTRACT, "add-decision *", "Create HITL decisions"
                    ),
                    Operation(OperationType.EGG_CONTRACT, "show *", "View contract state"),
                ],
                blocked_operations=[
                    Operation(OperationType.GIT, "push *", "Cannot push during plan"),
                    Operation(OperationType.GH, "pr create*", "Cannot create PRs during plan"),
                ],
                exit_requires="human",
            ),
            PipelinePhase.IMPLEMENT: PhasePermissions(
                allowed_operations=[
                    Operation(OperationType.GIT, "push *", "Push code"),
                    Operation(OperationType.EGG_CONTRACT, "add-commit *", "Link commits"),
                    Operation(OperationType.EGG_CONTRACT, "update-notes *", "Add notes"),
                    Operation(OperationType.EGG_CONTRACT, "mark-task *", "Mark task status"),
                    Operation(OperationType.EGG_CONTRACT, "mark-phase *", "Mark phase status"),
                    Operation(OperationType.EGG_CONTRACT, "show *", "View contract state"),
                ],
                blocked_operations=[
                    Operation(OperationType.GH, "pr create*", "Cannot create PRs until complete"),
                ],
                exit_requires="reviewer",
            ),
            PipelinePhase.PR: PhasePermissions(
                allowed_operations=[
                    Operation(OperationType.GH, "pr create *", "Create PRs"),
                    Operation(OperationType.GH, "pr edit *", "Edit PRs"),
                    Operation(OperationType.GIT, "push *", "Push code"),
                    Operation(OperationType.EGG_CONTRACT, "show *", "View contract state"),
                ],
                blocked_operations=[],
                exit_requires="human",
            ),
        }

    def get_permissions(self, phase: PipelinePhase) -> PhasePermissions | None:
        """Get the permissions for a phase.

        Args:
            phase: The pipeline phase

        Returns:
            PhasePermissions for the phase, or None if not found
        """
        self._load_permissions()
        return self._permissions.get(phase)

    def filter_operation(
        self,
        phase: PipelinePhase,
        operation_type: OperationType,
        command: str,
    ) -> FilterResult:
        """Filter an operation against phase permissions.

        Args:
            phase: Current pipeline phase
            operation_type: Type of operation (git, gh, egg-contract)
            command: The command being executed (e.g., "push origin main")

        Returns:
            FilterResult indicating whether the operation is allowed
        """
        self._load_permissions()

        permissions = self._permissions.get(phase)
        if not permissions:
            return FilterResult.allow("Phase permissions not configured, allowing by default")

        # Check blocked operations first (blocked takes precedence)
        for blocked_op in permissions.blocked_operations:
            if blocked_op.type == operation_type and blocked_op.matches(command):
                return FilterResult.block(
                    message=self._format_blocked_message(
                        operation_type, command, phase, blocked_op.description
                    ),
                    operation_type=operation_type,
                    phase=phase,
                    blocked_reason=blocked_op.description,
                )

        # Check if operation is in allowed list (if list is non-empty)
        if permissions.allowed_operations:
            for allowed_op in permissions.allowed_operations:
                if allowed_op.type == operation_type and allowed_op.matches(command):
                    return FilterResult.allow(f"Operation allowed: {allowed_op.description}")

            # If we have an allowed list and this operation isn't in it,
            # we need to decide whether to block or allow by default
            # For now, we only block explicitly blocked operations
            return FilterResult.allow("Operation not explicitly blocked")

        return FilterResult.allow("No restrictions configured for this phase")

    def _format_blocked_message(
        self,
        operation_type: OperationType,
        command: str,
        phase: PipelinePhase,
        reason: str,
    ) -> str:
        """Format a blocked operation message.

        Args:
            operation_type: Type of operation
            command: The command that was blocked
            phase: Current pipeline phase
            reason: Reason the operation was blocked

        Returns:
            Formatted error message
        """
        return (
            f"Operation blocked: {operation_type.value} {command}\n"
            f"Phase '{phase.value}' does not permit this operation.\n"
            f"Reason: {reason}\n"
            f"To perform this operation, the pipeline must advance to a later phase."
        )

    def is_operation_blocked(
        self,
        phase: PipelinePhase,
        operation_type: OperationType,
        command: str,
    ) -> bool:
        """Check if an operation is blocked (convenience method).

        Args:
            phase: Current pipeline phase
            operation_type: Type of operation
            command: The command being executed

        Returns:
            True if the operation is blocked
        """
        result = self.filter_operation(phase, operation_type, command)
        return not result.allowed

    def get_exit_requirement(self, phase: PipelinePhase) -> str | None:
        """Get the role required to exit a phase.

        Args:
            phase: The pipeline phase

        Returns:
            Role name required to exit, or None if not configured
        """
        permissions = self.get_permissions(phase)
        if permissions:
            return permissions.exit_requires
        return None


# Module-level instance for convenience
_filter: PhaseFilter | None = None


def get_phase_filter() -> PhaseFilter:
    """Get the global PhaseFilter instance."""
    global _filter
    if _filter is None:
        _filter = PhaseFilter()
    return _filter


def filter_operation(
    phase: str | PipelinePhase,
    operation_type: str | OperationType,
    command: str,
) -> FilterResult:
    """Filter an operation against phase permissions (convenience function).

    Args:
        phase: Current pipeline phase (string or PipelinePhase enum)
        operation_type: Type of operation (string or OperationType enum)
        command: The command being executed

    Returns:
        FilterResult indicating whether the operation is allowed
    """
    if isinstance(phase, str):
        phase = PipelinePhase(phase)
    if isinstance(operation_type, str):
        operation_type = OperationType(operation_type)

    return get_phase_filter().filter_operation(phase, operation_type, command)


def is_operation_blocked(
    phase: str | PipelinePhase,
    operation_type: str | OperationType,
    command: str,
) -> bool:
    """Check if an operation is blocked (convenience function).

    Args:
        phase: Current pipeline phase
        operation_type: Type of operation
        command: The command being executed

    Returns:
        True if the operation is blocked
    """
    if isinstance(phase, str):
        phase = PipelinePhase(phase)
    if isinstance(operation_type, str):
        operation_type = OperationType(operation_type)

    return get_phase_filter().is_operation_blocked(phase, operation_type, command)
