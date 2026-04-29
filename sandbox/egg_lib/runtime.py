"""Container execution for egg.

This module handles running containers in interactive and exec modes.

The gateway-managed worktree architecture:
- Gateway creates/manages worktrees before container starts
- Container mounts only working directory (no direct git metadata access)
- All git operations route through gateway API
- Gateway handles worktree cleanup when containers exit

Per-container session mode:
- Launcher registers session with gateway BEFORE container starts
- Session specifies repo visibility mode (private/public)
- Container receives EGG_SESSION_TOKEN for authenticated requests
- Gateway enforces mode on all git/gh operations
"""

import fcntl
import ipaddress
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from egg_container import (
    LIFECYCLE_FLAGS_INDEX,
    ContainerNetworkConfig,
    SandboxContainerConfig,
    build_sandbox_config,
    build_sandbox_docker_cmd,
    git_shadow_mounts,
    mount_spec_to_cli_args,
    to_k8s_job_kwargs,
)

# Runtime backend selection: "docker" (default) or "kubernetes"
EGG_RUNTIME = os.environ.get("EGG_RUNTIME", "docker")

# Import statusbar for quiet mode
from statusbar import status_finish

from .auth import get_anthropic_api_key
from .config import (
    GATEWAY_PORT,
    get_local_repos,
)
from .container_logging import (
    extract_task_id_from_command,
    extract_thread_ts_from_task_file,
    get_docker_log_config,
    save_container_logs,
)
from .context import get_context
from .docker import build_image, create_dockerfile, image_exists
from .gateway import (
    create_session,
    delete_session,
    delete_worktrees,
)
from .output import error, get_quiet_mode, info, warn
from .setup_flow import add_standard_mounts

# Valid repo_mode values
VALID_REPO_MODES = ("private", "public")


def _get_repos_config_file() -> Path:
    """Return the context-aware path to repositories.yaml.

    Uses RuntimeContext.config_dir when set (e.g. in GHA mode where
    EGG_CONFIG_DIR points to an ephemeral temp directory), falling
    back to the default ~/.config/egg/repositories.yaml.
    """
    ctx = get_context()
    return ctx.config_dir / "repositories.yaml"


def _get_reserved_ips(subnet: str, gateway_ip: str) -> set[str]:
    """Derive reserved IPs from subnet and gateway IP.

    Reserved IPs are the Docker gateway (*.*.*.1) and the egg-gateway
    sidecar (provided ``gateway_ip``).
    """
    base = subnet.rsplit(".", 1)[0]  # e.g. "172.32.0"
    return {f"{base}.1", gateway_ip}


def _validate_repo_mode(repo_mode: str | None) -> None:
    """Validate the repo_mode parameter.

    Args:
        repo_mode: Repository visibility mode (must be "private" or "public")

    Raises:
        ValueError: If repo_mode is not None and not a valid value
    """
    if repo_mode is not None and repo_mode not in VALID_REPO_MODES:
        raise ValueError(
            f"Invalid repo_mode: '{repo_mode}'. Must be one of: {', '.join(VALID_REPO_MODES)}"
        )


def _get_container_network_config(
    repo_mode: str | None,
) -> ContainerNetworkConfig:
    """Get network configuration for a container based on repo_mode.

    This centralizes the network selection logic to prevent divergence between
    run_claude() and exec_in_new_container().

    Args:
        repo_mode: Repository visibility mode ("private" or "public")

    Returns:
        A ContainerNetworkConfig with all mode-specific network parameters.
    """
    ctx = get_context()

    if repo_mode == "private":
        return ContainerNetworkConfig(
            network_name=ctx.isolated_network,
            gateway_hostname=ctx.gateway_container_name,
            gateway_ip=ctx.gateway_isolated_ip,
            gateway_port=ctx.gateway_port,
            repo_mode="private",
            proxy_url=f"http://{ctx.gateway_container_name}:{ctx.gateway_proxy_port}",
        )
    else:
        return ContainerNetworkConfig(
            network_name=ctx.external_network,
            gateway_hostname=ctx.gateway_container_name,
            gateway_ip=ctx.gateway_external_ip,
            gateway_port=ctx.gateway_port,
            repo_mode="public",
        )


