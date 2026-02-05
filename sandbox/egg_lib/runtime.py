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

import ipaddress
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

# Import statusbar for quiet mode
from statusbar import status, status_finish

from .auth import get_anthropic_api_key, get_anthropic_auth_method
from .config import (
    EGG_EXTERNAL_NETWORK,
    EGG_EXTERNAL_SUBNET,
    EGG_ISOLATED_NETWORK,
    EGG_ISOLATED_SUBNET,
    GATEWAY_CONTAINER_NAME,
    GATEWAY_EXTERNAL_IP,
    GATEWAY_ISOLATED_IP,
    GATEWAY_PORT,
    GATEWAY_PROXY_PORT,
    Config,
    get_local_repos,
)
from .container_logging import (
    extract_task_id_from_command,
    extract_thread_ts_from_task_file,
    generate_container_id,
    get_docker_log_config,
    save_container_logs,
)
from .docker import build_image, create_dockerfile, image_exists
from .gateway import (
    create_session,
    create_worktrees,
    delete_session,
    delete_worktrees,
    start_gateway_container,
)
from .output import error, get_quiet_mode, info, warn
from .setup_flow import add_standard_mounts
from .timing import _host_timer

# Reserved IPs in each subnet
RESERVED_ISOLATED_IPS = {
    "172.32.0.1",  # Docker gateway
    "172.32.0.2",  # egg-gateway sidecar (GATEWAY_ISOLATED_IP)
}
RESERVED_EXTERNAL_IPS = {
    "172.33.0.1",  # Docker gateway
    "172.33.0.2",  # egg-gateway sidecar (GATEWAY_EXTERNAL_IP)
}
# Legacy alias for backward compatibility
RESERVED_IPS = RESERVED_ISOLATED_IPS

# Valid repo_mode values
VALID_REPO_MODES = ("private", "public")


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
) -> tuple[str, str, list[str]]:
    """Get network configuration for a container based on repo_mode.

    This centralizes the network selection logic to prevent divergence between
    run_claude() and exec_in_new_container().

    Args:
        repo_mode: Repository visibility mode ("private" or "public")

    Returns:
        Tuple of (network_name, gateway_ip, extra_docker_args):
        - network_name: Docker network to use (egg-isolated or egg-external)
        - gateway_ip: IP address of the gateway on that network
        - extra_docker_args: Mode-specific docker arguments (DNS, proxy settings)
    """
    proxy_url = f"http://{GATEWAY_CONTAINER_NAME}:{GATEWAY_PROXY_PORT}"

    if repo_mode == "private":
        # PRIVATE: Internal isolated network with proxy (locked to api.anthropic.com)
        # DNS is disabled (0.0.0.0) to prevent direct hostname resolution.
        # HTTP clients using HTTP_PROXY/HTTPS_PROXY send CONNECT requests to the
        # proxy with the hostname, and Squid resolves DNS on behalf of the client.
        # If a tool bypasses the proxy, its requests fail with DNS errors (fail closed).
        extra_args = [
            # Disable DNS (no external DNS resolution - fail closed)
            "--dns",
            "0.0.0.0",
            # Set PRIVATE_MODE env var so container knows its mode
            "-e",
            "PRIVATE_MODE=true",
            # HTTP/HTTPS proxy environment variables for network lockdown
            "-e",
            f"HTTP_PROXY={proxy_url}",
            "-e",
            f"HTTPS_PROXY={proxy_url}",
            "-e",
            f"http_proxy={proxy_url}",
            "-e",
            f"https_proxy={proxy_url}",
            # Bypass proxy for local connections to gateway
            "-e",
            f"NO_PROXY=localhost,127.0.0.1,{GATEWAY_CONTAINER_NAME}",
            "-e",
            f"no_proxy=localhost,127.0.0.1,{GATEWAY_CONTAINER_NAME}",
        ]
        return EGG_ISOLATED_NETWORK, GATEWAY_ISOLATED_IP, extra_args
    else:
        # PUBLIC: External network with direct internet access (no proxy)
        # Uses Docker's default DNS. No proxy env vars set.
        # Container can access the internet directly.
        # Set PRIVATE_MODE=false so container knows its mode
        return EGG_EXTERNAL_NETWORK, GATEWAY_EXTERNAL_IP, ["-e", "PRIVATE_MODE=false"]


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
    except (subprocess.CalledProcessError, IndexError):
        return None


