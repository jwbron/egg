"""
Sandbox container template configuration.

Defines the container configuration for spawning sandbox containers
with correct network, volume, and environment settings.
"""

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Add shared directory to path for constants
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_config.constants import (
        EGG_ISOLATED_NETWORK,
        GATEWAY_ISOLATED_IP,
        GATEWAY_PORT,
        GATEWAY_PROXY_PORT,
    )
except ImportError:
    # Fallback values
    EGG_ISOLATED_NETWORK = "egg-isolated"
    GATEWAY_ISOLATED_IP = "172.32.0.2"
    GATEWAY_PORT = 9848
    GATEWAY_PROXY_PORT = 3129

from models import AgentRole


# Orchestrator constants (to be added to egg_config.constants)
ORCHESTRATOR_PORT = 9849
ORCHESTRATOR_CONTAINER_NAME = "egg-orchestrator"
ORCHESTRATOR_ISOLATED_IP = "172.32.0.3"


@dataclass
class SandboxConfig:
    """Configuration for spawning a sandbox container."""

    # Basic info
    pipeline_id: str
    agent_role: AgentRole | None = None
    container_name_suffix: str = ""

    # Image settings
    image: str = "egg-sandbox:latest"

    # Network settings
    network: str = EGG_ISOLATED_NETWORK
    enable_host_network: bool = False  # For debugging only

    # Repository settings
    repo_path: str = "/home/egg/repos"
    worktree_path: str | None = None

    # Gateway settings
    gateway_url: str = f"http://{GATEWAY_ISOLATED_IP}:{GATEWAY_PORT}"
    proxy_url: str = f"http://{GATEWAY_ISOLATED_IP}:{GATEWAY_PROXY_PORT}"

    # Orchestrator settings
    orchestrator_url: str = f"http://{ORCHESTRATOR_ISOLATED_IP}:{ORCHESTRATOR_PORT}"

    # Session settings
    session_token: str | None = None
    private_mode: bool = False

    # Agent settings
    prompt_file: str | None = None  # Path to prompt file for --print mode
    issue_number: int | None = None

    # Resource limits
    memory_limit: str = "4g"
    cpu_limit: float = 2.0

    # Additional environment
    extra_env: dict[str, str] = field(default_factory=dict)

    # Labels
    extra_labels: dict[str, str] = field(default_factory=dict)


