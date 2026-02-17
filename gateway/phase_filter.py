"""
Phase-based operation filtering for the SDLC pipeline.

This module enforces phase-specific operation restrictions. Each pipeline phase
(refine, plan, implement, pr) has a defined set of permitted and blocked operations.
The gateway uses this module to filter operations against the current phase.
"""

from __future__ import annotations

import fnmatch
import json
import posixpath
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class OperationType(StrEnum):
    """Types of operations that can be filtered."""

    GIT = "git"
    GH = "gh"
    EGG_CONTRACT = "egg-contract"


class PipelinePhase(StrEnum):
    """Pipeline phases.

    Note: This duplicates egg_contracts.models.PipelinePhase to avoid import
    complexity in the gateway module. Values must be kept in sync.
    """

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
class FileRestriction:
    """Role-based file path restriction for git push operations.

    File restrictions prevent certain roles from modifying specific files
    via git push. This is used to protect sensitive files like SDLC contracts
    that should only be modified through dedicated APIs.
    """

    role: str
    blocked_patterns: list[str] = field(default_factory=list)
    blocked_reason: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileRestriction:
        """Create FileRestriction from a dictionary."""
        return cls(
            role=data["role"],
            blocked_patterns=data.get("blocked_patterns", []),
            blocked_reason=data.get("blocked_reason", ""),
        )

    def is_file_blocked(self, file_path: str) -> bool:
        """Check if a file path is blocked by this restriction.

        Args:
            file_path: The file path to check (relative to repo root)

        Returns:
            True if the file matches any blocked pattern, or if the path escapes the repo
        """
        try:
            normalized = self._normalize_path(file_path)
        except ValueError:
            # Paths that escape the repository are always blocked
            return True
        return any(normalized.startswith(pattern) for pattern in self.blocked_patterns)

    @staticmethod
    def _normalize_path(file_path: str) -> str:
        """Normalize a file path to prevent bypass via path manipulation.

        Handles:
        - Leading ./ prefix (./egg-state/contracts/ -> .egg-state/contracts/)
        - Double slashes (.egg-state//contracts/ -> .egg-state/contracts/)
        - Trailing slashes on file paths

        Args:
            file_path: Raw file path from git diff

        Returns:
            Normalized file path

        Raises:
            ValueError: If the path escapes the repository (starts with ../ or /)
        """
        normalized = posixpath.normpath(file_path)
        if normalized.startswith("./"):
            normalized = normalized[2:]
        # Security: block paths that escape the repository
        if normalized.startswith("../") or normalized.startswith("/"):
            raise ValueError(f"Invalid path escapes repository: {file_path}")
        return normalized


@dataclass
class FileRestrictionResult:
    """Result of a file restriction check."""

    allowed: bool
    message: str
    role: str | None = None
    blocked_files: list[str] = field(default_factory=list)
    blocked_reason: str | None = None

    @classmethod
    def allow(cls, message: str = "Files allowed") -> FileRestrictionResult:
        """Create an allowed result."""
        return cls(allowed=True, message=message)

    @classmethod
    def block(
        cls,
        message: str,
        role: str,
        blocked_files: list[str],
        blocked_reason: str,
    ) -> FileRestrictionResult:
        """Create a blocked result."""
        return cls(
            allowed=False,
            message=message,
            role=role,
            blocked_files=blocked_files,
            blocked_reason=blocked_reason,
        )


