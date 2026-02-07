"""
File Protection Policy - Prevents agent from modifying protected files/lines.

This module provides protection for sensitive files and specific line ranges
within files. It validates git diffs before allowing pushes to proceed.

Configuration is loaded from repositories.yaml under the 'protected_files' key.

Example configuration:
    protected_files:
      - path: ".coveragerc"
        reason: "Test coverage configuration"
      - path: "pyproject.toml"
        lines: [50-55]
        reason: "Coverage threshold settings"
      - path: "gateway/policy.py"
        lines: [742-761]
        level: "immutable"
        reason: "Critical security policy"

Protection levels:
    - immutable (default): Block push entirely
    - warn_on_pr: Allow push, but add warning comment to PR
    - log_only: Allow push, but log for audit

Usage:
    from file_policy import (
        FileProtectionPolicy,
        get_file_protection_policy,
        ProtectionViolation,
    )

    policy = get_file_protection_policy()
    result = policy.check_diff_for_violations(repo, diff_output)
    if not result.allowed:
        # Block push, return error with result.violations
        pass
"""

import fnmatch
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Add shared directory to path for egg_logging
_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists():
    sys.path.insert(0, str(_shared_path))
from egg_logging import get_logger

logger = get_logger("gateway.file_policy")


@dataclass
class ProtectedFileConfig:
    """Configuration for a single protected file or pattern."""

    path: str  # File path or glob pattern
    lines: list[tuple[int, int]] | None = None  # List of (start, end) line ranges
    level: str = "immutable"  # immutable | warn_on_pr | log_only
    reason: str = ""  # Human-readable reason for protection


@dataclass
class ProtectionViolation:
    """A single protection rule violation."""

    file: str
    lines: list[int] | None  # Specific lines modified, or None for whole file
    rule: ProtectedFileConfig
    reason: str


