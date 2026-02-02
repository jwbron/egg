"""Configuration management for the egg CLI.

This module handles egg configuration loading, validation, and defaults.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Default configuration file name
DEFAULT_CONFIG_FILE = "egg.yaml"

# Default paths
DEFAULT_CONFIG_DIR = Path.home() / ".config" / "egg"
DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "egg"
DEFAULT_WORKTREE_DIR = Path.home() / ".egg-worktrees"

# Docker image names
SANDBOX_IMAGE = "egg-sandbox"
GATEWAY_IMAGE = "egg-gateway"

# Container names
SANDBOX_CONTAINER = "egg-sandbox"
GATEWAY_CONTAINER = "egg-gateway"

# Network configuration
ISOLATED_NETWORK = "egg-isolated"
EXTERNAL_NETWORK = "egg-external"
ISOLATED_SUBNET = "172.30.0.0/16"
EXTERNAL_SUBNET = "172.31.0.0/16"
GATEWAY_ISOLATED_IP = "172.30.0.2"
GATEWAY_EXTERNAL_IP = "172.31.0.2"

# Port configuration
GATEWAY_API_PORT = 9847
GATEWAY_PROXY_PORT = 3128


@dataclass
class Repository:
    """Configuration for a single repository."""

    path: Path
    name: str | None = None
    branch: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Repository":
        """Create a Repository from a dictionary."""
        return cls(
            path=Path(data["path"]).expanduser(),
            name=data.get("name"),
            branch=data.get("branch"),
        )


@dataclass
class EggConfig:
    """Configuration for an egg sandbox environment."""

    # Paths
    config_dir: Path = field(default_factory=lambda: DEFAULT_CONFIG_DIR)
    data_dir: Path = field(default_factory=lambda: DEFAULT_DATA_DIR)
    worktree_dir: Path = field(default_factory=lambda: DEFAULT_WORKTREE_DIR)

    # Repositories
    repositories: list[Repository] = field(default_factory=list)

    # Network mode
    private_mode: bool = False

    # Docker settings
    sandbox_image: str = SANDBOX_IMAGE
    gateway_image: str = GATEWAY_IMAGE

    # Runtime settings
    runtime_uid: int = field(default_factory=lambda: os.getuid())
    runtime_gid: int = field(default_factory=lambda: os.getgid())

    # GitHub App settings (optional)
    github_app_id: str | None = None
    github_app_private_key_path: Path | None = None
    github_installation_id: str | None = None

    @classmethod
    def from_file(cls, path: Path) -> "EggConfig":
        """Load configuration from a YAML file.

        Args:
            path: Path to egg.yaml file

        Returns:
            Loaded configuration

        Raises:
            FileNotFoundError: If config file doesn't exist
            yaml.YAMLError: If config file is invalid YAML
            ValueError: If config is invalid
        """
        with open(path) as f:
            data = yaml.safe_load(f) or {}

        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EggConfig":
        """Create configuration from a dictionary."""
        config = cls()

        # Paths
        if "config_dir" in data:
            config.config_dir = Path(data["config_dir"]).expanduser()
        if "data_dir" in data:
            config.data_dir = Path(data["data_dir"]).expanduser()
        if "worktree_dir" in data:
            config.worktree_dir = Path(data["worktree_dir"]).expanduser()

        # Repositories
        if "repositories" in data:
            config.repositories = [
                Repository.from_dict(r) if isinstance(r, dict) else Repository(path=Path(r))
                for r in data["repositories"]
            ]

        # Network mode
        if "private_mode" in data:
            config.private_mode = bool(data["private_mode"])

        # Docker settings
        if "sandbox_image" in data:
            config.sandbox_image = data["sandbox_image"]
        if "gateway_image" in data:
            config.gateway_image = data["gateway_image"]

        # Runtime settings
        if "runtime_uid" in data:
            config.runtime_uid = int(data["runtime_uid"])
        if "runtime_gid" in data:
            config.runtime_gid = int(data["runtime_gid"])

        # GitHub App settings
        if "github" in data:
            gh = data["github"]
            if "app_id" in gh:
                config.github_app_id = str(gh["app_id"])
            if "private_key_path" in gh:
                config.github_app_private_key_path = Path(gh["private_key_path"]).expanduser()
            if "installation_id" in gh:
                config.github_installation_id = str(gh["installation_id"])

        return config

    def validate(self) -> list[str]:
        """Validate the configuration.

        Returns:
            List of validation error messages (empty if valid)
        """
        errors = []

        # Check repositories exist
        for repo in self.repositories:
            if not repo.path.exists():
                errors.append(f"Repository path does not exist: {repo.path}")
            elif not (repo.path / ".git").exists():
                errors.append(f"Path is not a git repository: {repo.path}")

        # Check GitHub App config if any part is specified
        github_parts = [
            self.github_app_id,
            self.github_app_private_key_path,
            self.github_installation_id,
        ]
        if any(github_parts) and not all(github_parts):
            errors.append(
                "GitHub App config incomplete. Need: app_id, private_key_path, installation_id"
            )

        if self.github_app_private_key_path and not self.github_app_private_key_path.exists():
            errors.append(
                f"GitHub App private key not found: {self.github_app_private_key_path}"
            )

        return errors

    def ensure_directories(self) -> None:
        """Create necessary directories."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.worktree_dir.mkdir(parents=True, exist_ok=True)


def load_config(path: Path | None = None) -> EggConfig:
    """Load configuration from file or use defaults.

    Args:
        path: Optional path to config file. If None, looks for egg.yaml
              in current directory, then ~/.config/egg/egg.yaml

    Returns:
        Loaded configuration
    """
    if path is not None:
        return EggConfig.from_file(path)

    # Try current directory
    local_config = Path.cwd() / DEFAULT_CONFIG_FILE
    if local_config.exists():
        return EggConfig.from_file(local_config)

    # Try user config directory
    user_config = DEFAULT_CONFIG_DIR / DEFAULT_CONFIG_FILE
    if user_config.exists():
        return EggConfig.from_file(user_config)

    # Return defaults
    return EggConfig()


def validate_config(config: EggConfig) -> tuple[bool, list[str]]:
    """Validate a configuration.

    Args:
        config: Configuration to validate

    Returns:
        Tuple of (is_valid, error_messages)
    """
    errors = config.validate()
    return len(errors) == 0, errors