class SandboxTemplate:
    """Template for creating sandbox container configurations.

    Generates Docker container specifications from SandboxConfig.
    """

    def __init__(self, config: SandboxConfig):
        """Initialize template with configuration.

        Args:
            config: Sandbox configuration
        """
        self.config = config

    def get_container_name(self) -> str:
        """Generate container name.

        Returns:
            Container name (without prefix)
        """
        parts = [self.config.pipeline_id]
        if self.config.agent_role:
            parts.append(self.config.agent_role.value)
        if self.config.container_name_suffix:
            parts.append(self.config.container_name_suffix)
        return "-".join(parts)

    def get_environment(self) -> dict[str, str]:
        """Generate environment variables.

        Returns:
            Environment variable dictionary
        """
        env = {
            # Gateway connection
            "GATEWAY_URL": self.config.gateway_url,
            "EGG_GATEWAY_URL": self.config.gateway_url,
            # Proxy for internet access
            "HTTP_PROXY": self.config.proxy_url,
            "HTTPS_PROXY": self.config.proxy_url,
            "http_proxy": self.config.proxy_url,
            "https_proxy": self.config.proxy_url,
            # No proxy for internal communication
            "NO_PROXY": f"{GATEWAY_ISOLATED_IP},{ORCHESTRATOR_ISOLATED_IP},localhost,127.0.0.1",
            "no_proxy": f"{GATEWAY_ISOLATED_IP},{ORCHESTRATOR_ISOLATED_IP},localhost,127.0.0.1",
            # Orchestrator connection
            "ORCHESTRATOR_URL": self.config.orchestrator_url,
            "EGG_ORCHESTRATOR_URL": self.config.orchestrator_url,
            # Pipeline info
            "EGG_PIPELINE_ID": self.config.pipeline_id,
            # Mode
            "EGG_PRIVATE_MODE": "1" if self.config.private_mode else "0",
        }

        # Session token
        if self.config.session_token:
            env["EGG_SESSION_TOKEN"] = self.config.session_token

        # Agent role
        if self.config.agent_role:
            env["EGG_AGENT_ROLE"] = self.config.agent_role.value

        # Issue number
        if self.config.issue_number:
            env["EGG_ISSUE_NUMBER"] = str(self.config.issue_number)

        # Prompt file for --print mode
        if self.config.prompt_file:
            env["EGG_PROMPT_FILE"] = self.config.prompt_file

        # Extra environment
        env.update(self.config.extra_env)

        return env

    def get_volumes(self) -> dict[str, dict[str, str]]:
        """Generate volume mounts.

        Returns:
            Volume mount dictionary for Docker SDK
        """
        volumes = {}

        # Repository mount
        if self.config.worktree_path:
            # Mount worktree if specified
            volumes[self.config.worktree_path] = {
                "bind": "/home/egg/repos",
                "mode": "rw",
            }
        else:
            # Mount main repos directory
            volumes[self.config.repo_path] = {
                "bind": "/home/egg/repos",
                "mode": "rw",
            }

        return volumes

    def get_labels(self) -> dict[str, str]:
        """Generate container labels.

        Returns:
            Label dictionary
        """
        labels = {
            "egg.orchestrator": "true",
            "egg.pipeline.id": self.config.pipeline_id,
        }

        if self.config.agent_role:
            labels["egg.agent.role"] = self.config.agent_role.value

        if self.config.issue_number:
            labels["egg.issue.number"] = str(self.config.issue_number)

        labels.update(self.config.extra_labels)

        return labels

    def get_network_config(self) -> dict[str, Any]:
        """Generate network configuration.

        Returns:
            Network config dictionary
        """
        if self.config.enable_host_network:
            return {"network_mode": "host"}

        return {"network": self.config.network}

    def get_resource_limits(self) -> dict[str, Any]:
        """Generate resource limit configuration.

        Returns:
            Resource limits dictionary
        """
        return {
            "mem_limit": self.config.memory_limit,
            "nano_cpus": int(self.config.cpu_limit * 1e9),
        }

    def to_docker_config(self) -> dict[str, Any]:
        """Generate full Docker container configuration.

        Returns:
            Dictionary suitable for docker.containers.create()
        """
        config: dict[str, Any] = {
            "image": self.config.image,
            "name": f"egg-sandbox-{self.get_container_name()}",
            "environment": self.get_environment(),
            "volumes": self.get_volumes(),
            "labels": self.get_labels(),
            "detach": True,
            **self.get_resource_limits(),
        }

        # Add network config
        network_config = self.get_network_config()
        config.update(network_config)

        return config


def create_sandbox_config(
    pipeline_id: str,
    agent_role: AgentRole | None = None,
    session_token: str | None = None,
    issue_number: int | None = None,
    private_mode: bool = False,
    **kwargs: Any,
) -> SandboxConfig:
    """Create a sandbox configuration.

    Args:
        pipeline_id: Pipeline ID (e.g., "issue-496")
        agent_role: Agent role for multi-agent execution
        session_token: Gateway session token
        issue_number: GitHub issue number
        private_mode: Whether to use private (locked down) mode
        **kwargs: Additional configuration options

    Returns:
        SandboxConfig instance
    """
    return SandboxConfig(
        pipeline_id=pipeline_id,
        agent_role=agent_role,
        session_token=session_token,
        issue_number=issue_number,
        private_mode=private_mode,
        **kwargs,
    )


def create_agent_sandbox_configs(
    pipeline_id: str,
    roles: list[AgentRole],
    session_tokens: dict[AgentRole, str] | None = None,
    issue_number: int | None = None,
    **kwargs: Any,
) -> dict[AgentRole, SandboxConfig]:
    """Create sandbox configurations for multiple agents.

    Args:
        pipeline_id: Pipeline ID
        roles: List of agent roles
        session_tokens: Session tokens by role
        issue_number: GitHub issue number
        **kwargs: Additional configuration options

    Returns:
        Dictionary mapping roles to configs
    """
    configs = {}
    tokens = session_tokens or {}

    for role in roles:
        configs[role] = create_sandbox_config(
            pipeline_id=pipeline_id,
            agent_role=role,
            session_token=tokens.get(role),
            issue_number=issue_number,
            **kwargs,
        )

    return configs