@dataclass
class ProtectionCheckResult:
    """Result of checking diff against protected file rules."""

    allowed: bool
    violations: list[ProtectionViolation] = field(default_factory=list)
    warnings: list[ProtectionViolation] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API response."""
        return {
            "allowed": self.allowed,
            "violations": [
                {
                    "file": v.file,
                    "lines": v.lines,
                    "reason": v.reason,
                    "level": v.rule.level,
                }
                for v in self.violations
            ],
            "warnings": [
                {
                    "file": w.file,
                    "lines": w.lines,
                    "reason": w.reason,
                    "level": w.rule.level,
                }
                for w in self.warnings
            ],
        }


def parse_line_ranges(lines_config: list[str | int] | None) -> list[tuple[int, int]] | None:
    """Parse line range configuration into list of (start, end) tuples.

    Supports formats:
        - Single line: 50 or "50"
        - Range: "50-55"
        - Mixed: [50, "52-55", 60]

    Args:
        lines_config: List of line numbers or ranges from config

    Returns:
        List of (start, end) tuples (1-indexed, inclusive), or None if no lines specified
    """
    if not lines_config:
        return None

    ranges = []
    for item in lines_config:
        if isinstance(item, int):
            ranges.append((item, item))
        elif isinstance(item, str):
            if "-" in item:
                parts = item.split("-", 1)
                try:
                    start = int(parts[0].strip())
                    end = int(parts[1].strip())
                    ranges.append((start, end))
                except ValueError:
                    logger.warning(f"Invalid line range format: {item}")
            else:
                try:
                    line = int(item.strip())
                    ranges.append((line, line))
                except ValueError:
                    logger.warning(f"Invalid line number format: {item}")

    return ranges if ranges else None


def parse_unified_diff(diff_output: str) -> dict[str, list[int]]:
    """Parse unified diff output to extract modified files and line numbers.

    Args:
        diff_output: Output from git diff in unified format

    Returns:
        Dictionary mapping file paths to lists of modified line numbers (in new file)
    """
    file_changes: dict[str, list[int]] = {}
    current_file = None
    current_line = 0

    for line in diff_output.split("\n"):
        # Detect new file in diff
        if line.startswith("+++ b/"):
            current_file = line[6:]  # Remove "+++ b/" prefix
            if current_file not in file_changes:
                file_changes[current_file] = []
        # Detect deleted file
        elif line.startswith("+++ /dev/null"):
            # File was deleted, use the --- line for filename
            pass
        # Detect hunk header: @@ -old_start,old_count +new_start,new_count @@
        elif line.startswith("@@"):
            match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if match:
                current_line = int(match.group(1))
        # Added or modified line (not the +++ header)
        elif line.startswith("+") and not line.startswith("+++"):
            if current_file is not None:
                file_changes[current_file].append(current_line)
            current_line += 1
        # Context line (unchanged)
        elif line.startswith(" "):
            current_line += 1
        # Removed line doesn't affect new file line numbers
        elif line.startswith("-") and not line.startswith("---"):
            pass

    return file_changes


def file_matches_pattern(filepath: str, pattern: str) -> bool:
    """Check if a filepath matches a glob pattern.

    Args:
        filepath: File path to check
        pattern: Glob pattern (supports *, **, ?)

    Returns:
        True if the file matches the pattern
    """
    # Handle exact matches
    if pattern == filepath:
        return True

    # Handle glob patterns
    return fnmatch.fnmatch(filepath, pattern)


def lines_overlap(modified_lines: list[int], protected_ranges: list[tuple[int, int]]) -> list[int]:
    """Find which modified lines fall within protected ranges.

    Args:
        modified_lines: List of line numbers that were modified
        protected_ranges: List of (start, end) tuples defining protected ranges

    Returns:
        List of modified line numbers that are within protected ranges
    """
    violations = []
    for line in modified_lines:
        for start, end in protected_ranges:
            if start <= line <= end:
                violations.append(line)
                break
    return violations


class FileProtectionPolicy:
    """Policy engine for file/line protection."""

    def __init__(self, protected_files: list[ProtectedFileConfig] | None = None):
        """Initialize with optional list of protected file configs.

        Args:
            protected_files: List of protection rules. If None, loads from config.
        """
        self.protected_files = protected_files if protected_files is not None else []

    def add_rule(self, rule: ProtectedFileConfig) -> None:
        """Add a protection rule."""
        self.protected_files.append(rule)

    def check_file_modifications(
        self, file_changes: dict[str, list[int]]
    ) -> ProtectionCheckResult:
        """Check if file modifications violate protection rules.

        Args:
            file_changes: Dictionary mapping file paths to lists of modified line numbers

        Returns:
            ProtectionCheckResult with violations and warnings
        """
        violations = []
        warnings = []

        for filepath, modified_lines in file_changes.items():
            for rule in self.protected_files:
                if not file_matches_pattern(filepath, rule.path):
                    continue

                # File matches pattern - check if modification is allowed
                if rule.lines is None:
                    # Entire file is protected
                    violation = ProtectionViolation(
                        file=filepath,
                        lines=None,
                        rule=rule,
                        reason=rule.reason or f"File '{filepath}' is protected",
                    )
                else:
                    # Only specific lines are protected
                    overlapping = lines_overlap(modified_lines, rule.lines)
                    if not overlapping:
                        continue  # No overlap, no violation
                    violation = ProtectionViolation(
                        file=filepath,
                        lines=overlapping,
                        rule=rule,
                        reason=rule.reason
                        or f"Lines {overlapping} in '{filepath}' are protected",
                    )

                # Categorize by protection level
                if rule.level == "immutable":
                    violations.append(violation)
                elif rule.level == "warn_on_pr":
                    warnings.append(violation)
                elif rule.level == "log_only":
                    logger.info(
                        "Protected file modification (log_only)",
                        file=filepath,
                        lines=violation.lines,
                        reason=rule.reason,
                    )

        # Allowed only if no immutable violations
        allowed = len(violations) == 0

        return ProtectionCheckResult(
            allowed=allowed,
            violations=violations,
            warnings=warnings,
        )

    def check_diff_for_violations(
        self, diff_output: str
    ) -> ProtectionCheckResult:
        """Check a git diff output for protection violations.

        Args:
            diff_output: Output from git diff in unified format

        Returns:
            ProtectionCheckResult with violations and warnings
        """
        file_changes = parse_unified_diff(diff_output)
        return self.check_file_modifications(file_changes)

    def get_diff_between_commits(
        self, repo_path: str, base_commit: str, head_commit: str
    ) -> str:
        """Get the diff between two commits.

        Args:
            repo_path: Path to the git repository
            base_commit: Base commit (e.g., "origin/main")
            head_commit: Head commit (e.g., "HEAD")

        Returns:
            Unified diff output as string
        """
        try:
            result = subprocess.run(
                ["git", "diff", base_commit, head_commit],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return result.stdout
        except subprocess.TimeoutExpired:
            logger.error("git diff timed out", repo_path=repo_path)
            return ""
        except subprocess.SubprocessError as e:
            logger.error("git diff failed", repo_path=repo_path, error=str(e))
            return ""


def load_protected_files_from_config(repo: str) -> list[ProtectedFileConfig]:
    """Load protected files configuration for a repository.

    Args:
        repo: Repository in "owner/repo" format

    Returns:
        List of ProtectedFileConfig objects
    """
    try:
        # Import here to avoid circular imports
        _config_path = Path(__file__).parent.parent / "config"
        if _config_path.exists() and str(_config_path) not in sys.path:
            sys.path.insert(0, str(_config_path))
        from repo_config import _load_config
    except ImportError:
        logger.warning("Could not import repo_config, no protected files loaded")
        return []

    try:
        config = _load_config()
    except FileNotFoundError:
        logger.debug("No repositories.yaml found, no protected files loaded")
        return []

    # Get global protected_files
    global_protected = config.get("protected_files", [])

    # Get per-repo protected_files
    repo_settings = config.get("repo_settings", {})
    repo_protected = []
    repo_lower = repo.lower()
    for configured_repo, settings in repo_settings.items():
        if configured_repo.lower() == repo_lower:
            repo_protected = settings.get("protected_files", [])
            break

    # Combine both lists
    all_protected = global_protected + repo_protected

    # Parse into ProtectedFileConfig objects
    configs = []
    for item in all_protected:
        if isinstance(item, str):
            # Simple string format: just the path
            configs.append(ProtectedFileConfig(path=item))
        elif isinstance(item, dict):
            # Full format with all options
            configs.append(
                ProtectedFileConfig(
                    path=item.get("path", ""),
                    lines=parse_line_ranges(item.get("lines")),
                    level=item.get("level", "immutable"),
                    reason=item.get("reason", ""),
                )
            )

    return configs


# Global policy instance
_policy: FileProtectionPolicy | None = None


def get_file_protection_policy(repo: str | None = None) -> FileProtectionPolicy:
    """Get the file protection policy instance.

    Args:
        repo: Optional repository for repo-specific rules

    Returns:
        FileProtectionPolicy instance with loaded rules
    """
    global _policy

    # If repo is specified, create a new policy with repo-specific rules
    if repo:
        rules = load_protected_files_from_config(repo)
        return FileProtectionPolicy(rules)

    # Otherwise, return a cached global policy (no repo-specific rules)
    if _policy is None:
        _policy = FileProtectionPolicy([])
    return _policy


def reset_policy_cache() -> None:
    """Reset the global policy cache. For testing only."""
    global _policy
    _policy = None
