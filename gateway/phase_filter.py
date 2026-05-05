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
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

# Ensure the shared directory is on the path so egg_contracts is importable
# whether running in a Docker container (/app) or from the host (../../shared).
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from egg_contracts.models import PipelinePhase as PipelinePhase
from egg_restrictions.matchers import match_pattern
from egg_restrictions.patterns import AGENT_PATTERNS


class OperationType(StrEnum):
    """Types of operations that can be filtered."""

    GIT = "git"
    GH = "gh"
    EGG_CONTRACT = "egg-contract"


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

    Glob patterns (``**/*.md``, ``**/tests/``) are matched via the
    canonical ``egg_restrictions.matchers.match_pattern`` so the
    gateway's early-reject layer behaves consistently with the per-commit
    attribution layer (#1903) and the phase-level / role-definition
    layers (#2356).
    """

    role: str
    blocked_patterns: list[str] = field(default_factory=list)
    block_exempt_patterns: list[str] = field(default_factory=list)
    blocked_reason: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileRestriction:
        """Create FileRestriction from a dictionary."""
        return cls(
            role=data["role"],
            blocked_patterns=data.get("blocked_patterns", []),
            block_exempt_patterns=data.get("block_exempt_patterns", []),
            blocked_reason=data.get("blocked_reason", ""),
        )

    def is_file_blocked(self, file_path: str) -> bool:
        """Check if a file path is blocked by this restriction.

        Args:
            file_path: The file path to check (relative to repo root)

        Returns:
            True if the file matches any blocked pattern (and no exempt
            pattern), or if the path escapes the repo.
        """
        try:
            normalized = self._normalize_path(file_path)
        except ValueError:
            # Paths that escape the repository are always blocked
            return True

        if not any(match_pattern(normalized, p) for p in self.blocked_patterns):
            return False
        # Block-exempt carve-outs (e.g. ``.egg-state/agent-outputs/``
        # under coder's ``.egg-state/`` block).
        if any(match_pattern(normalized, p) for p in self.block_exempt_patterns):
            return False
        return True

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
            if match_pattern(normalized, pattern):
                return False, f"File '{file_path}' matches blocked pattern '{pattern}'"

        # If allowed_patterns is defined and non-empty, file must match one
        if self.allowed_patterns:
            # Special case: "*" means allow everything (short-circuit so
            # the PR phase doesn't pay per-file fnmatch cost).
            if "*" in self.allowed_patterns:
                return True, "All files allowed"

            for pattern in self.allowed_patterns:
                if match_pattern(normalized, pattern):
                    return (
                        True,
                        f"File '{file_path}' matches allowed pattern '{pattern}'",
                    )

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

        # Per-role file restrictions are now derived from
        # ``shared/egg_restrictions/patterns.py`` (#1903 — patterns.py is
        # the single source of truth). The legacy JSON ``file_restrictions``
        # key is ignored; a stale config that still carries it is logged
        # so operators see it during the transition.
        if "file_restrictions" in data:
            import warnings

            warnings.warn(
                "phase-permissions.json 'file_restrictions' key is ignored as of "
                "#1903 — per-role file boundaries are derived from "
                "shared/egg_restrictions/patterns.py. Remove the key from your "
                "config to silence this warning.",
                DeprecationWarning,
                stacklevel=2,
            )
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
                    Operation(OperationType.GIT, "push *", "Push state files to remote"),
                    Operation(
                        OperationType.EGG_CONTRACT,
                        "add-decision *",
                        "Create HITL decisions",
                    ),
                    Operation(OperationType.EGG_CONTRACT, "show *", "View contract state"),
                ],
                blocked_operations=[
                    Operation(
                        OperationType.GH,
                        "pr create*",
                        "Cannot create PRs during refine",
                    ),
                    Operation(
                        OperationType.GH,
                        "issue comment *",
                        "Agents cannot post comments to GitHub issues",
                    ),
                    Operation(
                        OperationType.GH,
                        "issue edit *",
                        "Agents cannot edit GitHub issues",
                    ),
                ],
                exit_requires="human",
            ),
            PipelinePhase.PLAN: PhasePermissions(
                allowed_operations=[
                    Operation(OperationType.GIT, "push *", "Push state files to remote"),
                    Operation(
                        OperationType.EGG_CONTRACT,
                        "add-decision *",
                        "Create HITL decisions",
                    ),
                    Operation(OperationType.EGG_CONTRACT, "show *", "View contract state"),
                ],
                blocked_operations=[
                    Operation(OperationType.GH, "pr create*", "Cannot create PRs during plan"),
                    Operation(
                        OperationType.GH,
                        "issue comment *",
                        "Agents cannot post comments to GitHub issues",
                    ),
                    Operation(
                        OperationType.GH,
                        "issue edit *",
                        "Agents cannot edit GitHub issues",
                    ),
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
                    Operation(
                        OperationType.GH,
                        "pr create*",
                        "Cannot create PRs until complete",
                    ),
                    Operation(
                        OperationType.GH,
                        "issue comment *",
                        "Agents cannot post comments to GitHub issues",
                    ),
                    Operation(
                        OperationType.GH,
                        "issue edit *",
                        "Agents cannot edit GitHub issues",
                    ),
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
        """Get the per-role file restrictions derived from ``AGENT_PATTERNS``.

        ``shared/egg_restrictions/patterns.py`` is the single source of
        truth for per-role file boundaries (#1903). Each
        ``AgentFilePattern`` is projected into a ``FileRestriction``
        carrying its blocklist + block-exempt patterns so the gateway's
        early-reject path matches the per-commit attribution path.

        ``blocked_reason`` is derived from the source
        ``AgentFilePattern.description`` (which describes the role's
        positive scope — what it IS allowed to touch) so error messages
        stay role-specific. The negative-lead "Role 'X' cannot modify
        these files. <positive description>" wording was rejected in
        review as misleading on first read (the positive description
        sounded like the disallowed scope). The current "is restricted
        to" framing keeps the description's positive framing readable
        after the negative lead.
        """
        restrictions: list[FileRestriction] = []
        for role, pattern in AGENT_PATTERNS.items():
            if not pattern.blocked_patterns:
                continue
            if pattern.description:
                blocked_reason = (
                    f"Role '{role}' is restricted to: {pattern.description} "
                    "(see shared/egg_restrictions/patterns.py)."
                )
            else:
                blocked_reason = (
                    f"Role '{role}' is not permitted to modify these files; "
                    "see shared/egg_restrictions/patterns.py."
                )
            restrictions.append(
                FileRestriction(
                    role=role,
                    blocked_patterns=list(pattern.blocked_patterns),
                    block_exempt_patterns=list(pattern.block_exempt_patterns),
                    blocked_reason=blocked_reason,
                )
            )
        return restrictions

    def _get_default_phase_file_restrictions(
        self,
    ) -> dict[PipelinePhase, PhaseFileRestriction]:
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
                    ".egg-state/agent-anchors/*",
                ],
                description="Refine phase can only push contracts, analysis drafts, checkpoints, agent outputs, reviews, and agent anchors",
            ),
            PipelinePhase.PLAN: PhaseFileRestriction(
                allowed_patterns=[
                    ".egg-state/contracts/*",
                    ".egg-state/drafts/*plan*",
                    ".egg-state/checkpoints/*",
                    ".egg-state/agent-outputs/*",
                    ".egg-state/reviews/*",
                    ".egg-state/agent-anchors/*",
                ],
                description="Plan phase can only push contracts, plan drafts, checkpoints, agent outputs, reviews, and agent anchors",
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
                # .egg-state/agent-anchors/* is allowed (not in blocked_patterns)
                description="Implement phase can push code but not .egg-state/ (except checkpoints, agent-outputs, and agent-anchors)",
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

    def check_anchor_write_permission(
        self,
        file_path: str,
        agent_anchor_id: str | None,
    ) -> FileRestrictionResult:
        """Check if an anchor file write is permitted for the given agent.

        Agents can only write to their own anchor file. The agent's anchor ID
        is set via the AGENT_ANCHOR_ID environment variable in the container.

        Args:
            file_path: The file path being written (relative to repo root)
            agent_anchor_id: The agent's anchor ID from AGENT_ANCHOR_ID env var

        Returns:
            FileRestrictionResult indicating whether the write is allowed
        """
        # Only applies to anchor files
        normalized = posixpath.normpath(file_path)
        if normalized.startswith("./"):
            normalized = normalized[2:]

        if not normalized.startswith(".egg-state/agent-anchors/"):
            return FileRestrictionResult.allow("Not an anchor file")

        if not agent_anchor_id:
            return FileRestrictionResult.block(
                message=f"No AGENT_ANCHOR_ID set — cannot write anchor file '{file_path}'",
                role="unknown",
                blocked_files=[file_path],
                blocked_reason="AGENT_ANCHOR_ID environment variable is not set",
            )

        # Extract expected filename from the path
        expected_filename = f"{agent_anchor_id}.json"
        actual_filename = posixpath.basename(normalized)

        if actual_filename != expected_filename:
            return FileRestrictionResult.block(
                message=f"Agent '{agent_anchor_id}' cannot write to anchor file '{actual_filename}'",
                role=agent_anchor_id,
                blocked_files=[file_path],
                blocked_reason=f"Agent can only write to its own anchor file ({expected_filename})",
            )

        return FileRestrictionResult.allow(
            f"Agent '{agent_anchor_id}' is permitted to write to its own anchor file"
        )


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


def check_agent_restrictions(
    agent_role: str,
    files: list[str],
) -> FileRestrictionResult:
    """Check if files are allowed for an agent role (convenience function).

    This function checks a list of files against the agent-role-specific
    file restrictions. Used during multi-agent orchestration to ensure
    each specialized agent only modifies files within its responsibility.

    SECURITY: Implements agent-role-based file access control:
    - Coder: source code and config, blocked from docs/tests/contracts
    - Tester: test files only
    - Documenter: documentation and markdown only

    Args:
        agent_role: The agent role (e.g., "coder", "tester")
        files: List of file paths being modified

    Returns:
        FileRestrictionResult indicating whether the files are allowed
    """
    try:
        from .agent_restrictions import validate_agent_push
    except ImportError:
        from agent_restrictions import validate_agent_push  # type: ignore[no-redef, import-untyped]  # noqa: I001

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


def check_anchor_write_permission(
    file_path: str,
    agent_anchor_id: str | None,
) -> FileRestrictionResult:
    """Check anchor file write permission (convenience function)."""
    return get_phase_filter().check_anchor_write_permission(file_path, agent_anchor_id)
