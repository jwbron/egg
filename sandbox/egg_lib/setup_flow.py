"""Interactive setup process for egg.

This module handles the setup flow, including checking host setup,
running the setup script, and adding standard mounts.
"""

import subprocess
import sys
from pathlib import Path

from .auth import get_anthropic_api_key
from .config import Colors, Config, get_local_repos
from .docker import (
    build_image,
    create_dockerfile,
    is_dangerous_dir,
)
from .output import error, info, success, warn


def get_setup_script_path() -> Path | None:
    """Find the setup.py script relative to the egg launcher location"""
    # Try to find setup.py relative to the egg script
    egg_script = Path(__file__).resolve().parent.parent

    # egg is at sandbox/egg, setup.py is at repo root
    repo_root = egg_script.parent
    setup_script = repo_root / "setup.py"

    if setup_script.exists():
        return setup_script

    # Fallback: check ~/repos/egg/setup.py
    fallback = Path.home() / "repos" / "egg" / "setup.py"
    if fallback.exists():
        return fallback

    return None


def run_setup_script() -> bool:
    """Run the setup.py script to configure egg"""
    setup_script = get_setup_script_path()

    if not setup_script:
        error("Could not find setup.py script")
        print()
        print("Please run setup manually:")
        print("  cd ~/repos/egg")
        print("  ./setup.py")
        return False

    info(f"Running setup: {setup_script}")
    print()

    try:
        # Run setup.py in its directory
        result = subprocess.run(
            [sys.executable, str(setup_script)], cwd=setup_script.parent, check=False
        )
        return result.returncode == 0
    except Exception as e:
        error(f"Failed to run setup.py: {e}")
        return False


