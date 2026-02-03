"""
Repository configuration for authentication mode.

Determines which authentication mode (bot or user) should be used
for different repositories. This allows egg to operate with either
a GitHub App (bot mode) or user personal access token (user mode)
depending on the repository.

Configuration can be:
- Global default (bot or user)
- Per-repository overrides via environment or config file
"""

import os
from pathlib import Path

import yaml

from shared.egg_logging import get_logger

logger = get_logger("gateway.repo-config")

# Default auth mode
DEFAULT_AUTH_MODE = "bot"

# Environment variables
AUTH_MODE_ENV = "EGG_AUTH_MODE"
USER_MODE_REPOS_ENV = "EGG_USER_MODE_REPOS"
CONFIG_FILE_ENV = "EGG_REPO_CONFIG"

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
