"""Centralized constants for egg gateway and sandbox configuration.

This module is the single source of truth for port numbers, network names,
container names, and other constants used across the egg ecosystem.

Usage:
    from egg_config import GATEWAY_PORT, GATEWAY_PROXY_PORT
    from egg_config.constants import TEST_GATEWAY_PORT  # For tests only
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

# Orchestrator container constants
ORCHESTRATOR_CONTAINER_NAME = "egg-orchestrator"
ORCHESTRATOR_IMAGE_NAME = "egg-orchestrator"
ORCHESTRATOR_PORT = 9849
ORCHESTRATOR_ISOLATED_IP = "172.32.0.3"  # Orchestrator IP in isolated network
ORCHESTRATOR_EXTERNAL_IP = "172.33.0.3"  # Orchestrator IP in external network

# Test constants - use these in unit tests to avoid coupling to production values
# Using a clearly fake port (1234) makes it obvious when tests accidentally
# connect to real services
TEST_GATEWAY_PORT = 1234
TEST_GATEWAY_PROXY_PORT = 5678

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
    "ORCHESTRATOR_CONTAINER_NAME",
    "ORCHESTRATOR_EXTERNAL_IP",
    "ORCHESTRATOR_IMAGE_NAME",
    "ORCHESTRATOR_ISOLATED_IP",
    "ORCHESTRATOR_PORT",
    "TEST_GATEWAY_PORT",
    "TEST_GATEWAY_PROXY_PORT",
]
