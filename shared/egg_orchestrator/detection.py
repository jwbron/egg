"""Orchestrator mode detection utilities.

Provides functions to detect whether the sandbox is running in orchestrator
mode and to retrieve orchestrator connection information.
"""

import os

from .constants import (
    ENV_AGENT_ROLE,
    ENV_ORCHESTRATOR_MODE,
    ENV_ORCHESTRATOR_URL,
    ENV_PIPELINE_ID,
    ORCHESTRATOR_ISOLATED_IP,
    ORCHESTRATOR_PORT,
)
from .types import DeploymentMode


def is_orchestrator_mode() -> bool:
    """Check if running in orchestrator-managed mode.

    Returns True if the sandbox is being managed by an orchestrator
    (vs interactive/local mode). Detection is based on:

    1. EGG_ORCHESTRATOR_MODE environment variable (explicit)
    2. EGG_PIPELINE_ID environment variable (implicit - set by orchestrator)
    3. EGG_ORCHESTRATOR_URL environment variable (implicit - connectivity)

    Returns:
        True if running in orchestrator mode
    """
    # Explicit mode setting
    mode = os.environ.get(ENV_ORCHESTRATOR_MODE, "").lower()
    if mode in ("remote-single", "distributed"):
        return True

    # Implicit detection from pipeline context
    if os.environ.get(ENV_PIPELINE_ID):
        return True

    # Implicit detection from orchestrator URL
    if os.environ.get(ENV_ORCHESTRATOR_URL):
        return True

    return False


def get_deployment_mode() -> DeploymentMode:
    """Get the current deployment mode.

    Returns:
        DeploymentMode enum value
    """
    return DeploymentMode.from_env()


def get_orchestrator_url() -> str | None:
    """Get the orchestrator API URL.

    Checks for explicit URL in environment, otherwise constructs from
    network defaults based on deployment mode.

    Returns:
        Orchestrator URL or None if not in orchestrator mode
    """
    # Explicit URL takes precedence
    explicit_url = os.environ.get(ENV_ORCHESTRATOR_URL)
    if explicit_url:
        return explicit_url

    # Only return URL if we're in orchestrator mode
    if not is_orchestrator_mode():
        return None

    # Construct from network defaults (isolated network)
    return f"http://{ORCHESTRATOR_ISOLATED_IP}:{ORCHESTRATOR_PORT}"


def get_pipeline_id() -> str | None:
    """Get the current pipeline ID from environment.

    Returns:
        Pipeline ID or None if not set
    """
    return os.environ.get(ENV_PIPELINE_ID)


def get_agent_role() -> str | None:
    """Get the current agent role from environment.

    Returns:
        Agent role or None if not set
    """
    return os.environ.get(ENV_AGENT_ROLE)


__all__ = [
    "get_agent_role",
    "get_deployment_mode",
    "get_orchestrator_url",
    "get_pipeline_id",
    "is_orchestrator_mode",
]
