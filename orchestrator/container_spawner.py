"""
Container spawner with integrated gateway session management.

Provides high-level container spawning that:
- Creates Docker containers
- Registers sessions with gateway
- Injects proper environment configuration
- Cleans up sessions on container removal
"""

import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# Add shared directory to path for logging and config
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)

try:
    from egg_config import (
        EGG_ISOLATED_NETWORK,
        EGG_CONTAINER_IP,
    )
except ImportError:
    EGG_ISOLATED_NETWORK = "egg-isolated"
    EGG_CONTAINER_IP = "172.32.0.10"

from docker_client import (
    DockerClient,
    DockerClientError,
    ContainerNotFoundError,
    ContainerOperationError,
    get_docker_client,
)
from gateway_client import (
    GatewayClient,
    GatewayError,
    SessionInfo,
    get_gateway_client,
)
from models import AgentRole, ContainerInfo, ContainerStatus

logger = get_logger("orchestrator.spawner")


@dataclass
class SpawnedContainer:
    """Information about a spawned container with gateway session."""

    container_info: ContainerInfo
    session_info: SessionInfo | None
    agent_role: AgentRole
    pipeline_id: str
    environment: dict[str, str]


class ContainerSpawner:
    """Spawns containers with integrated gateway session management.

    Handles the full lifecycle:
    1. Validate gateway health
    2. Create Docker container
    3. Register gateway session
    4. Start container with proper environment
    5. Clean up session on container removal
    """

    DEFAULT_SANDBOX_IMAGE = "egg-sandbox:latest"
    CONTAINER_NAME_FORMAT = "egg-{pipeline_id}-{role}"

    def __init__(
        self,
        docker_client: DockerClient | None = None,
        gateway_client: GatewayClient | None = None,
    ):
        """Initialize container spawner.

        Args:
            docker_client: Docker client (default: singleton)
            gateway_client: Gateway client (default: singleton)
        """
        self._docker = docker_client
        self._gateway = gateway_client

    @property
    def docker(self) -> DockerClient:
        """Get Docker client (lazy initialization)."""
        if self._docker is None:
            self._docker = get_docker_client()
        return self._docker

    @property
    def gateway(self) -> GatewayClient:
        """Get Gateway client (lazy initialization)."""
        if self._gateway is None:
            self._gateway = get_gateway_client()
        return self._gateway

    def spawn_agent_container(
        self,
        pipeline_id: str,
        agent_role: AgentRole,
        issue_number: int,
        repo_path: str,
        repo_mount: str | None = None,
        mode: str = "public",
        image: str | None = None,
        extra_env: dict[str, str] | None = None,
        extra_volumes: dict[str, dict[str, str]] | None = None,
        wait_for_gateway: bool = True,
    ) -> SpawnedContainer:
        """Spawn a container for an agent.

        Args:
            pipeline_id: Pipeline ID (e.g., "issue-496")
            agent_role: Agent role
            issue_number: GitHub issue number
            repo_path: Repository path inside container
            repo_mount: Host path to mount as repository (optional)
            mode: Gateway mode (public or private)
            image: Docker image (default: egg-sandbox:latest)
            extra_env: Additional environment variables
            extra_volumes: Additional volume mounts
            wait_for_gateway: Wait for gateway health before spawning

        Returns:
            SpawnedContainer with container and session info

        Raises:
            ContainerSpawnError: If spawning fails
        """
        container_name = self.CONTAINER_NAME_FORMAT.format(
            pipeline_id=pipeline_id,
            role=agent_role.value,
        )

        # Check gateway health
        if wait_for_gateway:
            health = self.gateway.check_health()
            if not health.healthy:
                raise ContainerSpawnError(
                    f"Gateway is not healthy: {health.error or health.status}"
                )

        # Prepare labels
        labels = {
            "egg.pipeline.id": pipeline_id,
            "egg.agent.role": agent_role.value,
            "egg.issue.number": str(issue_number),
        }

        # Prepare volumes
        volumes: dict[str, dict[str, str]] = {}
        if repo_mount:
            volumes[repo_mount] = {"bind": repo_path, "mode": "rw"}
        if extra_volumes:
            volumes.update(extra_volumes)

        session_info = None

        try:
            # Build base environment first (without session token if gateway fails)
            env = {
                "EGG_ISSUE_NUMBER": str(issue_number),
                "EGG_REPO_PATH": repo_path,
                "EGG_AGENT_ROLE": agent_role.value,
            }

            # Add extra environment
            if extra_env:
                env.update(extra_env)

            # Create the container with base environment
            # We create first, then register with gateway using the real container ID
            # This avoids the race condition of creating, deleting, and recreating
            container = self.docker.create_container(
                name=container_name,
                image=image or self.DEFAULT_SANDBOX_IMAGE,
                network=EGG_ISOLATED_NETWORK,
                environment=env,
                labels=labels,
                volumes=volumes if volumes else None,
            )

            logger.info(
                "Container created",
                container_id=container.container_id[:12],
                pipeline_id=pipeline_id,
                role=agent_role.value,
            )

            # Get container IP for gateway registration
            container_ip = self._get_container_ip(container.container_id)

            # Register session with gateway using real container ID
            try:
                session_info = self.gateway.register_session(
                    container_id=container.container_id,
                    container_ip=container_ip,
                    mode=mode,
                )

                # Get environment with session token and proxy config
                gateway_env = self.gateway.get_container_env(
                    session_token=session_info.session_token,
                    issue_number=issue_number,
                    repo_path=repo_path,
                    agent_role=agent_role.value,
                    mode=mode,
                )

                # Merge gateway env into container env
                # Note: Docker doesn't allow updating env after creation,
                # but the session token will be passed separately via the
                # gateway session binding (IP-based auth)
                env.update(gateway_env)

            except GatewayError as e:
                logger.warning(
                    "Failed to register gateway session",
                    container_id=container.container_id[:12],
                    error=str(e),
                )
                # Continue without session - container can still run
                # but won't have gateway access

            # Start the container
            container = self.docker.start_container(container.container_id)

            logger.info(
                "Agent container spawned",
                container_id=container.container_id[:12],
                pipeline_id=pipeline_id,
                role=agent_role.value,
                has_session=session_info is not None,
            )

            return SpawnedContainer(
                container_info=container,
                session_info=session_info,
                agent_role=agent_role,
                pipeline_id=pipeline_id,
                environment=env,
            )

        except DockerClientError as e:
            # Clean up gateway session if we registered one
            if session_info:
                try:
                    self.gateway.delete_session(session_info.session_token)
                except GatewayError:
                    pass  # Best effort cleanup
            raise ContainerSpawnError(f"Failed to spawn container: {e}") from e

    def stop_agent_container(
        self,
        container_id: str,
        cleanup_session: bool = True,
        timeout: int = 10,
    ) -> ContainerInfo:
        """Stop an agent container and optionally clean up session.

        Args:
            container_id: Container ID
            cleanup_session: Whether to delete gateway session
            timeout: Stop timeout in seconds

        Returns:
            Container info after stopping
        """
        try:
            # Stop container
            container = self.docker.stop_container(container_id, timeout=timeout)

            # Clean up gateway session
            if cleanup_session:
                try:
                    self.gateway.delete_session_by_container(container_id)
                except GatewayError as e:
                    logger.warning(
                        "Failed to clean up gateway session",
                        container_id=container_id[:12],
                        error=str(e),
                    )

            return container

        except ContainerNotFoundError:
            # Container already gone, try to clean up session anyway
            if cleanup_session:
                try:
                    self.gateway.delete_session_by_container(container_id)
                except GatewayError:
                    pass
            raise

    def remove_agent_container(
        self,
        container_id: str,
        force: bool = False,
        cleanup_session: bool = True,
    ) -> None:
        """Remove an agent container and clean up session.

        Args:
            container_id: Container ID
            force: Force removal
            cleanup_session: Whether to delete gateway session
        """
        try:
            self.docker.remove_container(container_id, force=force)
        finally:
            # Always try to clean up session
            if cleanup_session:
                try:
                    self.gateway.delete_session_by_container(container_id)
                except GatewayError as e:
                    logger.warning(
                        "Failed to clean up gateway session",
                        container_id=container_id[:12],
                        error=str(e),
                    )

    def list_pipeline_containers(
        self,
        pipeline_id: str,
    ) -> list[ContainerInfo]:
        """List all containers for a pipeline.

        Args:
            pipeline_id: Pipeline ID

        Returns:
            List of container info
        """
        return self.docker.list_containers(
            labels={"egg.pipeline.id": pipeline_id},
        )

    def cleanup_pipeline(
        self,
        pipeline_id: str,
        force: bool = True,
    ) -> int:
        """Clean up all containers and sessions for a pipeline.

        Args:
            pipeline_id: Pipeline ID
            force: Force removal

        Returns:
            Number of containers removed
        """
        containers = self.list_pipeline_containers(pipeline_id)
        removed = 0

        for container in containers:
            try:
                self.remove_agent_container(
                    container.container_id,
                    force=force,
                    cleanup_session=True,
                )
                removed += 1
            except (ContainerNotFoundError, ContainerOperationError) as e:
                logger.warning(
                    "Failed to remove container during cleanup",
                    container_id=container.container_id[:12],
                    error=str(e),
                )

        logger.info(
            "Pipeline cleanup complete",
            pipeline_id=pipeline_id,
            containers_removed=removed,
        )

        return removed

    def _get_container_ip(self, container_id: str) -> str:
        """Get or predict container IP address.

        Args:
            container_id: Container ID

        Returns:
            IP address string
        """
        try:
            # Try to get actual IP from Docker
            container = self.docker.client.containers.get(container_id)
            networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})

            if EGG_ISOLATED_NETWORK in networks:
                ip = networks[EGG_ISOLATED_NETWORK].get("IPAddress")
                if ip:
                    return ip

        except Exception:
            pass

        # Fall back to predictable IP based on container short ID
        # This is used when container hasn't been started yet
        # In production, we'd wait for the container to get an IP
        short_id = container_id[:8]
        # Use last 2 bytes of container ID to generate IP in 172.32.0.x range
        # This is a simplification - real implementation would track IPs
        ip_suffix = (int(short_id[:4], 16) % 200) + 10  # 10-209
        return f"172.32.0.{ip_suffix}"


class ContainerSpawnError(Exception):
    """Error during container spawning."""

    pass


# Singleton spawner instance
_spawner: ContainerSpawner | None = None


def get_container_spawner() -> ContainerSpawner:
    """Get the singleton container spawner.

    Returns:
        ContainerSpawner instance
    """
    global _spawner
    if _spawner is None:
        _spawner = ContainerSpawner()
    return _spawner
