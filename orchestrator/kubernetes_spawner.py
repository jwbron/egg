"""
Kubernetes spawner with integrated gateway session management.

Provides high-level agent spawning on Kubernetes that:
- Creates Kubernetes Jobs using KubernetesClient
- Registers sessions with gateway (token-only auth, no IP binding)
- Injects proper environment configuration
- Uses k8s hostPath volumes for worktrees
- Cleans up sessions on Job removal
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


# Must match the gateway's WORKTREE_BASE_DIR and volume mounts.
WORKTREE_BASE_DIR = Path("/home/egg/.egg-worktrees")

# Kubernetes service DNS for in-cluster communication
K8S_ORCHESTRATOR_URL = os.environ.get(
    "EGG_K8S_ORCHESTRATOR_URL",
    "http://orchestrator.egg-system.svc.cluster.local:9849",
)
K8S_GATEWAY_URL = os.environ.get(
    "EGG_K8S_GATEWAY_URL",
    "http://gateway.egg-system.svc.cluster.local:9848",
)

from container_backend import (
    JobOperationError,
    KubernetesClientError,
    PodNotFoundError,
)
from egg_agent import build_agent_command
from egg_container import (
    ensure_egg_state_dirs,
    git_shadow_mounts,
    phase_readonly_mounts,
)
from gateway_client import (
    GatewayClient,
    GatewayError,
    SessionInfo,
    get_gateway_client,
)
from kubernetes_client import (
    DEFAULT_AGENT_NAMESPACE,
    KubernetesClient,
    get_kubernetes_client,
)
from models import AgentRole, ContainerInfo

logger = get_logger("orchestrator.kubernetes_spawner")


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


@dataclass
class SpawnedContainer:
    """Information about a spawned container with gateway session."""

    container_info: ContainerInfo
    session_info: SessionInfo | None
    agent_role: AgentRole
    pipeline_id: str
    environment: dict[str, str]


class KubernetesSpawner:
    """Spawns Kubernetes Jobs with integrated gateway session management.

    Handles the full lifecycle:
    1. Validate gateway health
    2. Register gateway session (token-only, EGG_K8S_MODE=true)
    3. Create Kubernetes Job with proper env and volume mounts
    4. Clean up session on Job removal
    """

    DEFAULT_SANDBOX_IMAGE = os.environ.get(
        "EGG_SANDBOX_IMAGE", "egg:latest"
    )
    CONTAINER_NAME_FORMAT = "egg-{pipeline_id}-{role}"
    DEFAULT_AGENT_NAMESPACE = DEFAULT_AGENT_NAMESPACE

    def __init__(
        self,
        k8s_client: KubernetesClient | None = None,
        gateway_client: GatewayClient | None = None,
    ):
        """Initialize Kubernetes spawner.

        Args:
            k8s_client: Kubernetes client (default: singleton).
            gateway_client: Gateway client (default: singleton).
        """
        self._k8s = k8s_client
        self._gateway = gateway_client

    @property
    def k8s(self) -> KubernetesClient:
        """Get Kubernetes client (lazy initialization)."""
        if self._k8s is None:
            self._k8s = get_kubernetes_client()
        return self._k8s

    @property
    def gateway(self) -> GatewayClient:
        """Get Gateway client (lazy initialization)."""
        if self._gateway is None:
            self._gateway = get_gateway_client()
        return self._gateway

    def spawn_agent_job(
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
        extra_mounts: list[dict[str, str]] | None = None,
    ) -> SpawnedContainer:
        """Spawn a Kubernetes Job for an agent.

        Equivalent to ContainerSpawner.spawn_agent_container() but uses
        Kubernetes Jobs instead of Docker containers. Gateway sessions
        use token-only auth (EGG_K8S_MODE=true) instead of IP binding.

        Args:
            pipeline_id: Pipeline ID (e.g., "issue-496").
            agent_role: Agent role.
            issue_number: GitHub issue number (optional).
            repo_volumes: Mapping of repo_name -> host_path for mounts.
            mode: Gateway mode (public, private, or local).
            image: Container image (default: egg:latest).
            extra_env: Additional environment variables.
            wait_for_gateway: Wait for gateway health before spawning.
            repos: List of repositories for gateway session.
            phase: SDLC pipeline phase.
            command: Command to execute in the container.
            certs_volume: Volume name for gateway CA certs.
            branch: Git branch for worktree.
            extra_mounts: Additional volume mount specs.

        Returns:
            SpawnedContainer with Job and session info.

        Raises:
            ContainerSpawnError: If spawning fails.
        """
        job_name = self.CONTAINER_NAME_FORMAT.format(
            pipeline_id=pipeline_id,
            role=agent_role.value,
        )

        # Clean up any existing Job with the same name
        try:
            self.k8s.get_container_info(job_name)
            logger.info(
                "Found existing Job with same name, removing it",
                job_name=job_name,
            )
            self.remove_agent_job(job_name, force=True, cleanup_session=True)
        except PodNotFoundError:
            pass
        except KubernetesClientError as e:
            logger.debug(
                "Failed to clean up existing Job",
                job_name=job_name,
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

        # Host UID/GID for file ownership
        host_uid = int(os.environ.get("HOST_UID", 1000))
        host_gid = int(os.environ.get("HOST_GID", 1000))

        # Per-agent worktree isolation: create a dedicated worktree
        agent_worktree_id = f"{pipeline_id}-{agent_role.value}"
        if repo_volumes and repos:
            try:
                wt_result = self.gateway.create_worktrees(
                    container_id=agent_worktree_id,
                    repos=repos,
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
                    errors = wt_result.errors if wt_result else []
                    raise ContainerSpawnError(
                        f"Per-agent worktree creation returned no "
                        f"worktrees for {agent_worktree_id}: {errors}"
                    )
            except ContainerSpawnError:
                raise
            except GatewayError as e:
                details = e.details or {}
                logger.error(
                    "Per-agent worktree creation gateway error",
                    agent_worktree_id=agent_worktree_id,
                    error=str(e),
                    status_code=e.status_code,
                    details=details,
                )
                raise ContainerSpawnError(
                    f"Per-agent worktree creation failed for "
                    f"{agent_worktree_id}: {e}"
                ) from e
            except Exception as e:
                raise ContainerSpawnError(
                    f"Per-agent worktree creation failed for "
                    f"{agent_worktree_id}: {e}"
                ) from e

        # Build k8s volumes: repo volumes as hostPath mounts
        mounts: list = []
        volumes: dict[str, dict[str, str]] = {}
        if repo_volumes:
            for name, host_path in repo_volumes.items():
                volumes[host_path] = {
                    "bind": f"/home/egg/repos/{name}",
                    "mode": "rw",
                }

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

        # Convert MountSpec objects to k8s volume dict format
        for mount in mounts:
            source = mount.source or "/dev/null"
            mode = "ro" if mount.readonly else "rw"
            volumes[source] = {
                "bind": mount.destination,
                "mode": mode,
            }

        session_info = None

        try:
            # Register gateway session with token-only auth
            # (EGG_K8S_MODE=true means no IP binding)
            session_token = None
            agent_anchor_id = f"{agent_role.value}-{job_name[:8]}"
            try:
                session_info = self.gateway.register_session(
                    container_id=job_name,
                    container_ip="0.0.0.0",  # Token-only, no IP binding
                    mode=mode,
                    repos=repos,
                    uid=host_uid,
                    gid=host_gid,
                    phase=phase,
                    pipeline_id=pipeline_id,
                    agent_role=agent_role.value,
                    agent_anchor_id=agent_anchor_id,
                    issue_number=issue_number,
                    claude_code_version=os.environ.get(
                        "CLAUDE_CODE_VERSION"
                    ),
                    branch=branch,
                )
                session_token = session_info.session_token

                logger.info(
                    "Pre-registered gateway session (k8s token-only)",
                    job_name=job_name,
                    session_token=session_token[:12] + "...",
                )

            except GatewayError as e:
                raise ContainerSpawnError(
                    f"Failed to register gateway session for "
                    f"{job_name}: {e}"
                ) from e

            # Build environment variables
            environment: dict[str, str] = {
                "CONTAINER_ID": agent_worktree_id,
                "EGG_REPO_PATH": "/home/egg/repos",
                "EGG_AGENT_ROLE": agent_role.value,
                "EGG_PIPELINE_ID": pipeline_id,
                "EGG_ORCHESTRATOR_URL": K8S_ORCHESTRATOR_URL,
                "GATEWAY_URL": K8S_GATEWAY_URL,
                "EGG_K8S_MODE": "true",
                "AGENT_ANCHOR_ID": agent_anchor_id,
            }
            if session_token:
                environment["EGG_SESSION_TOKEN"] = session_token
            if issue_number is not None:
                environment["EGG_ISSUE_NUMBER"] = str(issue_number)
            if phase:
                environment["EGG_PHASE"] = phase
            if branch:
                environment["EGG_BRANCH"] = branch
            elif pipeline_id:
                environment["EGG_BRANCH"] = f"egg/{pipeline_id}/work"

            # Caller's extra_env overrides defaults
            if extra_env:
                environment.update(extra_env)

            # Create Kubernetes Job
            container = self.k8s.create_container(
                name=job_name,
                image=image or self.DEFAULT_SANDBOX_IMAGE,
                environment=environment,
                volumes=volumes if volumes else None,
                command=command,
                labels=labels,
            )

            # K8s Jobs auto-start; fetch current status
            container = self.k8s.start_container(container.container_id)

            logger.info(
                "Agent Job spawned",
                job_name=job_name,
                pipeline_id=pipeline_id,
                role=agent_role.value,
                has_session=session_info is not None,
            )

            return SpawnedContainer(
                container_info=container,
                session_info=session_info,
                agent_role=agent_role,
                pipeline_id=pipeline_id,
                environment=environment,
            )

        except KubernetesClientError as e:
            # Clean up gateway session if we registered one
            if session_info:
                try:
                    self.gateway.delete_session(
                        session_info.session_token
                    )
                except GatewayError:
                    pass
            # Clean up per-agent worktree
            try:
                self.gateway.delete_worktrees(
                    container_id=agent_worktree_id, force=True
                )
            except Exception:
                pass
            raise ContainerSpawnError(
                f"Failed to spawn Job: {e}"
            ) from e

    def stop_agent_job(
        self,
        job_name: str,
        cleanup_session: bool = True,
        timeout: int = 10,
    ) -> ContainerInfo:
        """Stop an agent Job and optionally clean up session.

        Args:
            job_name: Kubernetes Job name.
            cleanup_session: Whether to delete gateway session.
            timeout: Timeout in seconds (grace period).

        Returns:
            ContainerInfo after stopping.
        """
        try:
            info = self.k8s.stop_container(job_name, timeout=timeout)

            if cleanup_session:
                try:
                    self.gateway.delete_session_by_container(job_name)
                except GatewayError as e:
                    logger.warning(
                        "Failed to clean up gateway session",
                        job_name=job_name,
                        error=str(e),
                    )

            return info

        except PodNotFoundError:
            if cleanup_session:
                try:
                    self.gateway.delete_session_by_container(job_name)
                except GatewayError:
                    pass
            raise

    def remove_agent_job(
        self,
        job_name: str,
        force: bool = False,
        cleanup_session: bool = True,
    ) -> None:
        """Remove an agent Job and clean up session.

        Args:
            job_name: Kubernetes Job name.
            force: Force removal.
            cleanup_session: Whether to delete gateway session.
        """
        try:
            self.k8s.remove_container(job_name, force=force)
        finally:
            if cleanup_session:
                try:
                    self.gateway.delete_session_by_container(job_name)
                except GatewayError as e:
                    logger.warning(
                        "Failed to clean up gateway session",
                        job_name=job_name,
                        error=str(e),
                    )

    def list_pipeline_jobs(
        self,
        pipeline_id: str,
    ) -> list[ContainerInfo]:
        """List all Jobs for a pipeline.

        Args:
            pipeline_id: Pipeline ID.

        Returns:
            List of ContainerInfo.
        """
        return self.k8s.list_containers(
            labels={"egg.pipeline.id": pipeline_id},
        )

    def cleanup_pipeline(
        self,
        pipeline_id: str,
        force: bool = True,
    ) -> int:
        """Clean up all Jobs and sessions for a pipeline.

        Args:
            pipeline_id: Pipeline ID.
            force: Force removal.

        Returns:
            Number of Jobs removed.
        """
        jobs = self.list_pipeline_jobs(pipeline_id)
        removed = 0

        for job in jobs:
            try:
                self.remove_agent_job(
                    job.container_id,
                    force=force,
                    cleanup_session=True,
                )
                removed += 1
            except (PodNotFoundError, JobOperationError) as e:
                logger.warning(
                    "Failed to remove Job during cleanup",
                    job_name=job.container_id,
                    error=str(e),
                )

        # Clean up per-agent worktrees
        worktree_ids_to_clean = {pipeline_id}
        for job in jobs:
            labels = getattr(job, "labels", {}) or {}
            role = labels.get("egg.agent.role")
            if role:
                worktree_ids_to_clean.add(f"{pipeline_id}-{role}")
        # Scan filesystem for orphaned worktrees
        if WORKTREE_BASE_DIR.exists():
            prefix = f"{pipeline_id}-"
            try:
                for entry in WORKTREE_BASE_DIR.iterdir():
                    if entry.is_dir() and (
                        entry.name == pipeline_id
                        or entry.name.startswith(prefix)
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
                self.gateway.delete_worktrees(
                    container_id=wt_id, force=True
                )
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
            jobs_removed=removed,
        )

        return removed

    def detect_uncommitted_changes(
        self,
        pipeline_id: str,
        agent_role: str,
    ) -> dict | None:
        """Detect uncommitted changes in an agent's worktree.

        Checks the agent's worktree directly on the filesystem for
        uncommitted changes. Per-agent worktrees are at:
        /home/egg/.egg-worktrees/{pipeline_id}-{role}/{repo}/

        Returns:
            Dict with change info if uncommitted changes found,
            None otherwise.
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

    def spawn_overseer_job(
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
        """Spawn an overseer Job for phase-scoped health monitoring.

        The overseer runs without repository access (no git mounts)
        and monitors phase health via the orchestrator API.

        Args:
            pipeline_id: Pipeline ID.
            issue_number: GitHub issue number (optional).
            mode: Gateway mode (public or private).
            poll_interval: Polling interval in seconds.
            decision_model: LLM model for overseer decisions.
            image: Container image override.
            wait_for_gateway: Wait for gateway health.
            repos: List of repositories for gateway session.
            certs_volume: Certs volume name.

        Returns:
            SpawnedContainer with overseer Job info.
        """
        extra_env = {
            "EGG_OVERSEER_MODE": "true",
            "EGG_OVERSEER_POLL_INTERVAL": str(poll_interval),
            "EGG_OVERSEER_DECISION_MODEL": decision_model,
            "BASH_COMMAND_TIMEOUT": "0",
        }

        overseer_prompt = (
            f"You are the overseer agent for pipeline {pipeline_id}. "
            "CRITICAL: Your first action must be to run the pre-built "
            "monitoring script: "
            "`python3 /opt/egg-runtime/sandbox/overseer_monitor.py "
            "--once` "
            "DO NOT write your own monitoring loop or bash script. "
            "Run the script in single-cycle mode (`--once`) so you "
            "can classify and act between cycles. Each call outputs "
            "one JSON line to stdout. Read the output, classify "
            "alerts using the Haiku tier, decide corrective actions "
            "using the Sonnet tier, and execute them via egg-orch "
            "CLI commands. Then call the script with `--once` again. "
            "Repeat until the pipeline reaches a terminal state "
            "(complete, failed, or cancelled). After the pipeline "
            "ends, generate a final health summary."
        )
        command = build_agent_command(
            prompt=overseer_prompt,
            model=decision_model,
            max_turns=500,
        )

        return self.spawn_agent_job(
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
        """Create a spawn callable for ConcurrentPhaseExecutor.

        Returns a function with signature
        (role, branch, extra_env) that spawns a Job.

        Args:
            pipeline_id: Pipeline ID.
            issue_number: GitHub issue number.
            repo_volumes: Repo name to host path mappings.
            mode: Gateway mode (public/private/local).
            repos: Repositories for gateway session.
            phase: Current pipeline phase.
            sandbox_env: Base environment variables.
            image: Container image override.
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
            return self.spawn_agent_job(
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
_spawner: KubernetesSpawner | None = None


def get_kubernetes_spawner() -> KubernetesSpawner:
    """Get the singleton Kubernetes spawner.

    Returns:
        KubernetesSpawner instance.
    """
    global _spawner
    if _spawner is None:
        _spawner = KubernetesSpawner()
    return _spawner
