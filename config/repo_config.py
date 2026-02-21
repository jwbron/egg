#!/usr/bin/env python3
"""
Repository Configuration Module

Provides programmatic access to repository configuration defined in repositories.yaml.
This is the single source of truth for which repos egg has access to.

Usage:
    from config.repo_config import (
        get_github_username,
        get_writable_repos,
        is_writable_repo,
        get_default_reviewer,
        get_auth_mode,
        is_user_mode_repo,
        get_user_mode_config,
    )

    # Get configured GitHub username
    username = get_github_username()

    # Get all writable repos
    repos = get_writable_repos()

    # Check if a specific repo is writable
    if is_writable_repo(f"{username}/egg"):
        # Can push changes, create PRs, etc.
        pass

    # Get default reviewer for PRs
    reviewer = get_default_reviewer()
"""

import os
import time
from pathlib import Path
from typing import Any, cast

import yaml


def _get_config_path() -> Path:
    """Get the path to repositories.yaml config file.

    Search order:
    1. Environment variable EGG_REPO_CONFIG (explicit override)
    2. Host config: ~/.config/egg/repositories.yaml (preferred location)
    3. Container mount: ~/repos/egg/config/repositories.yaml
    """
    # Try environment variable first (allows explicit override)
    env_path = os.environ.get("EGG_REPO_CONFIG")
    if env_path:
        env_config = Path(env_path)
        if env_config.exists():
            return env_config

    # Try host config location (preferred - set up by setup.py)
    host_config = Path.home() / ".config" / "egg" / "repositories.yaml"
    if host_config.exists():
        return host_config

    # Try container mount path (when running inside egg container)
    container_config = Path.home() / "repos" / "egg" / "config" / "repositories.yaml"
    if container_config.exists():
        return container_config

    raise FileNotFoundError(
        f"Could not find repositories.yaml. Checked:\n"
        f"  - EGG_REPO_CONFIG env var\n"
        f"  - {host_config} (host config)\n"
        f"  - {container_config} (container mount)\n"
        f"\nRun ./setup.py to create the configuration."
    )


def _load_config() -> dict[str, Any]:
    """Load and return the repository configuration."""
    config_path = _get_config_path()
    with config_path.open() as f:
        return cast(dict[str, Any], yaml.safe_load(f))


def get_github_username() -> str:
    """
    Get the configured GitHub username.

    This is used to construct repo names and as the default reviewer.
    Set via 'egg --setup' or directly in repositories.yaml.

    Returns:
        GitHub username string

    Raises:
        ValueError: If github_username is not configured
    """
    config = _load_config()
    username = config.get("github_username")
    if not username:
        raise ValueError(
            "github_username not configured in repositories.yaml. "
            "Run ./setup.py to configure, or add 'github_username: your-username' to ~/.config/egg/repositories.yaml."
        )
    return cast(str, username)


def get_writable_repos() -> list[str]:
    """
    Get list of repositories where egg has write access.

    These are repos where egg can:
    - Respond to PR comments
    - Push code changes
    - Create PRs
    - Implement fixes for failed CI checks

    Returns:
        List of repo strings in "owner/repo" format
    """
    config = _load_config()
    return cast(list[str], config.get("writable_repos", []))


def get_readable_repos() -> list[str]:
    """
    Get list of repositories where egg has read-only access.

    These are repos where egg can:
    - Sync and analyze PRs, comments, and check failures
    - Send Slack notifications with feedback/analysis

    egg CANNOT:
    - Push code, create PRs, post comments
    - Make any modifications to these repos

    Read-only repos only require a GitHub PAT with read access.

    Returns:
        List of repo strings in "owner/repo" format
    """
    config = _load_config()
    return cast(list[str], config.get("readable_repos", []))


def is_writable_repo(repo: str) -> bool:
    """
    Check if a repository is in the writable repos list.

    Args:
        repo: Repository in "owner/repo" format

    Returns:
        True if egg has write access to this repo
    """
    writable = get_writable_repos()
    # Normalize comparison (case-insensitive)
    repo_lower = repo.lower()
    return any(r.lower() == repo_lower for r in writable)


def is_readable_repo(repo: str) -> bool:
    """
    Check if a repository is in the readable repos list.

    Args:
        repo: Repository in "owner/repo" format

    Returns:
        True if egg has read-only access to this repo
    """
    readable = get_readable_repos()
    # Normalize comparison (case-insensitive)
    repo_lower = repo.lower()
    return any(r.lower() == repo_lower for r in readable)


