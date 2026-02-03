"""
Repository configuration for authentication mode.

Determines which authentication mode (bot or user) should be used
for different repositories. This allows egg to operate with either
a GitHub App (bot mode) or user personal access token (user mode)
depending on the repository.

Configuration can be:
- Global default (bot or user)
- Per-repository overrides via environment or config file

User mode configuration includes:
- github_user: GitHub username for attribution
- git_name: Git author/committer name
- git_email: Git author/committer email
"""

import os
from pathlib import Path
from typing import Any

import yaml

from shared.egg_logging import get_logger

logger = get_logger("gateway.repo-config")

# Default auth mode
DEFAULT_AUTH_MODE = "bot"
DEFAULT_BOT_NAME = "egg"

# Environment variables
AUTH_MODE_ENV = "EGG_AUTH_MODE"
USER_MODE_REPOS_ENV = "EGG_USER_MODE_REPOS"
CONFIG_FILE_ENV = "EGG_REPO_CONFIG"
GITHUB_USERNAME_ENV = "EGG_GITHUB_USERNAME"
BOT_USERNAME_ENV = "EGG_BOT_USERNAME"

# User mode configuration environment variables
USER_MODE_GITHUB_USER_ENV = "EGG_USER_MODE_GITHUB_USER"
USER_MODE_GIT_NAME_ENV = "EGG_USER_MODE_GIT_NAME"
USER_MODE_GIT_EMAIL_ENV = "EGG_USER_MODE_GIT_EMAIL"

# Config file paths
DEFAULT_CONFIG_PATHS = [
    Path.home() / ".config" / "egg" / "repos.yaml",
    Path("/etc/egg/repos.yaml"),
]


class RepoConfig:
    """Repository authentication configuration."""

    def __init__(
        self,
        default_mode: str | None = None,
        user_mode_repos: set[str] | None = None,
        config_file: Path | None = None,
    ):
        """Initialize repo configuration.

        Args:
            default_mode: Default auth mode ("bot" or "user")
            user_mode_repos: Set of repos that should use user mode
            config_file: Path to configuration file
        """
        self._default_mode = default_mode or self._get_default_mode()
        self._user_mode_repos: set[str] = user_mode_repos or set()
        self._bot_mode_repos: set[str] = set()

        # Load from config file if present
        self._load_config(config_file)

        # Load from environment
        self._load_from_env()

    def _get_default_mode(self) -> str:
        """Get default mode from environment or use built-in default."""
        mode = os.environ.get(AUTH_MODE_ENV, DEFAULT_AUTH_MODE).lower()
        if mode not in ("bot", "user"):
            logger.warning(
                f"Invalid auth mode '{mode}', using default",
                default=DEFAULT_AUTH_MODE,
            )
            return DEFAULT_AUTH_MODE
        return mode

    def _load_config(self, config_file: Path | None) -> None:
        """Load configuration from file."""
        paths_to_try = [config_file] if config_file else DEFAULT_CONFIG_PATHS
        if config_file_env := os.environ.get(CONFIG_FILE_ENV):
            paths_to_try.insert(0, Path(config_file_env))

        for path in paths_to_try:
            if path and path.exists():
                try:
                    with open(path) as f:
                        config = yaml.safe_load(f) or {}

                    # Load mode overrides
                    repos_config = config.get("repos", {})
                    for repo, settings in repos_config.items():
                        mode = settings.get("mode") if isinstance(settings, dict) else settings
                        if mode == "user":
                            self._user_mode_repos.add(repo.lower())
                        elif mode == "bot":
                            self._bot_mode_repos.add(repo.lower())

                    logger.debug(f"Loaded repo config from {path}")
                    return

                except Exception as e:
                    logger.warning(f"Failed to load config from {path}: {e}")

    def _load_from_env(self) -> None:
        """Load user mode repos from environment variable."""
        repos_str = os.environ.get(USER_MODE_REPOS_ENV, "")
        if repos_str:
            repos = [r.strip().lower() for r in repos_str.split(",") if r.strip()]
            self._user_mode_repos.update(repos)
            logger.debug(
                "Loaded user mode repos from environment",
                repos=repos,
            )

    def get_auth_mode(self, repo: str) -> str:
        """Get the authentication mode for a repository.

        Args:
            repo: Repository in "owner/repo" format

        Returns:
            "bot" or "user"
        """
        if not repo:
            return self._default_mode

        repo_lower = repo.lower()

        # Check explicit bot mode repos first
        if repo_lower in self._bot_mode_repos:
            return "bot"

        # Check user mode repos
        if repo_lower in self._user_mode_repos:
            return "user"

        # Check if owner is in user mode repos (owner/* pattern)
        if "/" in repo_lower:
            owner = repo_lower.split("/")[0]
            if f"{owner}/*" in self._user_mode_repos:
                return "user"

        return self._default_mode

    @property
    def default_mode(self) -> str:
        """Get the default authentication mode."""
        return self._default_mode


