#!/usr/bin/env python3
"""
Egg Container Entrypoint

Sets up the sandboxed container environment for the autonomous AI agent.
Handles user setup, git configuration, service initialization, and launches
the appropriate LLM interface.

Converted from entrypoint.sh for better maintainability.
"""

# Capture container start time FIRST - before any other imports
# This measures from the moment Python starts executing this file
import time

_CONTAINER_START_TIME = time.time()

# Now import everything else
import contextlib
import errno
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from egg_config import GATEWAY_PORT, GATEWAY_PROXY_PORT

# Well-known path for subprocess stderr capture (read by signal_orchestrator_completion)
_SUBPROCESS_STDERR_LOG = Path("/tmp/egg-subprocess-stderr.log")

# =============================================================================
# Startup Timing (Debug)
# =============================================================================

# Enabled via EGG_TIMING=1 env var (set by `egg --time` on host)
ENABLE_STARTUP_TIMING = os.environ.get("EGG_TIMING", "0") == "1"


class StartupTimer:
    """Collects timing data for startup phases."""

    def __init__(self) -> None:
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
        import json

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
        if not ENABLE_STARTUP_TIMING or self._phase_start is None or self._phase_name is None:
            return
        elapsed = (time.perf_counter() - self._phase_start) * 1000  # ms
        self.timings.append((self._phase_name, elapsed))
        self._phase_name = None
        self._phase_start = None

    def phase(self, name: str) -> Any:
        """Context manager for timing a phase."""
        timer = self
        phase_name = name

        class PhaseContext:
            def __enter__(self) -> "PhaseContext":
                timer.start_phase(phase_name)
                return self

            def __exit__(self, *args: Any) -> None:
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
                bar = "█" * int(pct / 5)
                print(f"  {name:<38} {elapsed:>10.1f} {pct:>5.1f}% {bar}")
            print(f"  {'(host total)':<38} {self.host_total_time:>10.1f}")
            print()

        # Print docker startup gap (time from host launch to container Python starting)
        if self.docker_startup_time > 0:
            print("DOCKER:")
            pct = (self.docker_startup_time / grand_total) * 100 if grand_total > 0 else 0
            bar = "█" * int(pct / 5)
            print(
                f"  {'container_startup':<38} {self.docker_startup_time:>10.1f} {pct:>5.1f}% {bar}"
            )
            print()

        # Print container phases (% of container total for meaningful breakdown)
        if self.timings or self.python_init_time > 0:
            print("CONTAINER:")
            # Show python_init first (time for imports before StartupTimer was created)
            if self.python_init_time > 0:
                pct = (self.python_init_time / container_total) * 100 if container_total > 0 else 0
                bar = "█" * int(pct / 5)
                print(f"  {'python_init':<38} {self.python_init_time:>10.1f} {pct:>5.1f}% {bar}")
            for name, elapsed in self.timings:
                pct = (elapsed / container_total) * 100 if container_total > 0 else 0
                bar = "█" * int(pct / 5)
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
    container_user: str = "egg"
    runtime_uid: int = field(default_factory=lambda: int(os.environ.get("RUNTIME_UID", "1000")))
    runtime_gid: int = field(default_factory=lambda: int(os.environ.get("RUNTIME_GID", "1000")))
    quiet: bool = field(default_factory=lambda: os.environ.get("EGG_QUIET", "0") == "1")
    debug: bool = field(default_factory=lambda: os.environ.get("EGG_DEBUG", "0") == "1")

    # Orchestrator mode configuration
    # These are set when the sandbox is spawned by an orchestrator
    orchestrator_mode: str = field(
        default_factory=lambda: os.environ.get("EGG_ORCHESTRATOR_MODE", "local")
    )
    orchestrator_url: str | None = field(
        default_factory=lambda: os.environ.get("EGG_ORCHESTRATOR_URL")
    )
    pipeline_id: str | None = field(default_factory=lambda: os.environ.get("EGG_PIPELINE_ID"))
    agent_role: str | None = field(default_factory=lambda: os.environ.get("EGG_AGENT_ROLE"))

    @property
    def is_orchestrator_mode(self) -> bool:
        """Check if running in orchestrator-managed mode (vs interactive)."""
        # Explicit mode setting
        if self.orchestrator_mode in ("remote-single", "distributed"):
            return True
        # Implicit detection from pipeline context
        if self.pipeline_id:
            return True
        # Implicit detection from orchestrator URL
        if self.orchestrator_url:
            return True
        return False

    # LLM configuration
    # Auth method: "api_key" (default) or "oauth"
    # When oauth, don't warn about missing ANTHROPIC_API_KEY
    anthropic_auth_method: str = field(
        default_factory=lambda: os.environ.get("ANTHROPIC_AUTH_METHOD", "api_key").lower()
    )

    # Valid auth methods for validation
    VALID_AUTH_METHODS: ClassVar[tuple[str, ...]] = ("api_key", "oauth")
    anthropic_api_key: str | None = field(
        default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY")
    )

    # GitHub tokens
    github_token: str | None = field(default_factory=lambda: os.environ.get("GITHUB_TOKEN"))
    github_readonly_token: str | None = field(
        default_factory=lambda: os.environ.get("GITHUB_READONLY_TOKEN")
    )

    # Derived paths - fixed home directory for egg user
    @property
    def user_home(self) -> Path:
        return Path("/home/egg")

    @property
    def repos_dir(self) -> Path:
        """The directory containing mounted repositories."""
        return self.user_home / "repos"

    @property
    def claude_dir(self) -> Path:
        return self.user_home / ".claude"


# =============================================================================
# Logging
# =============================================================================


