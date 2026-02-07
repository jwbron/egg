"""
Phase-based operation filtering for SDLC pipeline.

Each pipeline phase permits only specific operations. The gateway blocks
operations not allowed in the current phase, preventing issues like #202
where code was pushed during the planning phase.
"""

import fnmatch
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Add shared directory to path
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import EggLogger, get_logger
except ImportError:
    import logging

    # Fallback implementation when egg_logging is not available
    EggLogger = logging.Logger  # type: ignore[misc, assignment]

    def get_logger(
        name: str,
        level: int | str = logging.INFO,
        component: str | None = None,
    ) -> EggLogger:
        return logging.getLogger(name)  # type: ignore[return-value]


try:
    from egg_contracts import Contract, load_contract
    from egg_contracts.audit import log_blocked_operation
    from egg_contracts.models import PipelinePhase
except ImportError:
    Contract = None  # type: ignore
    load_contract = None  # type: ignore
    PipelinePhase = None  # type: ignore
    log_blocked_operation = None  # type: ignore


logger = get_logger("gateway.phase_filter")


@dataclass
class PhaseFilterResult:
    """Result of a phase filter check."""

    allowed: bool
    reason: str
    phase: str | None = None
    operation: str | None = None
    hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API response."""
        result = {"allowed": self.allowed, "reason": self.reason}
        if self.phase:
            result["phase"] = self.phase
        if self.operation:
            result["operation"] = self.operation
        if self.hint:
            result["hint"] = self.hint
        return result


class PhasePermissions:
    """
    Manages phase-based operation permissions.

    Loads permissions from .egg/phase-permissions.json and checks
    operations against the current pipeline phase.
    """

    def __init__(self, permissions_path: Path | str | None = None):
        """
        Initialize with permissions configuration.

        Args:
            permissions_path: Path to phase-permissions.json.
                            If None, uses default location.
        """
        self._permissions: dict[str, Any] = {}
        self._loaded = False

        self._permissions_path: Path | None
        if permissions_path:
            self._permissions_path = Path(permissions_path)
        else:
            # Default locations to check
            self._permissions_path = None
            self._default_paths: list[Path] = [
                Path.cwd() / ".egg" / "phase-permissions.json",
                Path(__file__).parent.parent / ".egg" / "phase-permissions.json",
                Path(os.environ.get("GITHUB_WORKSPACE", "")) / ".egg" / "phase-permissions.json",
            ]

    def _load_permissions(self) -> None:
        """Load permissions from file."""
        if self._loaded:
            return

        paths_to_try = [self._permissions_path] if self._permissions_path else self._default_paths

        for path in paths_to_try:
            if path and path.exists():
                try:
                    with open(path) as f:
                        self._permissions = json.load(f)
                    self._loaded = True
                    logger.debug("Loaded phase permissions", path=str(path))
                    return
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning("Failed to load permissions", path=str(path), error=str(e))

        # Use default permissive permissions if file not found
        logger.warning("Phase permissions not found, using defaults")
        self._permissions = {"schemaVersion": "1.0", "phases": {}}
        self._loaded = True

    def get_phase_config(self, phase: str) -> dict[str, Any] | None:
        """
        Get configuration for a specific phase.

        Args:
            phase: Phase name (refine, plan, implement, pr)

        Returns:
            Phase configuration dict or None if not found
        """
        self._load_permissions()
        phases = self._permissions.get("phases", {})
        if isinstance(phases, dict):
            result: dict[str, Any] | None = phases.get(phase)
            return result
        return None

    def check_operation(
        self,
        phase: str,
        operation_type: str,
        operation: str,
    ) -> PhaseFilterResult:
        """
        Check if an operation is allowed in the current phase.

        Args:
            phase: Current pipeline phase
            operation_type: Type of operation (git, gh, egg-contract)
            operation: Full operation string (e.g., "push origin main")

        Returns:
            PhaseFilterResult indicating if operation is allowed
        """
        self._load_permissions()

        phase_config = self.get_phase_config(phase)
        if not phase_config:
            # If phase not configured, allow by default for backwards compatibility
            # with repos that don't have phase-permissions.json yet.
            #
            # Design note: This is intentionally different from the behavior when
            # a phase IS configured but the operation isn't in the allowed list.
            # - Unconfigured phase = permissive (no restrictions defined)
            # - Configured phase with missing operation = restrictive (allowlist model)
            logger.debug(
                "Phase not configured, allowing operation",
                phase=phase,
                operation_type=operation_type,
            )
            return PhaseFilterResult(
                allowed=True,
                reason=f"Phase '{phase}' not configured - operation allowed",
                phase=phase,
                operation=operation,
            )

        # Check if explicitly blocked
        blocked_ops = phase_config.get("blocked", [])
        for blocked in blocked_ops:
            if blocked.get("type") != operation_type:
                continue
            pattern = blocked.get("pattern", "")
            if self._matches_pattern(operation, pattern):
                description = blocked.get("description", "Operation blocked")
                logger.info(
                    "Operation blocked by phase filter",
                    phase=phase,
                    operation_type=operation_type,
                    operation=operation,
                    pattern=pattern,
                )
                return PhaseFilterResult(
                    allowed=False,
                    reason=f"Operation blocked in {phase} phase: {description}",
                    phase=phase,
                    operation=operation,
                    hint=f"This operation is not permitted until the {phase} phase is complete.",
                )

        # Check if explicitly allowed
        allowed_ops = phase_config.get("allowed", [])
        for allowed in allowed_ops:
            if allowed.get("type") != operation_type:
                continue
            pattern = allowed.get("pattern", "")
            if self._matches_pattern(operation, pattern):
                logger.debug(
                    "Operation allowed by phase filter",
                    phase=phase,
                    operation_type=operation_type,
                    operation=operation,
                )
                return PhaseFilterResult(
                    allowed=True,
                    reason="Operation allowed in current phase",
                    phase=phase,
                    operation=operation,
                )

        # If not explicitly allowed or blocked, block by default for safety.
        # This implements an allowlist model for configured phases: only operations
        # explicitly listed in "allowed" are permitted.
        logger.info(
            "Operation not in allowed list, blocking",
            phase=phase,
            operation_type=operation_type,
            operation=operation,
        )
        return PhaseFilterResult(
            allowed=False,
            reason=f"Operation not permitted in {phase} phase",
            phase=phase,
            operation=operation,
            hint="This operation is not in the allowed list for the current phase.",
        )

    def _matches_pattern(self, operation: str, pattern: str) -> bool:
        """
        Check if an operation matches a pattern.

        Patterns support:
        - Exact match: "push origin main"
        - Wildcard suffix: "push *" (matches "push", "push origin", etc.)
        - Single command: "status"
        """
        # Handle exact match
        if pattern == operation:
            return True

        # Handle wildcard patterns
        if "*" in pattern:
            # Special case: "cmd *" should also match bare "cmd" (no args)
            if pattern.endswith(" *"):
                base_cmd = pattern[:-2]  # Remove " *"
                if operation == base_cmd:
                    return True
            return fnmatch.fnmatch(operation, pattern)

        # Handle prefix match (pattern is a prefix of operation)
        if operation.startswith(pattern + " ") or operation == pattern:
            return True

        return False

    def get_exit_requirement(self, phase: str) -> str | None:
        """
        Get the exit requirement for a phase.

        Args:
            phase: Phase name

        Returns:
            Exit requirement (human, reviewer, auto) or None
        """
        self._load_permissions()
        phase_config = self.get_phase_config(phase)
        if phase_config:
            return phase_config.get("exit_requires")
        return None


# Global instance
_phase_permissions: PhasePermissions | None = None


def get_phase_permissions() -> PhasePermissions:
    """Get the global PhasePermissions instance."""
    global _phase_permissions
    if _phase_permissions is None:
        _phase_permissions = PhasePermissions()
    return _phase_permissions


def check_phase_operation(
    repo_root: Path | str,
    issue_number: int,
    operation_type: str,
    operation: str,
) -> PhaseFilterResult:
    """
    Check if an operation is allowed based on current contract phase.

    Args:
        repo_root: Path to repository root
        issue_number: GitHub issue number
        operation_type: Type of operation (git, gh, egg-contract)
        operation: Full operation string

    Returns:
        PhaseFilterResult
    """
    if load_contract is None:
        # Contracts not installed
        return PhaseFilterResult(
            allowed=True,
            reason="Contract system not available",
        )

    contract = load_contract(repo_root, issue_number)
    if not contract:
        # No contract = no phase restrictions
        return PhaseFilterResult(
            allowed=True,
            reason=f"No contract found for issue #{issue_number}",
        )

    current_phase = contract.currentPhase.value if contract.currentPhase else "refine"

    permissions = get_phase_permissions()
    result = permissions.check_operation(current_phase, operation_type, operation)

    # Log blocked operations to contract audit
    if not result.allowed and log_blocked_operation is not None:
        try:
            log_blocked_operation(
                contract,
                actor="phase_filter",
                role="system",
                field_path=f"{operation_type}.{operation}",
                attempted_value=operation,
                reason=result.reason,
            )
            from egg_contracts import save_contract

            save_contract(contract, repo_root)
        except Exception as e:
            logger.warning("Failed to log blocked operation", error=str(e))

    return result
