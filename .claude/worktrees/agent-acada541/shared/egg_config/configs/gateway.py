"""
Gateway sidecar configuration.

Manages the gateway launcher secret and rate limit settings.
"""

import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..base import BaseConfig, HealthCheckResult, ValidationResult
from ..constants import GATEWAY_PORT
from ..validators import mask_secret, validate_non_empty, validate_port


@dataclass
class RateLimitConfig:
    """Rate limit settings per operation type."""

    git_push: int = 1000
    gh_pr_create: int = 500
    gh_pr_comment: int = 2000
    gh_pr_edit: int = 500
    gh_pr_close: int = 500
    gh_execute: int = 2000
    combined: int = 4000  # Safety buffer


@dataclass
class GatewayConfig(BaseConfig):
    """Configuration for the gateway sidecar.

    Attributes:
        host: Host address to bind (0.0.0.0 for all interfaces)
        port: Port to listen on
        secret: Shared secret for authentication
        rate_limits: Per-operation rate limits
    """

    host: str = "0.0.0.0"
    port: int = GATEWAY_PORT
    secret: str = ""
    rate_limits: RateLimitConfig = field(default_factory=RateLimitConfig)

    # Track secret source for debugging
    _secret_source: str = field(default="", repr=False)

    def validate(self) -> ValidationResult:
        """Validate gateway configuration."""
        errors: list[str] = []
        warnings: list[str] = []

        # Port must be valid
        is_valid, error = validate_port(self.port)
        if not is_valid:
            errors.append(f"port: {error}")

        # Secret must be set
        is_valid, error = validate_non_empty(self.secret, "secret")
        if not is_valid:
            if error is not None:
                errors.append(error)
        elif len(self.secret) < 32:
            warnings.append("secret is shorter than recommended (32+ chars)")

        # Warn about binding to all interfaces
        if self.host == "0.0.0.0":
            warnings.append("Gateway bound to all interfaces (0.0.0.0)")

        if errors:
            return ValidationResult.invalid(errors, warnings)

        return ValidationResult.valid(warnings)

    def health_check(self, timeout: float = 5.0) -> HealthCheckResult:
        """Check gateway health endpoint.

        Calls the gateway's /api/v1/health endpoint.
        """
        if not self.secret:
            return HealthCheckResult(
                healthy=False,
                service_name="gateway",
                message="Launcher secret not configured",
            )

        try:
            import json
            import urllib.request

            # Use localhost for health checks when bound to all interfaces
            health_host = "127.0.0.1" if self.host == "0.0.0.0" else self.host

            start = time.time()
            req = urllib.request.Request(
                f"http://{health_host}:{self.port}/api/v1/health",
                headers={
                    "Authorization": f"Bearer {self.secret}",
                },
            )

            with urllib.request.urlopen(req, timeout=timeout) as response:
                data = json.loads(response.read().decode())
                latency = (time.time() - start) * 1000

                status = data.get("status", "unknown")
                return HealthCheckResult(
                    healthy=status == "ok",
                    service_name="gateway",
                    message=f"Gateway status: {status}",
                    latency_ms=latency,
                )

        except urllib.error.HTTPError as e:
            if e.code == 401:
                return HealthCheckResult(
                    healthy=False,
                    service_name="gateway",
                    message="Invalid launcher secret",
                )
            return HealthCheckResult(
                healthy=False,
                service_name="gateway",
                message=f"Gateway error: HTTP {e.code}",
            )
        except urllib.error.URLError:
            return HealthCheckResult(
                healthy=False,
                service_name="gateway",
                message="Gateway not reachable",
            )
        except Exception as e:
            return HealthCheckResult(
                healthy=False,
                service_name="gateway",
                message=f"Health check failed: {e}",
            )

    def to_dict(self) -> dict[str, Any]:
        """Return config with secrets masked."""
        return {
            "host": self.host,
            "port": self.port,
            "secret": mask_secret(self.secret),
            "rate_limits": {
                "git_push": self.rate_limits.git_push,
                "gh_pr_create": self.rate_limits.gh_pr_create,
                "gh_pr_comment": self.rate_limits.gh_pr_comment,
                "gh_pr_edit": self.rate_limits.gh_pr_edit,
                "gh_pr_close": self.rate_limits.gh_pr_close,
                "gh_execute": self.rate_limits.gh_execute,
                "combined": self.rate_limits.combined,
            },
            "_secret_source": self._secret_source,
        }

    @classmethod
    def from_env(cls) -> "GatewayConfig":
        """Load gateway configuration from environment and files.

        Secret sources (in priority order):
        1. EGG_LAUNCHER_SECRET environment variable
        2. ~/.config/egg/launcher-secret file
        3. Auto-generate and save new secret
        """
        config = cls()

        # Host and port from environment
        config.host = os.environ.get("GATEWAY_HOST", "0.0.0.0")
        port_str = os.environ.get("GATEWAY_PORT", str(GATEWAY_PORT))
        try:
            config.port = int(port_str)
        except ValueError:
            config.port = GATEWAY_PORT

        # Load launcher secret from environment
        env_secret = os.environ.get("EGG_LAUNCHER_SECRET", "")
        if env_secret:
            config.secret = env_secret
            config._secret_source = "environment"
        else:
            secret_file = Path.home() / ".config" / "egg" / "launcher-secret"
            if secret_file.exists():
                try:
                    config.secret = secret_file.read_text().strip()
                    config._secret_source = "launcher-secret file"
                except Exception:
                    pass

            # Auto-generate if still missing
            if not config.secret:
                config.secret = secrets.token_urlsafe(32)
                config._secret_source = "auto-generated"

                # Try to save the generated secret
                try:
                    secret_file.parent.mkdir(parents=True, exist_ok=True)
                    secret_file.write_text(config.secret)
                    secret_file.chmod(0o600)
                except Exception:
                    pass

        return config

    def ensure_secret_persisted(self) -> bool:
        """Ensure the secret is saved to disk.

        Returns:
            True if secret was saved or already exists, False on error
        """
        secret_file = Path.home() / ".config" / "egg" / "launcher-secret"

        try:
            if secret_file.exists():
                existing = secret_file.read_text().strip()
                if existing == self.secret:
                    return True

            secret_file.parent.mkdir(parents=True, exist_ok=True)
            secret_file.write_text(self.secret)
            secret_file.chmod(0o600)
            return True
        except Exception:
            return False
