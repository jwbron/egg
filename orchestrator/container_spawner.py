"""
Container spawner with integrated gateway session management.

Provides high-level container spawning that:
- Creates Docker containers using the shared config builder
- Registers sessions with gateway
- Injects proper environment configuration (GATEWAY_URL, proxy, DNS, etc.)
- Adds .git shadow mounts and --add-host / extra_hosts for gateway hostname
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
        GATEWAY_CONTAINER_NAME,
        GATEWAY_EXTERNAL_IP,
        GATEWAY_ISOLATED_IP,
        GATEWAY_PORT,
    )
    from egg_config import (
        EGG_EXTERNAL_NETWORK as _DEFAULT_EXTERNAL_NETWORK,
    )
    from egg_config import (
        EGG_ISOLATED_NETWORK as _DEFAULT_ISOLATED_NETWORK,
    )
except ImportError:
    _DEFAULT_ISOLATED_NETWORK = "egg-isolated"
    _DEFAULT_EXTERNAL_NETWORK = "egg-external"
    EGG_CONTAINER_IP = "172.32.0.10"
    GATEWAY_CONTAINER_NAME = "egg-gateway"
    GATEWAY_PORT = 9848
    GATEWAY_ISOLATED_IP = "172.32.0.2"
    GATEWAY_EXTERNAL_IP = "172.33.0.2"

# Allow override via environment for test stacks with non-standard network names
EGG_ISOLATED_NETWORK = os.environ.get("EGG_ISOLATED_NETWORK", _DEFAULT_ISOLATED_NETWORK)
EGG_EXTERNAL_NETWORK = os.environ.get("EGG_EXTERNAL_NETWORK", _DEFAULT_EXTERNAL_NETWORK)

from docker_client import (
    ContainerNotFoundError,
    ContainerOperationError,
    DockerClient,
    DockerClientError,
    get_docker_client,
)
from egg_container import (
    ContainerNetworkConfig,
    MountSpec,
    build_sandbox_config,
    git_shadow_mounts,
    to_dockerpy_kwargs,
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
    2. Register gateway session
    3. Build container config using shared builder
    4. Create Docker container via docker-py
    5. Start container
    6. Clean up session on container removal
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

    def _build_network_config(self, mode: str) -> ContainerNetworkConfig:
        """Build ContainerNetworkConfig for the given gateway mode.

        Args:
            mode: Gateway mode (public, private, or local)

        Returns:
            ContainerNetworkConfig with correct network, IPs, and repo_mode.
        """
        if mode == "private":
            return ContainerNetworkConfig(
                network_name=EGG_ISOLATED_NETWORK,
                gateway_hostname=GATEWAY_CONTAINER_NAME,
                gateway_ip=GATEWAY_ISOLATED_IP,
                gateway_port=GATEWAY_PORT,
                repo_mode="private",
            )
        elif mode == "local":
            # Local mode: isolated network but no proxy/DNS lockdown
            return ContainerNetworkConfig(
                network_name=EGG_ISOLATED_NETWORK,
                gateway_hostname=GATEWAY_CONTAINER_NAME,
                gateway_ip=GATEWAY_ISOLATED_IP,
                gateway_port=GATEWAY_PORT,
                repo_mode="public",
            )
        else:  # "public"
            return ContainerNetworkConfig(
                network_name=EGG_EXTERNAL_NETWORK,
                gateway_hostname=GATEWAY_CONTAINER_NAME,
                gateway_ip=GATEWAY_EXTERNAL_IP,
                gateway_port=GATEWAY_PORT,
                repo_mode="public",
            )

    def spawn_agent_container(
        self,
        pipeline_id: str,
        agent_role: AgentRole,
        issue_number: int | None = None,
        repo_volumes: dict[str, str] | None = None,
        mode: str = "public",
        image: str | None = None,
        extra_env: dict[str, str] | None = None,
        wait_for_gateway: bool = True,
        repos: list[str] | None = None,
        phase: str | None = None,
        command: list[str] | None = None,
        certs_volume: str | None = None,
    ) -> SpawnedContainer:
        """Spawn a container for an agent.

        Uses the shared ``build_sandbox_config()`` to ensure the container
        gets the same GATEWAY_URL, proxy, DNS, and .git shadow configuration
        as CLI-launched containers.

        Args:
            pipeline_id: Pipeline ID (e.g., "issue-496" or "local-a1b2c3d4")
            agent_role: Agent role
            issue_number: GitHub issue number (optional for local pipelines)
            repo_volumes: Mapping of repo_name -> host_path for volume mounts.
                Each entry is mounted at /home/egg/repos/<name> and gets a
                .git shadow mount to force git operations through the gateway.
            mode: Gateway mode (public, private, or local)
            image: Docker image (default: egg-sandbox:latest)
            extra_env: Additional environment variables
            wait_for_gateway: Wait for gateway health before spawning
            repos: List of repositories in owner/name format for gateway session
            phase: SDLC pipeline phase for gateway session
            command: Command to execute in the container
            certs_volume: Docker named volume for gateway CA certs

        Returns:
            SpawnedContainer with container and session info

        Raises:
            ContainerSpawnError: If spawning fails
        """
        container_name = self.CONTAINER_NAME_FORMAT.format(
            pipeline_id=pipeline_id,
            role=agent_role.value,
        )

        # Clean up any existing container with the same name (e.g., from a canceled pipeline)
        # This prevents 409 Conflict errors when restarting pipelines.
        # The docker client adds "egg-sandbox-" prefix to the name, so we need to check
        # for the full prefixed name that Docker will use.
        full_container_name = f"{self.docker.CONTAINER_PREFIX}{container_name}"
        try:
            info = self.docker.get_container_info(full_container_name)
            logger.info(
                "Found existing container with same name, removing it",
                container_name=full_container_name,
                existing_id=info.container_id[:12],
            )
            self.remove_agent_container(
                info.container_id,
                force=True,
                cleanup_session=True,
            )
        except ContainerNotFoundError:
            # No existing container, good to proceed
            pass
        except DockerClientError as e:
            # Couldn't remove existing container
            # Log it but continue - if it really exists, Docker will give a clear error
            logger.debug(
                "Failed to clean up existing container",
                container_name=full_container_name,
                error=str(e),
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

        # Build mounts: repo volumes + .git shadows + certs
        mounts: list[MountSpec] = []
        if repo_volumes:
            for name, host_path in repo_volumes.items():
                mounts.append(
                    MountSpec(
                        mount_type="bind",
                        source=host_path,
                        destination=f"/home/egg/repos/{name}",
                    )
                )
            # Shadow .git in each mounted repo to force gateway git operations.
            # Orchestrator can't stat host paths, so assume_worktree=True (/dev/null bind).
            mounts.extend(git_shadow_mounts(repo_volumes, assume_worktree=True))
        if certs_volume:
            mounts.append(
                MountSpec(
                    mount_type="volume",
                    source=certs_volume,
                    destination="/shared/certs",
                    readonly=True,
                )
            )

        # Build network config from mode
        net_config = self._build_network_config(mode)

        session_info = None
        container = None
        host_uid = int(os.environ.get("HOST_UID", 1000))
        host_gid = int(os.environ.get("HOST_GID", 1000))

        try:
            # Register gateway session so the container gets a session token.
            # Even local-mode containers need a session: the sandbox git/gh
            # wrappers require EGG_SESSION_TOKEN, and the gateway enforces
            # local-mode restrictions (push blocking) at the session level.
            session_token = None
            try:
                session_info = self.gateway.register_session(
                    container_id=container_name,
                    container_ip=EGG_CONTAINER_IP,
                    mode=mode,
                    repos=repos,
                    uid=host_uid,
                    gid=host_gid,
                    phase=phase,
                    pipeline_id=pipeline_id,
                    agent_role=agent_role.value,
                    issue_number=issue_number,
                    claude_code_version=os.environ.get("CLAUDE_CODE_VERSION"),
                )
                session_token = session_info.session_token

                logger.info(
                    "Pre-registered gateway session",
                    container_name=container_name,
                    session_token=session_token[:12] + "...",
                )

            except GatewayError as e:
                logger.warning(
                    "Failed to pre-register gateway session",
                    container_name=container_name,
                    error=str(e),
                )
                # Continue without session - container can still run
                # but won't have gateway access

            # Build spawner-specific env vars that override the shared defaults.
            # CONTAINER_ID must match the worktree container_id so the gateway
            # git proxy can map /home/egg/repos/<name> to the correct worktree
            # at /home/egg/.egg-worktrees/<id>/<name>.
            spawner_env: dict[str, str] = {
                "CONTAINER_ID": pipeline_id,
                "EGG_REPO_PATH": "/home/egg/repos",
                "EGG_AGENT_ROLE": agent_role.value,
                "EGG_PIPELINE_ID": pipeline_id,
            }
            if issue_number is not None:
                spawner_env["EGG_ISSUE_NUMBER"] = str(issue_number)
            # Caller's extra_env overrides spawner defaults
            if extra_env:
                spawner_env.update(extra_env)

            # Build the unified container config using the shared builder.
            # This sets GATEWAY_URL (hostname-based), proxy vars, DNS lockdown,
            # extra_hosts for gateway hostname, etc.
            config = build_sandbox_config(
                container_name=container_name,
                image=image or self.DEFAULT_SANDBOX_IMAGE,
                network=net_config,
                session_token=session_token,
                runtime_uid=host_uid,
                runtime_gid=host_gid,
                extra_env=spawner_env,
                mounts=mounts,
                labels=labels,
                command=command,
            )

            # Convert to docker-py kwargs and create the container
            kwargs = to_dockerpy_kwargs(config)
            container = self.docker.create_container(**kwargs)

            logger.info(
                "Container created",
                container_id=container.container_id[:12],
                pipeline_id=pipeline_id,
                role=agent_role.value,
            )

            # Start the container
            container = self.docker.start_container(container.container_id)

            # Update gateway session with actual container IP
            if session_token:
                try:
                    actual_ip = self._get_container_ip(container.container_id)
                    self.gateway.update_session(
                        session_token=session_token,
                        container_id=container.container_id,
                        container_ip=actual_ip,
                    )
                    logger.info(
                        "Updated session with actual container IP",
                        container_id=container.container_id[:12],
                        actual_ip=actual_ip,
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to update session IP",
                        container_id=container.container_id[:12],
                        error=str(e),
                    )

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
                environment=config.environment,
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

            for net_name in (EGG_ISOLATED_NETWORK, EGG_EXTERNAL_NETWORK):
                if net_name in networks:
                    ip = networks[net_name].get("IPAddress")
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
