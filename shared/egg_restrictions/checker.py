"""
Agent file access checking logic.

Provides functions to validate whether an agent role is allowed to modify
a set of files, used by the gateway during git push validation and by
other components that need to enforce agent restrictions.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .patterns import AgentFilePattern, get_agent_pattern_for_repo


def get_agent_pattern(role: str, repo: str | None = None) -> AgentFilePattern | None:
    """Get the file pattern for an agent role.

    Args:
        role: The agent role identifier
        repo: Optional repository in ``owner/repo`` format. When set, the
            returned pattern reflects per-repo overrides from
            ``repositories.yaml`` (#2528). When ``None``, returns the
            global default pattern for the role.

    Returns:
        AgentFilePattern for the role, or None if not found
    """
    return get_agent_pattern_for_repo(role.lower(), repo)


def check_agent_file_access(
    role: str,
    files: list[str],
    repo: str | None = None,
) -> tuple[bool, list[str], str]:
    """Check if an agent can modify the given files.

    Args:
        role: The agent role identifier
        files: List of file paths being modified
        repo: Optional repository for per-repo pattern overrides (#2528).

    Returns:
        Tuple of (allowed, blocked_files, reason)
    """
    pattern = get_agent_pattern(role, repo=repo)
    if pattern is None:
        # Unknown role - deny all (deny-by-default)
        return False, files, f"Unknown agent role '{role}' \u2014 access denied (deny-by-default)"

    blocked_files = []
    for file_path in files:
        if not pattern.can_write(file_path):
            blocked_files.append(file_path)

    if blocked_files:
        return (
            False,
            blocked_files,
            f"Agent role '{role}' cannot modify: {', '.join(blocked_files[:5])}"
            + (f" and {len(blocked_files) - 5} more" if len(blocked_files) > 5 else ""),
        )

    return True, [], "All files allowed for agent role"


@dataclass
class AgentRestrictionResult:
    """Result of checking agent file restrictions."""

    allowed: bool
    message: str
    role: str
    blocked_files: list[str] = field(default_factory=list)

    @classmethod
    def allow(cls, role: str, message: str = "Files allowed") -> AgentRestrictionResult:
        """Create an allowed result."""
        return cls(allowed=True, message=message, role=role)

    @classmethod
    def block(
        cls,
        role: str,
        blocked_files: list[str],
        message: str,
    ) -> AgentRestrictionResult:
        """Create a blocked result."""
        return cls(
            allowed=False,
            message=message,
            role=role,
            blocked_files=blocked_files,
        )


def validate_agent_push(
    role: str,
    files: list[str],
    repo: str | None = None,
) -> AgentRestrictionResult:
    """Validate that an agent can push changes to the given files.

    This is the main entry point for gateway validation of agent pushes.

    Args:
        role: The agent role identifier (e.g., "coder", "tester")
        files: List of file paths being modified in the push
        repo: Optional repository for per-repo pattern overrides (#2528).

    Returns:
        AgentRestrictionResult indicating whether the push is allowed
    """
    if not role:
        return AgentRestrictionResult.allow("", "No agent role specified")

    if not files:
        return AgentRestrictionResult.allow(role, "No files to validate")

    allowed, blocked_files, reason = check_agent_file_access(role, files, repo=repo)

    if allowed:
        return AgentRestrictionResult.allow(role, reason)
    else:
        return AgentRestrictionResult.block(role, blocked_files, reason)
