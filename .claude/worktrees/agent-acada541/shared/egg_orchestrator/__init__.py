"""
Shared orchestrator types and utilities for egg components.

This package provides common types and utilities used by both the gateway
and sandbox containers for orchestrator integration. It enables:

- Typed orchestrator API communication (OrchestratorClient)
- Orchestrator mode detection (is_orchestrator_mode)
- Common types and enums (DeploymentMode, SignalType)
- Configuration constants (ORCHESTRATOR_PORT, etc.)

Usage:
    from egg_orchestrator import (
        OrchestratorClient,
        is_orchestrator_mode,
        DeploymentMode,
        SignalType,
        ORCHESTRATOR_PORT,
    )

    # Check if running in orchestrator mode
    if is_orchestrator_mode():
        client = OrchestratorClient()
        client.signal_complete(pipeline_id, agent_role)

    # Detect deployment mode
    mode = DeploymentMode.from_env()
"""

from .client import (
    OrchestratorClient,
    OrchestratorError,
    OrchestratorHealth,
    get_orchestrator_client,
)
from .constants import (
    ORCHESTRATOR_CONTAINER_NAME,
    ORCHESTRATOR_EXTERNAL_IP,
    ORCHESTRATOR_HEALTH_ENDPOINT,
    ORCHESTRATOR_ISOLATED_IP,
    ORCHESTRATOR_PORT,
    ORCHESTRATOR_SIGNAL_ENDPOINT,
)
from .detection import (
    get_orchestrator_url,
    is_orchestrator_mode,
)
from .types import (
    CompletionData,
    DeploymentMode,
    ErrorData,
    HeartbeatData,
    ProgressData,
    SignalPayload,
    SignalResponse,
    SignalType,
)

__all__ = [
    # Client
    "OrchestratorClient",
    "OrchestratorError",
    "OrchestratorHealth",
    "get_orchestrator_client",
    # Constants
    "ORCHESTRATOR_CONTAINER_NAME",
    "ORCHESTRATOR_EXTERNAL_IP",
    "ORCHESTRATOR_HEALTH_ENDPOINT",
    "ORCHESTRATOR_ISOLATED_IP",
    "ORCHESTRATOR_PORT",
    "ORCHESTRATOR_SIGNAL_ENDPOINT",
    # Detection
    "get_orchestrator_url",
    "is_orchestrator_mode",
    # Types
    "CompletionData",
    "DeploymentMode",
    "ErrorData",
    "HeartbeatData",
    "ProgressData",
    "SignalPayload",
    "SignalResponse",
    "SignalType",
]

__version__ = "0.1.0"
