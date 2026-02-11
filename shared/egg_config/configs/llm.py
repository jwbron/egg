"""
LLM provider configuration for egg services.

Supports Claude Code with Anthropic API via API key or OAuth.
"""

import os
import time
from dataclasses import dataclass
from typing import Any

from ..base import BaseConfig, HealthCheckResult, ValidationResult
from ..constants import MODEL_HEALTH_CHECK
from ..validators import mask_secret, validate_anthropic_key, validate_non_empty

# Model used for health checks - imported from centralized constants
# Uses cheapest/fastest model alias (haiku) to minimize costs
# Note: Health checks make minimal API calls (max_tokens=1) but are billable
ANTHROPIC_HEALTH_CHECK_MODEL = MODEL_HEALTH_CHECK


@dataclass
class LLMConfig(BaseConfig):
    """Configuration for Claude Code / Anthropic API.

    Attributes:
        anthropic_api_key: Anthropic API key (sk-ant-...)
        anthropic_base_url: Custom base URL for Anthropic API
        model: Optional model override
        timeout: Request timeout in seconds
    """

    anthropic_api_key: str = ""
    anthropic_base_url: str = ""
    model: str = ""
    timeout: int = 7200  # 2 hours default

    def validate(self) -> ValidationResult:
        """Validate LLM configuration."""
        errors: list[str] = []
        warnings: list[str] = []

        # Check for API key (not required if using OAuth)
        auth_method = os.environ.get("ANTHROPIC_AUTH_METHOD", "api_key").lower()
        if auth_method == "api_key":
            is_valid, error = validate_non_empty(self.anthropic_api_key, "anthropic_api_key")
            if not is_valid:
                if error is not None:
                    errors.append(error)
            else:
                is_valid, error = validate_anthropic_key(self.anthropic_api_key)
                if not is_valid:
                    errors.append(f"anthropic_api_key: {error}")
        elif auth_method == "oauth":
            warnings.append("Using OAuth authentication (no API key required)")
        else:
            warnings.append(f"Unknown auth method: {auth_method}")

        # Warn about custom base URL
        if self.anthropic_base_url:
            warnings.append(f"Using custom Anthropic base URL: {self.anthropic_base_url}")

        if errors:
            return ValidationResult.invalid(errors, warnings)

        return ValidationResult.valid(warnings)

    def health_check(self, timeout: float = 5.0) -> HealthCheckResult:
        """Check Anthropic API connectivity."""
        if not self.anthropic_api_key:
            # Check if using OAuth
            auth_method = os.environ.get("ANTHROPIC_AUTH_METHOD", "api_key").lower()
            if auth_method == "oauth":
                return HealthCheckResult(
                    healthy=True,
                    service_name="llm",
                    message="Using OAuth authentication (health check skipped)",
                )
            return HealthCheckResult(
                healthy=False,
                service_name="llm",
                message="Anthropic API key not configured",
            )

        try:
            import json
            import urllib.request

            base_url = self.anthropic_base_url or "https://api.anthropic.com"
            start = time.time()

            # Use a minimal messages request to verify API key
            # Note: This is a billable API call (minimal cost with max_tokens=1)
            req = urllib.request.Request(
                f"{base_url}/v1/messages",
                data=json.dumps(
                    {
                        "model": ANTHROPIC_HEALTH_CHECK_MODEL,
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "hi"}],
                    }
                ).encode(),
                headers={
                    "x-api-key": self.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=timeout):
                latency = (time.time() - start) * 1000
                return HealthCheckResult(
                    healthy=True,
                    service_name="llm",
                    message="Anthropic API connected",
                    latency_ms=latency,
                )

        except urllib.error.HTTPError as e:
            if e.code == 401:
                return HealthCheckResult(
                    healthy=False,
                    service_name="llm",
                    message="Invalid Anthropic API key",
                )
            elif e.code == 400:
                # Bad request but auth worked
                latency = (time.time() - start) * 1000
                return HealthCheckResult(
                    healthy=True,
                    service_name="llm",
                    message="Anthropic API connected (auth verified)",
                    latency_ms=latency,
                )
            return HealthCheckResult(
                healthy=False,
                service_name="llm",
                message=f"Anthropic API error: HTTP {e.code}",
            )
        except Exception as e:
            return HealthCheckResult(
                healthy=False,
                service_name="llm",
                message=f"Anthropic connection failed: {e}",
            )

    def to_dict(self) -> dict[str, Any]:
        """Return config with secrets masked."""
        return {
            "anthropic_api_key": mask_secret(self.anthropic_api_key),
            "anthropic_base_url": self.anthropic_base_url or "[default]",
            "model": self.model or "[provider default]",
            "timeout": self.timeout,
        }

    @classmethod
    def from_env(cls) -> "LLMConfig":
        """Load LLM configuration from environment variables.

        Environment variables:
        - ANTHROPIC_API_KEY: Anthropic API key
        - ANTHROPIC_BASE_URL: Custom base URL
        - ANTHROPIC_AUTH_METHOD: "api_key" (default) or "oauth"
        """
        config = cls()

        # Load API key from environment
        config.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")

        # Optional settings
        config.anthropic_base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
        config.model = os.environ.get("LLM_MODEL", "")

        timeout_str = os.environ.get("LLM_TIMEOUT", "7200")
        try:
            config.timeout = int(timeout_str)
        except ValueError:
            config.timeout = 7200

        return config