def get_repo_access_level(repo: str) -> str:
    """
    Get the access level for a repository.

    Args:
        repo: Repository in "owner/repo" format

    Returns:
        One of: "writable", "readable", or "none"
    """
    if is_writable_repo(repo):
        return "writable"
    if is_readable_repo(repo):
        return "readable"
    return "none"


def get_default_reviewer() -> str:
    """
    Get the default reviewer for PRs created by egg.

    Falls back to github_username if default_reviewer is not explicitly set.

    Returns:
        GitHub username of default reviewer
    """
    config = _load_config()
    reviewer = config.get("default_reviewer")
    if reviewer:
        return cast(str, reviewer)
    # Fall back to github_username
    return get_github_username()


def get_sync_config() -> dict[str, Any]:
    """
    Get GitHub sync configuration.

    Returns:
        Dictionary with sync settings:
        - sync_all_prs: bool - whether to sync all PRs or just user's
        - sync_interval_minutes: int - sync interval in minutes
    """
    config = _load_config()
    return cast(
        dict[str, Any],
        config.get("github_sync", {"sync_all_prs": True, "sync_interval_minutes": 5}),
    )


def get_repos_for_sync() -> list[str]:
    """
    Get list of repositories to sync from GitHub.

    Returns both writable and readable repos, as both need
    to be monitored for activity.

    Returns:
        List of repo strings in "owner/repo" format
    """
    writable = get_writable_repos()
    readable = get_readable_repos()
    # Combine and deduplicate, preserving order (writable repos first)
    # Note: If a repo appears in both lists, it's treated as writable
    all_repos = list(dict.fromkeys(writable + readable))
    return all_repos


def get_repo_setting(repo: str, setting: str, default: Any | None = None) -> Any:
    """
    Get a specific setting for a repository.

    Args:
        repo: Repository in "owner/repo" format
        setting: Setting name to retrieve
        default: Default value if setting not found

    Returns:
        Setting value, or default if not found
    """
    config = _load_config()
    repo_settings = config.get("repo_settings", {})
    # Normalize repo name for case-insensitive lookup
    repo_lower = repo.lower()
    for configured_repo, settings in repo_settings.items():
        if configured_repo.lower() == repo_lower:
            return settings.get(setting, default)
    return default


def should_restrict_to_configured_users(repo: str) -> bool:
    """
    Check if a repository is configured to only auto-respond to configured users.

    When enabled, egg will only respond to comments/PRs from:
    - bot_username (the bot's own identity)
    - github_username (the configured owner/user)

    Comments and PRs from other users will be ignored.

    Args:
        repo: Repository in "owner/repo" format

    Returns:
        True if auto-responses should be restricted to configured users only
    """
    return cast(bool, get_repo_setting(repo, "restrict_to_configured_users", False))


def should_disable_auto_fix(repo: str) -> bool:
    """
    Check if auto-fix for check failures is disabled for a repository.

    When enabled, egg will NOT automatically attempt to fix failing CI checks.
    This is useful for repos where:
    - GitHub Actions minutes are limited/exhausted
    - Auto-fix attempts are not desired
    - The repo should only be monitored for comments/reviews

    Other functionality (comments, reviews, merge conflicts) is unaffected.

    Args:
        repo: Repository in "owner/repo" format

    Returns:
        True if auto-fix should be disabled for this repo
    """
    return cast(bool, get_repo_setting(repo, "disable_auto_fix", False))


try:
    from egg_config.validators import validate_checks
except ImportError:

    def validate_checks(checks: list[Any]) -> list[dict[str, str]]:
        """Validate and normalize a list of check command entries.

        Filters out malformed entries and coerces values to strings.

        Args:
            checks: Raw list of check entries (e.g. from YAML or JSON).

        Returns:
            List of {"name": "...", "command": "..."} dicts with only
            valid entries retained.
        """
        if not isinstance(checks, list):
            return []
        return [
            {"name": str(c["name"]), "command": str(c["command"])}
            for c in checks
            if isinstance(c, dict) and "name" in c and "command" in c
        ]


def get_checkpoint_repo(repo: str) -> str | None:
    """Get the checkpoint destination repo for a repository.

    When set, checkpoints are pushed to a separate repository instead of the
    same repo being worked on. This is useful for privacy, keeping checkpoint
    data (session transcripts, tool calls) out of the source repo's history.

    Args:
        repo: Repository in "owner/repo" format

    Returns:
        Checkpoint repo in "owner/repo" format, or None to use the same repo.
    """
    return cast(str | None, get_repo_setting(repo, "checkpoint_repo", None))