def _allocate_container_ip(network: str = EGG_ISOLATED_NETWORK) -> str | None:
    """Allocate an available IP address from the specified network.

    Pre-allocates an IP before container start for session-container binding.
    The IP is used to verify requests come from the expected container.

    Args:
        network: Docker network name (egg-isolated or egg-external)

    Returns:
        Available IP address string, or None if allocation fails
    """
    # Select subnet and reserved IPs based on network
    if network == EGG_EXTERNAL_NETWORK:
        subnet_str = EGG_EXTERNAL_SUBNET
        reserved_ips = RESERVED_EXTERNAL_IPS
    else:
        subnet_str = EGG_ISOLATED_SUBNET
        reserved_ips = RESERVED_ISOLATED_IPS

    try:
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


def _setup_repo_mounts(
    container_id: str,
    mount_args: list[str],
    quiet: bool = False,
    use_gateway_worktrees: bool = True,
) -> dict:
    """Configure repository mounts for a container.

    In the gateway-managed worktree architecture:
    - Gateway creates worktrees for the container before it starts
    - Container mounts gateway-created worktrees at /home/egg/repos/{repo_name}
    - The .git directory is shadowed by a tmpfs mount (no git metadata in container)
    - All git operations must go through the gateway API

    Args:
        container_id: Unique container identifier
        mount_args: List to append mount arguments to
        quiet: Suppress output
        use_gateway_worktrees: If True, request worktrees from gateway (default)
                               If False, mount repos directly (fallback)

    Returns:
        Dict of repo_name -> repo_path for tracking
    """
    repos = {}
    local_repos = get_local_repos()

    if not local_repos:
        if not quiet:
            info("No local repositories configured.")
        return repos

    repo_names = [repo_path.name for repo_path in local_repos if repo_path.is_dir()]

    if not repo_names:
        return repos

    if use_gateway_worktrees:
        # Request gateway to create worktrees with correct ownership
        wt_success, worktrees, wt_errors = create_worktrees(
            container_id, repo_names, uid=os.getuid(), gid=os.getgid()
        )

        if wt_errors and not quiet:
            for err in wt_errors:
                warn(f"Worktree error: {err}")

        if not wt_success and not worktrees:
            # Fall back to direct mounts if gateway fails
            if not quiet:
                warn("Gateway worktree creation failed, using direct mounts")
            use_gateway_worktrees = False

    for repo_path in local_repos:
        if not repo_path.is_dir():
            continue

        repo_name = repo_path.name
        container_path = f"/home/egg/repos/{repo_name}"

        if use_gateway_worktrees and repo_name in worktrees:
            # Mount gateway-created worktree
            worktree_path = worktrees[repo_name]
            mount_args.extend(["-v", f"{worktree_path}:{container_path}:rw"])

            if not quiet:
                print(f"  • ~/repos/{repo_name} (gateway worktree)")
        else:
            # Fallback: mount repo directly
            mount_args.extend(["-v", f"{repo_path}:{container_path}:rw"])

            if not quiet:
                print(f"  • ~/repos/{repo_name} (direct mount)")

        # Shadow .git to prevent local git operations
        # This forces all git operations through the gateway
        # In worktrees, .git is a FILE (not directory) containing "gitdir: ..."
        # For directories: use tmpfs mount
        # For files: use bind mount of /dev/null
        if use_gateway_worktrees and repo_name in worktrees:
            # Worktree: .git is a file
            git_path = Path(worktrees[repo_name]) / ".git"
            if git_path.exists() and git_path.is_file():
                mount_args.extend(
                    [
                        "--mount",
                        f"type=bind,source=/dev/null,destination={container_path}/.git,readonly",
                    ]
                )
            else:
                # Fallback to tmpfs if it's somehow a directory
                mount_args.extend(["--mount", f"type=tmpfs,destination={container_path}/.git"])
        else:
            # Direct mount: .git is a directory
            mount_args.extend(["--mount", f"type=tmpfs,destination={container_path}/.git"])

        repos[repo_name] = repo_path

    return repos


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

    Args:
        session_token: Session token (if available)
        container_id: Container identifier
    """
    if session_token:
        try:
            success_flag, err = delete_session(session_token)
            if not success_flag and err:
                warn(f"Session cleanup warning: {err}")
        except Exception as e:
            warn(f"Session cleanup failed: {e}")
    else:
        # Fall back to worktree cleanup only
        _cleanup_worktrees(container_id)


def _setup_session_repos(
    container_id: str,
    container_ip: str,
    mode: str,
    mount_args: list[str],
    quiet: bool = False,
) -> tuple[str | None, dict, list[str]]:
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

    Returns:
        Tuple of (session_token, repos_dict, filtered_repos)
        - session_token: Token for container authentication
        - repos_dict: Dict of repo_name -> repo_path for tracking
        - filtered_repos: List of repos that passed visibility filtering
    """
    repos = {}
    local_repos = get_local_repos()

    if not local_repos:
        if not quiet:
            info("No local repositories configured.")
        return None, repos, []

    # Convert local repos to owner/repo format for visibility checking
    repo_list = []
    for repo_path in local_repos:
        if repo_path.is_dir():
            # Get owner/repo from git remote URL
            owner_repo = _get_repo_owner_name(repo_path)
            if owner_repo:
                repo_list.append(owner_repo)
            else:
                # Fallback to just repo name (visibility check will skip it)
                if not quiet:
                    warn(
                        f"Could not determine owner for {repo_path.name}, skipping visibility check"
                    )
                repo_list.append(repo_path.name)

    if not repo_list:
        return None, repos, []

    # Create session with atomic visibility filtering
    success_flag, session_token, worktrees, filtered_repos, errors = create_session(
        container_id=container_id,
        container_ip=container_ip,
        mode=mode,
        repos=repo_list,
        uid=os.getuid(),
        gid=os.getgid(),
    )

    if errors and not quiet:
        for err in errors:
            warn(f"Session creation warning: {err}")

    if not success_flag:
        if not quiet:
            warn("Session creation failed, falling back to legacy mode")
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

        # Shadow .git to prevent local git operations
        git_path = Path(worktree_path) / ".git"
        if git_path.exists() and git_path.is_file():
            mount_args.extend(
                [
                    "--mount",
                    f"type=bind,source=/dev/null,destination={container_path}/.git,readonly",
                ]
            )
        else:
            mount_args.extend(["--mount", f"type=tmpfs,destination={container_path}/.git"])

        # Track repo path for cleanup
        for local_repo in local_repos:
            if local_repo.name == repo_name:
                repos[repo_name] = local_repo
                break

    return session_token, repos, filtered_repos


