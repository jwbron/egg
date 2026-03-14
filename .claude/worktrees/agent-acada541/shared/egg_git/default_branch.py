"""Default branch detection for git repositories."""

import subprocess
from pathlib import Path


def get_default_branch(repo_path: Path | str) -> str:
    """Detect the default branch for a repository.

    Tries to determine the default branch by:
    1. Checking git remote show origin (most reliable)
    2. Falling back to checking for common branch names (main, master)
    3. Defaulting to "main" if nothing else works

    Args:
        repo_path: Path to the repository

    Returns:
        The default branch name (e.g., "main" or "master")
    """
    repo_path = Path(repo_path) if isinstance(repo_path, str) else repo_path

    # Try to get the default branch from the remote
    try:
        result = subprocess.run(
            ["git", "remote", "show", "origin"],
            check=False,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "HEAD branch:" in line:
                    return line.split(":")[-1].strip()
    except (subprocess.TimeoutExpired, subprocess.SubprocessError):
        pass

    # Fallback: check which common branches exist
    try:
        result = subprocess.run(
            ["git", "branch", "-r"],
            check=False,
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            branches = result.stdout
            if "origin/master" in branches:
                return "master"
            if "origin/main" in branches:
                return "main"
    except subprocess.SubprocessError:
        pass

    # Ultimate fallback
    return "main"
