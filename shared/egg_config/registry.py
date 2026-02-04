"""
Configuration registry for managing service configurations.

The registry provides:
- Central registration of all service configs
- Bulk validation (validate_all)
- Bulk health checks (health_check_all)
- Dry-run mode for testing

Thread Safety:
    All public methods that access the config dictionary are thread-safe.
    The registry uses a lock to protect concurrent access.
"""

from dataclasses import dataclass, field
from datetime import datetime
from threading import RLock
from typing import Any

from .base import BaseConfig, HealthCheckResult, ValidationResult


@dataclass
class AggregateHealthResult:
    """Aggregated health check results from all services.

    Attributes:
        status: Overall status (healthy/degraded/unhealthy)
        services: Individual health check results by service name
        timestamp: When the health check was performed
    """

    status: str  # "healthy", "degraded", "unhealthy"
    services: dict[str, HealthCheckResult]
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "status": self.status,
            "services": {name: result.to_dict() for name, result in self.services.items()},
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class AggregateValidationResult:
    """Aggregated validation results from all services.

    Attributes:
        all_valid: True if all configs are valid
        results: Individual validation results by service name
    """

    all_valid: bool
    results: dict[str, ValidationResult]

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "all_valid": self.all_valid,
            "results": {
                name: {
                    "status": result.status.value,
                    "errors": result.errors,
                    "warnings": result.warnings,
                }
                for name, result in self.results.items()
            },
        }


class ConfigRegistry:
    """Central registry for all service configurations.

    This is a singleton that holds all registered configs and provides
    methods for bulk operations. All methods are thread-safe.

    Usage:
        registry = get_registry()
        registry.register(slack_config)
        registry.register(github_config)

        # Validate all configs
        result = registry.validate_all()
        if not result.all_valid:
            for name, validation in result.results.items():
                if not validation.is_valid:
                    print(f"{name}: {validation.errors}")

        # Health check all configs
        health = registry.health_check_all()
        print(health.status)  # "healthy", "degraded", or "unhealthy"
    """

    _instance: "ConfigRegistry | None" = None
    _instance_lock: RLock = RLock()

    def __new__(cls) -> "ConfigRegistry":
        """Ensure only one instance exists (singleton pattern)."""
        with cls._instance_lock:
            if cls._instance is None:
                instance = super().__new__(cls)
                instance._configs: dict[str, BaseConfig] = {}
                instance._dry_run: bool = False
                instance._lock: RLock = RLock()
                cls._instance = instance
            return cls._instance

    def __init__(self) -> None:
        # Initialization happens in __new__ to avoid re-init on each call
        pass

    @property
    def configs(self) -> dict[str, BaseConfig]:
        """Return a copy of all registered configs (thread-safe)."""
        with self._lock:
            return dict(self._configs)

    @property
    def dry_run(self) -> bool:
        """Return whether dry-run mode is enabled."""
        with self._lock:
            return self._dry_run

    def set_dry_run(self, enabled: bool) -> None:
        """Enable or disable dry-run mode.

        In dry-run mode, validation runs but writes are logged instead of executed.
        """
        with self._lock:
            self._dry_run = enabled

    def register(self, config: BaseConfig, name: str | None = None) -> None:
        """Register a configuration (thread-safe).

        Args:
            config: The config instance to register
            name: Optional name override (defaults to config.service_name)
        """
        service_name = name or config.service_name
        with self._lock:
            self._configs[service_name] = config

    def unregister(self, name: str) -> None:
        """Unregister a configuration by name (thread-safe).

        Args:
            name: The service name to unregister
        """
        with self._lock:
            self._configs.pop(name, None)

    def get(self, name: str) -> BaseConfig | None:
        """Get a registered config by name (thread-safe).

        Args:
            name: The service name

        Returns:
            The config instance or None if not found
        """
        with self._lock:
            return self._configs.get(name)

    def validate_all(self) -> AggregateValidationResult:
        """Validate all registered configurations (thread-safe).

        Returns:
            AggregateValidationResult with individual results
        """
        # Take a snapshot of configs under lock
        with self._lock:
            configs_snapshot = dict(self._configs)

        # Run validation outside lock (validation may be slow)
        results: dict[str, ValidationResult] = {}
        all_valid = True

        for name, config in configs_snapshot.items():
            result = config.validate()
            results[name] = result
            if not result.is_valid:
                all_valid = False

        return AggregateValidationResult(all_valid=all_valid, results=results)

    def health_check_all(self, timeout: float = 5.0) -> AggregateHealthResult:
        """Run health checks on all registered configurations (thread-safe).

        Args:
            timeout: Maximum time per health check in seconds

        Returns:
            AggregateHealthResult with individual results
        """
        # Take a snapshot of configs under lock
        with self._lock:
            configs_snapshot = dict(self._configs)

        # Run health checks outside lock (may be slow due to network)
        results: dict[str, HealthCheckResult] = {}
        unhealthy_count = 0
        total_count = len(configs_snapshot)

        for name, config in configs_snapshot.items():
            result = config.health_check(timeout=timeout)
            results[name] = result
            if not result.healthy:
                unhealthy_count += 1

        # Determine overall status
        if unhealthy_count == 0:
            status = "healthy"
        elif unhealthy_count == total_count:
            status = "unhealthy"
        else:
            status = "degraded"

        return AggregateHealthResult(status=status, services=results)

    def clear(self) -> None:
        """Clear all registered configurations (thread-safe).

        Primarily useful for testing.
        """
        with self._lock:
            self._configs.clear()

    def to_dict(self) -> dict[str, Any]:
        """Return all configs as a dictionary with secrets masked (thread-safe).

        Returns:
            Dictionary of service names to masked config dictionaries
        """
        with self._lock:
            configs_snapshot = dict(self._configs)

        return {name: config.to_dict() for name, config in configs_snapshot.items()}


def get_registry() -> ConfigRegistry:
    """Get the global configuration registry instance.

    Returns:
        The singleton ConfigRegistry instance
    """
    return ConfigRegistry()


def reset_registry() -> None:
    """Reset the registry to a fresh state.

    Primarily useful for testing to ensure clean state between tests.
    """
    with ConfigRegistry._instance_lock:
        if ConfigRegistry._instance is not None:
            ConfigRegistry._instance.clear()
        ConfigRegistry._instance = None
