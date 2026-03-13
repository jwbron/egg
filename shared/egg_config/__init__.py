"""
Unified configuration framework for egg components.

This module provides:
- BaseConfig: Abstract base class for service configurations
- ValidationResult: Result of configuration validation
- HealthCheckResult: Result of service health checks
- ConfigRegistry: Central registry for all configs
- Validators: Reusable validation functions
- Constants: Centralized gateway/sandbox constants (ports, networks, etc.)

Usage:
    from egg_config import BaseConfig, ValidationResult, get_registry
    from egg_config import GATEWAY_PORT, GATEWAY_PROXY_PORT  # Constants
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

# Centralized constants
from .constants import (
    CHECKPOINT_BRANCH,
    EGG_CONTAINER_IP,
    EGG_EXTERNAL_NETWORK,
    EGG_EXTERNAL_SUBNET,
    EGG_ISOLATED_NETWORK,
    EGG_ISOLATED_SUBNET,
    GATEWAY_CONTAINER_NAME,
    GATEWAY_EXTERNAL_IP,
    GATEWAY_IMAGE_NAME,
    GATEWAY_ISOLATED_IP,
    GATEWAY_PORT,
    GATEWAY_PROXY_PORT,
    MCP_SERVER_PORT,
    ORCHESTRATOR_CONTAINER_NAME,
    ORCHESTRATOR_EXTERNAL_IP,
    ORCHESTRATOR_IMAGE_NAME,
    ORCHESTRATOR_ISOLATED_IP,
    ORCHESTRATOR_PORT,
    PIPELINE_STATE_BRANCH,
    TEST_GATEWAY_PORT,
    TEST_GATEWAY_PROXY_PORT,
    TRANSCRIPT_BUFFER_DIR,
)
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
    # Constants
    "CHECKPOINT_BRANCH",
    "EGG_CONTAINER_IP",
    "EGG_EXTERNAL_NETWORK",
    "EGG_EXTERNAL_SUBNET",
    "EGG_ISOLATED_NETWORK",
    "EGG_ISOLATED_SUBNET",
    "GATEWAY_CONTAINER_NAME",
    "GATEWAY_EXTERNAL_IP",
    "GATEWAY_IMAGE_NAME",
    "GATEWAY_ISOLATED_IP",
    "GATEWAY_PORT",
    "GATEWAY_PROXY_PORT",
    "MCP_SERVER_PORT",
    # Service configurations
    "GatewayConfig",
    # Orchestrator constants
    "ORCHESTRATOR_CONTAINER_NAME",
    "ORCHESTRATOR_EXTERNAL_IP",
    "ORCHESTRATOR_IMAGE_NAME",
    "ORCHESTRATOR_ISOLATED_IP",
    "ORCHESTRATOR_PORT",
    "PIPELINE_STATE_BRANCH",
    "GitHubConfig",
    "HealthCheckResult",
    "LLMConfig",
    # Test constants
    "TEST_GATEWAY_PORT",
    "TEST_GATEWAY_PROXY_PORT",
    # Transcript buffer
    "TRANSCRIPT_BUFFER_DIR",
    "ValidationResult",
    "get_local_repos",
    "get_registry",
    "get_repos_config_file",
    "reset_registry",
]