def _get_repo_owner_name(repo_path: Path) -> str | None:
    """Get owner/repo from git remote URL.

    Args:
        repo_path: Path to the git repository

    Returns:
        "owner/repo" string, or None if not parseable
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        )
        url = result.stdout.strip()

        # Parse SSH format: git@github.com:owner/repo.git
        if url.startswith("git@"):
            # git@github.com:owner/repo.git -> owner/repo
            path = url.split(":", 1)[-1]
            if path.endswith(".git"):
                path = path[:-4]
            return path

        # Parse HTTPS format: https://github.com/owner/repo.git
        if "github.com" in url:
            # Extract path after github.com
            parts = url.split("github.com")[-1]
            path = parts.lstrip("/:")
            if path.endswith(".git"):
                path = path[:-4]
            return path

        return None
    except subprocess.CalledProcessError, IndexError:
        return None


def _allocate_container_ip(
    network: str | None = None, exclude_ips: set[str] | None = None
) -> str | None:
    """Allocate an available IP address from the specified network.

    Pre-allocates an IP before container start for session-container binding.
    The IP is used to verify requests come from the expected container.

    Uses a file lock to serialize concurrent allocations, preventing race
    conditions where two processes inspect the network simultaneously and
    both pick the same "next available" IP.

    Args:
        network: Docker network name (defaults to ctx.isolated_network)
        exclude_ips: Additional IPs to skip (e.g., previously failed allocations)

    Returns:
        Available IP address string, or None if allocation fails
    """
    ctx = get_context()
    if network is None:
        network = ctx.isolated_network

    # Select subnet and reserved IPs based on network
    if network == ctx.external_network:
        subnet_str = ctx.external_subnet
        reserved_ips = _get_reserved_ips(ctx.external_subnet, ctx.gateway_external_ip)
    else:
        subnet_str = ctx.isolated_subnet
        reserved_ips = _get_reserved_ips(ctx.isolated_subnet, ctx.gateway_isolated_ip)

    # Use a file lock to serialize concurrent IP allocations.
    # This prevents two egg processes from inspecting the network at the
    # same time and picking the same "next available" IP.
    # Note: the lock is released before `docker run`, so two processes can
    # still allocate sequentially, both complete before either starts a
    # container, and collide. The retry logic in callers handles this
    # remaining race.
    lock_path = Path(f"/tmp/egg-ip-alloc-{network}.lock")
    lock_fd = None
    try:
        lock_fd = open(lock_path, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        # Get network info to find assigned IPs
        result = subprocess.run(
            ["docker", "network", "inspect", network, "--format", "{{json .Containers}}"],
            capture_output=True,
            text=True,
            check=True,
        )
        containers_json = result.stdout.strip()

        # Parse assigned IPs from running containers
        assigned_ips = set(reserved_ips)
        if exclude_ips:
            assigned_ips.update(exclude_ips)
        if containers_json and containers_json != "null":
            containers = json.loads(containers_json)
            for container_info in containers.values():
                ip = container_info.get("IPv4Address", "")
                if ip:
                    # Remove CIDR suffix (e.g., "172.32.0.3/24" -> "172.32.0.3")
                    assigned_ips.add(ip.split("/")[0])

        # Find next available IP in subnet
        subnet = ipaddress.ip_network(subnet_str)
        for ip in subnet.hosts():
            ip_str = str(ip)
            if ip_str not in assigned_ips:
                return ip_str

        warn("No available IPs in network subnet")
        return None

    except subprocess.CalledProcessError as e:
        warn(f"Failed to inspect network for IP allocation: {e}")
        return None
    except json.JSONDecodeError as e:
        warn(f"Failed to parse network info: {e}")
        return None
    except Exception as e:
        warn(f"IP allocation failed: {e}")
        return None
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
            except OSError:
                pass


def _cleanup_worktrees(container_id: str, force: bool = True) -> None:
    """Clean up gateway worktrees for a container.

    Called when container exits to release worktree resources.

    Args:
        container_id: Container identifier
        force: Force removal even with uncommitted changes
    """
    try:
        _success_flag, _deleted, errors = delete_worktrees(container_id, force=force)
        if errors:
            for err in errors:
                warn(f"Worktree cleanup warning: {err}")
    except Exception as e:
        warn(f"Worktree cleanup failed: {e}")


def _cleanup_session(session_token: str | None, container_id: str) -> None:
    """Clean up session and worktrees for a container.

    Called when container exits to release session and worktree resources.
    Retries transient gateway failures with exponential backoff to handle
    cases where the gateway is briefly unreachable during shutdown.

    Args:
        session_token: Session token (if available)
        container_id: Container identifier
    """
    if session_token:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                success_flag, err = delete_session(session_token)
                if success_flag:
                    return
                if attempt < max_retries - 1:
                    time.sleep(1 << attempt)  # 1s, 2s backoff
                    continue
                if err:
                    warn(f"Session cleanup warning: {err}")
            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(1 << attempt)
                    continue
                warn(f"Session cleanup failed: {e}")
        # All retries exhausted — gateway didn't clean up, try local worktree cleanup
        _cleanup_worktrees(container_id)
    else:
        # Fall back to worktree cleanup only
        _cleanup_worktrees(container_id)


def _setup_session_repos(
    container_id: str,
    container_ip: str,
    mode: str,
    mount_args: list[str],
    quiet: bool = False,
    phase: str | None = None,
    issue_number: int | None = None,
    pr_number: int | None = None,
    pipeline_id: str | None = None,
    agent_role: str | None = None,
) -> tuple[str | None, dict[str, Path], list[str]]:
    """Configure repository mounts using session-based visibility filtering.

    This is the per-container repository mode flow. It:
    1. Creates a session with the gateway, specifying the mode
    2. Gateway filters repos based on visibility (private=private/internal, public=public)
    3. Gateway creates worktrees for filtered repos
    4. Returns session token and worktree mounts

    Args:
        container_id: Unique container identifier
        container_ip: Container's IP address on the Docker network
        mode: Repository visibility mode ("private" or "public")
        mount_args: List to append mount arguments to
        quiet: Suppress output
        phase: SDLC pipeline phase (e.g., "refine", "plan", "implement", "pr")
        issue_number: GitHub issue number for checkpoint metadata
        pr_number: GitHub PR number for checkpoint metadata
        pipeline_id: Pipeline run ID for multi-agent correlation
        agent_role: Agent role (e.g., "coder", "tester") for checkpoint metadata

    Returns:
        Tuple of (session_token, repos_dict, filtered_repos)
        - session_token: Token for container authentication
        - repos_dict: Dict of repo_name -> repo_path for tracking
        - filtered_repos: List of repos that passed visibility filtering
    """
    repos: dict[str, Path] = {}
    local_repos = get_local_repos(config_file=_get_repos_config_file())

    if not local_repos:
        if not quiet:
            info("No local repositories configured.")
        return None, repos, []

    # Convert local repos to owner/repo format for visibility checking.
    # Repos with no GitHub remote are collected separately as local-only repos;
    # they skip the GitHub visibility check and are mounted directly in private mode.
    repo_list = []
    local_only_repo_names = []
    for repo_path in local_repos:
        if repo_path.is_dir():
            owner_repo = _get_repo_owner_name(repo_path)
            if owner_repo:
                repo_list.append(owner_repo)
            else:
                local_only_repo_names.append(repo_path.name)
                if not quiet:
                    info(
                        f"  {repo_path.name}: no GitHub remote, treating as local-only (private mode only)"
                    )

    if not repo_list and not local_only_repo_names:
        return None, repos, []

    # Create session with atomic visibility filtering
    success_flag, session_token, worktrees, filtered_repos, errors = create_session(
        container_id=container_id,
        container_ip=container_ip,
        mode=mode,
        repos=repo_list,
        local_only_repos=local_only_repo_names,
        uid=os.getuid(),
        gid=os.getgid(),
        phase=phase,
        issue_number=issue_number,
        pr_number=pr_number,
        pipeline_id=pipeline_id,
        agent_role=agent_role,
    )

    if errors and not quiet:
        for err in errors:
            warn(f"Session creation warning: {err}")

    if not success_flag:
        if not quiet:
            warn("Session creation failed — cannot start container without a session")
        return None, repos, []

    if not quiet:
        mode_desc = (
            "PRIVATE (private/internal repos only)"
            if mode == "private"
            else "PUBLIC (public repos only)"
        )
        info(f"Session mode: {mode_desc}")
        if filtered_repos:
            info(f"Filtered repos ({len(filtered_repos)}): {', '.join(filtered_repos)}")

    # Set up mounts for filtered repos
    for repo_name, worktree_path in worktrees.items():
        container_path = f"/home/egg/repos/{repo_name}"
        mount_args.extend(["-v", f"{worktree_path}:{container_path}:rw"])

        if not quiet:
            print(f"  * ~/repos/{repo_name} (session-filtered worktree)")

        # Shadow .git using shared helper to prevent local git operations
        for shadow in git_shadow_mounts({repo_name: worktree_path}):
            mount_args.extend(mount_spec_to_cli_args(shadow))

        # Track repo path for cleanup
        for local_repo in local_repos:
            if local_repo.name == repo_name:
                repos[repo_name] = local_repo
                break

    return session_token, repos, filtered_repos


def _is_k8s_runtime() -> bool:
    """Check if the runtime backend is Kubernetes."""
    return EGG_RUNTIME == "kubernetes"


def _get_k8s_client() -> Any:
    """Create and return a Kubernetes API client.

    Uses in-cluster config when running inside a pod, otherwise
    falls back to kubeconfig.
    """
    try:
        from kubernetes import client
        from kubernetes import config as k8s_config

        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()
        return client
    except ImportError as err:
        raise RuntimeError(
            "kubernetes Python package is required for EGG_RUNTIME=kubernetes. "
            "Install with: pip install kubernetes"
        ) from err


def _get_k8s_network_config(
    repo_mode: str | None,
) -> ContainerNetworkConfig:
    """Get network configuration for k8s pods using Service DNS.

    In Kubernetes, the gateway is accessed via k8s Service DNS rather
    than static IPs and Docker networks.
    """
    ctx = get_context()
    gateway_hostname = "egg-gateway.egg-system.svc.cluster.local"
    gateway_ip = gateway_hostname  # In k8s, DNS handles resolution

    if repo_mode == "private":
        return ContainerNetworkConfig(
            network_name="egg-system",  # namespace as "network"
            gateway_hostname=gateway_hostname,
            gateway_ip=gateway_ip,
            gateway_port=ctx.gateway_port,
            repo_mode="private",
            proxy_url=f"http://{gateway_hostname}:{ctx.gateway_proxy_port}",
        )
    else:
        return ContainerNetworkConfig(
            network_name="egg-system",
            gateway_hostname=gateway_hostname,
            gateway_ip=gateway_ip,
            gateway_port=ctx.gateway_port,
            repo_mode="public",
        )


def _k8s_create_job(
    config: SandboxContainerConfig,
    *,
    namespace: str = "egg-agents",
    timeout_seconds: int | None = None,
) -> str:
    """Create a Kubernetes Job from a SandboxContainerConfig.

    Returns the job name for subsequent operations (log streaming, deletion).
    """
    k8s_client = _get_k8s_client()
    batch_v1 = k8s_client.BatchV1Api()

    job_kwargs = to_k8s_job_kwargs(
        config,
        namespace=namespace,
        active_deadline_seconds=timeout_seconds,
    )

    # Convert dict spec to V1Job object
    job = batch_v1.create_namespaced_job(
        namespace=namespace,
        body=job_kwargs,
    )
    return str(job.metadata.name)


def _k8s_wait_for_pod(
    job_name: str,
    namespace: str = "egg-agents",
    timeout: int = 120,
) -> str | None:
    """Wait for the Job's pod to be created and return the pod name."""
    k8s_client = _get_k8s_client()
    core_v1 = k8s_client.CoreV1Api()

    label_selector = f"job-name={job_name}"
    deadline = time.time() + timeout

    while time.time() < deadline:
        pods = core_v1.list_namespaced_pod(
            namespace=namespace,
            label_selector=label_selector,
        )
        if pods.items:
            return str(pods.items[0].metadata.name)
        time.sleep(1)

    return None