@dataclass
class PhaseFileRestriction:
    """Phase-based file restrictions for git push operations.

    Each phase can define:
    - allowed_patterns: Only files matching these patterns can be pushed (if set)
    - blocked_patterns: Files matching these patterns are always blocked
    - description: Human-readable explanation of the restriction
    """

    allowed_patterns: list[str] = field(default_factory=list)
    blocked_patterns: list[str] = field(default_factory=list)
    description: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PhaseFileRestriction:
        """Create PhaseFileRestriction from a dictionary."""
        return cls(
            allowed_patterns=data.get("allowed_patterns", []),
            blocked_patterns=data.get("blocked_patterns", []),
            description=data.get("description", ""),
        )

    def is_file_allowed(self, file_path: str) -> tuple[bool, str]:
        """Check if a file is allowed for this phase.

        Returns:
            Tuple of (allowed: bool, reason: str)
        """
        try:
            normalized = self._normalize_path(file_path)
        except ValueError as e:
            # Paths that escape the repository are never allowed
            return False, str(e)

        # Check blocked patterns first (explicit blocks take priority)
        for pattern in self.blocked_patterns:
            if self._matches_pattern(normalized, pattern):
                return False, f"File '{file_path}' matches blocked pattern '{pattern}'"

        # If allowed_patterns is defined and non-empty, file must match one
        if self.allowed_patterns:
            # Special case: "*" means allow everything
            if "*" in self.allowed_patterns:
                return True, "All files allowed"

            for pattern in self.allowed_patterns:
                if self._matches_pattern(normalized, pattern):
                    return True, f"File '{file_path}' matches allowed pattern '{pattern}'"

            return False, f"File '{file_path}' does not match any allowed pattern"

        # No allowed_patterns defined = allow by default (only blocked patterns matter)
        return True, "No explicit restrictions"

    @staticmethod
    def _normalize_path(file_path: str) -> str:
        """Normalize a file path to prevent bypass via path manipulation.

        Raises:
            ValueError: If the path escapes the repository (starts with ../ or /)
        """
        normalized = posixpath.normpath(file_path)
        if normalized.startswith("./"):
            normalized = normalized[2:]
        # Security: block paths that escape the repository
        if normalized.startswith("../") or normalized.startswith("/"):
            raise ValueError(f"Invalid path escapes repository: {file_path}")
        return normalized

    @staticmethod
    def _matches_pattern(file_path: str, pattern: str) -> bool:
        """Check if a file path matches a glob-like pattern.

        Supports:
        - Exact prefix match (e.g., ".egg-state/contracts/" matches ".egg-state/contracts/foo.json")
        - Wildcard suffix match (e.g., ".egg-state/drafts/*analysis*" matches "*-analysis.md")
        - Full wildcard ("*" matches everything)
        """
        if pattern == "*":
            return True

        # If pattern ends with *, use fnmatch for glob matching
        if "*" in pattern:
            return fnmatch.fnmatch(file_path, pattern)

        # Otherwise, use prefix matching (for directory patterns)
        if pattern.endswith("/"):
            return file_path.startswith(pattern)

        return file_path.startswith(pattern)


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
        self._file_restrictions: list[FileRestriction] = []
        self._phase_file_restrictions: dict[PipelinePhase, PhaseFileRestriction] = {}
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
            self._file_restrictions = self._get_default_file_restrictions()
            self._phase_file_restrictions = self._get_default_phase_file_restrictions()
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

        # Load file restrictions
        # SECURITY: When file_restrictions key is missing from config, use defaults
        # to ensure protection even for legacy configs that predate this feature.
        file_restrictions_data = data.get("file_restrictions", [])
        if file_restrictions_data:
            self._file_restrictions = [
                FileRestriction.from_dict(fr) for fr in file_restrictions_data
            ]
        else:
            # Use defaults when file_restrictions not configured (backwards compatibility)
            self._file_restrictions = self._get_default_file_restrictions()

        # Load phase-based file restrictions
        phase_file_restrictions_data = data.get("phase_file_restrictions", {})
        if phase_file_restrictions_data:
            for phase_name, restriction_data in phase_file_restrictions_data.items():
                try:
                    phase = PipelinePhase(phase_name)
                    self._phase_file_restrictions[phase] = PhaseFileRestriction.from_dict(
                        restriction_data
                    )
                except ValueError:
                    # Skip unknown phases
                    pass
        else:
            # Use defaults when phase_file_restrictions not configured
            self._phase_file_restrictions = self._get_default_phase_file_restrictions()

        self._loaded = True

    def _get_default_permissions(self) -> dict[PipelinePhase, PhasePermissions]:
        """Get default permissions when no file is available."""
        return {
            PipelinePhase.REFINE: PhasePermissions(
                allowed_operations=[
                    Operation(OperationType.GH, "issue comment *", "Comment on issues"),
                    Operation(OperationType.GH, "issue edit *", "Edit issues"),
                    Operation(OperationType.GIT, "push *", "Push state files to remote"),
                    Operation(
                        OperationType.EGG_CONTRACT, "add-decision *", "Create HITL decisions"
                    ),
                    Operation(OperationType.EGG_CONTRACT, "show *", "View contract state"),
                ],
                blocked_operations=[
                    Operation(OperationType.GH, "pr create*", "Cannot create PRs during refine"),
                ],
                exit_requires="human",
            ),
            PipelinePhase.PLAN: PhasePermissions(
                allowed_operations=[
                    Operation(OperationType.GH, "issue comment *", "Comment on issues"),
                    Operation(OperationType.GH, "issue edit *", "Edit issues"),
                    Operation(OperationType.GIT, "push *", "Push state files to remote"),
                    Operation(
                        OperationType.EGG_CONTRACT, "add-decision *", "Create HITL decisions"
                    ),
                    Operation(OperationType.EGG_CONTRACT, "show *", "View contract state"),
                ],
                blocked_operations=[
                    Operation(OperationType.GH, "pr create*", "Cannot create PRs during plan"),
                ],
                exit_requires="human",
            ),
            PipelinePhase.IMPLEMENT: PhasePermissions(
                allowed_operations=[
                    Operation(OperationType.GIT, "push *", "Push code"),
                    Operation(OperationType.EGG_CONTRACT, "add-commit *", "Link commits"),
                    Operation(OperationType.EGG_CONTRACT, "update-notes *", "Add notes"),
                    Operation(OperationType.EGG_CONTRACT, "show *", "View contract state"),
                ],
                blocked_operations=[
                    Operation(OperationType.GH, "pr create*", "Cannot create PRs until complete"),
                ],
                exit_requires="reviewer",
            ),
            PipelinePhase.PR: PhasePermissions(
                allowed_operations=[
                    Operation(OperationType.GH, "pr create*", "Create PRs"),
                    Operation(OperationType.GH, "pr edit *", "Edit PRs"),
                    Operation(OperationType.GIT, "push *", "Push code"),
                    Operation(OperationType.EGG_CONTRACT, "show *", "View contract state"),
                ],
                blocked_operations=[],
                exit_requires="human",
            ),
        }

    def _get_default_file_restrictions(self) -> list[FileRestriction]:
        """Get default file restrictions when no file is available.

        These defaults protect contract files from being modified directly
        via git push by implementer agents.
        """
        return [
            FileRestriction(
                role="implementer",
                blocked_patterns=[".egg-state/contracts/"],
                blocked_reason="Contract files can only be modified through the contract API",
            ),
        ]

    def _get_default_phase_file_restrictions(self) -> dict[PipelinePhase, PhaseFileRestriction]:
        """Get default phase-based file restrictions.

        These defaults define which files can be pushed during each phase:
        - refine: Only .egg-state/ files (contracts, drafts, checkpoints, agent-outputs, reviews)
        - plan: Only .egg-state/ files (contracts, drafts, checkpoints, agent-outputs, reviews)
        - implement: Code only, not .egg-state/ (except checkpoints and agent-outputs)
        - pr: Everything
        """
        return {
            PipelinePhase.REFINE: PhaseFileRestriction(
                allowed_patterns=[
                    ".egg-state/contracts/*",
                    ".egg-state/drafts/*analysis*",
                    ".egg-state/checkpoints/*",
                    ".egg-state/agent-outputs/*",
                    ".egg-state/reviews/*",
                ],
                description="Refine phase can only push contracts, analysis drafts, checkpoints, agent outputs, and reviews",
            ),
            PipelinePhase.PLAN: PhaseFileRestriction(
                allowed_patterns=[
                    ".egg-state/contracts/*",
                    ".egg-state/drafts/*plan*",
                    ".egg-state/checkpoints/*",
                    ".egg-state/agent-outputs/*",
                    ".egg-state/reviews/*",
                ],
                description="Plan phase can only push contracts, plan drafts, checkpoints, agent outputs, and reviews",
            ),
            PipelinePhase.IMPLEMENT: PhaseFileRestriction(
                blocked_patterns=[
                    ".egg-state/contracts/*",
                    ".egg-state/drafts/*",
                    ".egg-state/pipelines/*",
                    ".egg-state/reviews/*",
                ],
                # No allowed_patterns = allow everything except blocked
                # Checkpoints and agent-outputs are not blocked since they don't match any blocked_patterns
                description="Implement phase can push code but not .egg-state/ (except checkpoints and agent-outputs)",
            ),
            PipelinePhase.PR: PhaseFileRestriction(
                allowed_patterns=["*"],
                description="PR phase can push everything",
            ),
        }

    def get_file_restrictions(self) -> list[FileRestriction]:
        """Get all configured file restrictions.

        Returns:
            List of FileRestriction objects
        """
        self._load_permissions()
        return self._file_restrictions

    def get_file_restrictions_for_role(self, role: str) -> list[FileRestriction]:
        """Get file restrictions that apply to a specific role.

        Args:
            role: The role to get restrictions for (e.g., "implementer")

        Returns:
            List of FileRestriction objects that apply to this role
        """
        self._load_permissions()
        role_lower = role.lower()
        return [fr for fr in self._file_restrictions if fr.role.lower() == role_lower]

    def check_file_restrictions(self, role: str, files: list[str]) -> FileRestrictionResult:
        """Check if any files are blocked for the given role.

        This method checks a list of files against the configured file restrictions
        for the specified role. It's used to validate git push operations.

        SECURITY: This method implements role-based file access control. The implementer
        role is blocked from modifying contract files to prevent bypassing the SDLC
        contract system via direct git commits.

        Args:
            role: The role attempting the operation (e.g., "implementer")
            files: List of file paths being modified

        Returns:
            FileRestrictionResult indicating whether the files are allowed
        """
        self._load_permissions()

        if not role or not files:
            return FileRestrictionResult.allow("No role or files to check")

        restrictions = self.get_file_restrictions_for_role(role)
        if not restrictions:
            return FileRestrictionResult.allow(f"No file restrictions for role: {role}")

        blocked_files: list[str] = []
        blocked_reason = ""

        for restriction in restrictions:
            for file_path in files:
                if restriction.is_file_blocked(file_path):
                    if file_path not in blocked_files:
                        blocked_files.append(file_path)
                    blocked_reason = restriction.blocked_reason

        if blocked_files:
            return FileRestrictionResult.block(
                message=f"Role '{role}' cannot modify: {', '.join(blocked_files)}",
                role=role,
                blocked_files=blocked_files,
                blocked_reason=blocked_reason,
            )

        return FileRestrictionResult.allow("All files allowed")

    def check_phase_file_restrictions(
        self, phase: PipelinePhase | str, files: list[str]
    ) -> FileRestrictionResult:
        """Check if files are allowed for the given pipeline phase.

        This method enforces phase-based file restrictions for git push operations.
        Different phases have different allowed/blocked file patterns to ensure
        proper separation of concerns in the SDLC pipeline.

        SECURITY: This is a critical control for pipeline integrity:
        - refine/plan phases can only modify .egg-state/ files (contracts, drafts, checkpoints, agent-outputs, reviews)
        - implement phase can modify code but not .egg-state/ (except checkpoints and agent-outputs)
        - pr phase has full access

        Args:
            phase: The current pipeline phase
            files: List of file paths being modified in the push

        Returns:
            FileRestrictionResult indicating whether the files are allowed
        """
        self._load_permissions()

        if isinstance(phase, str):
            try:
                phase = PipelinePhase(phase)
            except ValueError:
                # Security: fail closed for unknown phases to prevent bypass
                return FileRestrictionResult.block(
                    message=f"Unknown phase '{phase}' - blocking by default",
                    role="unknown",
                    blocked_files=files,
                    blocked_reason="Security precaution: unknown phases are not allowed",
                )

        if not files:
            return FileRestrictionResult.allow("No files to check")

        restrictions = self._phase_file_restrictions.get(phase)
        if not restrictions:
            return FileRestrictionResult.allow(
                f"No phase file restrictions for phase: {phase.value}"
            )

        blocked_files: list[str] = []
        blocked_reasons: list[str] = []

        for file_path in files:
            allowed, reason = restrictions.is_file_allowed(file_path)
            if not allowed:
                blocked_files.append(file_path)
                if reason not in blocked_reasons:
                    blocked_reasons.append(reason)

        if blocked_files:
            return FileRestrictionResult.block(
                message=f"Phase '{phase.value}' cannot modify: {', '.join(blocked_files)}",
                role=f"phase:{phase.value}",
                blocked_files=blocked_files,
                blocked_reason="; ".join(blocked_reasons),
            )

        return FileRestrictionResult.allow(f"All files allowed for phase '{phase.value}'")

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