def run_claude(repo_mode: str | None = None) -> bool:
    """Run Claude Code CLI in the sandboxed container (interactive mode).

    Args:
        repo_mode: Optional repository visibility mode for per-container sessions.
                   - None: Legacy mode (all repos accessible, global env vars)
                   - "private": Only mount private/internal repos
                   - "public": Only mount public repos

    Returns:
        True if container ran successfully, False otherwise

    Raises:
        ValueError: If repo_mode is not None and not "private" or "public"
    """
    # Validate repo_mode before any other work
    _validate_repo_mode(repo_mode)

    quiet = get_quiet_mode()

    # Check if image exists - build non-interactively if missing
    with _host_timer.phase("check_image"):
        if quiet:
            status("Checking Docker image...")
        if not image_exists():
            info("Docker image not found. Building...")
            create_dockerfile()

    # Check repository configuration - warn but continue if missing
    with _host_timer.phase("check_config"):
        if not Config.REPOS_CONFIG_FILE.exists():
            warn("No repositories configured. Run 'egg --setup' to add repositories.")
            warn("Continuing with no mounted repositories...")

    # Get Anthropic API key (used for env var passthrough, but not required with OAuth)
    api_key = get_anthropic_api_key()

    # Build/update image (Docker uses cache for unchanged layers - usually instant)
    with _host_timer.phase("build_image"):
        if quiet:
            status("Building Docker image...")
        if not build_image():
            error("Docker build failed")
            return False

    # Start gateway sidecar container (if not already running)
    with _host_timer.phase("start_gateway"):
        if quiet:
            status("Starting gateway sidecar...")
        if not start_gateway_container(interactive=True):
            error("Failed to start gateway sidecar")
            return False

    # Generate unique container ID
    with _host_timer.phase("prepare_container"):
        if quiet:
            status("Preparing container...")
        container_id = generate_container_id()

        if not quiet:
            info("Launching sandboxed Claude Code environment...")
            print()
            info(f"Container ID: {container_id}")
            print()

    # Build mount configuration
    _host_timer.start_phase("configure_mounts")
    if quiet:
        status("Configuring mounts...")
    else:
        info("Configuring repository mounts...")
        print()
    mount_args = []

    # Track session token for cleanup and container env
    session_token = None
    container_ip = None

    # Get network configuration based on mode (centralized in helper to prevent divergence)
    container_network, gateway_ip, network_extra_args = _get_container_network_config(repo_mode)

    # Choose mount strategy based on repo_mode
    if repo_mode:
        # Per-container session mode: allocate IP first for session binding
        container_ip = _allocate_container_ip(network=container_network)
        if not container_ip:
            error("Failed to allocate container IP for session mode")
            return False

        if not quiet:
            info(f"Session mode: {repo_mode}")
            info(f"Pre-allocated IP: {container_ip}")

        # Use session-based repo setup with visibility filtering
        session_token, repos, _filtered_repos = _setup_session_repos(
            container_id=container_id,
            container_ip=container_ip,
            mode=repo_mode,
            mount_args=mount_args,
            quiet=quiet,
        )

        if not session_token:
            # Session creation failed - cannot proceed without a session
            # since git/gh wrappers require EGG_SESSION_TOKEN (PR #666)
            error("Session creation failed. Check that:")
            error("  1. Gateway sidecar is running: curl http://localhost:9848/api/v1/health")
            error("  2. Launcher secret exists: ~/.config/egg/launcher-secret")
            error("  Fix: Re-run gateway/setup.sh to sync secrets")
            return False
    else:
        # repo_mode is required since PR #669 - all containers need sessions
        error("repo_mode is required - cannot start container without session")
        return False

    if repos and not quiet:
        print()
        mode_info = f" ({repo_mode} mode)" if repo_mode else ""
        info(f"Mounted {len(repos)} repo(s){mode_info} (all git operations via gateway)")
        print()

    # Add standard mounts (shared-certs)
    add_standard_mounts(mount_args, quiet=quiet)

    # Note: Host ~/.claude is NOT mounted - container uses gateway-injected
    # Anthropic credentials instead of host Claude configuration

    if not quiet:
        print()
    _host_timer.end_phase()  # configure_mounts

    # Remove old container if exists (cleanup any previous runs)
    with _host_timer.phase("cleanup_old_container"):
        subprocess.run(
            ["docker", "rm", "-f", container_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    # Build docker run command
    _host_timer.start_phase("build_docker_cmd")

    cmd = [
        "docker",
        "run",
        "--rm",  # Auto-remove container after exit
        "-it",  # Interactive with TTY
        "--security-opt",
        "label=disable",  # Disable SELinux labeling for faster startup
        "--name",
        container_id,
        # Network selection based on mode
        "--network",
        container_network,
    ]

    # If session mode with pre-allocated IP, use static IP assignment
    # This binds the container to the session for security verification
    if container_ip:
        cmd.extend(["--ip", container_ip])

    # Add gateway hostname for API access (both modes need this)
    cmd.extend(
        [
            "--add-host",
            f"{GATEWAY_CONTAINER_NAME}:{gateway_ip}",
            # Environment variables
            "-e",
            f"RUNTIME_UID={os.getuid()}",
            "-e",
            f"RUNTIME_GID={os.getgid()}",
            "-e",
            f"CONTAINER_ID={container_id}",
            "-e",
            f"EGG_QUIET={'1' if quiet else '0'}",
            "-e",
            f"EGG_TIMING={'1' if _host_timer.enabled else '0'}",
            "-e",
            f"GATEWAY_URL=http://{GATEWAY_CONTAINER_NAME}:{GATEWAY_PORT}",
        ]
    )

    # Mode-specific network settings (DNS, proxy) from centralized helper
    if network_extra_args:
        cmd.extend(network_extra_args)

    # If session mode, pass session token for container authentication
    if session_token:
        cmd.extend(["-e", f"EGG_SESSION_TOKEN={session_token}"])

    # GitHub authentication is handled by the gateway sidecar
    # The container does NOT receive GITHUB_TOKEN - all git/gh operations
    # route through the gateway which holds the credentials
    if not quiet:
        info("GitHub auth: Via gateway sidecar (credentials not in container)")

    # Add Anthropic auth configuration
    anthropic_auth_method = get_anthropic_auth_method()
    cmd.extend(["-e", f"ANTHROPIC_AUTH_METHOD={anthropic_auth_method}"])

    # Set Anthropic API key if available
    if api_key:
        cmd.extend(["-e", f"ANTHROPIC_API_KEY={api_key}"])

    if not quiet:
        info(f"Claude auth method: {anthropic_auth_method}")
        if repo_mode == "private":
            info("Network mode: PRIVATE (isolated network, proxy filtering)")
            if container_ip:
                print(f"  Network: {container_network} (IP: {container_ip})")
            else:
                print(f"  Network: {container_network} (IP assigned dynamically)")
            print(f"  Gateway: {GATEWAY_CONTAINER_NAME} at {gateway_ip}")
            print(f"  Gateway API: http://{GATEWAY_CONTAINER_NAME}:{GATEWAY_PORT}")
            print(f"  Proxy: http://{GATEWAY_CONTAINER_NAME}:{GATEWAY_PROXY_PORT}")
            print("  Container can: Access Claude API, GitHub (via gateway sidecar)")
            print("  Container cannot: Access any other websites, install packages at runtime")
        else:
            info("Network mode: PUBLIC (direct internet access)")
            if container_ip:
                print(f"  Network: {container_network} (IP: {container_ip})")
            else:
                print(f"  Network: {container_network} (IP assigned dynamically)")
            print(f"  Gateway: {GATEWAY_CONTAINER_NAME} at {gateway_ip}")
            print(f"  Gateway API: http://{GATEWAY_CONTAINER_NAME}:{GATEWAY_PORT}")
            print("  Container can: Access internet directly, GitHub (via gateway sidecar)")
            print("  Container cannot: Access private repos")
        if session_token:
            print(f"  Session: Active ({repo_mode} mode)")
        print()

    # Add mount arguments
    cmd.extend(mount_args)

    # Add image name
    cmd.append(Config.IMAGE_NAME)

    # End timing for command build
    _host_timer.end_phase()  # build_docker_cmd

    # Pass host timing data to container (must be after all host phases complete)
    host_timing_json = _host_timer.to_json()
    if host_timing_json:
        # Insert timing env var before the image name (last element)
        cmd.insert(-1, "-e")
        cmd.insert(-1, f"EGG_HOST_TIMING={host_timing_json}")

    # Final status update before launching
    if quiet:
        status_finish("Launching Claude Code...")

    # Record launch timestamp for measuring docker startup time
    # This captures the gap between host finishing and container Python starting
    if _host_timer.enabled:
        launch_time = time.time()
        cmd.insert(-1, "-e")
        cmd.insert(-1, f"EGG_HOST_LAUNCH_TIME={launch_time}")

    # Run container
    try:
        subprocess.run(cmd, check=False)
        return True
    except KeyboardInterrupt:
        print()
        warn("Interrupted by user")
        return False
    except Exception as e:
        error(f"Failed to run container: {e}")
        return False
    finally:
        # Clean up session and worktrees when container exits
        if repos:
            _cleanup_session(session_token, container_id)


def exec_in_new_container(
    command: list[str],
    timeout_minutes: int = 30,
    task_id: str | None = None,
    thread_ts: str | None = None,
    auth_mode: str = "oauth-token",
    repo_mode: str | None = None,
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

    # Check if image exists - build non-interactively if missing
    if not image_exists():
        info("Docker image not found. Building...")
        create_dockerfile()

    # Check repository configuration - warn but continue if missing
    if not Config.REPOS_CONFIG_FILE.exists():
        warn("No repositories configured. Run 'egg --setup' to add repositories.")
        warn("Continuing with no mounted repositories...")

    # Build/update image
    if not build_image():
        error("Docker build failed")
        return False

    # Start gateway sidecar container (if not already running)
    if not start_gateway_container():
        error("Failed to start gateway sidecar")
        return False

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
    if task_id:
        info(f"Task ID: {task_id}")
    if thread_ts:
        info(f"Thread TS: {thread_ts}")
    print(f"Command: {' '.join(command)}")
    print(f"Timeout: {timeout_minutes} minutes")
    print()

    # Build mount configuration
    info("Configuring repository mounts...")
    mount_args = []

    # Track session token for cleanup and container env
    session_token = None
    container_ip = None

    # Get network configuration based on mode (centralized in helper to prevent divergence)
    container_network, gateway_ip, network_extra_args = _get_container_network_config(repo_mode)

    # Choose mount strategy based on repo_mode
    if repo_mode:
        # Per-container session mode: allocate IP first for session binding
        container_ip = _allocate_container_ip(network=container_network)
        if not container_ip:
            error("Failed to allocate container IP for session mode")
            return False

        info(f"Session mode: {repo_mode}")
        info(f"Pre-allocated IP: {container_ip}")

        # Use session-based repo setup with visibility filtering
        session_token, repos, _filtered_repos = _setup_session_repos(
            container_id=container_id,
            container_ip=container_ip,
            mode=repo_mode,
            mount_args=mount_args,
            quiet=False,
        )

        if not session_token:
            # Session creation failed - cannot proceed without a session
            # since git/gh wrappers require EGG_SESSION_TOKEN (PR #666)
            error("Session creation failed. Check that:")
            error("  1. Gateway sidecar is running: curl http://localhost:9848/api/v1/health")
            error("  2. Launcher secret exists: ~/.config/egg/launcher-secret")
            error("  Fix: Re-run gateway/setup.sh to sync secrets")
            return False
    else:
        # repo_mode is required since PR #669 - all containers need sessions
        error("repo_mode is required - cannot start container without session")
        return False

    if repos:
        mode_info = f" ({repo_mode} mode)" if repo_mode else ""
        info(f"Mounted {len(repos)} repo(s){mode_info} (all git operations via gateway)")
        print()

    # Add standard mounts (shared-certs)
    add_standard_mounts(mount_args, quiet=False)

    # Note: Host ~/.claude is NOT mounted - container uses gateway-injected
    # Anthropic credentials instead of host Claude configuration

    print()

    # Build docker run command
    # Note: We don't use --rm so we can save logs before cleanup

    cmd = [
        "docker",
        "run",
        "--security-opt",
        "label=disable",  # Disable SELinux labeling for faster startup
        "--name",
        container_id,
        # Network selection based on mode
        "--network",
        container_network,
    ]

    # If session mode with pre-allocated IP, use static IP assignment
    if container_ip:
        cmd.extend(["--ip", container_ip])

    # Add gateway hostname for API access (both modes need this)
    cmd.extend(
        [
            "--add-host",
            f"{GATEWAY_CONTAINER_NAME}:{gateway_ip}",
            # Environment variables
            "-e",
            f"RUNTIME_UID={os.getuid()}",
            "-e",
            f"RUNTIME_GID={os.getgid()}",
            "-e",
            f"CONTAINER_ID={container_id}",
            "-e",
            "PYTHONUNBUFFERED=1",  # Force Python to use unbuffered output
            "-e",
            f"GATEWAY_URL=http://{GATEWAY_CONTAINER_NAME}:{GATEWAY_PORT}",
        ]
    )

    # Mode-specific network settings (DNS, proxy) from centralized helper
    if network_extra_args:
        cmd.extend(network_extra_args)

    # If session mode, pass session token for container authentication
    if session_token:
        cmd.extend(["-e", f"EGG_SESSION_TOKEN={session_token}"])

    # Add logging configuration for log persistence
    log_config = get_docker_log_config(container_id, task_id)
    cmd.extend(log_config)

    # Add correlation environment variables for log tracing
    if task_id:
        cmd.extend(["-e", f"EGG_TASK_ID={task_id}"])
    if thread_ts:
        cmd.extend(["-e", f"EGG_THREAD_TS={thread_ts}"])

    # GitHub authentication is handled by the gateway sidecar
    # The container does NOT receive GITHUB_TOKEN - all git/gh operations
    # route through the gateway which holds the credentials

    # Add Anthropic auth configuration based on CLI auth_mode
    # Map CLI values to ANTHROPIC_AUTH_METHOD values
    auth_method_map = {"api-key": "api_key", "oauth-token": "oauth"}
    anthropic_auth_method = auth_method_map[auth_mode]
    cmd.extend(["-e", f"ANTHROPIC_AUTH_METHOD={anthropic_auth_method}"])

    # Pass API key when using api-key auth mode
    if auth_mode == "api-key":
        api_key = get_anthropic_api_key()
        if api_key:
            cmd.extend(["-e", f"ANTHROPIC_API_KEY={api_key}"])

    # Add mount arguments
    cmd.extend(mount_args)

    # Add image name
    cmd.append(Config.IMAGE_NAME)

    # Add the command to execute
    cmd.extend(command)

    # Run container with configurable timeout
    timeout_seconds = timeout_minutes * 60
    run_success = False

    def cleanup_container():
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

    try:
        result = subprocess.run(cmd, timeout=timeout_seconds, check=False)
        run_success = result.returncode == 0
    except subprocess.TimeoutExpired:
        print()
        error(f"Container execution timed out after {timeout_minutes} minutes")
        # Kill the container if it's still running
        subprocess.run(
            ["docker", "kill", container_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except KeyboardInterrupt:
        print()
        warn("Interrupted by user")
        # Kill container on interrupt
        subprocess.run(
            ["docker", "kill", container_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception as e:
        error(f"Failed to run container: {e}")
    finally:
        # Always save logs and cleanup container
        cleanup_container()

    return run_success
