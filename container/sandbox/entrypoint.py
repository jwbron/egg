#!/usr/bin/env python3
"""
Egg Sandbox Container Entrypoint

Sets up the sandboxed container environment for the AI agent.
Handles user setup, git configuration, service initialization, and launches
the appropriate command.
"""

# ruff: noqa: E402
# Capture container start time FIRST - before any other imports
import time

_CONTAINER_START_TIME = time.time()

# Now import everything else
import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# =============================================================================
# Startup Timing (Debug)
# =============================================================================

# Enabled via EGG_TIMING=1 env var
ENABLE_STARTUP_TIMING = os.environ.get("EGG_TIMING", "0") == "1"


class StartupTimer:
    """Collects timing data for startup phases."""

    def __init__(self):
        self.timings: list[tuple[str, float]] = []
        self.start_time: float = time.perf_counter()
        self._phase_start: float | None = None
        self._phase_name: str | None = None
        self.host_timings: list[tuple[str, float]] = []
        self.host_total_time: float = 0.0
        self.docker_startup_time: float = 0.0  # Gap between host launch and container start
        # Capture time spent in Python init (imports) before this point
        # Uses wall clock since _CONTAINER_START_TIME is wall clock
        self.python_init_time: float = (time.time() - _CONTAINER_START_TIME) * 1000
        self._load_host_timing()

    def _load_host_timing(self) -> None:
        """Load host timing data from environment variable."""
        host_timing_json = os.environ.get("EGG_HOST_TIMING", "")
        if host_timing_json:
            try:
                data = json.loads(host_timing_json)
                self.host_timings = data.get("timings", [])
                self.host_total_time = data.get("total_time", 0.0)
            except (json.JSONDecodeError, KeyError):
                pass

        # Calculate docker startup gap (time between host launching container and Python starting)
        host_launch_time_str = os.environ.get("EGG_HOST_LAUNCH_TIME", "")
        if host_launch_time_str:
            try:
                host_launch_time = float(host_launch_time_str)
                # Gap = container start time - host launch time (in milliseconds)
                self.docker_startup_time = (_CONTAINER_START_TIME - host_launch_time) * 1000
            except (ValueError, TypeError):
                pass

    def start_phase(self, name: str) -> None:
        """Start timing a phase."""
        if not ENABLE_STARTUP_TIMING:
            return
        self._phase_name = name
        self._phase_start = time.perf_counter()

    def end_phase(self) -> None:
        """End timing the current phase."""
        if not ENABLE_STARTUP_TIMING or self._phase_start is None:
            return
        elapsed = (time.perf_counter() - self._phase_start) * 1000  # ms
        self.timings.append((self._phase_name, elapsed))
        self._phase_name = None
        self._phase_start = None

    def phase(self, name: str):
        """Context manager for timing a phase."""
        timer = self
        phase_name = name

        class PhaseContext:
            def __enter__(self):
                timer.start_phase(phase_name)
                return self

            def __exit__(self, *args):
                timer.end_phase()

        return PhaseContext()

    def print_summary(self) -> None:
        """Print combined timing summary (host + container phases)."""
        if not ENABLE_STARTUP_TIMING:
            return
        if not self.timings and not self.host_timings:
            return

        # Container total includes python_init (imports) + all phases
        phases_total = (time.perf_counter() - self.start_time) * 1000
        container_total = self.python_init_time + phases_total
        grand_total = self.host_total_time + self.docker_startup_time + container_total

        print("\n" + "=" * 60)
        print("STARTUP TIMING SUMMARY")
        print("=" * 60)
        print(f"{'Phase':<40} {'Time (ms)':>10} {'%':>6}")
        print("-" * 60)

        # Print host phases (% of grand total)
        if self.host_timings:
            print("HOST:")
            for name, elapsed in self.host_timings:
                pct = (elapsed / grand_total) * 100 if grand_total > 0 else 0
                bar = "#" * int(pct / 5)
                print(f"  {name:<38} {elapsed:>10.1f} {pct:>5.1f}% {bar}")
            print(f"  {'(host total)':<38} {self.host_total_time:>10.1f}")
            print()

        # Print docker startup gap
        if self.docker_startup_time > 0:
            print("DOCKER:")
            pct = (self.docker_startup_time / grand_total) * 100 if grand_total > 0 else 0
            bar = "#" * int(pct / 5)
            print(
                f"  {'container_startup':<38} {self.docker_startup_time:>10.1f} {pct:>5.1f}% {bar}"
            )
            print()

        # Print container phases
        if self.timings or self.python_init_time > 0:
            print("CONTAINER:")
            if self.python_init_time > 0:
                pct = (
                    (self.python_init_time / container_total) * 100
                    if container_total > 0
                    else 0
                )
                bar = "#" * int(pct / 5)
                print(
                    f"  {'python_init':<38} {self.python_init_time:>10.1f} {pct:>5.1f}% {bar}"
                )
            for name, elapsed in self.timings:
                pct = (elapsed / container_total) * 100 if container_total > 0 else 0
                bar = "#" * int(pct / 5)
                print(f"  {name:<38} {elapsed:>10.1f} {pct:>5.1f}% {bar}")
            print(f"  {'(container total)':<38} {container_total:>10.1f}")

        print("-" * 60)
        print(f"{'GRAND TOTAL':<40} {grand_total:>10.1f}")
        print("=" * 60 + "\n")


