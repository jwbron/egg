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

# Deployment validation (DinD) network configuration
# Third network for devserver containers during check phase deployment validation.
# Internal-only (no gateway, no DNS, no internet) — services communicate within
# the bridge but cannot reach external networks.
EGG_CHECK_NETWORK_PREFIX = "egg-check"  # Actual name: egg-check-{pipeline_id}
EGG_CHECK_SUBNET = "172.34.0.0/24"  # Must not overlap with isolated/external subnets

# Resource limits for devserver containers during deployment validation.
# These prevent agent-modified code from exhausting host resources.
DEVSERVER_CPU_LIMIT = "1.0"  # CPU quota per container (1 full core)
DEVSERVER_MEMORY_LIMIT = "512m"  # Memory limit per container
DEVSERVER_PIDS_LIMIT = 256  # Max PIDs per container (prevents fork bombs)
DEVSERVER_HARD_TIMEOUT_SECONDS = 300  # Hard time cap for entire devserver lifecycle

# Test constants - use these in unit tests to avoid coupling to production values
# Using a clearly fake port (1234) makes it obvious when tests accidentally
# connect to real services
TEST_GATEWAY_PORT = 1234
TEST_GATEWAY_PROXY_PORT = 5678

__all__ = [
    "DEVSERVER_CPU_LIMIT",
    "DEVSERVER_HARD_TIMEOUT_SECONDS",
    "DEVSERVER_MEMORY_LIMIT",
    "DEVSERVER_PIDS_LIMIT",
    "EGG_CHECK_NETWORK_PREFIX",
    "EGG_CHECK_SUBNET",
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