def _load_yaml_config() -> dict[str, Any]:
    """Load the YAML configuration file.

    Returns:
        Configuration dictionary, or empty dict if no config found.
    """
    paths_to_try = list(DEFAULT_CONFIG_PATHS)
    if config_file_env := os.environ.get(CONFIG_FILE_ENV):
        paths_to_try.insert(0, Path(config_file_env))

    for path in paths_to_try:
        if path and path.exists():
            try:
                with open(path) as f:
                    return yaml.safe_load(f) or {}
            except Exception as e:
                logger.warning(f"Failed to load config from {path}: {e}")

    return {}


def get_user_mode_config() -> dict[str, str]:
    """Get the global user mode configuration.

    Returns configuration for user mode authentication including:
    - github_user: The GitHub username for attribution
    - git_name: Git author/committer name
    - git_email: Git author/committer email

    Configuration sources (in priority order):
    1. Environment variables (EGG_USER_MODE_*)
    2. Config file (user_mode section)

    Returns:
        Dictionary with user mode settings, or empty values if not configured.
    """
    # First check environment variables
    github_user = os.environ.get(USER_MODE_GITHUB_USER_ENV, "")
    git_name = os.environ.get(USER_MODE_GIT_NAME_ENV, "")
    git_email = os.environ.get(USER_MODE_GIT_EMAIL_ENV, "")

    # Fall back to config file if environment not set
    if not (github_user or git_name or git_email):
        config = _load_yaml_config()
        user_mode = config.get("user_mode", {})
        github_user = user_mode.get("github_user", "")
        git_name = user_mode.get("git_name", "")
        git_email = user_mode.get("git_email", "")

    return {
        "github_user": github_user,
        "git_name": git_name,
        "git_email": git_email,
    }


def get_bot_username() -> str:
    """Get the configured bot username.

    This is the bot's identity for:
    - Filtering out bot's own comments
    - Identifying bot's own PRs

    Configuration sources (in priority order):
    1. EGG_BOT_USERNAME environment variable
    2. Config file (bot_username field)
    3. Default: "egg"

    Returns:
        Bot username string.
    """
    # Check environment first
    if bot_username := os.environ.get(BOT_USERNAME_ENV, ""):
        return bot_username

    # Check config file
    config = _load_yaml_config()
    return config.get("bot_username", DEFAULT_BOT_NAME)


def get_github_username() -> str | None:
    """Get the configured GitHub username.

    This is used to construct repo names and as the default reviewer.

    Configuration sources (in priority order):
    1. EGG_GITHUB_USERNAME environment variable
    2. Config file (github_username field)

    Returns:
        GitHub username string, or None if not configured.
    """
    # Check environment first
    if username := os.environ.get(GITHUB_USERNAME_ENV, ""):
        return username

    # Check config file
    config = _load_yaml_config()
    return config.get("github_username")


def get_writable_repos() -> list[str]:
    """Get list of repositories where egg has write access.

    These are repos where egg can:
    - Push code changes
    - Create PRs
    - Comment on PRs

    Returns:
        List of repo strings in "owner/repo" format.
    """
    config = _load_yaml_config()
    return config.get("writable_repos", [])


def get_readable_repos() -> list[str]:
    """Get list of repositories where egg has read-only access.

    These are repos where egg can read but NOT modify.

    Returns:
        List of repo strings in "owner/repo" format.
    """
    config = _load_yaml_config()
    return config.get("readable_repos", [])


def is_writable_repo(repo: str) -> bool:
    """Check if a repository is in the writable repos list.

    Args:
        repo: Repository in "owner/repo" format

    Returns:
        True if egg has write access to this repo.
    """
    writable = get_writable_repos()
    repo_lower = repo.lower()
    return any(r.lower() == repo_lower for r in writable)


def is_readable_repo(repo: str) -> bool:
    """Check if a repository is in the readable repos list.

    Args:
        repo: Repository in "owner/repo" format

    Returns:
        True if egg has read-only access to this repo.
    """
    readable = get_readable_repos()
    repo_lower = repo.lower()
    return any(r.lower() == repo_lower for r in readable)


def is_user_mode_repo(repo: str) -> bool:
    """Check if a repository is configured to use user mode.

    In user mode, operations are attributed to a personal GitHub account
    instead of the bot.

    Args:
        repo: Repository in "owner/repo" format

    Returns:
        True if the repo uses user mode authentication.
    """
    return get_auth_mode(repo) == "user"


def get_default_reviewer() -> str | None:
    """Get the default reviewer for PRs created by egg.

    Falls back to github_username if default_reviewer is not explicitly set.

    Returns:
        GitHub username of default reviewer, or None if not configured.
    """
    config = _load_yaml_config()
    reviewer = config.get("default_reviewer")
    if reviewer:
        return reviewer
    return get_github_username()


# Global config instance
_config: RepoConfig | None = None


def get_repo_config() -> RepoConfig:
    """Get the global repo config instance."""
    global _config
    if _config is None:
        _config = RepoConfig()
    return _config


def get_auth_mode(repo: str | None) -> str:
    """Get authentication mode for a repository (convenience function).

    Args:
        repo: Repository in "owner/repo" format, or None for default

    Returns:
        "bot" or "user"
    """
    return get_repo_config().get_auth_mode(repo or "")


def reset_config() -> None:
    """Reset the global config (for testing)."""
    global _config
    _config = None