# Global timer instance
_startup_timer = StartupTimer()


# =============================================================================
# Configuration
# =============================================================================


@dataclass
class Config:
    """Container configuration from environment variables."""

    # Fixed container user - UID/GID adjusted at runtime to match host
    container_user: str = "sandbox"
    runtime_uid: int = field(
        default_factory=lambda: int(os.environ.get("RUNTIME_UID", "1000"))
    )
    runtime_gid: int = field(
        default_factory=lambda: int(os.environ.get("RUNTIME_GID", "1000"))
    )
    quiet: bool = field(default_factory=lambda: os.environ.get("EGG_QUIET", "0") == "1")

    # Derived paths - fixed home directory for sandbox user
    @property
    def user_home(self) -> Path:
        return Path("/home/sandbox")

    @property
    def repos_dir(self) -> Path:
        """The directory containing mounted repositories."""
        return self.user_home / "repos"

    @property
    def sharing_dir(self) -> Path:
        return self.user_home / "sharing"


# =============================================================================
# Logging
# =============================================================================


class Logger:
    """Simple logger with quiet mode support."""

    def __init__(self, quiet: bool = False):
        self.quiet = quiet

    def info(self, msg: str) -> None:
        """Info message (hidden in quiet mode)."""
        if not self.quiet:
            print(msg)

    def success(self, msg: str) -> None:
        """Success message with checkmark (hidden in quiet mode)."""
        if not self.quiet:
            print(f"[OK] {msg}")

    def warn(self, msg: str) -> None:
        """Warning message (always shown)."""
        print(f"[WARN] {msg}")

    def error(self, msg: str) -> None:
        """Error message (always shown, to stderr)."""
        print(f"[ERROR] {msg}", file=sys.stderr)


# =============================================================================
# Utility Functions
# =============================================================================


def run_cmd(
    cmd: list[str],
    check: bool = True,
    capture: bool = False,
    timeout: int = 30,
    as_user: tuple[int, int] | None = None,
) -> subprocess.CompletedProcess:
    """Run a command, optionally as a different user via gosu."""
    if as_user:
        uid, gid = as_user
        cmd = ["gosu", f"{uid}:{gid}"] + cmd

    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
        timeout=timeout,
    )


def chown_recursive(path: Path, uid: int, gid: int) -> None:
    """Recursively change ownership of a path."""
    run_cmd(["chown", "-R", f"{uid}:{gid}", str(path)])


# =============================================================================
# Setup Functions
# =============================================================================