def reset_phase_filter() -> None:
    """Reset the global PhaseFilter instance.

    This clears the cached filter, causing the next call to get_phase_filter()
    to create a fresh instance. Useful for testing and when configuration
    files are updated.
    """
    global _filter
    _filter = None


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


def check_file_restrictions(role: str, files: list[str]) -> FileRestrictionResult:
    """Check if files are blocked for a role (convenience function).

    This function checks a list of files against the configured file restrictions
    for the specified role, following the same pattern as filter_operation().

    SECURITY: Implements role-based file access control. The implementer role is
    blocked from modifying contract files to prevent bypassing the SDLC contract
    system via direct git commits.

    Args:
        role: The role attempting the operation (e.g., "implementer")
        files: List of file paths being modified

    Returns:
        FileRestrictionResult indicating whether the files are allowed
    """
    return get_phase_filter().check_file_restrictions(role, files)


def check_phase_file_restrictions(
    phase: str | PipelinePhase, files: list[str]
) -> FileRestrictionResult:
    """Check if files are allowed for a pipeline phase (convenience function).

    This function checks a list of files against the phase-based file restrictions.
    Different phases have different allowed/blocked patterns to enforce proper
    separation of concerns in the SDLC pipeline.

    SECURITY: Enforces phase-based file access control:
    - refine/plan: Can only push .egg-state/ files (contracts, drafts, checkpoints, agent-outputs, reviews)
    - implement: Can push code but not .egg-state/ (except checkpoints and agent-outputs)
    - pr: Can push everything

    Args:
        phase: The current pipeline phase (string or PipelinePhase enum)
        files: List of file paths being modified

    Returns:
        FileRestrictionResult indicating whether the files are allowed
    """
    # Delegate to class method which blocks unknown phases (fail-closed for security)
    return get_phase_filter().check_phase_file_restrictions(phase, files)


def check_agent_restrictions(agent_role: str, files: list[str]) -> FileRestrictionResult:
    """Check if files are allowed for an agent role (convenience function).

    This function checks a list of files against the agent-role-specific
    file restrictions. Used during multi-agent orchestration to ensure
    each specialized agent only modifies files within its responsibility.

    SECURITY: Implements agent-role-based file access control:
    - Coder: source code and config, blocked from docs/tests/contracts
    - Tester: test files only
    - Documenter: documentation and markdown only
    - Integrator: handoff output only (read-only for everything else)

    Args:
        agent_role: The agent role (e.g., "coder", "tester")
        files: List of file paths being modified

    Returns:
        FileRestrictionResult indicating whether the files are allowed
    """
    try:
        from .agent_restrictions import validate_agent_push
    except ImportError:
        from agent_restrictions import validate_agent_push  # type: ignore[no-redef]

    result = validate_agent_push(agent_role, files)

    if result.allowed:
        return FileRestrictionResult.allow(result.message)
    else:
        return FileRestrictionResult.block(
            message=result.message,
            role=result.role,
            blocked_files=result.blocked_files,
            blocked_reason=f"Agent role '{result.role}' is not permitted to modify these files",
        )
