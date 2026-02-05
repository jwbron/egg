"""
GitHub configuration for egg services.

Consolidates token loading from multiple sources:
1. Environment variables (highest priority)
2. ~/.config/egg/secrets.env
3. ~/.config/egg/github-token (plain text)
"""

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..base import BaseConfig, HealthCheckResult, ValidationResult
from ..utils import load_env_file, load_yaml_file
from ..validators import mask_secret, validate_github_token, validate_non_empty


@dataclass
class GitHubConfig(BaseConfig):
    """Configuration for GitHub integration.

    Attributes:
        token: Primary GitHub token for API access
        readonly_token: Optional read-only token for public repos
        user_mode_token: Optional token for personal account attribution
        username: GitHub username (for egg identity)
        token_expires_at: Token expiration time (for GitHub App tokens)
    """

    token: str = ""
    readonly_token: str = ""
    user_mode_token: str = ""
    username: str = "egg"
    token_expires_at: datetime | None = None

    # Token source tracking (for debugging)
    _token_source: str = field(default="", repr=False)

    def validate(self) -> ValidationResult:
        """Validate GitHub configuration."""
        errors: list[str] = []
        warnings: list[str] = []

        # Primary token is required
        is_valid, error = validate_non_empty(self.token, "token")
        if not is_valid:
            if error is not None:
                errors.append(error)
        else:
            is_valid, error = validate_github_token(self.token)
            if not is_valid:
                errors.append(f"token: {error}")

        # Check token expiration
        if self.token_expires_at:
            now = datetime.now()
            if self.token_expires_at < now:
                errors.append("GitHub token has expired")
            elif (self.token_expires_at - now).total_seconds() < 300:
                warnings.append("GitHub token expires in less than 5 minutes")

        # Readonly token is optional but must be valid if provided
        if self.readonly_token:
            is_valid, error = validate_github_token(self.readonly_token)
            if not is_valid:
                warnings.append(f"readonly_token: {error}")

        # User mode token is optional but must be valid if provided
        if self.user_mode_token:
            is_valid, error = validate_github_token(self.user_mode_token)
            if not is_valid:
                warnings.append(f"user_mode_token: {error}")

        if errors:
            return ValidationResult.invalid(errors, warnings)

        return ValidationResult.valid(warnings)

    def health_check(self, timeout: float = 5.0) -> HealthCheckResult:
        """Check GitHub API connectivity.

        Calls /user to verify authentication.
        """
        if not self.token:
            return HealthCheckResult(
                healthy=False,
                service_name="github",
                message="Token not configured",
            )

        try:
            import urllib.request

            start = time.time()
            req = urllib.request.Request(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "egg-config/1.0",
                },
            )

            with urllib.request.urlopen(req, timeout=timeout) as response:
                data = json.loads(response.read().decode())
                latency = (time.time() - start) * 1000

                login = data.get("login", "unknown")
                return HealthCheckResult(
                    healthy=True,
                    service_name="github",
                    message=f"Authenticated as {login}",
                    latency_ms=latency,
                )

        except urllib.error.HTTPError as e:
            if e.code == 401:
                return HealthCheckResult(
                    healthy=False,
                    service_name="github",
                    message="Token is invalid or expired",
                )
            return HealthCheckResult(
                healthy=False,
                service_name="github",
                message=f"API error: HTTP {e.code}",
            )
        except Exception as e:
            return HealthCheckResult(
                healthy=False,
                service_name="github",
                message=f"Connection failed: {e}",
            )

    def to_dict(self) -> dict[str, Any]:
        """Return config with secrets masked."""
        result: dict[str, Any] = {
            "token": mask_secret(self.token),
            "readonly_token": mask_secret(self.readonly_token),
            "user_mode_token": mask_secret(self.user_mode_token),
            "username": self.username,
        }
        if self.token_expires_at:
            result["token_expires_at"] = self.token_expires_at.isoformat()
        if self._token_source:
            result["_token_source"] = self._token_source
        return result

    @property
    def is_token_expired(self) -> bool:
        """Check if the token has expired."""
        if not self.token_expires_at:
            return False
        return datetime.now() > self.token_expires_at

    @property
    def token_expires_soon(self) -> bool:
        """Check if the token expires within 5 minutes."""
        if not self.token_expires_at:
            return False
        remaining = (self.token_expires_at - datetime.now()).total_seconds()
        return remaining < 300

    @classmethod
    def from_env(cls) -> "GitHubConfig":
        """Load GitHub configuration from environment and config files.

        Token sources (in priority order):
        1. GITHUB_TOKEN environment variable
        2. ~/.config/egg/secrets.env
        3. ~/.config/egg/github-token (plain text)
        """
        config = cls()

        # Try environment variable first
        env_token = os.environ.get("GITHUB_TOKEN", "")
        if env_token:
            config.token = env_token
            config._token_source = "environment"
        else:
            # Try secrets.env
            secrets_file = Path.home() / ".config" / "egg" / "secrets.env"
            secrets = load_env_file(secrets_file)
            if secrets.get("GITHUB_TOKEN"):
                config.token = secrets["GITHUB_TOKEN"]
                config._token_source = "secrets.env"
            else:
                # Try plain text file
                token_file = Path.home() / ".config" / "egg" / "github-token"
                if token_file.exists():
                    try:
                        config.token = token_file.read_text().strip()
                        config._token_source = "github-token file"
                    except Exception:
                        pass

        # Load optional tokens
        secrets_file = Path.home() / ".config" / "egg" / "secrets.env"
        secrets = load_env_file(secrets_file)

        config.readonly_token = os.environ.get(
            "GITHUB_READONLY_TOKEN",
            secrets.get("GITHUB_READONLY_TOKEN", ""),
        )

        config.user_mode_token = os.environ.get(
            "GITHUB_USER_TOKEN",
            secrets.get("GITHUB_USER_TOKEN", ""),
        )

        # Load username from repo config
        config.username = _get_github_username()

        return config


def _get_github_username() -> str:
    """Get GitHub username from repositories.yaml."""
    config_file = Path.home() / ".config" / "egg" / "repositories.yaml"
    config = load_yaml_file(config_file)
    return str(config.get("github_username", "egg"))
