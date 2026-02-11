"""Centralized constants for egg gateway, sandbox, and LLM configuration.

This module is the single source of truth for port numbers, network names,
container names, model aliases, and other constants used across the egg ecosystem.

Usage:
    from egg_config import GATEWAY_PORT, GATEWAY_PROXY_PORT
    from egg_config.constants import TEST_GATEWAY_PORT  # For tests only
    from egg_config.constants import MODEL_OPUS, MODEL_SONNET, MODEL_HAIKU
"""

# Gateway container constants
GATEWAY_CONTAINER_NAME = "egg-gateway"
GATEWAY_IMAGE_NAME = "egg-gateway"
GATEWAY_PORT = 9848
GATEWAY_PROXY_PORT = 3129

# Network lockdown configuration
# Dual-network architecture: egg-isolated (internal) + egg-external (for gateway)
# egg container connects only to egg-isolated and routes all traffic through gateway proxy
EGG_ISOLATED_NETWORK = "egg-isolated"
EGG_EXTERNAL_NETWORK = "egg-external"
EGG_ISOLATED_SUBNET = "172.32.0.0/24"  # Subnet for egg-isolated network
EGG_EXTERNAL_SUBNET = "172.33.0.0/24"  # Subnet for egg-external network
EGG_CONTAINER_IP = "172.32.0.10"  # Fixed IP for egg container in isolated network
GATEWAY_ISOLATED_IP = "172.32.0.2"  # Gateway IP in isolated network
GATEWAY_EXTERNAL_IP = "172.33.0.2"  # Gateway IP in external network

# Test constants - use these in unit tests to avoid coupling to production values
# Using a clearly fake port (1234) makes it obvious when tests accidentally
# connect to real services
TEST_GATEWAY_PORT = 1234
TEST_GATEWAY_PROXY_PORT = 5678

# =============================================================================
# LLM Model Constants
# =============================================================================
# Model aliases that map to the latest version of each model tier.
# Using aliases instead of full model IDs (e.g., "claude-opus-4-5-20251101")
# ensures we always use the latest version without hardcoding timestamps.
#
# Model Selection Guidelines:
#   - OPUS: Complex reasoning, code review, conflict resolution, architectural
#           decisions, multi-step analysis. Use when quality is critical.
#   - SONNET: Documentation updates, design reviews (workloop), integration
#             tests. Good balance of quality and cost for less critical tasks.
#   - HAIKU: Health checks, simple validation, high-volume low-stakes calls.
#            Use for minimal API connectivity tests or trivial operations.
#
# Rate Limit Optimization:
#   The Anthropic API has rate limits per model tier. Using lower-tier models
#   for appropriate tasks helps stay within limits while reducing costs.
#   Cost ratios (approximate):
#     - Opus: 1x (baseline)
#     - Sonnet: 0.6x
#     - Haiku: 0.2x

MODEL_OPUS = "opus"
MODEL_SONNET = "sonnet"
MODEL_HAIKU = "haiku"

# Default model for agent operations requiring full reasoning capability
MODEL_DEFAULT = MODEL_OPUS

# Model for health checks (cheapest tier, just verifying API connectivity)
MODEL_HEALTH_CHECK = MODEL_HAIKU

# Model for integration tests (sonnet is sufficient, saves cost)
MODEL_INTEGRATION_TESTS = MODEL_SONNET

__all__ = [
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
    # LLM Model constants
    "MODEL_DEFAULT",
    "MODEL_HAIKU",
    "MODEL_HEALTH_CHECK",
    "MODEL_INTEGRATION_TESTS",
    "MODEL_OPUS",
    "MODEL_SONNET",
    "TEST_GATEWAY_PORT",
    "TEST_GATEWAY_PROXY_PORT",
]
