#!/usr/bin/env python3
"""
Egg Sandbox Container Entrypoint

Sets up the sandboxed container environment for the autonomous AI agent.
Handles user setup, git configuration, service initialization, and launches
the appropriate LLM interface.
"""

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

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

    # Derived paths - fixed home directory for egg user
    @property
    def user_home(self) -> Path:
        return Path("/home/egg")

    @property
    def repos_dir(self) -> Path:
        """The directory containing mounted repositories."""
        return self.user_home / "repos"

    @property
    def sharing_dir(self) -> Path:
        return self.user_home / "sharing"

    @property
    def claude_dir(self) -> Path:
        return self.user_home / ".claude"


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
        chown_recursive(config.user_home, config.runtime_uid, config.runtime_gid)


def setup_environment(config: Config) -> None:
    """Set up environment variables."""
    os.environ["HOME"] = str(config.user_home)
    os.environ["USER"] = config.container_user

    # Add user's local bin (Claude Code native install) and egg runtime scripts to PATH
    current_path = os.environ.get("PATH", "")
    local_bin = config.user_home / ".local" / "bin"
    os.environ[
        "PATH"
    ] = f"{local_bin}:/opt/egg-runtime/scripts:/opt/egg-runtime/tools:/usr/local/bin:{current_path}"

    # Python settings
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PYTHONUNBUFFERED"] = "1"

    # Claude settings
    os.environ["DISABLE_AUTOUPDATER"] = "1"

    # Git editor - use 'true' (no-op) for non-interactive environment
    # This allows git rebase --continue to work without an interactive editor.
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
                "/opt/egg-runtime/scripts/git-credential-github-token",
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
    gateway HTTP endpoint, this CA trust is no longer required for
    Anthropic API traffic. The Squid proxy now only does peek/splice (SNI
    inspection without MITM), so clients validate origin server certificates
    directly.

    This function is kept for potential future HTTPS interception needs.
    """
    gateway_ca_src = Path("/shared/certs/gateway-ca.crt")
    gateway_ca_dst = Path("/usr/local/share/ca-certificates/gateway-ca.crt")

    if not gateway_ca_src.exists():
        # With ANTHROPIC_BASE_URL, missing CA is not a critical error
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
        logger.info(f"Gateway CA not added to trust store: {result.stderr}")

    # Configure Python and Node.js to use system CA bundle
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
    """
    gateway_url = "http://egg-gateway:9847"

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
    tmp_link.symlink_to(config.sharing_dir / "tmp")

    # Ensure subdirectories exist
    subdirs = ["tmp", "notifications", "context", "logs"]
    for subdir in subdirs:
        (config.sharing_dir / subdir).mkdir(parents=True, exist_ok=True)

    chown_recursive(config.sharing_dir, config.runtime_uid, config.runtime_gid)

    logger.success("Shared directories configured:")
    logger.info("  ~/sharing/tmp/           (temporary files)")
    logger.info("  ~/sharing/notifications/ (async notifications)")
    logger.info("  ~/sharing/context/       (context storage)")
    logger.info("  ~/sharing/logs/          (logs)")
    logger.info("  Convenience symlink: ~/tmp -> ~/sharing/tmp")


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

    # Create settings.json
    settings = {
        "alwaysThinkingEnabled": True,
        "defaultPermissionMode": "bypassPermissions",
        "autoApproveEdits": True,
        "editorMode": "normal",
        "autoUpdate": False,
        "outputStyle": "default",
        "defaultModel": "opus",
    }

    settings_file = config.claude_dir / "settings.json"
    settings_file.write_text(json.dumps(settings, indent=2))
    os.chown(settings_file, config.runtime_uid, config.runtime_gid)

    # Ensure ~/.claude.json has required settings to skip onboarding prompts
    user_state_file = config.user_home / ".claude.json"
    required_settings = {
        "hasCompletedOnboarding": True,
        "autoUpdates": False,
        "bypassPermissionsModeAccepted": True,
    }
    default_settings = {
        "lastOnboardingVersion": "2.0.69",
        "numStartups": 1,
        "installMethod": "api_key",
    }

    # Read existing config if present
    file_existed = user_state_file.exists()
    existing_config = {}
    if file_existed:
        try:
            existing_config = json.loads(user_state_file.read_text())
        except json.JSONDecodeError:
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

    # Write back if changes needed
    if needs_update:
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
                os.replace(temp_path, user_state_file)
            except OSError:
                shutil.copy2(temp_path, user_state_file)
                os.unlink(temp_path)
        except Exception:
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

    Returns:
        True if gateway is ready, False on timeout
    """
    import socket
    import time

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

    while elapsed < timeout:
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

                    if health_status == "healthy":
                        logger.success("Gateway ready!")
                        return True
                except (ValueError, KeyError):
                    pass

        except RequestException:
            pass

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
    """Launch interactive Claude Code session."""
    logger.info("")
    logger.info("Starting interactive mode...")
    logger.info("")

    # Change to repos directory
    if config.repos_dir.exists():
        os.chdir(config.repos_dir)
    else:
        os.chdir(config.user_home)

    # Build environment for Claude Code
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": "/opt/egg-runtime/shared",
            "DISABLE_TELEMETRY": os.environ.get("DISABLE_TELEMETRY", ""),
            "DISABLE_COST_WARNINGS": os.environ.get("DISABLE_COST_WARNINGS", ""),
        }
    )

    # Remove proxy vars for Claude Code - it only talks to ANTHROPIC_BASE_URL (gateway)
    for proxy_var in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
        env.pop(proxy_var, None)

    logger.info("Launching Claude Code interactive mode...")

    # Launch via gosu
    os.execvpe(
        "gosu",
        [
            "gosu",
            f"{config.runtime_uid}:{config.runtime_gid}",
            "claude",
            "--dangerously-skip-permissions",
        ],
        env,
    )


def run_exec(config: Config, logger: Logger, args: list[str]) -> None:
    """Run a command in exec mode."""
    env = os.environ.copy()

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

    # Run setup
    setup_user(config, logger)
    setup_environment(config)
    setup_git(config, logger)
    setup_gateway_ca(config, logger)

    if not setup_worktrees(config, logger):
        logger.error("")
        logger.error("Container startup aborted due to worktree configuration failure.")
        sys.exit(1)

    setup_sharing(config, logger)
    setup_claude(config, logger)
    setup_bashrc(config, logger)

    # Wait for gateway readiness (network lockdown mode)
    if not check_gateway_health(config, logger):
        logger.error("")
        logger.error("Container startup aborted: gateway not ready.")
        logger.error("Ensure the gateway sidecar is running.")
        sys.exit(1)

    # Configure Anthropic API to route through gateway
    setup_anthropic_api(config, logger)

    # Run appropriate mode
    if len(sys.argv) == 1:
        run_interactive(config, logger)
    else:
        run_exec(config, logger, sys.argv[1:])


if __name__ == "__main__":
    main()
