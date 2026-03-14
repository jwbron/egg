"""
Base classes and types for the configuration framework.

This module provides:
- ConfigStatus: Enum for validation states (VALID, INVALID, DEGRADED)
- ValidationResult: Result of config validation with errors/warnings
- HealthCheckResult: Result of a service health check
- BaseConfig: Abstract base class for all config classes
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ConfigStatus(Enum):
    """Status of configuration validation."""

    VALID = "valid"
    INVALID = "invalid"
    DEGRADED = "degraded"  # Partially valid, some features may not work


@dataclass
class ValidationResult:
    """Result of validating a configuration.

    Attributes:
        status: Overall validation status
        errors: List of validation errors (config is invalid if non-empty)
        warnings: List of validation warnings (config may work but has issues)
    """

    status: ConfigStatus
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        """Return True if config is valid (no errors)."""
        return self.status == ConfigStatus.VALID

    @property
    def is_usable(self) -> bool:
        """Return True if config is usable (valid or degraded)."""
        return self.status in (ConfigStatus.VALID, ConfigStatus.DEGRADED)

    @classmethod
    def valid(cls, warnings: list[str] | None = None) -> "ValidationResult":
        """Create a valid result with optional warnings."""
        return cls(
            status=ConfigStatus.VALID,
            warnings=warnings or [],
        )

    @classmethod
    def invalid(cls, errors: list[str], warnings: list[str] | None = None) -> "ValidationResult":
        """Create an invalid result with errors."""
        return cls(
            status=ConfigStatus.INVALID,
            errors=errors,
            warnings=warnings or [],
        )

    @classmethod
    def degraded(cls, errors: list[str], warnings: list[str] | None = None) -> "ValidationResult":
        """Create a degraded result (partially valid)."""
        return cls(
            status=ConfigStatus.DEGRADED,
            errors=errors,
            warnings=warnings or [],
        )


@dataclass
class HealthCheckResult:
    """Result of a service health check.

    Attributes:
        healthy: Whether the service is healthy
        service_name: Name of the service checked
        message: Human-readable status message
        latency_ms: Response time in milliseconds (if applicable)
    """

    healthy: bool
    service_name: str
    message: str
    latency_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result: dict[str, Any] = {
            "healthy": self.healthy,
            "message": self.message,
        }
        if self.latency_ms is not None:
            result["latency_ms"] = self.latency_ms
        return result


class BaseConfig(ABC):
    """Abstract base class for service configurations.

    All service-specific config classes should inherit from this and implement:
    - validate(): Check if the configuration is valid
    - health_check(): Test connectivity to the service
    - to_dict(): Return config as dict with secrets masked
    - from_env(): Class method to load config from environment
    """

    @abstractmethod
    def validate(self) -> ValidationResult:
        """Validate the configuration.

        Returns:
            ValidationResult with status, errors, and warnings
        """
        ...

    @abstractmethod
    def health_check(self, timeout: float = 5.0) -> HealthCheckResult:
        """Check if the service is reachable and properly configured.

        Args:
            timeout: Maximum time to wait for health check in seconds

        Returns:
            HealthCheckResult with health status and message
        """
        ...

    @abstractmethod
    def to_dict(self) -> dict[str, Any]:
        """Return configuration as a dictionary with secrets masked.

        Sensitive values (tokens, passwords, API keys) should be replaced
        with masked values like "****" or "[REDACTED]".

        Returns:
            Dictionary representation of the config
        """
        ...

    @classmethod
    @abstractmethod
    def from_env(cls) -> "BaseConfig":
        """Load configuration from environment variables and config files.

        This method should:
        1. Check environment variables first
        2. Fall back to config files (~/.config/egg/)
        3. Apply defaults for optional values

        Returns:
            New instance of the config class

        Raises:
            ValueError: If required configuration is missing
        """
        ...

    @property
    def service_name(self) -> str:
        """Return the name of the service this config is for.

        Default implementation returns the class name without 'Config' suffix.
        """
        name = self.__class__.__name__
        if name.endswith("Config"):
            name = name[:-6]
        return name.lower()
