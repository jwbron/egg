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
from pathlib import Path

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
        EGG_CONTAINER_IP,
    )
    from egg_config import (
        EGG_ISOLATED_NETWORK as _DEFAULT_ISOLATED_NETWORK,
    )
except ImportError:
    _DEFAULT_ISOLATED_NETWORK = "egg-isolated"
    EGG_CONTAINER_IP = "172.32.0.10"

# Allow override via environment for test stacks with non-standard network names
EGG_ISOLATED_NETWORK = os.environ.get("EGG_ISOLATED_NETWORK", _DEFAULT_ISOLATED_NETWORK)

from docker_client import (
    ContainerNotFoundError,
    ContainerOperationError,
    DockerClient,
    DockerClientError,
    get_docker_client,
)
from gateway_client import (
    GatewayClient,
    GatewayError,
    SessionInfo,
    get_gateway_client,
)
from models import AgentRole, ContainerInfo

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

    DEFAULT_SANDBOX_IMAGE = os.environ.get("EGG_SANDBOX_IMAGE", "egg:latest")
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
        issue_number: int | None = None,
        repo_path: str = "/home/egg/repos",
        repo_mount: str | None = None,
        mode: str = "public",
        image: str | None = None,
        extra_env: dict[str, str] | None = None,
        extra_volumes: dict[str, dict[str, str]] | None = None,
        wait_for_gateway: bool = True,
        repos: list[str] | None = None,
        phase: str | None = None,
    ) -> SpawnedContainer:
        """Spawn a container for an agent.

        Args:
            pipeline_id: Pipeline ID (e.g., "issue-496" or "local-a1b2c3d4")
            agent_role: Agent role
            issue_number: GitHub issue number (optional for local pipelines)
            repo_path: Repository path inside container
            repo_mount: Host path to mount as repository (optional)
            mode: Gateway mode (public, private, or local)
            image: Docker image (default: egg-sandbox:latest)
            extra_env: Additional environment variables
            extra_volumes: Additional volume mounts
            wait_for_gateway: Wait for gateway health before spawning
            repos: List of repositories in owner/name format for gateway session
            phase: SDLC pipeline phase for gateway session

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
        }
        if issue_number is not None:
            labels["egg.issue.number"] = str(issue_number)

        # Prepare volumes
        volumes: dict[str, dict[str, str]] = {}
        if repo_mount:
            volumes[repo_mount] = {"bind": repo_path, "mode": "rw"}
        if extra_volumes:
            volumes.update(extra_volumes)

        session_info = None
        container = None

        try:
            # Build base environment first
            env = {
                "EGG_REPO_PATH": repo_path,
                "EGG_AGENT_ROLE": agent_role.value,
            }
            if issue_number is not None:
                env["EGG_ISSUE_NUMBER"] = str(issue_number)

            # Add extra environment
            if extra_env:
                env.update(extra_env)

            # Register gateway session so the container gets a session token
            # and proxy config. Skip for local mode — local pipelines don't
            # need gateway access (no git push, no PR creation).
            if mode != "local" and repos:
                try:
                    host_uid = int(os.environ.get("HOST_UID", 1000))
                    host_gid = int(os.environ.get("HOST_GID", 1000))
                    session_info = self.gateway.register_session(
                        container_id=container_name,
                        container_ip=EGG_CONTAINER_IP,
                        mode=mode,
                        repos=repos,
                        uid=host_uid,
                        gid=host_gid,
                        phase=phase,
                    )

                    # Get environment with session token and proxy config
                    gateway_env = self.gateway.get_container_env(
                        session_token=session_info.session_token,
                        issue_number=issue_number,
                        repo_path=repo_path,
                        agent_role=agent_role.value,
                        mode=mode,
                    )

                    # Add gateway environment to container env BEFORE creation
                    env.update(gateway_env)

                    logger.info(
                        "Pre-registered gateway session",
                        container_name=container_name,
                        session_token=session_info.session_token[:12] + "...",
                    )

                except GatewayError as e:
                    logger.warning(
                        "Failed to pre-register gateway session",
                        container_name=container_name,
                        error=str(e),
                    )
                    # Continue without session - container can still run
                    # but won't have gateway access

            # Create the container with full environment including gateway config
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