def _k8s_stream_logs(
    pod_name: str,
    namespace: str = "egg-agents",
) -> None:
    """Stream logs from a pod to stdout."""
    k8s_client = _get_k8s_client()
    core_v1 = k8s_client.CoreV1Api()

    # Wait for container to be running
    deadline = time.time() + 120
    while time.time() < deadline:
        pod = core_v1.read_namespaced_pod(pod_name, namespace)
        phase = pod.status.phase
        if phase in ("Running", "Succeeded", "Failed"):
            break
        time.sleep(1)

    try:
        log_stream = core_v1.read_namespaced_pod_log(
            pod_name,
            namespace,
            follow=True,
            _preload_content=False,
        )
        for line in log_stream:
            sys.stdout.write(line.decode("utf-8", errors="replace"))
    except Exception as e:
        warn(f"Log streaming ended: {e}")


def _k8s_wait_for_job(
    job_name: str,
    namespace: str = "egg-agents",
    timeout: int = 1800,
) -> bool:
    """Wait for a Kubernetes Job to complete.

    Returns True if the job succeeded, False otherwise.
    """
    k8s_client = _get_k8s_client()
    batch_v1 = k8s_client.BatchV1Api()

    deadline = time.time() + timeout
    while time.time() < deadline:
        job = batch_v1.read_namespaced_job(job_name, namespace)
        if job.status.succeeded and job.status.succeeded > 0:
            return True
        if job.status.failed and job.status.failed > 0:
            return False
        time.sleep(2)

    warn(f"Job {job_name} timed out after {timeout}s")
    return False