_checkpoint_repos_cache: tuple[float, frozenset[str]] | None = None
_CHECKPOINT_REPOS_TTL = 60  # seconds


def get_all_checkpoint_repos() -> frozenset[str]:
    """Get the set of all configured checkpoint repositories.

    Scans all repo_settings entries and collects every checkpoint_repo value.
    Used by the gateway to exempt checkpoint repos from private mode policy.

    Results are cached for 60 seconds to avoid redundant config file I/O
    on every git request.

    Returns:
        Frozenset of checkpoint repo names in "owner/repo" format, lowercased.
        Returns empty frozenset if config cannot be loaded or has no checkpoint repos.
    """
    global _checkpoint_repos_cache

    now = time.monotonic()
    if _checkpoint_repos_cache is not None:
        cached_time, cached_result = _checkpoint_repos_cache
        if now - cached_time < _CHECKPOINT_REPOS_TTL:
            return cached_result

    try:
        config = _load_config()
    except Exception:
        result: frozenset[str] = frozenset()
        _checkpoint_repos_cache = (now, result)
        return result

    repo_settings = config.get("repo_settings", {})
    if not isinstance(repo_settings, dict):
        result = frozenset()
        _checkpoint_repos_cache = (now, result)
        return result

    repos: set[str] = set()
    for settings in repo_settings.values():
        if isinstance(settings, dict):
            checkpoint_repo = settings.get("checkpoint_repo")
            if checkpoint_repo and isinstance(checkpoint_repo, str):
                repos.add(checkpoint_repo.lower())
    result = frozenset(repos)
    _checkpoint_repos_cache = (now, result)
    return result


def is_checkpoint_repo(owner: str, repo: str) -> bool:
    """Check if a repository is configured as a checkpoint destination.

    Args:
        owner: Repository owner (e.g. "jwbron")
        repo: Repository name (e.g. "egg-checkpoints")

    Returns:
        True if owner/repo is a configured checkpoint_repo.
        False on any config error (fail-closed).
    """
    try:
        full_name = f"{owner}/{repo}".lower()
        return full_name in get_all_checkpoint_repos()
    except Exception:
        return False


def get_repo_checks(repo: str) -> list[dict[str, str]]:
    """Get configured check commands for a repository.

    These are the commands to run during the SDLC pipeline implement phase
    checker step. Each check has a "name" (display label) and "command"
    (shell command to execute). They run sequentially.

    Args:
        repo: Repository in "owner/repo" format

    Returns:
        List of {"name": "...", "command": "..."} dicts,
        or empty list if no checks configured.
    """
    checks = get_repo_setting(repo, "checks", [])
    result: list[dict[str, str]] = validate_checks(checks)
    return result


def get_auth_mode(repo: str) -> str:
    """
    Get the authentication mode for a repository.

    Auth modes:
    - "bot": Use the GitHub App bot identity (default)
    - "user": Use a personal access token with user identity

    User mode allows operations to be attributed to a personal GitHub
    account instead of the egg bot, useful for contributing to external repos
    where bot accounts may not be appropriate.

    Args:
        repo: Repository in "owner/repo" format

    Returns:
        "bot" (default) or "user"
    """
    auth_mode = get_repo_setting(repo, "auth_mode", "bot")
    if auth_mode not in ("bot", "user"):
        return "bot"
    return cast(str, auth_mode)


def is_user_mode_repo(repo: str) -> bool:
    """
    Check if a repository is configured to use user mode.

    In user mode, operations are attributed to a personal GitHub account
    instead of the egg bot.

    Args:
        repo: Repository in "owner/repo" format

    Returns:
        True if the repo uses user mode authentication
    """
    return get_auth_mode(repo) == "user"


def get_user_mode_config() -> dict[str, str]:
    """
    Get the global user mode configuration.

    Returns configuration for user mode authentication including:
    - github_user: The GitHub username for attribution
    - git_name: Git author/committer name
    - git_email: Git author/committer email

    Returns:
        Dictionary with user mode settings, or empty dict if not configured
    """
    config = _load_config()
    user_mode = config.get("user_mode", {})
    return {
        "github_user": user_mode.get("github_user", ""),
        "git_name": user_mode.get("git_name", ""),
        "git_email": user_mode.get("git_email", ""),
    }


