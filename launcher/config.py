"""Configuration handling for egg-launcher.

This module handles reading and validating configuration from the host
volume mounts and environment variables.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class LauncherConfig:
    """Configuration for the egg launcher.

    This class encapsulates all configuration needed to run the egg stack,
    including paths, credentials, and runtime options.
    """

    # Directory paths (from volume mounts)
    config_dir: Path = field(default_factory=lambda: Path("/config"))
    repos_dir: Path = field(default_factory=lambda: Path("/repos"))

    # Docker images
    gateway_image: str = "ghcr.io/jwbron/egg-gateway:latest"
    sandbox_image: str = "ghcr.io/jwbron/egg-sandbox:latest"

    # Network mode
    mode: str = "public"

    # API ports
    gateway_port: int = 9848
    proxy_port: int = 3129
    status_port: int = 8080

    # Secrets (loaded from config or environment)
    launcher_secret: str | None = None
    github_token: str | None = None

    # Git identity
    git_name: str = "egg"
    git_email: str = "egg@localhost"

    # Host UID/GID for file ownership
    host_uid: int = 1000
    host_gid: int = 1000

    @classmethod
    def from_environment(cls) -> "LauncherConfig":
        """Create configuration from environment variables.

        Environment variables:
            EGG_CONFIG_DIR: Path to config directory
            EGG_REPOS_DIR: Path to repos directory
            EGG_GATEWAY_IMAGE: Gateway Docker image
            EGG_SANDBOX_IMAGE: Sandbox Docker image
            EGG_MODE: Network mode (public/private)
            EGG_LAUNCHER_SECRET: Launcher secret
            GITHUB_USER_TOKEN: GitHub token
            EGG_USER_GIT_NAME: Git name for commits
            EGG_USER_GIT_EMAIL: Git email for commits
            HOST_UID: Host user ID
            HOST_GID: Host group ID

        Returns:
            LauncherConfig instance
        """
        config = cls()

        # Directory paths
        if config_dir := os.environ.get("EGG_CONFIG_DIR"):
            config.config_dir = Path(config_dir)
        if repos_dir := os.environ.get("EGG_REPOS_DIR"):
            config.repos_dir = Path(repos_dir)

        # Docker images
        if gateway_image := os.environ.get("EGG_GATEWAY_IMAGE"):
            config.gateway_image = gateway_image
        if sandbox_image := os.environ.get("EGG_SANDBOX_IMAGE"):
            config.sandbox_image = sandbox_image

        # Mode
        if mode := os.environ.get("EGG_MODE"):
            config.mode = mode

        # Ports
        if gateway_port := os.environ.get("EGG_GATEWAY_PORT"):
            config.gateway_port = int(gateway_port)
        if proxy_port := os.environ.get("EGG_PROXY_PORT"):
            config.proxy_port = int(proxy_port)
        if status_port := os.environ.get("EGG_STATUS_PORT"):
            config.status_port = int(status_port)

        # Secrets
        config.launcher_secret = os.environ.get("EGG_LAUNCHER_SECRET")
        config.github_token = os.environ.get("GITHUB_USER_TOKEN") or os.environ.get(
            "GITHUB_TOKEN"
        )

        # Git identity
        if git_name := os.environ.get("EGG_USER_GIT_NAME"):
            config.git_name = git_name
        if git_email := os.environ.get("EGG_USER_GIT_EMAIL"):
            config.git_email = git_email

        # Host UID/GID
        if host_uid := os.environ.get("HOST_UID"):
            config.host_uid = int(host_uid)
        if host_gid := os.environ.get("HOST_GID"):
            config.host_gid = int(host_gid)

        # Load from config files
        config._load_from_files()

        return config

    def _load_from_files(self) -> None:
        """Load configuration from files in config directory."""
        # Load launcher secret from file if not set
        if not self.launcher_secret:
            secret_file = self.config_dir / "launcher-secret"
            if secret_file.exists():
                self.launcher_secret = secret_file.read_text().strip()

        # Load repositories.yaml for additional config
        repos_file = self.config_dir / "repositories.yaml"
        if repos_file.exists():
            try:
                with open(repos_file) as f:
                    repos_config = yaml.safe_load(f)

                # Extract user_mode config if present
                user_mode = repos_config.get("user_mode", {})
                if git_name := user_mode.get("git_name"):
                    self.git_name = git_name
                if git_email := user_mode.get("git_email"):
                    self.git_email = git_email
            except Exception:
                pass  # Ignore YAML parsing errors

        # Load secrets.env if present
        secrets_file = self.config_dir / "secrets.env"
        if secrets_file.exists():
            try:
                with open(secrets_file) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            key, _, value = line.partition("=")
                            key = key.strip()
                            value = value.strip()

                            if key == "GITHUB_USER_TOKEN" and not self.github_token:
                                self.github_token = value
                            elif key == "EGG_LAUNCHER_SECRET" and not self.launcher_secret:
                                self.launcher_secret = value
            except Exception:
                pass

    def validate(self) -> list[str]:
        """Validate the configuration.

        Returns:
            List of validation error messages (empty if valid)
        """
        errors = []

        # Check required directories
        if not self.config_dir.exists():
            errors.append(f"Config directory does not exist: {self.config_dir}")

        if not self.repos_dir.exists():
            errors.append(f"Repos directory does not exist: {self.repos_dir}")

        # Check required files
        repos_file = self.config_dir / "repositories.yaml"
        if not repos_file.exists():
            errors.append(f"repositories.yaml not found in {self.config_dir}")

        # Check secrets
        if not self.launcher_secret:
            errors.append("Launcher secret not configured")

        if not self.github_token:
            errors.append("GitHub token not configured")

        # Validate mode
        if self.mode not in ("public", "private"):
            errors.append(f"Invalid mode: {self.mode} (must be 'public' or 'private')")

        return errors

    def to_gateway_env(self) -> dict[str, str]:
        """Convert configuration to gateway environment variables.

        Returns:
            Dictionary of environment variables for gateway container
        """
        env = {
            "EGG_REPO_CONFIG": "/config/repositories.yaml",
            "HOST_UID": str(self.host_uid),
            "HOST_GID": str(self.host_gid),
        }

        if self.launcher_secret:
            env["EGG_LAUNCHER_SECRET"] = self.launcher_secret

        if self.github_token:
            env["GITHUB_USER_TOKEN"] = self.github_token

        if self.git_name:
            env["EGG_USER_GIT_NAME"] = self.git_name

        if self.git_email:
            env["EGG_USER_GIT_EMAIL"] = self.git_email

        return env

    def to_sandbox_env(self) -> dict[str, str]:
        """Convert configuration to sandbox environment variables.

        Returns:
            Dictionary of environment variables for sandbox container
        """
        env = {
            "EGG_GATEWAY_HOST": "egg-gateway",
            "EGG_GATEWAY_PORT": str(self.gateway_port),
            "EGG_MODE": self.mode,
        }

        if self.launcher_secret:
            env["EGG_LAUNCHER_SECRET"] = self.launcher_secret

        return env
