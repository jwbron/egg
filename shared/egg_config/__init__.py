"""
Unified configuration framework for egg components.

This module provides:
- BaseConfig: Abstract base class for service configurations
- ValidationResult: Result of configuration validation
- HealthCheckResult: Result of service health checks
- ConfigRegistry: Central registry for all configs
- Validators: Reusable validation functions

Usage:
    from egg_config import BaseConfig, ValidationResult, get_registry
    from egg_config.validators import validate_url, mask_secret

    # Register a config
    registry = get_registry()
    registry.register(my_config)

    # Validate all configs
    result = registry.validate_all()
    if not result.all_valid:
        print("Configuration errors found")

Legacy exports (deprecated):
    - Config: Use BaseConfig instead
    - get_local_repos, get_repos_config_file: Moving to dedicated config classes
"""

# Legacy exports for backward compatibility
# New framework exports
from .base import (
    BaseConfig,
    ConfigStatus,
    HealthCheckResult,
    ValidationResult,
)
from .config import Config, get_local_repos, get_repos_config_file

# Service configurations
from .configs.gateway import GatewayConfig
from .configs.github import GitHubConfig
from .configs.llm import LLMConfig
from .registry import (
    AggregateHealthResult,
    AggregateValidationResult,
    ConfigRegistry,
    get_registry,
    reset_registry,
)

__all__ = [
    "AggregateHealthResult",
    "AggregateValidationResult",
    # Base classes
    "BaseConfig",
    # Legacy (deprecated)
    "Config",
    # Registry
    "ConfigRegistry",
    "ConfigStatus",
    # Service configurations
    "GatewayConfig",
    "GitHubConfig",
    "HealthCheckResult",
    "LLMConfig",
    "ValidationResult",
    "get_local_repos",
    "get_registry",
    "get_repos_config_file",
    "reset_registry",
]