def get_bot_username() -> str:
    """
    Get the configured bot username.

    This is the bot's GitHub identity, used for:
    - Filtering out bot's own comments (to avoid self-response loops)
    - Identifying bot's own PRs for review response handling

    Returns:
        Bot username string
    """
    config = _load_config()
    username = config.get("bot_username")
    if not username:
        raise ValueError(
            "bot_username not configured in repositories.yaml. "
            "Run ./setup.py to configure, or add 'bot_username: your-bot-name' to ~/.config/egg/repositories.yaml."
        )
    return cast(str, username)


def get_github_token_for_repo(repo: str) -> tuple[str | None, str, str]:
    """
    Get the appropriate GitHub token for accessing a repository.

    Uses:
    - GITHUB_TOKEN for writable repos (or repos not in any list)
    - GITHUB_READONLY_TOKEN for readable repos (falls back to GITHUB_TOKEN)

    This enables separate tokens with different permission levels:
    - Writable repos: Full access via GitHub App or PAT with write permissions
    - Readable repos: Read-only PAT for external repos (e.g., org/webapp)

    Args:
        repo: Repository in "owner/repo" format

    Returns:
        GitHub token string, or None if no token is configured
    """
    # Import here to avoid circular imports
    from config.host_config import HostConfig

    config = HostConfig()
    access_level = get_repo_access_level(repo)

    if access_level == "readable":
        # Use readonly token for readable repos
        token = config.github_readonly_token or None
        token_type = "GITHUB_READONLY_TOKEN"
    else:
        # Use main token for writable repos (or unknown repos)
        token = config.github_token or None
        token_type = "GITHUB_TOKEN"

    # Return both token and metadata for debugging
    # The calling code can log the token_type and access_level
    return token, token_type, access_level


# Convenience function for shell scripts
def main() -> None:
    """CLI interface for shell scripts to query config."""
    import argparse

    parser = argparse.ArgumentParser(description="Query repository configuration")
    parser.add_argument(
        "--github-username", action="store_true", help="Print configured GitHub username"
    )
    parser.add_argument(
        "--list-writable", action="store_true", help="List all writable repos (one per line)"
    )
    parser.add_argument(
        "--list-readable", action="store_true", help="List all readable repos (one per line)"
    )
    parser.add_argument(
        "--list-all", action="store_true", help="List all repos for sync (one per line)"
    )
    parser.add_argument(
        "--check-writable",
        metavar="REPO",
        help="Check if REPO is writable (exit 0 if yes, 1 if no)",
    )
    parser.add_argument(
        "--check-readable",
        metavar="REPO",
        help="Check if REPO is readable (exit 0 if yes, 1 if no)",
    )
    parser.add_argument(
        "--access-level",
        metavar="REPO",
        help="Print access level for REPO (writable, readable, or none)",
    )
    parser.add_argument(
        "--default-reviewer", action="store_true", help="Print default reviewer username"
    )
    parser.add_argument(
        "--sync-all-prs", action="store_true", help="Print 'true' if sync_all_prs is enabled"
    )

    args = parser.parse_args()

    if args.github_username:
        print(get_github_username())
    elif args.list_writable:
        for repo in get_writable_repos():
            print(repo)
    elif args.list_readable:
        for repo in get_readable_repos():
            print(repo)
    elif args.list_all:
        for repo in get_repos_for_sync():
            print(repo)
    elif args.check_writable:
        import sys

        sys.exit(0 if is_writable_repo(args.check_writable) else 1)
    elif args.check_readable:
        import sys

        sys.exit(0 if is_readable_repo(args.check_readable) else 1)
    elif args.access_level:
        print(get_repo_access_level(args.access_level))
    elif args.default_reviewer:
        print(get_default_reviewer())
    elif args.sync_all_prs:
        config = get_sync_config()
        print("true" if config.get("sync_all_prs") else "false")
    else:
        # Default: print summary
        print("Repository Configuration")
        print("=" * 40)
        print(f"Config file: {_get_config_path()}")
        print(f"\nGitHub username: {get_github_username()}")
        print(f"\nWritable repos ({len(get_writable_repos())}):")
        for repo in get_writable_repos():
            print(f"  - {repo}")
        print(f"\nReadable repos ({len(get_readable_repos())}):")
        for repo in get_readable_repos():
            print(f"  - {repo}")
        print(f"\nDefault reviewer: {get_default_reviewer()}")
        sync = get_sync_config()
        print(f"Sync all PRs: {sync.get('sync_all_prs')}")
        print(f"Sync interval: {sync.get('sync_interval_minutes')} minutes")


if __name__ == "__main__":
    main()
