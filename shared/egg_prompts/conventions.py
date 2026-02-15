"""
2-tier conventions and rules file loading.

Provides the core lookup pattern used across all prompt builders:
1. Repo-specific override: .egg/{name}-conventions.md (or -rules.md)
2. Default: action/{name}-conventions.md (bundled with egg)

This ensures repo owners can customize agent behavior while providing
sensible defaults out of the box.
"""

from pathlib import Path


def _find_project_root() -> Path:
    """Find the egg project root directory.

    Looks for the action/ directory relative to this module's location,
    walking up the directory tree.

    Returns:
        Path to the project root containing action/.
    """
    current = Path(__file__).resolve().parent
    for _ in range(5):  # Don't walk too far
        if (current / "action").is_dir():
            return current
        current = current.parent
    # Fallback: assume standard layout
    return Path(__file__).resolve().parent.parent.parent


def load_conventions(
    name: str,
    repo_path: Path | str | None = None,
) -> str:
    """Load a conventions file with 2-tier lookup.

    Lookup order:
    1. Repo-specific: {repo_path}/.egg/{name}-conventions.md
    2. Default: {project_root}/action/{name}-conventions.md

    Args:
        name: Base name (e.g., "review", "autofixer", "conflict").
        repo_path: Path to the target repository. If provided, checks
            for repo-specific overrides first.

    Returns:
        Conventions text, or empty string if not found.
    """
    filename = f"{name}-conventions.md"

    # Tier 1: Repo-specific override
    if repo_path:
        repo_file = Path(repo_path) / ".egg" / filename
        if repo_file.exists():
            return repo_file.read_text()

    # Tier 2: Default from action/ directory
    project_root = _find_project_root()
    default_file = project_root / "action" / filename
    if default_file.exists():
        return default_file.read_text()

    return ""


def load_rules(
    name: str,
    repo_path: Path | str | None = None,
) -> str:
    """Load a rules file with 2-tier lookup.

    Lookup order:
    1. Repo-specific: {repo_path}/.egg/{name}-rules.md
    2. Default: inline (caller provides default)

    Unlike conventions, rules files don't have a bundled default — the caller
    provides the default content as an inline heredoc.

    Args:
        name: Base name (e.g., "review", "conflict").
        repo_path: Path to the target repository. If provided, checks
            for repo-specific overrides first.

    Returns:
        Rules text from repo-specific file, or empty string if not found.
        Caller should provide inline default when empty string is returned.
    """
    filename = f"{name}-rules.md"

    # Tier 1: Repo-specific override
    if repo_path:
        repo_file = Path(repo_path) / ".egg" / filename
        if repo_file.exists():
            return repo_file.read_text()

    return ""