def setup_user(config: Config, logger: Logger) -> None:
    """Adjust sandbox user's UID/GID to match host user for proper file permissions."""
    import grp
    import pwd

    logger.info(
        f"Setting up sandboxed environment for user: {config.container_user} "
        f"(uid={config.runtime_uid}, gid={config.runtime_gid})"
    )

    # Get current sandbox user's UID/GID
    try:
        current_uid = pwd.getpwnam(config.container_user).pw_uid
        current_gid = grp.getgrnam(config.container_user).gr_gid
    except KeyError:
        logger.error(
            f"User {config.container_user} not found - container image may be corrupt"
        )
        raise

    # Adjust GID if needed
    if current_gid != config.runtime_gid:
        logger.info(
            f"Adjusting {config.container_user} group GID: "
            f"{current_gid} -> {config.runtime_gid}"
        )
        run_cmd(["groupmod", "-g", str(config.runtime_gid), config.container_user])

    # Adjust UID if needed
    if current_uid != config.runtime_uid:
        logger.info(
            f"Adjusting {config.container_user} user UID: "
            f"{current_uid} -> {config.runtime_uid}"
        )
        run_cmd(["usermod", "-u", str(config.runtime_uid), config.container_user])

    # Fix ownership of home directory after UID/GID change
    if current_uid != config.runtime_uid or current_gid != config.runtime_gid:
        logger.info("Fixing home directory ownership...")
        start_time = time.time()
        chown_recursive(config.user_home, config.runtime_uid, config.runtime_gid)
        elapsed = time.time() - start_time
        if elapsed > 1.0:
            logger.info(f"  chown completed in {elapsed:.1f}s")


def setup_environment(config: Config) -> None:
    """Set up environment variables."""
    os.environ["HOME"] = str(config.user_home)
    os.environ["USER"] = config.container_user

    # Add user's local bin and egg runtime scripts to PATH
    current_path = os.environ.get("PATH", "")
    local_bin = config.user_home / ".local" / "bin"
    os.environ["PATH"] = (
        f"{local_bin}:/opt/egg-runtime/sandbox/bin:/usr/local/bin:{current_path}"
    )

    # Python settings
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PYTHONUNBUFFERED"] = "1"

    # Git editor - use 'true' (no-op) for non-interactive environment
    os.environ["GIT_EDITOR"] = "true"


def setup_git(config: Config, logger: Logger) -> None:
    """Configure git for sandbox identity and credential helper."""
    user_tuple = (config.runtime_uid, config.runtime_gid)

    # Set git identity
    run_cmd(["git", "config", "--global", "user.name", "sandbox"], as_user=user_tuple)
    run_cmd(
        ["git", "config", "--global", "user.email", "sandbox@localhost"],
        as_user=user_tuple,
    )

    # Configure credential helper
    github_token = os.environ.get("GITHUB_TOKEN")
    if github_token:
        run_cmd(
            [
                "git",
                "config",
                "--global",
                "credential.helper",
                "/opt/egg-runtime/sandbox/bin/git-credential-github-token",
            ],
            as_user=user_tuple,
        )
        run_cmd(
            ["git", "config", "--global", "credential.useHttpPath", "true"],
            as_user=user_tuple,
        )
        logger.success("Git credential helper configured for GitHub push")
    else:
        run_cmd(["git", "config", "--global", "credential.helper", ""], as_user=user_tuple)

    # Never embed tokens in URLs
    run_cmd(
        ["git", "config", "--global", "advice.pushUpdateRejected", "false"],
        as_user=user_tuple,
    )

    logger.success("Git configured to commit as sandbox <sandbox@localhost>")


def setup_worktrees(config: Config, logger: Logger) -> bool:
    """Validate gateway-managed worktree configuration.

    In the gateway-managed worktree architecture:
    - Gateway creates/manages worktrees before container starts
    - Container mounts only working directory (no git metadata access)
    - All git operations route through gateway API
    - No path rewriting needed - gateway controls all paths

    Returns False if setup failed fatally.
    """
    if not config.repos_dir.exists():
        logger.warn("Repos workspace not found - check mount configuration")
        return True

    # Count repos for logging
    repo_count = 0
    for repo_dir in config.repos_dir.iterdir():
        if repo_dir.is_dir():
            repo_count += 1

    if repo_count > 0:
        logger.success(f"Repos mounted: {repo_count} repo(s) (gateway-managed worktrees)")
        logger.info("  All git operations route through gateway API")

    return True