class Logger:
    """Simple logger with quiet mode and debug mode support.

    Debug mode (EGG_DEBUG=1) logs each startup phase to stderr for
    post-mortem analysis when containers hang or timeout.
    """

    def __init__(self, quiet: bool = False, debug: bool = False):
        self.quiet = quiet
        self.debug = debug

    def info(self, msg: str) -> None:
        """Info message (hidden in quiet mode)."""
        if not self.quiet:
            print(msg)

    def success(self, msg: str) -> None:
        """Success message with checkmark (hidden in quiet mode)."""
        if not self.quiet:
            print(f"✓ {msg}")

    def warn(self, msg: str) -> None:
        """Warning message (always shown)."""
        print(f"⚠ {msg}")

    def error(self, msg: str) -> None:
        """Error message (always shown, to stderr)."""
        print(f"✗ {msg}", file=sys.stderr)

    def phase_start(self, phase: str) -> None:
        """Log the start of a startup phase (debug mode only, to stderr).

        These messages go to stderr so they're captured even if the
        container hangs during startup (stdout may be buffered).
        """
        if self.debug:
            elapsed_ms = (time.time() - _CONTAINER_START_TIME) * 1000
            print(f"[DEBUG +{elapsed_ms:7.0f}ms] Starting: {phase}", file=sys.stderr)
            sys.stderr.flush()

    def phase_end(self, phase: str) -> None:
        """Log the end of a startup phase (debug mode only, to stderr)."""
        if self.debug:
            elapsed_ms = (time.time() - _CONTAINER_START_TIME) * 1000
            print(f"[DEBUG +{elapsed_ms:7.0f}ms] Finished: {phase}", file=sys.stderr)
            sys.stderr.flush()


@contextlib.contextmanager
def timed_phase(name: str, logger: Logger) -> Generator[None, None, None]:
    """Context manager that combines startup timing with debug logging.

    Wraps both _startup_timer.phase() and logger.phase_start/phase_end
    to reduce repetition in the main startup sequence.
    """
    logger.phase_start(name)
    try:
        with _startup_timer.phase(name):
            yield
    finally:
        logger.phase_end(name)


# =============================================================================
# Utility Functions
# =============================================================================