def _k8s_delete_job(
    job_name: str,
    namespace: str = "egg-agents",
) -> None:
    """Delete a Kubernetes Job and its pods."""
    try:
        k8s_client = _get_k8s_client()
        batch_v1 = k8s_client.BatchV1Api()
        batch_v1.delete_namespaced_job(
            job_name,
            namespace,
            propagation_policy="Background",
        )
    except Exception as e:
        warn(f"Failed to delete job {job_name}: {e}")


def exec_in_new_container(
    command: list[str],
    timeout_minutes: int = 30,
    task_id: str | None = None,
    thread_ts: str | None = None,
    auth_mode: str = "oauth-token",
    repo_mode: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> bool:
    """Execute a command in a new ephemeral container.

    In the gateway-managed worktree architecture:
    - Repos are mounted directly with .git shadowed by tmpfs
    - All git operations route through the gateway API
    - Container logs persisted to ~/.cache/egg/container-logs/

    Args:
        command: Command to execute
        timeout_minutes: Timeout in minutes (default: 30)
        task_id: Optional task ID for log correlation (auto-detected from command if not provided)
        thread_ts: Optional Slack thread timestamp for correlation
        auth_mode: Anthropic authentication method - 'api-key' or 'oauth-token'
        repo_mode: Optional repository visibility mode for per-container sessions.
                   - None: Legacy mode (all repos accessible, global env vars)
                   - "private": Only mount private/internal repos
                   - "public": Only mount public repos
        extra_env: Additional environment variables to pass to the container

    Returns:
        True if successful, False otherwise

    Raises:
        ValueError: If auth_mode is not 'api-key' or 'oauth-token'
    """
    # Validate auth_mode parameter
    valid_auth_modes = ("api-key", "oauth-token")
    if auth_mode not in valid_auth_modes:
        raise ValueError(f"Invalid auth_mode '{auth_mode}'. Must be one of: {valid_auth_modes}")

    # Validate repo_mode parameter
    _validate_repo_mode(repo_mode)

    ctx = get_context()
    quiet = get_quiet_mode()

    # Check if image exists - build non-interactively if missing
    if not image_exists():
        info("Docker image not found. Building...")
        create_dockerfile()

    # Check repository configuration - warn but continue if missing
    if not _get_repos_config_file().exists():
        warn("No repositories configured. Run 'egg --setup' to add repositories.")
        warn("Continuing with no mounted repositories...")

    # Build/update image
    if not build_image():
        error("Docker build failed")
        return False

    # Compose-based service bring-up was removed in #1762; operators
    # running locally are expected to have the gateway + orchestrator
    # already running (e.g. via ``kubectl apply -f k8s/``). GHA's
    # ``gha_exec()`` flow continues to start the gateway container
    # directly earlier in its own orchestration.

    # Generate unique container ID for this exec
    container_id = f"egg-exec-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"

    # Auto-detect task_id from command if not provided
    if not task_id:
        task_id = extract_task_id_from_command(command)

    # Auto-detect thread_ts from task file if not provided
    if not thread_ts and task_id:
        for arg in command:
            if ".md" in arg:
                thread_ts = extract_thread_ts_from_task_file(arg)
                break

    info(f"Executing command in new container: {container_id}")
    if not quiet:
        if task_id:
            info(f"Task ID: {task_id}")
        if thread_ts:
            info(f"Thread TS: {thread_ts}")
        print(f"Command: {' '.join(command)}")
        print(f"Timeout: {timeout_minutes} minutes")
        print()

    # Build mount configuration
    info("Configuring repository mounts...")
    mount_args: list[str] = []

    # Track session token for cleanup and container env
    session_token = None
    container_ip = None

    # Get network configuration based on mode (centralized in helper to prevent divergence)
    if _is_k8s_runtime():
        net_config = _get_k8s_network_config(repo_mode)
    else:
        net_config = _get_container_network_config(repo_mode)

    # Choose mount strategy based on repo_mode
    if repo_mode:
        # Per-container session mode: allocate IP first for session binding
        # In k8s mode, pod IPs are assigned by the cluster -- use a placeholder
        if _is_k8s_runtime():
            container_ip = "0.0.0.0"  # k8s assigns pod IPs dynamically
        else:
            container_ip = _allocate_container_ip(network=net_config.network_name)
            if not container_ip:
                error("Failed to allocate container IP for session mode")
                return False

        if not quiet:
            info(f"Session mode: {repo_mode}")
            if not _is_k8s_runtime():
                info(f"Pre-allocated IP: {container_ip}")

        # Use session-based repo setup with visibility filtering
        # Pass pipeline phase and checkpoint metadata from environment
        pipeline_phase = os.environ.get("EGG_PIPELINE_PHASE")
        issue_number_str = os.environ.get("EGG_ISSUE_NUMBER")
        try:
            issue_number = int(issue_number_str) if issue_number_str else None
        except ValueError:
            warn(f"Invalid EGG_ISSUE_NUMBER: {issue_number_str!r}, ignoring")
            issue_number = None
        pr_number_str = os.environ.get("EGG_PR_NUMBER")
        try:
            pr_number = int(pr_number_str) if pr_number_str else None
        except ValueError:
            warn(f"Invalid EGG_PR_NUMBER: {pr_number_str!r}, ignoring")
            pr_number = None
        pipeline_id = os.environ.get("EGG_PIPELINE_ID")
        agent_role = os.environ.get("EGG_AGENT_ROLE")
        session_token, repos, _filtered_repos = _setup_session_repos(
            container_id=container_id,
            container_ip=container_ip,
            mode=repo_mode,
            mount_args=mount_args,
            quiet=quiet,
            phase=pipeline_phase,
            issue_number=issue_number,
            pr_number=pr_number,
            pipeline_id=pipeline_id,
            agent_role=agent_role,
        )

        if not session_token:
            # Session creation failed - cannot proceed without a session
            # since git/gh wrappers require EGG_SESSION_TOKEN (PR #666)
            error("Session creation failed. Check that:")
            error(
                f"  1. Gateway sidecar is running: curl http://localhost:{GATEWAY_PORT}/api/v1/health"
            )
            error("  2. Launcher secret exists: ~/.config/egg/launcher-secret")
            error("  Fix: Re-run 'egg --setup' to sync secrets")
            return False
    else:
        # repo_mode is required since PR #669 - all containers need sessions
        error("repo_mode is required - cannot start container without session")
        return False

    if repos:
        mode_info = f" ({repo_mode} mode)" if repo_mode else ""
        info(f"Mounted {len(repos)} repo(s){mode_info} (all git operations via gateway)")
        if not quiet:
            print()

    # Add standard mounts (shared-certs)
    add_standard_mounts(mount_args, quiet=quiet)

    # Note: Host ~/.claude is NOT mounted - container uses gateway-injected
    # Anthropic credentials instead of host Claude configuration

    if not quiet:
        print()

    # Caller-specific env vars
    caller_env: dict[str, str] = {
        "PYTHONUNBUFFERED": "1",
        "EGG_QUIET": "1" if quiet else "0",
    }

    # Pass filtered repos (owner/repo format) so in-container tools like
    # egg-sdlc can determine the repo without relying on .git (which is
    # shadowed by tmpfs in the gateway-managed worktree architecture).
    if _filtered_repos:
        caller_env["EGG_REPOS"] = ",".join(_filtered_repos)

    # Add correlation environment variables for log tracing
    if task_id:
        caller_env["EGG_TASK_ID"] = task_id
    if thread_ts:
        caller_env["EGG_THREAD_TS"] = thread_ts

    # Add Anthropic auth configuration based on CLI auth_mode
    auth_method_map = {"api-key": "api_key", "oauth-token": "oauth"}
    anthropic_auth_method = auth_method_map[auth_mode]
    caller_env["ANTHROPIC_AUTH_METHOD"] = anthropic_auth_method

    # Pass API key when using api-key auth mode
    if auth_mode == "api-key":
        api_key = get_anthropic_api_key()
        if api_key:
            caller_env["ANTHROPIC_API_KEY"] = api_key

    # Merge in any extra environment variables from caller
    if extra_env:
        caller_env.update(extra_env)

    # --- Kubernetes runtime path ---
    if _is_k8s_runtime():
        info("Using Kubernetes runtime backend")

        # Build container config using shared builder
        sandbox_config = build_sandbox_config(
            container_name=container_id,
            image=ctx.sandbox_image,
            network=net_config,
            container_ip=None,  # k8s assigns pod IPs
            session_token=session_token,
            runtime_uid=os.getuid(),
            runtime_gid=os.getgid(),
            extra_env=caller_env,
            command=command,
        )

        namespace = os.environ.get("EGG_K8S_NAMESPACE", "egg-agents")
        timeout_seconds = timeout_minutes * 60
        job_name = None

        try:
            job_name = _k8s_create_job(
                sandbox_config,
                namespace=namespace,
                timeout_seconds=timeout_seconds,
            )
            info(f"Created Kubernetes Job: {job_name}")

            # Wait for pod to be scheduled
            pod_name = _k8s_wait_for_pod(job_name, namespace=namespace)
            if not pod_name:
                error(f"Pod for job {job_name} was not created within timeout")
                return False

            info(f"Pod started: {pod_name}")

            # Stream logs from the pod
            _k8s_stream_logs(pod_name, namespace=namespace)

            # Wait for job completion
            success = _k8s_wait_for_job(
                job_name,
                namespace=namespace,
                timeout=timeout_seconds,
            )
            return success

        except KeyboardInterrupt:
            print()
            warn("Interrupted by user")
            return False
        except Exception as e:
            error(f"Kubernetes job execution failed: {e}")
            return False
        finally:
            # Clean up job
            if job_name:
                _k8s_delete_job(job_name, namespace=namespace)

            # Clean up session and worktrees
            if repos:
                _cleanup_session(session_token, container_id)

            # In ephemeral mode (GHA), tear down gateway
            if ctx.ephemeral:
                from .gateway import cleanup_gateway

                try:
                    cleanup_gateway()
                except Exception as e:
                    error(f"Ephemeral gateway cleanup failed: {e}")

    # --- Docker runtime path (default) ---

    # Add logging configuration for log persistence
    log_config = get_docker_log_config(container_id, task_id)

    cmd = build_sandbox_docker_cmd(
        container_name=container_id,
        image=ctx.sandbox_image,
        network=net_config,
        container_ip=container_ip,
        session_token=session_token,
        runtime_uid=os.getuid(),
        runtime_gid=os.getgid(),
        extra_env=caller_env,
        extra_args=log_config,
    )

    # Insert lifecycle flags: always keep stdin open (-i) so exec'd commands
    # can read input; allocate a pseudo-TTY (-t) when the host has one.
    lifecycle_flags = ["-i"]
    if sys.stdin.isatty():
        lifecycle_flags.append("-t")
    cmd[LIFECYCLE_FLAGS_INDEX:LIFECYCLE_FLAGS_INDEX] = lifecycle_flags

    # Insert mount arguments before the image name (last element)
    cmd[-1:-1] = mount_args

    # Add the command to execute (after image name)
    cmd.extend(command)

    # Clear statusbar before launching container
    if quiet:
        status_finish(f"Launching {command[0]}...")

    # Run container with configurable timeout and IP conflict retry.
    timeout_seconds = timeout_minutes * 60
    run_success = False
    max_ip_retries = 3
    failed_ips: set[str] = set()

    def cleanup_container() -> None:
        """Save logs, remove container, and clean up session/worktrees."""
        try:
            # Save container logs before removal (with correlation info)
            save_container_logs(container_id, task_id, thread_ts)
        except Exception as e:
            error(f"Failed to save container logs: {e}")
            # Don't re-raise - continue with cleanup
        finally:
            # Remove container
            try:
                subprocess.run(
                    ["docker", "rm", "-f", container_id],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            except Exception as e:
                error(f"Failed to remove container: {e}")
                # Don't re-raise - original error is more important

            # Clean up session and worktrees
            if repos:
                _cleanup_session(session_token, container_id)

            # In ephemeral mode (GHA), tear down gateway and networks
            if ctx.ephemeral:
                from .gateway import cleanup_gateway

                try:
                    cleanup_gateway()
                except Exception as e:
                    error(f"Ephemeral gateway cleanup failed: {e}")

    try:
        for ip_attempt in range(max_ip_retries):
            try:
                result = subprocess.run(
                    cmd,
                    timeout=timeout_seconds,
                    check=False,
                    capture_output=(ip_attempt > 0),
                )
                run_success = result.returncode == 0
                if run_success:
                    break

                # Check for IP conflict error
                is_ip_conflict = False
                if ip_attempt > 0 and result.stderr:
                    stderr_text = result.stderr.decode("utf-8", errors="replace")
                    if "Address already in use" in stderr_text:
                        is_ip_conflict = True
                    else:
                        sys.stderr.write(stderr_text)
                elif result.returncode == 125 and container_ip and ip_attempt == 0:
                    # First attempt: stderr went to terminal so we can't inspect
                    # the error message. Exit code 125 covers all Docker daemon
                    # errors (not just IP conflicts), so this is an imprecise
                    # heuristic — a non-IP error will trigger one unnecessary
                    # retry cycle before failing on the second attempt.
                    is_ip_conflict = True

                if not is_ip_conflict:
                    break
                warn("Container start failed (IP address conflict), retrying...")
            except subprocess.TimeoutExpired:
                print()
                error(f"Container execution timed out after {timeout_minutes} minutes")
                subprocess.run(
                    ["docker", "kill", container_id],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                break
            except KeyboardInterrupt:
                print()
                warn("Interrupted by user")
                subprocess.run(
                    ["docker", "kill", container_id],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                break
            except Exception as e:
                error(f"Failed to run container: {e}")
                break

            # Re-allocate IP and rebuild command for retry
            if container_ip:
                failed_ips.add(container_ip)
            # Clean up old session
            if repos:
                _cleanup_session(session_token, container_id)
            subprocess.run(
                ["docker", "rm", "-f", container_id],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            container_ip = _allocate_container_ip(
                network=net_config.network_name, exclude_ips=failed_ips
            )
            if not container_ip:
                error("Failed to allocate a new container IP after conflict")
                break
            info(f"Retrying with new IP: {container_ip}")
            mount_args_retry: list[str] = []
            session_token, repos, _filtered_repos = _setup_session_repos(
                container_id=container_id,
                container_ip=container_ip,
                mode=repo_mode,
                mount_args=mount_args_retry,
                quiet=True,
                phase=pipeline_phase,
                issue_number=issue_number,
                pr_number=pr_number,
                pipeline_id=pipeline_id,
                agent_role=agent_role,
            )
            if not session_token:
                error("Failed to re-create session for retry")
                break
            add_standard_mounts(mount_args_retry, quiet=True)
            cmd = build_sandbox_docker_cmd(
                container_name=container_id,
                image=ctx.sandbox_image,
                network=net_config,
                container_ip=container_ip,
                session_token=session_token,
                runtime_uid=os.getuid(),
                runtime_gid=os.getgid(),
                extra_env=caller_env,
                extra_args=log_config,
            )
            lifecycle_flags = ["-i"]
            if sys.stdin.isatty():
                lifecycle_flags.append("-t")
            cmd[LIFECYCLE_FLAGS_INDEX:LIFECYCLE_FLAGS_INDEX] = lifecycle_flags
            cmd[-1:-1] = mount_args_retry
            cmd.extend(command)
            time.sleep(0.5)
    finally:
        # Always save logs and cleanup container
        cleanup_container()

    return run_success