def setup_sharing(config: Config, logger: Logger) -> None:
    """Set up shared directories and symlinks."""
    if not config.sharing_dir.exists():
        logger.warn("Sharing directory not found - check mount configuration")
        return

    # Create symlink: ~/tmp -> ~/sharing/tmp
    tmp_link = config.user_home / "tmp"
    if tmp_link.is_symlink():
        tmp_link.unlink()
    elif not tmp_link.exists():
        tmp_link.symlink_to(config.sharing_dir / "tmp")

    # Ensure subdirectories exist
    subdirs = ["tmp", "notifications", "context", "tracking", "traces", "logs"]
    for subdir in subdirs:
        (config.sharing_dir / subdir).mkdir(parents=True, exist_ok=True)

    chown_recursive(config.sharing_dir, config.runtime_uid, config.runtime_gid)

    logger.success("Shared directories configured")


def setup_bashrc(config: Config, logger: Logger) -> None:
    """Set up .bashrc with useful settings."""
    bashrc = config.user_home / ".bashrc"

    # Append our settings
    with open(bashrc, "a") as f:
        f.write("\n# Added by egg entrypoint\n")
        f.write(
            r"export PS1='\[\033[01;32m\]\u@sandboxed\[\033[00m\]:\[\033[01;34m\]\w\[\033[00m\]\$ '"
            + "\n"
        )

    os.chown(bashrc, config.runtime_uid, config.runtime_gid)
    logger.success("Shell prompt configured for sandboxed environment")


def check_gateway_health(config: Config, logger: Logger) -> bool:
    """Wait for gateway readiness before starting.

    In network lockdown mode, the container cannot reach the internet directly.
    All traffic must go through the gateway's proxy. This function ensures
    the gateway and proxy are ready before the agent starts.

    Returns:
        True if gateway is ready, False on timeout
    """
    import socket

    import requests
    from requests.exceptions import RequestException

    gateway_url = os.environ.get("GATEWAY_URL", "http://egg-gateway:9847")
    proxy_url = os.environ.get("HTTPS_PROXY")

    # Detect network mode: private mode has HTTPS_PROXY set, public mode doesn't
    is_private_mode = proxy_url is not None
    if is_private_mode:
        logger.info("Network mode: PRIVATE (lockdown, proxy filtering)")
    else:
        logger.info("Network mode: PUBLIC (direct internet access)")

    # Log configuration for debugging
    logger.info("Gateway configuration:")
    logger.info(f"  GATEWAY_URL: {gateway_url}")
    if is_private_mode:
        logger.info(f"  HTTPS_PROXY: {proxy_url}")
    else:
        logger.info("  HTTPS_PROXY: (not set - direct internet access)")

    # Check hostname resolution
    gateway_host = "egg-gateway"
    try:
        resolved_ip = socket.gethostbyname(gateway_host)
        logger.info(f"  {gateway_host} resolves to: {resolved_ip}")
    except socket.gaierror as e:
        logger.error(f"  DNS resolution failed for {gateway_host}: {e}")
        logger.error("  Check --add-host configuration in container startup")

    logger.info("Waiting for gateway readiness...")

    timeout = 60  # seconds
    interval = 2  # seconds
    elapsed = 0

    api_health_passed = False
    api_health_error = None

    while elapsed < timeout:
        # Check Gateway API health endpoint
        try:
            health_url = f"{gateway_url}/api/v1/health"
            health_response = requests.get(
                health_url,
                timeout=5,
                proxies={"http": None, "https": None},
            )
            if health_response.status_code == 200:
                try:
                    health_data = health_response.json()
                    health_status = health_data.get("status", "unknown")

                    if not api_health_passed:
                        logger.success(
                            f"  Gateway API responding (HTTP {health_response.status_code})"
                        )
                        logger.info(f"    Status: {health_status}")

                    if health_status == "healthy":
                        api_health_passed = True
                    else:
                        api_health_error = f"Status: {health_status}"
                        api_health_passed = True  # Proceed anyway
                except (ValueError, KeyError) as e:
                    api_health_error = f"Invalid JSON response: {e}"
                    api_health_passed = True
            else:
                api_health_error = (
                    f"HTTP {health_response.status_code}: {health_response.text[:100]}"
                )

        except RequestException as e:
            api_health_error = f"{type(e).__name__}: {e}"
            if not config.quiet and elapsed % 10 == 0:
                logger.info(f"  Gateway API check failed: {api_health_error}")

        # Check proxy connectivity (only in private mode)
        if api_health_passed:
            if not is_private_mode:
                logger.success("Gateway ready! (public mode - direct internet access)")
                return True

            # Private mode: verify proxy connectivity to Anthropic API
            try:
                proxies = {"http": proxy_url, "https": proxy_url}
                api_response = requests.get(
                    "https://api.anthropic.com/",
                    proxies=proxies,
                    timeout=10,
                    verify=True,
                )
                if api_response.status_code in (200, 401, 403, 404):
                    logger.success(
                        f"  Proxy connectivity verified (Anthropic returned "
                        f"HTTP {api_response.status_code})"
                    )
                    logger.success("Gateway ready!")
                    return True
            except RequestException as e:
                if not config.quiet and elapsed % 10 == 0:
                    logger.info(f"  Proxy check failed: {type(e).__name__}: {e}")

        if not config.quiet and elapsed > 0 and elapsed % 10 == 0:
            logger.info(f"  Still waiting... ({elapsed}/{timeout}s)")

        time.sleep(interval)
        elapsed += interval

    logger.error(f"Gateway not ready after {timeout} seconds")
    return False


