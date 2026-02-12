"""Orchestrator constants.

Centralized constants for orchestrator communication. Re-exports relevant
constants from egg_config for convenience.
"""

# Re-export from egg_config for consistency
try:
    from egg_config.constants import (
        ORCHESTRATOR_CONTAINER_NAME,
        ORCHESTRATOR_EXTERNAL_IP,
        ORCHESTRATOR_ISOLATED_IP,
        ORCHESTRATOR_PORT,
    )
except ImportError:
    # Fallback values if egg_config is not available
    ORCHESTRATOR_CONTAINER_NAME = "egg-orchestrator"
    ORCHESTRATOR_PORT = 9849
    ORCHESTRATOR_ISOLATED_IP = "172.32.0.3"
    ORCHESTRATOR_EXTERNAL_IP = "172.33.0.3"

# API endpoints
ORCHESTRATOR_HEALTH_ENDPOINT = "/api/v1/health"
ORCHESTRATOR_SIGNAL_ENDPOINT = "/api/v1/pipelines/{pipeline_id}/signal"

# Environment variable names
ENV_ORCHESTRATOR_URL = "EGG_ORCHESTRATOR_URL"
ENV_ORCHESTRATOR_MODE = "EGG_ORCHESTRATOR_MODE"
ENV_PIPELINE_ID = "EGG_PIPELINE_ID"
ENV_AGENT_ROLE = "EGG_AGENT_ROLE"

__all__ = [
    "ENV_AGENT_ROLE",
    "ENV_ORCHESTRATOR_MODE",
    "ENV_ORCHESTRATOR_URL",
    "ENV_PIPELINE_ID",
    "ORCHESTRATOR_CONTAINER_NAME",
    "ORCHESTRATOR_EXTERNAL_IP",
    "ORCHESTRATOR_HEALTH_ENDPOINT",
    "ORCHESTRATOR_ISOLATED_IP",
    "ORCHESTRATOR_PORT",
    "ORCHESTRATOR_SIGNAL_ENDPOINT",
]