def run_cmd(
    cmd: list[str],
    check: bool = True,
    capture: bool = False,
    timeout: int = 30,
    as_user: tuple[int, int] | None = None,
) -> subprocess.CompletedProcess[str]:
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
    """Recursively change ownership of a path, tolerating read-only mounts."""
    result = subprocess.run(
        ["chown", "-R", f"{uid}:{gid}", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Filter out read-only filesystem errors (from bind mounts like .git shadow)
        real_errors = [
            line
            for line in result.stderr.strip().splitlines()
            if "Read-only file system" not in line
        ]
        if real_errors:
            raise subprocess.CalledProcessError(
                result.returncode, result.args, result.stdout, result.stderr
            )


# =============================================================================
# Setup Functions
# =============================================================================


def setup_user(config: Config, logger: Logger) -> None:
    """Adjust egg user's UID/GID to match host user for proper file permissions."""
    import grp
    import pwd

    logger.info(
        f"Setting up sandboxed environment for user: {config.container_user} "
        f"(uid={config.runtime_uid}, gid={config.runtime_gid})"
    )

    # Get current egg user's UID/GID
    try:
        current_uid = pwd.getpwnam(config.container_user).pw_uid
        current_gid = grp.getgrnam(config.container_user).gr_gid
    except KeyError:
        logger.error(f"User {config.container_user} not found - container image may be corrupt")
        raise

    # Adjust GID if needed
    if current_gid != config.runtime_gid:
        logger.info(
            f"Adjusting {config.container_user} group GID: {current_gid} -> {config.runtime_gid}"
        )
        run_cmd(["groupmod", "-g", str(config.runtime_gid), config.container_user])

    # Adjust UID if needed
    if current_uid != config.runtime_uid:
        logger.info(
            f"Adjusting {config.container_user} user UID: {current_uid} -> {config.runtime_uid}"
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


def setup_repo_permissions(config: Config, logger: Logger) -> None:
    """Ensure repo bind-mount points are writable by the egg user.

    Docker bind mounts preserve host ownership, so repo directories may
    be root-owned inside the container.  This must run regardless of
    whether the egg user's UID was adjusted (setup_user only chowns when
    UID/GID change, but the mounts are always root-owned).

    Only chown the top-level repo directories (not recursive) — repo file
    contents are managed by git/gateway worktree operations.
    """
    repos_dir = config.repos_dir
    if not repos_dir.exists():
        return

    try:
        os.chown(repos_dir, config.runtime_uid, config.runtime_gid)
    except OSError:
        pass  # May be read-only

    for repo_dir in repos_dir.iterdir():
        if repo_dir.is_dir():
            try:
                os.chown(repo_dir, config.runtime_uid, config.runtime_gid)
            except OSError:
                pass  # Tolerate read-only mounts (e.g. .git tmpfs)

    logger.success("Repo mount permissions verified")


# NOTE: PostgreSQL and Redis service startup removed for now.
# If needed in the future, add a setup_services() function here that starts them:
#   service postgresql start
#   service redis-server start
# The container image still includes these services if installed via docker-setup.py.


def setup_environment(config: Config) -> None:
    """Set up environment variables."""
    os.environ["HOME"] = str(config.user_home)
    os.environ["USER"] = config.container_user

    # Add user's local bin (Claude Code native install) and egg runtime scripts to PATH
    current_path = os.environ.get("PATH", "")
    local_bin = config.user_home / ".local" / "bin"
    os.environ["PATH"] = f"{local_bin}:/opt/egg-runtime/sandbox/bin:/usr/local/bin:{current_path}"

    # Python settings
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PYTHONUNBUFFERED"] = "1"

    # Claude settings
    os.environ["DISABLE_AUTOUPDATER"] = "1"

    # Git editor - use 'true' (no-op) for non-interactive environment
    # This allows git rebase --continue to work without an interactive editor.
    # Side effects: git commit without -m creates empty messages, git rebase -i
    # applies default picks. This is intentional for autonomous operation.
    os.environ["GIT_EDITOR"] = "true"


def setup_git(config: Config, logger: Logger) -> None:
    """Configure git for egg identity and credential helper."""
    user_tuple = (config.runtime_uid, config.runtime_gid)

    # Set git identity
    run_cmd(["git", "config", "--global", "user.name", "egg"], as_user=user_tuple)
    run_cmd(["git", "config", "--global", "user.email", "egg@localhost"], as_user=user_tuple)

    # Configure credential helper if token available
    if config.github_token:
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

    logger.success("Git configured to commit as egg <egg@localhost>")


def setup_gateway_ca(config: Config, logger: Logger) -> None:
    """Add gateway CA certificate to container trust store.

    Note: With ANTHROPIC_BASE_URL routing Claude Code traffic directly to the
    gateway HTTP endpoint (PR #701), this CA trust is no longer required for
    Anthropic API traffic. The Squid proxy now only does peek/splice (SNI
    inspection without MITM), so clients validate origin server certificates
    directly.

    This function is kept for:
    1. Backwards compatibility during transition
    2. Potential future HTTPS interception needs (if we ever need to MITM
       other traffic through the proxy)

    The CA cert is copied from the shared volume (populated by gateway
    entrypoint) to the system CA store.

    Note on idempotency: update-ca-certificates is idempotent and can
    be called multiple times safely.
    """
    gateway_ca_src = Path("/shared/certs/gateway-ca.crt")
    gateway_ca_dst = Path("/usr/local/share/ca-certificates/gateway-ca.crt")

    if not gateway_ca_src.exists():
        # With ANTHROPIC_BASE_URL, missing CA is not a critical error
        # (Anthropic traffic goes directly to gateway HTTP endpoint)
        logger.info("Gateway CA certificate not found (not required with ANTHROPIC_BASE_URL)")
        return

    # Copy cert to ca-certificates directory
    shutil.copy(gateway_ca_src, gateway_ca_dst)
    gateway_ca_dst.chmod(0o644)

    # Update system trust store
    result = run_cmd(["update-ca-certificates"], check=False, capture=True)
    if result.returncode == 0:
        logger.success("Gateway CA certificate added to trust store")
    else:
        # Not critical with ANTHROPIC_BASE_URL - just log info
        logger.info(f"Gateway CA not added to trust store: {result.stderr}")

    # Configure Python and Node.js to use system CA bundle
    # Python's requests library uses certifi by default, not the system store
    # Node.js needs NODE_EXTRA_CA_CERTS for additional CAs
    system_ca_bundle = "/etc/ssl/certs/ca-certificates.crt"
    os.environ["REQUESTS_CA_BUNDLE"] = system_ca_bundle
    os.environ["SSL_CERT_FILE"] = system_ca_bundle
    os.environ["NODE_EXTRA_CA_CERTS"] = str(gateway_ca_dst)


def setup_anthropic_api(config: Config, logger: Logger) -> None:
    """Configure Anthropic API to route through gateway for credential injection.

    Sets ANTHROPIC_BASE_URL to route Claude Code API calls through the gateway,
    where credentials are injected. This approach:
    - Uses Claude Code's documented ANTHROPIC_BASE_URL configuration
    - No SSL MITM needed for Anthropic traffic (HTTP to gateway, HTTPS to API)
    - Credentials never exist in container environment
    - Works for both API key and OAuth modes

    A placeholder OAuth token is set to satisfy Claude Code's startup validation.
    The gateway strips this placeholder and injects real credentials.

    Reference: PR #701 - ANTHROPIC_BASE_URL credential injection plan
    """
    gateway_url = os.environ.get("GATEWAY_URL", f"http://egg-gateway:{GATEWAY_PORT}")

    # Placeholder OAuth token to satisfy Claude Code's startup validation
    # Must match sk-ant-oat01-* format for Claude Code to accept it
    # Gateway strips this and injects real credentials from secrets.env
    oauth_placeholder = (
        "sk-ant-oat01-PROXY-INJECTED-gateway-handles-real-credential-"
        "00000000000000000000000000000000000000000000000000000000000000-000000AAAA"
    )

    # Set ANTHROPIC_BASE_URL to route API calls through gateway
    os.environ["ANTHROPIC_BASE_URL"] = gateway_url

    # Set placeholder OAuth token for Claude Code's startup validation
    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_placeholder

    # Remove any Anthropic API key from container environment
    # Credentials are held by gateway only - this prevents accidental exposure
    for key in ["ANTHROPIC_API_KEY"]:
        if key in os.environ:
            del os.environ[key]

    logger.success(f"Anthropic API routed through gateway: {gateway_url}")
    logger.info("  Credentials injected by gateway (not in container)")


def setup_worktrees(config: Config, logger: Logger) -> bool:
    """Validate gateway-managed worktree configuration.

    This implements the Gateway-Managed Worktrees ADR:
    - Gateway creates/manages worktrees before container starts
    - Container mounts only working directory (no git metadata access)
    - All git operations route through gateway API
    - No path rewriting needed - gateway controls all paths

    The .git file/directory is shadowed by tmpfs mount, so container
    cannot perform local git operations - they must go through gateway.

    Returns False if setup failed fatally.
    """
    if not config.repos_dir.exists():
        logger.warn("Repos workspace not found - check mount configuration")
        return True

    # Count repos and validate working trees
    repo_count = 0
    for repo_dir in config.repos_dir.iterdir():
        if repo_dir.is_dir():
            repo_count += 1
            # Check if working tree is populated (should have more than just .git)
            visible_files = [f for f in repo_dir.iterdir() if f.name != ".git"]
            if not visible_files:
                logger.warn(f"Working tree empty for {repo_dir.name}, re-populating via gateway")
                result = subprocess.run(
                    ["git", "-C", str(repo_dir), "checkout", "HEAD", "--", "."],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if result.returncode == 0:
                    logger.success(f"Re-populated working tree for {repo_dir.name}")
                else:
                    logger.error(f"Failed to re-populate {repo_dir.name}: {result.stderr}")

    if repo_count > 0:
        logger.success(f"Repos mounted: {repo_count} repo(s) (gateway-managed worktrees)")
        logger.info("  All git operations route through gateway API")

    return True


def setup_egg_symlink(config: Config, logger: Logger) -> None:
    """Create ~/egg symlink to runtime scripts.

    This provides a consistent, short path to egg runtime scripts that:
    - Points to /opt/egg-runtime/sandbox (baked into Docker image)
    - Is independent of the mounted ~/repos/egg
    - Matches the container image version
    """
    egg_link = config.user_home / "egg"
    target = Path("/opt/egg-runtime/sandbox")

    # Validate target exists (should always be true if Docker image built correctly)
    if not target.is_dir():
        logger.error(f"Runtime directory not found: {target}")
        logger.error("  This indicates a problem with the Docker image build")
        return

    if egg_link.is_symlink():
        egg_link.unlink()
    elif egg_link.exists():
        logger.warn("~/egg exists but is not a symlink, skipping")
        return

    egg_link.symlink_to(target)
    os.lchown(egg_link, config.runtime_uid, config.runtime_gid)

    logger.success("Runtime symlink created: ~/egg -> /opt/egg-runtime/sandbox")
    logger.info("  Use ~/egg/ for runtime scripts instead of ~/repos/egg/sandbox/")


def setup_agent_rules(config: Config, logger: Logger) -> None:
    """Set up CLAUDE.md agent rules."""
    rules_dir = Path("/opt/claude-rules")

    # All rules always included so CLI tools are discoverable in any session
    rules_order = [
        "mission.md",
        "environment.md",
        "code-standards.md",
        "test-workflow.md",
        "pr-descriptions.md",
        "orchestrator.md",
        "contract.md",
        "checkpoint.md",
    ]

    if not (rules_dir / "mission.md").exists():
        return

    # Combine rules into CLAUDE.md
    claude_md = config.user_home / "CLAUDE.md"
    content_parts = []

    for rule_file in rules_order:
        rule_path = rules_dir / rule_file
        if rule_path.exists():
            content_parts.append(rule_path.read_text())

    claude_md.write_text("\n\n---\n\n".join(content_parts))
    os.chown(claude_md, config.runtime_uid, config.runtime_gid)

    # Symlink in ~/repos/
    if config.repos_dir.exists():
        repos_claude = config.repos_dir / "CLAUDE.md"
        if repos_claude.is_symlink():
            repos_claude.unlink()
        repos_claude.symlink_to(claude_md)
        os.lchown(repos_claude, config.runtime_uid, config.runtime_gid)

    logger.success("AI agent rules installed: ~/CLAUDE.md (symlinked to ~/repos/)")
    logger.info(f"  Combined {len(rules_order)} rule files (index-based per LLM Doc ADR)")
    logger.info("  Note: Reference docs at $EGG_REPO_PATH/docs/ (fetched on-demand)")


def setup_claude(config: Config, logger: Logger) -> None:
    """Set up Claude CLI configuration."""
    # Create directories
    config.claude_dir.mkdir(parents=True, exist_ok=True)
    (config.claude_dir / "commands").mkdir(exist_ok=True)
    (config.user_home / ".config" / "claude-code").mkdir(parents=True, exist_ok=True)

    # Check API key (only warn if using api_key auth method)
    # Validate auth method
    if config.anthropic_auth_method not in config.VALID_AUTH_METHODS:
        logger.warn(
            f"Invalid ANTHROPIC_AUTH_METHOD '{config.anthropic_auth_method}', "
            f"expected one of: {', '.join(config.VALID_AUTH_METHODS)}"
        )

    if config.anthropic_api_key:
        logger.success("Anthropic API key configured")
    elif config.anthropic_auth_method == "oauth":
        logger.success("Anthropic OAuth authentication enabled")
    else:
        logger.warn("ANTHROPIC_API_KEY not set")
        logger.info("  Set via: export ANTHROPIC_API_KEY=sk-ant-...")
        logger.info("  Or use OAuth: export ANTHROPIC_AUTH_METHOD=oauth")

    # Copy custom commands
    commands_src = Path("/usr/local/share/claude-commands")
    if commands_src.exists():
        for cmd in commands_src.glob("*.md"):
            if cmd.name != "README.md":
                (config.claude_dir / "commands" / cmd.name).write_text(cmd.read_text())
        logger.success("Custom commands installed:")
        if not config.quiet:
            for cmd in (config.claude_dir / "commands").glob("*.md"):
                print(f"    @{cmd.stem}")

    # Create settings.json
    settings = {
        "defaultPermissionMode": "bypassPermissions",
        "autoApproveEdits": True,
        "editorMode": "normal",
        "autoUpdate": False,
        "outputStyle": "default",
        "defaultModel": "opus",
        "showResumeCommand": False,
    }

    settings_file = config.claude_dir / "settings.json"
    settings_file.write_text(json.dumps(settings, indent=2))
    os.chown(settings_file, config.runtime_uid, config.runtime_gid)

    # Ensure ~/.claude.json has required settings to skip onboarding prompts
    # We merge with any existing settings rather than overwriting
    user_state_file = config.user_home / ".claude.json"
    required_settings: dict[str, Any] = {
        "hasCompletedOnboarding": True,
        "autoUpdates": False,
        "bypassPermissionsModeAccepted": True,
    }
    # These are only set on new files, not forced on existing ones
    default_settings: dict[str, Any] = {
        "lastOnboardingVersion": "2.0.69",
        "numStartups": 1,
        "installMethod": "api_key",
    }

    # Read existing config if present
    file_existed = user_state_file.exists()
    existing_config: dict[str, Any] = {}
    if file_existed:
        try:
            existing_config = json.loads(user_state_file.read_text())
        except json.JSONDecodeError as e:
            logger.warn(f"~/.claude.json contains invalid JSON (line {e.lineno}, col {e.colno})")
            logger.warn("  File will be recreated with default settings")
            logger.warn("  This can cause Claude Code to prompt for config reset")
            existing_config = {}
        except OSError as e:
            logger.warn(f"Could not read ~/.claude.json: {e}")
            existing_config = {}

    # Check if required settings need updating
    needs_update = False
    for key, value in required_settings.items():
        if existing_config.get(key) != value:
            needs_update = True
            existing_config[key] = value

    # Add defaults only for missing keys
    for key, value in default_settings.items():
        if key not in existing_config:
            needs_update = True
            existing_config[key] = value

    # Write back if changes needed (using atomic write to prevent corruption)
    if needs_update:
        # Write to temp file first, then atomically replace
        # This prevents partial writes if the process is interrupted
        fd, temp_path = tempfile.mkstemp(
            dir=str(user_state_file.parent),
            prefix=".claude.json.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(existing_config, f, indent=2)
            os.chown(temp_path, config.runtime_uid, config.runtime_gid)
            os.chmod(temp_path, 0o600)
            try:
                os.replace(temp_path, user_state_file)  # Atomic on POSIX
            except OSError as e:
                if e.errno == errno.EBUSY:
                    # File is bind-mounted from host - can't atomically replace
                    # Fall back to direct write (still safe: we validated JSON above)
                    logger.warn("~/.claude.json is bind-mounted, using direct write")
                    shutil.copy2(temp_path, user_state_file)
                    os.unlink(temp_path)
                else:
                    raise
        except Exception:
            # Clean up temp file on failure
            with contextlib.suppress(OSError):
                os.unlink(temp_path)
            raise
        user_state_status = "created" if not file_existed else "updated"
    else:
        user_state_status = "unchanged"

    # Fix ownership
    chown_recursive(config.claude_dir, config.runtime_uid, config.runtime_gid)
    chown_recursive(
        config.user_home / ".config/claude-code", config.runtime_uid, config.runtime_gid
    )
    config.claude_dir.chmod(0o700)

    logger.success(f"Claude settings created: {settings_file}")
    logger.success(f"Claude user state {user_state_status}: {user_state_file}")
    if not config.quiet:
        print(json.dumps(settings, indent=2))
        print()


def setup_bashrc(config: Config, logger: Logger) -> None:
    """Set up .bashrc with aliases."""
    bashrc = config.user_home / ".bashrc"

    # Append our settings
    with open(bashrc, "a") as f:
        f.write("\n# Added by egg entrypoint\n")
        f.write("alias claude='claude --dangerously-skip-permissions'\n")
        f.write(
            r"export PS1='\[\033[01;32m\]\u@sandboxed\[\033[00m\]:\[\033[01;34m\]\w\[\033[00m\]\$ '"
            + "\n"
        )

    os.chown(bashrc, config.runtime_uid, config.runtime_gid)
    logger.success("Claude alias created (bypasses permissions in sandbox)")


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

    gateway_url = os.environ.get("GATEWAY_URL", f"http://egg-gateway:{GATEWAY_PORT}")
    proxy_url = os.environ.get("HTTPS_PROXY")

    # Parse gateway hostname from URL (supports dynamic names in GHA)
    from urllib.parse import urlparse

    parsed = urlparse(gateway_url)
    gateway_host = parsed.hostname or "egg-gateway"

    # Detect network mode from EGG_PRIVATE_MODE env var (set by orchestrator/gateway)
    # Fallback: if EGG_PRIVATE_MODE is not set, assume private when proxy is configured
    private_mode_env = os.environ.get("EGG_PRIVATE_MODE", "").lower()
    if private_mode_env in ("true", "1"):
        is_private_mode = True
    elif private_mode_env in ("false", "0"):
        is_private_mode = False
    else:
        # Legacy fallback: infer from proxy presence
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
    try:
        resolved_ip = socket.gethostbyname(gateway_host)
        logger.info(f"  {gateway_host} resolves to: {resolved_ip}")
    except socket.gaierror as e:
        logger.error(f"  DNS resolution failed for {gateway_host}: {e}")
        logger.error("  Check --add-host configuration in container startup")

    # Show /etc/hosts entry for gateway
    try:
        with open("/etc/hosts") as f:
            hosts_content = f.read()
            for line in hosts_content.splitlines():
                if gateway_host in line:
                    logger.info(f"  /etc/hosts entry: {line.strip()}")
                    break
            else:
                logger.warn(f"  No /etc/hosts entry found for {gateway_host}")
    except Exception as e:
        logger.warn(f"  Could not read /etc/hosts: {e}")

    # Show network interfaces and verify container is on expected subnet
    # Private mode: egg-isolated (172.32.0.x), Public mode: egg-external (172.33.0.x)
    expected_subnet = "172.32.0." if is_private_mode else "172.33.0."
    network_name = "egg-isolated" if is_private_mode else "egg-external"
    found_expected_subnet = False
    container_ip = None

    try:
        result = subprocess.run(
            ["ip", "addr", "show"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            lines = result.stdout.strip().split("\n")
            logger.info("  Network interfaces:")
            for line in lines:
                if "inet " in line and "127.0.0.1" not in line:
                    logger.info(f"    {line.strip()}")
                    if expected_subnet in line:
                        found_expected_subnet = True
                        # Extract IP address from line like "inet 172.32.0.5/24 ..."
                        parts = line.strip().split()
                        for i, part in enumerate(parts):
                            if part == "inet" and i + 1 < len(parts):
                                container_ip = parts[i + 1].split("/")[0]
                                break

            if found_expected_subnet:
                logger.info(f"  ✓ Container on {network_name} network ({container_ip})")
            else:
                logger.warn(f"  ✗ Not on {network_name} subnet ({expected_subnet}x)!")
                logger.warn("    Container may not be on the correct network")
    except Exception as e:
        logger.warn(f"  Could not get network interfaces: {e}")

    # Test basic TCP connectivity to gateway ports
    logger.info("Testing TCP connectivity to gateway...")

    def test_tcp_port(host: str, port: int, timeout: float = 2.0) -> tuple[bool, str]:
        """Test TCP connectivity to a host:port."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((host, port))
            sock.close()
            if result == 0:
                return True, "connected"
            else:
                return False, f"connection refused (errno {result})"
        except TimeoutError:
            return False, "timeout"
        except socket.gaierror as e:
            return False, f"DNS error: {e}"
        except Exception as e:
            return False, f"error: {e}"

    api_port = parsed.port or GATEWAY_PORT
    proxy_port = GATEWAY_PROXY_PORT

    api_tcp_ok, api_tcp_msg = test_tcp_port(gateway_host, api_port)
    proxy_tcp_ok, proxy_tcp_msg = test_tcp_port(gateway_host, proxy_port)

    logger.info(
        f"  TCP {gateway_host}:{api_port} (API): {'✓' if api_tcp_ok else '✗'} {api_tcp_msg}"
    )
    logger.info(
        f"  TCP {gateway_host}:{proxy_port} (Proxy): {'✓' if proxy_tcp_ok else '✗'} {proxy_tcp_msg}"
    )

    if not api_tcp_ok and not proxy_tcp_ok:
        logger.error("  Cannot reach gateway on either port!")
        logger.error("  This indicates a network configuration issue.")
        logger.error("  Verify egg container and egg-gateway are on the same network.")

    logger.info("Waiting for gateway readiness...")

    # Timeout is configurable via EGG_GATEWAY_TIMEOUT for faster test feedback
    # Default 60s for production, but tests can set lower values
    timeout_str = os.environ.get("EGG_GATEWAY_TIMEOUT", "60")
    try:
        timeout = int(timeout_str)
    except ValueError:
        logger.warn(f"Invalid EGG_GATEWAY_TIMEOUT '{timeout_str}', using default 60s")
        timeout = 60
    interval = 2  # seconds
    elapsed = 0

    # Track which checks have passed for final diagnostic
    api_health_passed = False
    api_health_error = None
    proxy_check_passed = False
    proxy_check_error = None
    tcp_api_ok = api_tcp_ok
    tcp_proxy_ok = proxy_tcp_ok

    while elapsed < timeout:
        # Check 1: Gateway API health endpoint
        try:
            health_url = f"{gateway_url}/api/v1/health"
            health_response = requests.get(
                health_url,
                timeout=5,
                proxies={"http": "", "https": ""},
            )
            if health_response.status_code == 200:
                # Parse health response to check actual status
                try:
                    health_data = health_response.json()
                    health_status = health_data.get("status", "unknown")
                    github_token_valid = health_data.get("github_token_valid", False)
                    auth_configured = health_data.get("auth_configured", False)

                    if not api_health_passed:
                        logger.success(
                            f"  Gateway API responding (HTTP {health_response.status_code})"
                        )
                        logger.info(f"    Status: {health_status}")
                        logger.info(f"    GitHub token valid: {github_token_valid}")
                        logger.info(f"    Auth configured: {auth_configured}")

                    if health_status == "healthy":
                        api_health_passed = True
                    else:
                        # Gateway is responding but not fully healthy
                        api_health_error = f"Status: {health_status} (github_token={github_token_valid}, auth={auth_configured})"
                        if not api_health_passed and not config.quiet:
                            logger.warn(f"  Gateway degraded: {api_health_error}")
                        # Still proceed to proxy check - degraded might still work
                        api_health_passed = True
                except (ValueError, KeyError) as e:
                    # Could not parse JSON response
                    api_health_error = f"Invalid JSON response: {e}"
                    if not config.quiet:
                        logger.warn(
                            f"  Gateway API returned non-JSON: {health_response.text[:100]}"
                        )
                    api_health_passed = True  # Proceed anyway - API is responding
            else:
                api_health_error = (
                    f"HTTP {health_response.status_code}: {health_response.text[:100]}"
                )
                if not config.quiet:
                    logger.info(f"  Gateway API returned: {api_health_error}")

        except RequestException as e:
            api_health_error = f"{type(e).__name__}: {e}"
            if not config.quiet and elapsed % 10 == 0:  # Log every 10 seconds
                logger.info(f"  Gateway API check failed: {api_health_error}")

        # Check 2: Proxy connectivity (only in private mode, only if API is healthy)
        # In public mode, the container has direct internet access and doesn't use the proxy
        if api_health_passed:
            if not is_private_mode:
                # Public mode: no proxy check needed, gateway API is sufficient
                logger.success("Gateway ready! (public mode - direct internet access)")
                return True

            # Private mode: verify proxy connectivity to Anthropic API
            try:
                if proxy_url is None:
                    raise RuntimeError("proxy_url must be set in private mode")
                proxies = {"http": proxy_url, "https": proxy_url}
                api_response = requests.get(
                    "https://api.anthropic.com/",
                    proxies=proxies,
                    timeout=10,
                    verify=True,
                )
                # Any HTTP response from Anthropic proves the proxy is working.
                # The root path may return 404 (no endpoint), 401 (auth required),
                # 403 (forbidden), or 200 - all indicate successful connectivity.
                if api_response.status_code in (200, 401, 403, 404):
                    logger.success(
                        f"  Proxy connectivity verified (Anthropic returned HTTP {api_response.status_code})"
                    )
                    logger.success("Gateway ready!")
                    return True
                else:
                    proxy_check_error = f"Unexpected HTTP {api_response.status_code}"
                    if not config.quiet:
                        logger.info(f"  Proxy check: {proxy_check_error}")

            except RequestException as e:
                proxy_check_error = f"{type(e).__name__}: {e}"
                if not config.quiet and elapsed % 10 == 0:
                    logger.info(f"  Proxy check failed: {proxy_check_error}")

        if not config.quiet and elapsed > 0 and elapsed % 10 == 0:
            logger.info(f"  Still waiting... ({elapsed}/{timeout}s)")

        time.sleep(interval)
        elapsed += interval

    # Final diagnostic output
    logger.error(f"Gateway not ready after {timeout} seconds")
    logger.error("")
    logger.error("Diagnostic summary:")
    logger.error(f"  TCP connectivity to {gateway_host}:")
    logger.error(
        f"    Port {api_port} (API): {'✓ connected' if tcp_api_ok else '✗ ' + api_tcp_msg}"
    )
    logger.error(
        f"    Port {proxy_port} (Proxy): {'✓ connected' if tcp_proxy_ok else '✗ ' + proxy_tcp_msg}"
    )
    logger.error(f"  Gateway API ({gateway_url}/api/v1/health):")
    if api_health_passed:
        logger.error("    ✓ Responding")
    else:
        logger.error(f"    ✗ Failed: {api_health_error}")
    if is_private_mode:
        logger.error(f"  Proxy ({proxy_url} → api.anthropic.com):")
        if proxy_check_passed:
            logger.error("    ✓ Working")
        else:
            logger.error(
                f"    ✗ Failed: {proxy_check_error or 'Not tested (API health check failed first)'}"
            )
    else:
        logger.error("  Proxy: (not used in public mode)")
    logger.error("")

    # Provide targeted troubleshooting based on what failed
    logger.error("Troubleshooting steps:")
    if not tcp_api_ok and not tcp_proxy_ok:
        logger.error("  [Network issue] Cannot reach gateway - check container networking:")
        logger.error("    1. Verify egg-gateway is running: docker ps | grep egg-gateway")
        network_to_check = "egg-isolated" if is_private_mode else "egg-external"
        logger.error(f"    2. Check both containers are on {network_to_check} network:")
        logger.error(f"       docker network inspect {network_to_check}")
        expected_ip = "172.32.0.2" if is_private_mode else "172.33.0.2"
        logger.error(f"    3. Verify gateway has IP {expected_ip} in {network_to_check} network")
        logger.error("    4. Check /etc/hosts has correct egg-gateway entry")
    elif not api_health_passed:
        logger.error("  [API issue] TCP works but HTTP fails - gateway may be starting:")
        logger.error("    1. Check gateway logs: docker logs egg-gateway")
        logger.error(f"    2. Test from host: curl http://localhost:{GATEWAY_PORT}/api/v1/health")
        logger.error("    3. Verify gateway.py is running in container")
    elif is_private_mode:
        logger.error("  [Proxy issue] Gateway API works but proxy check failed:")
        logger.error("    1. Check Squid is running: docker exec egg-gateway squid -k check")
        logger.error(
            "    2. Check Squid logs: docker exec egg-gateway cat /var/log/squid/cache.log"
        )
        logger.error("    3. Test proxy from host:")
        logger.error(
            f"       curl -x http://localhost:{GATEWAY_PROXY_PORT} https://api.anthropic.com/"
        )
        logger.error("    4. Verify allowed_domains.txt includes api.anthropic.com")
    return False


# =============================================================================
# Cleanup
# =============================================================================


def signal_orchestrator_completion(
    config: Config,
    logger: Logger,
    exit_code: int = 0,
    error_message: str | None = None,
) -> None:
    """Signal completion to orchestrator if running in orchestrator mode.

    Uses the OrchestratorClient from egg_orchestrator package for consistency
    with other orchestrator communication.

    Args:
        config: Container configuration
        logger: Logger instance
        exit_code: Process exit code (0 = success)
        error_message: Optional error message if failed
    """
    if not config.is_orchestrator_mode:
        return

    if not config.orchestrator_url or not config.pipeline_id:
        logger.warn("Orchestrator mode enabled but missing URL or pipeline_id")
        return

    if not config.agent_role:
        logger.warn("Orchestrator mode enabled but missing agent_role")
        return

    try:
        from egg_orchestrator import OrchestratorClient

        client = OrchestratorClient(orchestrator_url=config.orchestrator_url)

        if exit_code == 0:
            # Success - signal completion
            response = client.signal_complete(
                pipeline_id=config.pipeline_id,
                agent_role=config.agent_role,
            )
            signal_type = "complete"
        else:
            # Failure - signal error with stderr context for debugging
            error_msg = error_message or f"Container exited with code {exit_code}"
            stderr_tail = _read_subprocess_stderr_tail(20)
            if stderr_tail:
                error_msg += f"\n--- subprocess stderr (last 20 lines) ---\n{stderr_tail}"
            response = client.signal_error(
                pipeline_id=config.pipeline_id,
                agent_role=config.agent_role,
                error=error_msg,
                recoverable=False,
            )
            signal_type = "error"

        if response.success:
            logger.info(f"Signaled {signal_type} to orchestrator")
        else:
            logger.warn(f"Orchestrator signal failed: {response.message}")

    except Exception as e:
        # Don't fail the exit process if signaling fails
        logger.warn(f"Failed to signal orchestrator: {e}")


def cleanup_on_exit(config: Config, logger: Logger, exit_code: int = 0) -> None:
    """Cleanup handler for container shutdown.

    In the gateway-managed worktree architecture, the container doesn't
    have access to git metadata, so there's minimal cleanup needed.
    The gateway handles worktree cleanup when containers exit.

    If running in orchestrator mode, signals completion/error to orchestrator.
    """
    # Signal completion to orchestrator if in orchestrator mode
    signal_orchestrator_completion(config, logger, exit_code)

    if not config.quiet:
        print("")
        print("Cleaning up on container exit...")
        print("✓ Cleanup complete")


# =============================================================================
# Main Entry Points
# =============================================================================


def _tee_stderr_to_file(
    process: subprocess.Popen[bytes],
    log_path: Path,
    max_lines: int = 500,
) -> None:
    """Tee subprocess stderr to both sys.stderr and a bounded log file.

    Runs in a background thread. Reads from process.stderr (PIPE) and
    writes each line to the container's stderr in real time.  Only the
    last *max_lines* lines are kept in memory and flushed to *log_path*
    when the stream ends, preventing unbounded file growth.
    """
    from collections import deque

    try:
        stderr_out: Any = getattr(sys.stderr, "buffer", sys.stderr)
        ring: deque[bytes] = deque(maxlen=max_lines)
        if process.stderr is None:
            return
        while True:
            line = process.stderr.readline()
            if not line:
                break
            stderr_out.write(line)
            stderr_out.flush()
            ring.append(line)
        # Write the bounded tail to disk for _read_subprocess_stderr_tail()
        # This is best-effort — stderr was already forwarded in real time.
        # During container shutdown, filesystems may become read-only.
        try:
            with open(log_path, "wb") as log_file:
                for saved_line in ring:
                    log_file.write(saved_line)
        except OSError:
            pass
    except Exception as exc:
        # Best-effort diagnostic — log so failures aren't completely silent.
        try:
            print(
                f"[DEBUG] _tee_stderr_to_file failed: {exc}",
                file=sys.stderr,
            )
            sys.stderr.flush()
        except Exception:
            pass


def _run_with_stderr_capture(
    cmd: list[str],
    env: dict[str, str],
    logger: Logger,
) -> int:
    """Run a subprocess, capturing stderr to a log file while passing it through.

    Returns the subprocess exit code. Stderr is tee'd to both the container's
    stderr (for docker logs) and _SUBPROCESS_STDERR_LOG (for error signals).
    """
    process = subprocess.Popen(
        cmd,
        env=env,
        stdin=sys.stdin,
        stdout=sys.stdout,
        stderr=subprocess.PIPE,
    )

    tee_thread = threading.Thread(
        target=_tee_stderr_to_file,
        args=(process, _SUBPROCESS_STDERR_LOG),
        daemon=True,
    )
    tee_thread.start()
    process.wait()
    tee_thread.join(timeout=5)

    exit_code = process.returncode

    if exit_code != 0:
        # Log error with subprocess stderr context so it's visible in docker logs
        stderr_tail = _read_subprocess_stderr_tail(30)
        if stderr_tail:
            logger.error(f"Subprocess failed (exit code {exit_code}). Last stderr:\n{stderr_tail}")
        else:
            logger.error(f"Subprocess failed (exit code {exit_code}) with no stderr output")

    return exit_code


def _read_subprocess_stderr_tail(max_lines: int = 20) -> str:
    """Read the last N lines from the subprocess stderr log, if it exists."""
    try:
        if _SUBPROCESS_STDERR_LOG.exists():
            content = _SUBPROCESS_STDERR_LOG.read_text(errors="replace").strip()
            if content:
                lines = content.splitlines()[-max_lines:]
                return "\n".join(lines)
    except Exception:
        pass
    return ""


def run_interactive(config: Config, logger: Logger) -> int:
    """Launch interactive Claude Code session.

    Uses subprocess.Popen() to maintain control after process exits,
    enabling completion signaling back to orchestrator.

    Returns:
        Exit code from the subprocess
    """

    # Change to repos directory
    if config.repos_dir.exists():
        os.chdir(config.repos_dir)
    else:
        os.chdir(config.user_home)

    # Build environment for Claude Code
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": "/opt/egg-runtime/sandbox:/opt/egg-runtime/shared",
            "DISABLE_TELEMETRY": os.environ.get("DISABLE_TELEMETRY", ""),
            "DISABLE_COST_WARNINGS": os.environ.get("DISABLE_COST_WARNINGS", ""),
        }
    )

    # Remove proxy vars for Claude Code - it only talks to ANTHROPIC_BASE_URL (gateway)
    # Node.js HTTP clients don't respect NO_PROXY, so we must unset the proxy entirely
    # Other tools in the container (bash, curl) will still use the proxy from shell env
    for proxy_var in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
        env.pop(proxy_var, None)

    # Remove launcher secret from Claude's environment — it's a privileged
    # credential used only by the entrypoint (root) for orchestrator auth.
    # Leaving it accessible would let Claude bypass SDLC token gating.
    env.pop("EGG_LAUNCHER_SECRET", None)

    logger.info("Launching Claude Code interactive mode...")

    # Print timing summary right before launching LLM
    _startup_timer.print_summary()

    # Launch via gosu, capturing stderr to log file for error reporting
    # This allows us to include stderr context in orchestrator error signals
    return _run_with_stderr_capture(
        [
            "gosu",
            f"{config.runtime_uid}:{config.runtime_gid}",
            "python3",
            "-c",
            "from llm import run_interactive; run_interactive()",
        ],
        env=env,
        logger=logger,
    )


def run_exec(config: Config, logger: Logger, args: list[str]) -> int:
    """Run a command in exec mode.

    Uses subprocess.Popen() to maintain control after process exits,
    enabling completion signaling back to orchestrator.

    Returns:
        Exit code from the subprocess
    """
    # Change to repos directory (same as interactive mode) so that tools
    # like `gh repo view` can auto-detect the repository context.
    if config.repos_dir.exists():
        os.chdir(config.repos_dir)
    else:
        os.chdir(config.user_home)

    env = os.environ.copy()
    # Remove launcher secret — privileged credential not for Claude's use
    env.pop("EGG_LAUNCHER_SECRET", None)

    # Print timing summary before exec
    _startup_timer.print_summary()

    # Launch via gosu, capturing stderr to log file for error reporting
    return _run_with_stderr_capture(
        ["gosu", f"{config.runtime_uid}:{config.runtime_gid}"] + args,
        env=env,
        logger=logger,
    )


# =============================================================================
# Main
# =============================================================================


def main() -> None:
    """Main entry point."""
    config = Config()
    logger = Logger(config.quiet, config.debug)

    if config.debug:
        logger.phase_start("entrypoint_init")

    # Log orchestrator mode if enabled
    if config.is_orchestrator_mode:
        logger.info(
            f"Running in orchestrator mode: {config.orchestrator_mode}, "
            f"pipeline={config.pipeline_id}, role={config.agent_role}"
        )

    # Track subprocess completion state for signal handling
    # If SIGTERM arrives before subprocess completes, we signal interrupted (128+signum)
    # If it arrives after, the subprocess already signaled its exit code
    subprocess_completed = [False]  # Use list to allow modification from nested function

    # Register cleanup handler
    def signal_handler(signum: int, frame: Any) -> None:
        # If subprocess hasn't completed, this is an interruption - use signal-based exit code
        # SIGTERM = 128+15=143, SIGINT = 128+2=130
        if not subprocess_completed[0]:
            cleanup_on_exit(config, logger, exit_code=128 + signum)
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Run setup with timing instrumentation
    # Debug logging goes to stderr for capture even on container hang
    with timed_phase("setup_user", logger):
        setup_user(config, logger)

    with timed_phase("setup_repo_permissions", logger):
        setup_repo_permissions(config, logger)

    with timed_phase("setup_environment", logger):
        setup_environment(config)

    with timed_phase("setup_egg_symlink", logger):
        setup_egg_symlink(config, logger)

    with timed_phase("setup_git", logger):
        setup_git(config, logger)

    with timed_phase("setup_gateway_ca", logger):
        setup_gateway_ca(config, logger)

    with timed_phase("setup_worktrees", logger):
        if not setup_worktrees(config, logger):
            logger.error("")
            logger.error("Container startup aborted due to worktree configuration failure.")
            logger.error("Please check your egg setup and try again.")
            sys.exit(1)

    with timed_phase("setup_agent_rules", logger):
        setup_agent_rules(config, logger)

    with timed_phase("setup_claude", logger):
        setup_claude(config, logger)

    with timed_phase("setup_bashrc", logger):
        setup_bashrc(config, logger)

    # Wait for gateway readiness (network lockdown mode)
    with timed_phase("check_gateway", logger):
        if not check_gateway_health(config, logger):
            logger.error("")
            logger.error("Container startup aborted: gateway not ready.")
            logger.error("Ensure the gateway sidecar is running.")
            sys.exit(1)

    # Configure Anthropic API to route through gateway
    with timed_phase("setup_anthropic_api", logger):
        setup_anthropic_api(config, logger)

    # Remove launcher secret from process environment before launching Claude.
    os.environ.pop("EGG_LAUNCHER_SECRET", None)

    # Run appropriate mode (timing summary is printed inside each mode)
    if len(sys.argv) == 1:
        exit_code = run_interactive(config, logger)
    else:
        exit_code = run_exec(config, logger, sys.argv[1:])

    # Mark subprocess as completed - signal handler should not override exit code now
    subprocess_completed[0] = True

    # Signal completion to orchestrator (if in orchestrator mode)
    # This runs after subprocess exits, thanks to subprocess.run() instead of os.execvpe()
    cleanup_on_exit(config, logger, exit_code)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