# =============================================================================
# Cleanup
# =============================================================================


def cleanup_on_exit(config: Config, logger: Logger) -> None:
    """Cleanup handler for container shutdown."""
    if not config.quiet:
        print("")
        print("Cleaning up on container exit...")
        print("[OK] Cleanup complete")


# =============================================================================
# Main Entry Points
# =============================================================================


def run_interactive(config: Config, logger: Logger) -> None:
    """Launch interactive shell session."""
    logger.info("")
    logger.info("Starting interactive shell session...")

    # Change to repos directory
    if config.repos_dir.exists():
        os.chdir(config.repos_dir)
    else:
        os.chdir(config.user_home)

    # Build environment
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": "/opt/egg-runtime/sandbox:/opt/egg-runtime/shared",
            "NO_PROXY": os.environ.get("NO_PROXY", "127.0.0.1"),
        }
    )

    # Print timing summary right before launching shell
    _startup_timer.print_summary()

    # Launch via gosu
    os.execvpe(
        "gosu",
        [
            "gosu",
            f"{config.runtime_uid}:{config.runtime_gid}",
            "/bin/bash",
        ],
        env,
    )


def run_exec(config: Config, logger: Logger, args: list[str]) -> None:
    """Run a command in exec mode."""
    env = os.environ.copy()

    # Print timing summary before exec
    _startup_timer.print_summary()

    os.execvpe(
        "gosu",
        ["gosu", f"{config.runtime_uid}:{config.runtime_gid}"] + args,
        env,
    )


# =============================================================================
# Main
# =============================================================================


def main() -> None:
    """Main entry point."""
    config = Config()
    logger = Logger(config.quiet)

    # Register cleanup handler
    def signal_handler(signum, frame):
        cleanup_on_exit(config, logger)
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Run setup with timing instrumentation
    with _startup_timer.phase("setup_user"):
        setup_user(config, logger)

    with _startup_timer.phase("setup_environment"):
        setup_environment(config)

    with _startup_timer.phase("setup_git"):
        setup_git(config, logger)

    with _startup_timer.phase("setup_worktrees"):
        if not setup_worktrees(config, logger):
            logger.error("")
            logger.error("Container startup aborted due to worktree configuration failure.")
            logger.error("Please check your setup and try again.")
            sys.exit(1)

    with _startup_timer.phase("setup_sharing"):
        setup_sharing(config, logger)

    with _startup_timer.phase("setup_bashrc"):
        setup_bashrc(config, logger)

    # Wait for gateway readiness (network lockdown mode)
    with _startup_timer.phase("check_gateway"):
        if not check_gateway_health(config, logger):
            logger.error("")
            logger.error("Container startup aborted: gateway not ready.")
            logger.error("Ensure the gateway sidecar is running.")
            sys.exit(1)

    # Run appropriate mode (timing summary is printed inside each mode)
    if len(sys.argv) == 1:
        run_interactive(config, logger)
    else:
        run_exec(config, logger, sys.argv[1:])


if __name__ == "__main__":
    main()