def check_host_setup() -> bool:
    """Check if host setup is complete (services installed, directories exist)"""
    # Systemd services that should be installed (gateway can be containerized instead)
    slack_services = [
        "slack-notifier.service",
        "slack-receiver.service",
    ]

    # Important directories that should exist
    critical_dirs = [
        Path.home() / ".egg-sharing" / "notifications",
        Path.home() / ".egg-sharing" / "incoming",
        Path.home() / ".egg-sharing" / "responses",
    ]

    # Configuration file (consolidated location since PR #549)
    config_file = Path.home() / ".config" / "egg" / "config.yaml"

    issues_found = []

    # Check if Slack services are installed
    for service in slack_services:
        result = subprocess.run(
            ["systemctl", "--user", "list-unit-files", service],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 or service not in result.stdout:
            issues_found.append(f"Service not installed: {service}")

    # Check gateway setup: either systemd service OR containerized gateway (launcher secret file)
    # The containerized gateway is started on-demand by egg, so we just need the launcher secret
    launcher_secret = Config.USER_CONFIG_DIR / "launcher-secret"
    gateway_systemd_result = subprocess.run(
        ["systemctl", "--user", "list-unit-files", "gateway.service"],
        capture_output=True,
        text=True,
        check=False,
    )
    gateway_systemd_ok = (
        gateway_systemd_result.returncode == 0
        and "gateway.service" in gateway_systemd_result.stdout
    )
    gateway_container_ok = launcher_secret.exists()

    if not gateway_systemd_ok and not gateway_container_ok:
        issues_found.append("Gateway not configured: run host-services/gateway/setup.sh")

    # Check if critical directories exist
    for dir_path in critical_dirs:
        if not dir_path.exists():
            issues_found.append(f"Directory not found: {dir_path}")

    # Check if config exists (warning only, not critical)
    config_warning = None
    if not config_file.exists():
        config_warning = f"Configuration file not found: {config_file}"

    # If critical issues found, automatically run setup
    if issues_found:
        warn("Host setup appears incomplete")
        print()

        error("Critical issues found:")
        for issue in issues_found:
            print(f"  ✗ {issue}")
        print()

        if config_warning:
            warn(config_warning)
            print()

        print("Egg requires host services to be installed for full functionality:")
        print("  • Slack integration (notifier and receiver)")
        print("  • Gateway sidecar (git/gh policy enforcement)")
        print("  • Shared directories for notifications and task communication")
        print()

        # Auto-run setup.py when config is missing
        info("Running setup.py to configure egg...")
        print()
        if run_setup_script():
            success("Setup completed!")
            return True
        else:
            error("Setup failed")
            return False

    # Config warning only (not critical) - just warn and continue
    if config_warning:
        warn(config_warning)
        print()

    return True


def setup() -> bool:
    """Interactive setup process"""
    print()
    info("=== Autonomous Software Engineering Agent - Setup ===")
    print()
    print("🤖 AUTONOMOUS ENGINEERING AGENT")
    print()
    print("This sets up a sandboxed environment for Claude to work as an autonomous")
    print("software engineer with minimal supervision.")
    print()
    print("OPERATING MODEL:")
    print("  • Agent: Plans, implements, tests, documents, creates PRs")
    print("  • Human: Reviews, approves, deploys")
    print()
    print("AGENT CAPABILITIES:")
    print("  ✓ Edit code and create commits in ~/repos/")
    print("  ✓ Run tests, linters, development servers")
    print("  ✓ Access Confluence docs (ADRs, runbooks, best practices)")
    print("  ✓ Create pull requests with @create-pr command")
    print("  ✓ Build accumulated knowledge with @save-context")
    print("  ✓ Network access for Claude API and package installs")
    print()
    print("SECURITY ISOLATION:")
    print("  ✗ NO access to SSH keys (cannot git push)")
    print("  ✗ NO access to gcloud credentials (cannot deploy)")
    print("  ✗ NO access to GSM secrets")
    print()
    print("HOW IT WORKS:")
    print("  1. Your local repos are mounted into the container as ~/repos/")
    print("  2. Git worktrees isolate container changes from your working directory")
    print("  3. Agent works on code, creates commits in worktrees")
    print("  4. YOU review commits and push from host (with credentials)")
    print()
    print("FUTURE CAPABILITIES (Roadmap):")
    print("  🔄 GitHub PR context")
    print("  🔄 Slack message context")
    print("  🔄 JIRA ticket context")
    print("  🔄 Email thread context")
    print()

    response = input("Continue? (yes/no): ").strip().lower()
    if response != "yes":
        info("Setup cancelled")
        return False

    print()
    info("Setting up mounts...")
    print()

    Config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    mounts = []

    # Check for configured local repositories
    local_repos = get_local_repos()
    if local_repos:
        info(f"Found {len(local_repos)} configured local repository(ies):")
        for repo in local_repos:
            print(f"    • {repo}")
        print()
        print("    These will be mounted as ~/repos/<repo-name>/ with git worktrees")
        print("    for isolated development.")
    else:
        warn("No local repositories configured.")
        print("    Run ./setup.py to configure local repositories.")
        print("    These will be mounted as ~/repos/<repo-name>/")

    # Add context-sync directory (read-only) - includes Confluence, JIRA, and more
    print()
    context_sync_dir = Path.home() / "context-sync"
    if context_sync_dir.exists():
        context_container_path = "/home/egg/context-sync"
        mounts.append(f"{context_sync_dir}:{context_container_path}:ro")
        print(f"  ✓ Context sources: {context_sync_dir}")
        print("    Mounted as: ~/context-sync/ (read-only)")

        # Show available context sources
        subdirs = []
        if (context_sync_dir / "confluence").exists():
            subdirs.append("confluence (ADRs, runbooks, docs)")
        if (context_sync_dir / "jira").exists():
            subdirs.append("jira (tickets, issues)")
        if (context_sync_dir / "github").exists():
            subdirs.append("github (PRs, issues)")
        if (context_sync_dir / "slack").exists():
            subdirs.append("slack (messages)")

        if subdirs:
            print(f"    Contains: {', '.join(subdirs)}")
        else:
            print("    Note: No context subdirectories found yet")
    else:
        warn(f"Context sync directory not found: {context_sync_dir}")
        warn("Expected directory with confluence/, jira/, etc. subdirectories")

    # Create and mount persistent directories for agent
    print()
    info("Setting up persistent directories...")

    # Sharing directory - single location for ALL persistent data
    Config.SHARING_DIR.mkdir(parents=True, exist_ok=True)
    Config.TMP_DIR.mkdir(parents=True, exist_ok=True)  # tmp/ inside sharing/

    sharing_container_path = "/home/egg/sharing"
    mounts.append(f"{Config.SHARING_DIR}:{sharing_container_path}:rw")
    print(f"  ✓ Sharing: {Config.SHARING_DIR}")
    print("    Mounted as: ~/sharing/ (read-write)")
    print("    Purpose: All persistent data")
    print("    - ~/sharing/tmp/           Persistent workspace (also at ~/tmp)")
    print("    - ~/sharing/context/       Context documents (@save-context)")
    print("    - ~/sharing/notifications/ Notifications to human")
    print("    - ~/sharing/incoming/      Incoming tasks from Slack")
    print("    - ~/sharing/analysis/      Analysis reports")

    # Create convenience symlink in container for tmp
    # Note: Actual symlink creation happens in container entrypoint

    # Check Anthropic API key authentication
    print()
    print(f"{Colors.BOLD}Claude Code authentication...{Colors.NC}")

    api_key = get_anthropic_api_key()
    if api_key:
        success("Anthropic API key configured")
        print(f"  API key: {api_key[:12]}...{api_key[-4:]}")
    else:
        warn("Anthropic API key not configured")
        print("  Set via: export ANTHROPIC_API_KEY=sk-ant-...")
        print(f"  Or save to: {Config.USER_CONFIG_DIR / 'anthropic-api-key'}")
        print()
        info("Container will not be able to use Claude without an API key.")

    print()
    print("Add additional directories? (optional)")
    print("Format: /path/to/dir        (read-write)")
    print("    or: /path/to/dir:ro     (read-only)")
    print("Press Enter on empty line when done")
    print()

    # Collect additional directories
    while True:
        dir_input = input("Additional directory (or Enter to finish): ").strip()
        if not dir_input:
            break

        # Parse mode
        if ":ro" in dir_input or ":rw" in dir_input:
            mount_path_str, mode = dir_input.rsplit(":", 1)
            if mode not in ["ro", "rw"]:
                warn(f"Invalid mode '{mode}', use 'ro' or 'rw'")
                continue
        else:
            mount_path_str = dir_input
            mode = "rw"

        # Expand and validate path
        mount_path = Path(mount_path_str).expanduser().resolve()

        # Check if dangerous
        if is_dangerous_dir(mount_path):
            print(f"⛔ BLOCKED: {mount_path}")
            print("   This directory contains credentials and will not be mounted.")
            print("   This is intentional to prevent AI from accessing sensitive files.")
            continue

        if not mount_path.exists():
            warn(f"Directory does not exist: {mount_path}")
            create = input("Create it? (yes/no): ").strip().lower()
            if create == "yes":
                try:
                    mount_path.mkdir(parents=True, exist_ok=True)
                    success(f"Created: {mount_path}")
                except Exception as e:
                    error(f"Failed to create directory: {e}")
                    continue
            else:
                continue

        mounts.append(f"{mount_path}:{mode}")
        print(f"Added: {mount_path} ({mode})")

    print()
    info("Summary of mounted directories:")
    for mount in mounts:
        print(f"  • {mount}")
    print()

    proceed = input("Proceed with this configuration? (yes/no): ").strip().lower()
    if proceed != "yes":
        info("Setup cancelled")
        return False

    # Create Dockerfile and build image
    create_dockerfile()
    print()

    # Let Docker's cache handle what needs rebuilding
    info("Building Docker image (Docker will cache unchanged layers)...")
    if not build_image():
        return False

    print()
    success("Setup complete!")
    print()
    return True


def add_standard_mounts(mount_args: list[str], quiet: bool = False) -> None:
    """Add standard mounts (sharing, context-sync, shared-certs) to mount_args list.

    These mounts are always added dynamically rather than relying on config files,
    ensuring they're always available even if setup hasn't been run recently.
    """
    # Mount sharing directory (beads, notifications, incoming tasks, etc.)
    if Config.SHARING_DIR.exists():
        sharing_container_path = "/home/egg/sharing"
        mount_args.extend(["-v", f"{Config.SHARING_DIR}:{sharing_container_path}:rw"])
        if not quiet:
            print("  • ~/sharing/ (beads, notifications, tasks)")

    # Mount context-sync directory if it exists (Confluence, JIRA docs)
    context_sync_dir = Path.home() / "context-sync"
    if context_sync_dir.exists():
        context_sync_container = "/home/egg/context-sync"
        mount_args.extend(["-v", f"{context_sync_dir}:{context_sync_container}:ro"])
        if not quiet:
            print("  • ~/context-sync/ (Confluence, JIRA - read-only)")

    # Mount shared certs directory for SSL bump CA certificate
    # Gateway writes its CA cert here, container adds it to trust store
    # This enables credential injection via gateway proxy
    shared_certs_dir = Path.home() / ".egg-shared-certs"
    if shared_certs_dir.exists():
        mount_args.extend(["-v", f"{shared_certs_dir}:/shared/certs:ro"])
        if not quiet:
            print("  • /shared/certs/ (gateway CA cert - read-only)")
