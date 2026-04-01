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


# Must match the gateway's WORKTREE_BASE_DIR and docker-compose volume mounts.
WORKTREE_BASE_DIR = Path("/home/egg/.egg-worktrees")


from sandbox_template import (
    ORCHESTRATOR_ISOLATED_IP,
    ORCHESTRATOR_PORT,
)

try:
    from egg_config import (
        EGG_CONTAINER_IP,
        GATEWAY_CONTAINER_NAME,
        GATEWAY_EXTERNAL_IP,
        GATEWAY_ISOLATED_IP,
        GATEWAY_PORT,
        ORCHESTRATOR_EXTERNAL_IP,
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
    GATEWAY_PORT = 9848  # noqa: EGG002
    GATEWAY_ISOLATED_IP = "172.32.0.2"
    GATEWAY_EXTERNAL_IP = "172.33.0.2"
    ORCHESTRATOR_EXTERNAL_IP = "172.33.0.3"

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
from egg_agent import build_agent_command
from egg_container import (
    ContainerNetworkConfig,
    MountSpec,
    build_sandbox_config,
    ensure_egg_state_dirs,
    git_shadow_mounts,
    phase_readonly_mounts,
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


def _host_to_local_volumes(repo_volumes: dict[str, str]) -> dict[str, str]:
    """Translate host paths to orchestrator-local paths for filesystem ops.

    The gateway returns worktree paths relative to the Docker host
    (e.g. ``/home/jwies/.egg-worktrees/...``), but the orchestrator
    container only sees these via a volume mount at ``/home/egg/...``.
    Uses the ``HOST_HOME`` env var to perform the translation.
    """
    host_home = os.environ.get("HOST_HOME", "").rstrip("/")
    container_home = "/home/egg"
    if not host_home or host_home == container_home:
        return repo_volumes
    return {
        name: path.replace(host_home, container_home, 1) if path.startswith(host_home) else path
        for name, path in repo_volumes.items()
    }


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
            mode: Gateway mode (public or private)

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
        branch: str | None = None,
        extra_mounts: list[MountSpec] | None = None,
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

        # Host UID/GID for file ownership in worktrees and mounts
        host_uid = int(os.environ.get("HOST_UID", 1000))
        host_gid = int(os.environ.get("HOST_GID", 1000))

        # Per-agent worktree isolation (#1481): create a dedicated worktree
        # for this agent so concurrent agents cannot stomp on each other's
        # uncommitted work.  The pipeline-level worktree (worktree_id ==
        # pipeline_id) is retained for orchestrator-side reads (contracts,
        # drafts); agents get their own worktree branched from the same ref.
        agent_worktree_id = f"{pipeline_id}-{agent_role.value}"
        if repo_volumes and repos:
            try:
                wt_repos = repos
                wt_result = self.gateway.create_worktrees(
                    container_id=agent_worktree_id,
                    repos=wt_repos,
                    uid=host_uid,
                    gid=host_gid,
                    base_branch=branch,
                )
                if wt_result and wt_result.success and wt_result.worktrees:
                    repo_volumes = wt_result.worktrees
                    logger.info(
                        "Per-agent worktree created",
                        agent_worktree_id=agent_worktree_id,
                        role=agent_role.value,
                        pipeline_id=pipeline_id,
                        worktrees=list(repo_volumes.keys()),
                    )
                else:
                    logger.warning(
                        "Per-agent worktree creation returned no worktrees, "
                        "falling back to shared pipeline volumes",
                        agent_worktree_id=agent_worktree_id,
                        errors=wt_result.errors if wt_result else [],
                    )
            except Exception as e:
                logger.warning(
                    "Per-agent worktree creation failed, using shared volumes",
                    agent_worktree_id=agent_worktree_id,
                    error=str(e),
                )

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

            # Phase-based readonly mounts: make .egg-state/ subdirectories
            # readonly during implement phase to prevent direct modifications.
            # Translate host paths to orchestrator-local paths for filesystem ops
            # (the orchestrator can't access host paths like /home/jwies/...).
            if phase:
                local_volumes = _host_to_local_volumes(repo_volumes)
                ensure_egg_state_dirs(
                    local_volumes,
                    uid=host_uid,
                    gid=host_gid,
                    phase=phase,
                    agent_role=agent_role.value,
                )
                mounts.extend(
                    phase_readonly_mounts(
                        repo_volumes,
                        phase,
                        local_volumes=local_volumes,
                        agent_role=agent_role.value,
                    )
                )
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

        try:
            # Register gateway session so the container gets a session token.
            # Even local-mode containers need a session: the sandbox git/gh
            # wrappers require EGG_SESSION_TOKEN, and the gateway enforces
            # local-mode restrictions (push blocking) at the session level.
            session_token = None
            agent_anchor_id = f"{agent_role.value}-{container_name[:8]}"
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
                    agent_anchor_id=agent_anchor_id,
                    issue_number=issue_number,
                    claude_code_version=os.environ.get("CLAUDE_CODE_VERSION"),
                    branch=branch,
                )
                session_token = session_info.session_token

                logger.info(
                    "Pre-registered gateway session",
                    container_name=container_name,
                    session_token=session_token[:12] + "...",
                )

            except GatewayError as e:
                raise ContainerSpawnError(
                    f"Failed to register gateway session for {container_name}: {e}"
                ) from e

            # Build spawner-specific env vars that override the shared defaults.
            # CONTAINER_ID must match the worktree container_id so the gateway
            # git proxy can map /home/egg/repos/<name> to the correct worktree
            # at /home/egg/.egg-worktrees/<id>/<name>.
            #
            # Per-agent worktree isolation (#1481): CONTAINER_ID is now per-agent.
            # agent_worktree_id computed once at the top of this function.
            orchestrator_host = (
                ORCHESTRATOR_ISOLATED_IP if mode == "private" else ORCHESTRATOR_EXTERNAL_IP
            )
            orchestrator_url = f"http://{orchestrator_host}:{ORCHESTRATOR_PORT}"
            spawner_env: dict[str, str] = {
                "CONTAINER_ID": agent_worktree_id,
                "EGG_REPO_PATH": "/home/egg/repos",
                "EGG_AGENT_ROLE": agent_role.value,
                "EGG_PIPELINE_ID": pipeline_id,
                "EGG_ORCHESTRATOR_URL": orchestrator_url,
            }
            if issue_number is not None:
                spawner_env["EGG_ISSUE_NUMBER"] = str(issue_number)
            if phase:
                spawner_env["EGG_PHASE"] = phase
            if branch:
                spawner_env["EGG_BRANCH"] = branch
            elif pipeline_id:
                spawner_env["EGG_BRANCH"] = f"egg/{pipeline_id}/work"

            # Set agent anchor ID for post-compaction recovery.
            # Format: {role}-{short_container_id} where short_container_id is first 8 chars.
            # This ID is used by the gateway to scope anchor file writes.
            spawner_env["AGENT_ANCHOR_ID"] = agent_anchor_id

            # Caller's extra_env overrides spawner defaults
            if extra_env:
                spawner_env.update(extra_env)

            if extra_mounts:
                mounts.extend(extra_mounts)

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
            # Clean up per-agent worktree created before Docker spawn (#1494 review)
            try:
                self.gateway.delete_worktrees(container_id=agent_worktree_id, force=True)
            except Exception:
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

        # Clean up per-agent worktrees (#1481).  Each agent gets a worktree
        # with container_id "{pipeline_id}-{role}".  We collect worktree IDs
        # from both container labels AND the filesystem, because containers
        # may have been removed (OOM kill, daemon cleanup) before this runs.
        # (#1494 review)
        worktree_ids_to_clean = {pipeline_id}
        for container in containers:
            labels = getattr(container, "labels", {}) or {}
            role = labels.get("egg.agent.role")
            if role:
                worktree_ids_to_clean.add(f"{pipeline_id}-{role}")
        # Also scan filesystem for any per-agent worktrees whose containers
        # no longer exist (e.g. OOM-killed, daemon-cleaned).
        if WORKTREE_BASE_DIR.exists():
            prefix = f"{pipeline_id}-"
            try:
                for entry in WORKTREE_BASE_DIR.iterdir():
                    if entry.is_dir() and (
                        entry.name == pipeline_id or entry.name.startswith(prefix)
                    ):
                        worktree_ids_to_clean.add(entry.name)
            except Exception as e:
                logger.warning(
                    "Filesystem worktree scan failed during cleanup",
                    pipeline_id=pipeline_id,
                    error=str(e),
                )

        for wt_id in worktree_ids_to_clean:
            try:
                self.gateway.delete_worktrees(container_id=wt_id, force=True)
                logger.info(
                    "Worktree cleaned up",
                    pipeline_id=pipeline_id,
                    worktree_id=wt_id,
                )
            except Exception as e:
                logger.warning(
                    "Worktree cleanup failed",
                    pipeline_id=pipeline_id,
                    worktree_id=wt_id,
                    error=str(e),
                )

        logger.info(
            "Pipeline cleanup complete",
            pipeline_id=pipeline_id,
            containers_removed=removed,
        )

        return removed

    def detect_uncommitted_changes(
        self,
        pipeline_id: str,
        agent_role: str,
    ) -> dict | None:
        """Detect uncommitted changes in an agent's worktree after container exit.

        Checks the agent's worktree directly on the filesystem for uncommitted
        changes. Per-agent worktrees (#1481) are at:
        /home/egg/.egg-worktrees/{pipeline_id}-{role}/{repo}/

        Returns:
            Dict with change info if uncommitted changes found, None otherwise.
        """
        import subprocess

        agent_worktree_id = f"{pipeline_id}-{agent_role}"
        worktree_base = WORKTREE_BASE_DIR / agent_worktree_id

        if not worktree_base.exists():
            return None

        for repo_dir in worktree_base.iterdir():
            if not repo_dir.is_dir():
                continue
            try:
                result = subprocess.run(
                    [
                        "/usr/bin/git",
                        "-c",
                        "safe.directory=*",
                        "-c",
                        "core.hooksPath=/dev/null",
                        "-c",
                        "gc.auto=0",
                        "status",
                        "--porcelain",
                    ],
                    cwd=str(repo_dir),
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                if result.returncode == 0 and result.stdout.strip():
                    files = [
                        line[3:].strip()
                        for line in result.stdout.splitlines()
                        if line and len(line) > 3
                    ]
                    logger.info(
                        "Agent exited with uncommitted changes",
                        event_type="agent_uncommitted_changes",
                        pipeline_id=pipeline_id,
                        agent_role=agent_role,
                        worktree_path=str(repo_dir),
                        file_count=len(files),
                        changed_files=files[:20],
                    )
                    return {
                        "pipeline_id": pipeline_id,
                        "agent_role": agent_role,
                        "worktree_id": agent_worktree_id,
                        "worktree_path": str(repo_dir),
                        "file_count": len(files),
                        "changed_files": files[:20],
                    }
            except Exception as e:
                logger.warning(
                    "Failed to check worktree status",
                    repo_dir=str(repo_dir),
                    error=str(e),
                )
        return None

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

    def spawn_overseer_container(
        self,
        pipeline_id: str,
        issue_number: int | None = None,
        mode: str = "public",
        poll_interval: int = 30,
        decision_model: str = "sonnet",
        image: str | None = None,
        wait_for_gateway: bool = True,
        repos: list[str] | None = None,
        certs_volume: str | None = None,
    ) -> SpawnedContainer:
        """Spawn an overseer container for pipeline health monitoring.

        The overseer runs without repository access (no git mounts) and
        monitors pipeline health via the orchestrator API.  It receives
        overseer-specific environment variables for polling and decision-making.

        Args:
            pipeline_id: Pipeline ID.
            issue_number: GitHub issue number (optional).
            mode: Gateway mode (public or private).
            poll_interval: Polling interval in seconds for health checks.
            decision_model: LLM model for overseer decision-making tier.
            image: Docker image override.
            wait_for_gateway: Wait for gateway health before spawning.
            repos: List of repositories for gateway session.
            certs_volume: Certs volume name.

        Returns:
            SpawnedContainer with overseer container and session info.
        """
        extra_env = {
            "EGG_OVERSEER_MODE": "true",
            "EGG_OVERSEER_POLL_INTERVAL": str(poll_interval),
            "EGG_OVERSEER_DECISION_MODEL": decision_model,
            # Disable per-command bash timeout for the overseer.  The overseer
            # runs a continuous monitoring loop for the entire pipeline lifetime
            # (30+ minutes).  The default 300s timeout kills the loop mid-cycle
            # (see issue #1333).  Setting to "0" disables the timeout wrapper.
            "BASH_COMMAND_TIMEOUT": "0",
        }

        # Build an Agent SDK command for the overseer.  The overseer is a
        # long-running monitor that polls the orchestrator API — it cannot
        # use ``claude --print`` (which requires a one-shot prompt and exits).
        # The overseer rules in sandbox/agent-config/rules/overseer.md are picked
        # up automatically by the SDK via setting_sources=["project","user"].
        overseer_prompt = (
            f"You are the overseer agent for pipeline {pipeline_id}. "
            "CRITICAL: Your first action must be to run the pre-built "
            "monitoring script: "
            "`python3 /opt/egg-runtime/sandbox/overseer_monitor.py` "
            "DO NOT write your own monitoring loop or bash script. "
            "The script polls the orchestrator for pipeline status, health "
            "alerts, progress events, and escalation messages. It outputs "
            "one JSON line per cycle to stdout. Read the output and act on "
            "anomalies: classify alerts using the Haiku tier, decide "
            "corrective actions using the Sonnet tier, and execute them "
            "via egg-orch CLI commands. The script handles heartbeats and "
            "exits automatically when the pipeline reaches a terminal "
            "state (complete, failed, or cancelled). After the script "
            "exits, generate a final health summary."
        )
        command = build_agent_command(
            prompt=overseer_prompt,
            model=decision_model,
            max_turns=500,
        )

        return self.spawn_agent_container(
            pipeline_id=pipeline_id,
            agent_role=AgentRole.OVERSEER,
            issue_number=issue_number,
            repo_volumes=None,
            mode=mode,
            image=image,
            extra_env=extra_env,
            wait_for_gateway=wait_for_gateway,
            repos=repos,
            certs_volume=certs_volume,
            command=command,
        )

    def create_concurrent_spawn_fn(
        self,
        pipeline_id: str,
        issue_number: int | None,
        repo_volumes: dict[str, str] | None,
        mode: str,
        repos: list[str] | None,
        phase: str | None,
        sandbox_env: dict[str, str] | None = None,
        image: str | None = None,
        certs_volume: str | None = None,
    ):
        """Create a spawn callable compatible with ConcurrentPhaseExecutor.

        Returns a function with signature (role, branch, extra_env) that spawns
        a container via spawn_agent_container.

        Args:
            pipeline_id: Pipeline ID.
            issue_number: GitHub issue number.
            repo_volumes: Repo name to host path mappings.
            mode: Gateway mode (public/private/local).
            repos: Repositories for gateway session.
            phase: Current pipeline phase.
            sandbox_env: Base environment variables.
            image: Docker image override.
            certs_volume: Certs volume name.

        Returns:
            Callable suitable for ConcurrentPhaseExecutor.spawn_fn.
        """

        def _spawn(
            role: AgentRole,
            branch: str | None = None,
            extra_env: dict[str, str] | None = None,
            command: list[str] | None = None,
        ) -> SpawnedContainer:
            merged_env = {**(sandbox_env or {}), **(extra_env or {})}
            return self.spawn_agent_container(
                pipeline_id=pipeline_id,
                agent_role=role,
                issue_number=issue_number,
                repo_volumes=repo_volumes,
                mode=mode,
                image=image,
                extra_env=merged_env,
                repos=repos,
                phase=phase,
                certs_volume=certs_volume,
                branch=branch,
                command=command,
            )

        return _spawn


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
