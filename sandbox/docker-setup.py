#!/usr/bin/env python3
"""
Docker Development Environment Setup

Installs common development utilities in the Docker container.
For additional packages, configure extra_packages in ~/.config/egg/repositories.yaml.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


def run(cmd: list[str], check: bool = True, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """Run a command and return the result"""
    print(f"Running: {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, **kwargs)


def run_shell(cmd: str, check: bool = True, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """Run a shell command"""
    print(f"Running: {cmd}")
    return subprocess.run(cmd, shell=True, check=check, executable="/bin/bash", **kwargs)


def detect_distro() -> str:
    """Detect Linux distribution"""
    if Path("/etc/fedora-release").exists():
        return "fedora"
    elif Path("/etc/lsb-release").exists() or Path("/etc/debian_version").exists():
        return "ubuntu"
    return "unknown"


def get_arch() -> str:
    """Get system architecture"""
    return os.uname().machine  # x86_64, aarch64, etc


def load_config() -> dict[str, Any]:
    """Load repository configuration to get extra packages.

    Search order:
    1. EGG_REPO_CONFIG env var (explicit override)
    2. Host config: ~/.config/egg/repositories.yaml
    3. Container mount: ~/repos/egg/config/repositories.yaml

    Returns empty dict if no config found (uses defaults only).
    """
    # Check env var first
    env_path = os.environ.get("EGG_REPO_CONFIG")
    if env_path:
        env_config = Path(env_path)
        if env_config.exists():
            with env_config.open() as f:
                return yaml.safe_load(f) or {}

    # Check host config location (preferred)
    host_config = Path.home() / ".config" / "egg" / "repositories.yaml"
    if host_config.exists():
        with host_config.open() as f:
            return yaml.safe_load(f) or {}

    # Check container mount path
    container_config = Path.home() / "repos" / "egg" / "config" / "repositories.yaml"
    if container_config.exists():
        with container_config.open() as f:
            return yaml.safe_load(f) or {}

    # No config found - that's OK, just use defaults
    return {}


def get_extra_packages(config: dict[str, Any], distro: str) -> tuple[list[str], list[str]]:
    """
    Get extra packages from config.

    Returns:
        Tuple of (apt_packages, dnf_packages)
    """
    docker_setup = config.get("docker_setup", {})
    extra = docker_setup.get("extra_packages", {})

    # Get distro-specific packages
    apt_packages = extra.get("apt", [])
    dnf_packages = extra.get("dnf", [])

    # Also support generic "packages" that apply to both
    generic = extra.get("packages", [])
    apt_packages.extend(generic)
    dnf_packages.extend(generic)

    return apt_packages, dnf_packages


def install_core_packages(distro: str) -> None:
    """Install core development packages that most developers need."""
    print("\n=== Installing core development packages ===")

    if distro == "ubuntu":
        packages = [
            # Essential tools
            "git",
            "curl",
            "wget",
            "jq",
            "unzip",
            # Build tools
            "build-essential",
            "pkg-config",
            # Editors
            "vim",
            # Terminal utilities
            "lsof",
            "htop",
            "tree",
        ]
        run(["apt-get", "update", "-qq", "-y"])
        run(["apt-get", "install", "-y"] + packages)

    elif distro == "fedora":
        packages = [
            # Essential tools
            "git",
            "curl",
            "wget",
            "jq",
            "unzip",
            # Build tools
            "gcc",
            "gcc-c++",
            "make",
            "pkgconf",
            # Editors
            "vim",
            # Terminal utilities
            "lsof",
            "htop",
            "tree",
        ]
        run(["dnf", "install", "-y", "--skip-unavailable"] + packages)


def install_extra_packages(distro: str, apt_packages: list[str], dnf_packages: list[str]) -> None:
    """Install user-configured extra packages."""
    if distro == "ubuntu" and apt_packages:
        print(f"\n=== Installing extra packages: {', '.join(apt_packages)} ===")
        run(["apt-get", "install", "-y"] + apt_packages, check=False)

    elif distro == "fedora" and dnf_packages:
        print(f"\n=== Installing extra packages: {', '.join(dnf_packages)} ===")
        run(["dnf", "install", "-y", "--skip-unavailable"] + dnf_packages, check=False)


def get_build_commands(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract build_commands from all repo_settings entries.

    Returns:
        List of dicts with 'repo', 'watch_files', 'commands', and 'persist_dirs' keys.
        Only includes repos that have non-empty commands lists.
    """
    repo_settings = config.get("repo_settings", {})
    if not isinstance(repo_settings, dict):
        return []

    result = []
    for repo_name, settings in repo_settings.items():
        if not isinstance(settings, dict):
            continue
        build_cmds = settings.get("build_commands")
        if not isinstance(build_cmds, dict):
            continue
        commands = build_cmds.get("commands", [])
        if not isinstance(commands, list) or not commands:
            continue
        watch_files = build_cmds.get("watch_files", [])
        if not isinstance(watch_files, list):
            watch_files = []
        persist_dirs = build_cmds.get("persist_dirs", [])
        if not isinstance(persist_dirs, list):
            persist_dirs = []
        persist_system_dirs = build_cmds.get("persist_system_dirs", [])
        if not isinstance(persist_system_dirs, list):
            persist_system_dirs = []
        result.append(
            {
                "repo": repo_name,
                "watch_files": [str(f) for f in watch_files],
                "commands": [str(c) for c in commands],
                "persist_dirs": [str(d) for d in persist_dirs],
                "persist_system_dirs": [str(d) for d in persist_system_dirs],
            }
        )
    return result


def load_build_commands_manifest(
    manifest_path: str = "/tmp/repo-deps/manifest.json",
) -> list[dict[str, Any]]:
    """Load build commands from the manifest file written by create_dockerfile().

    During Docker builds, repositories.yaml is not available in the build context.
    Instead, the host-side create_dockerfile() writes a manifest.json into repo-deps/
    containing the build commands. This function reads that manifest.

    Supports two manifest formats:
    - New: {"extra_packages": {...}, "build_commands": [...]}
    - Old (legacy list): [{"repo": ..., "commands": [...], ...}]

    Args:
        manifest_path: Path to the manifest file (default: /tmp/repo-deps/manifest.json)

    Returns:
        List of dicts with 'repo', 'watch_files', 'commands', and 'persist_dirs' keys.
    """
    path = Path(manifest_path)
    if not path.exists():
        return []
    try:
        with path.open() as f:
            data = json.load(f)
        # New dict format: {"extra_packages": {...}, "build_commands": [...]}
        if isinstance(data, dict):
            raw_list = data.get("build_commands", [])
        elif isinstance(data, list):
            raw_list = data
        else:
            return []
        # Validate each entry has required fields
        result = []
        for entry in raw_list:
            if not isinstance(entry, dict):
                continue
            if "repo" not in entry or "commands" not in entry:
                continue
            commands = entry["commands"]
            if not isinstance(commands, list) or not commands:
                continue
            result.append(entry)
        return result
    except (json.JSONDecodeError, OSError):
        return []


def load_extra_packages_manifest(
    manifest_path: str = "/tmp/repo-deps/manifest.json",
) -> tuple[list[str], list[str]]:
    """Load extra_packages from the manifest file written by create_dockerfile().

    Returns:
        Tuple of (apt_packages, dnf_packages). Empty lists if not found.
    """
    path = Path(manifest_path)
    if not path.exists():
        return [], []
    try:
        with path.open() as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return [], []
        extra = data.get("extra_packages", {})
        if not isinstance(extra, dict):
            return [], []
        apt = extra.get("apt", [])
        dnf = extra.get("dnf", [])
        if not isinstance(apt, list):
            apt = []
        if not isinstance(dnf, list):
            dnf = []
        return [str(p) for p in apt], [str(p) for p in dnf]
    except (json.JSONDecodeError, OSError):
        return [], []


def run_build_commands(build_commands: list[dict[str, Any]]) -> None:
    """Execute build commands for each repo during Docker image build.

    Each repo's commands run in its watch files directory at /tmp/repo-deps/<repo-name>.
    Commands run as root (same as the rest of docker-setup.py).

    Args:
        build_commands: List of dicts from get_build_commands()
    """
    if not build_commands:
        return

    print("\n=== Running build commands ===")

    for entry in build_commands:
        repo = entry["repo"]
        commands = entry["commands"]
        # Sanitize repo name for directory path (owner/repo -> owner--repo)
        repo_dir_name = repo.replace("/", "--")
        work_dir = Path("/tmp/repo-deps") / repo_dir_name

        print(f"\n--- Build commands for {repo} ---")

        if not work_dir.exists():
            print(
                f"  Warning: Watch files directory {work_dir} does not exist, "
                f"running commands in /tmp"
            )
            work_dir = Path("/tmp")

        for cmd in commands:
            print(f"  Running: {cmd}")
            try:
                result = subprocess.run(
                    cmd,
                    shell=True,
                    executable="/bin/bash",
                    cwd=str(work_dir),
                    check=False,
                    capture_output=False,
                )
                if result.returncode != 0:
                    print(f"  Warning: Command exited with code {result.returncode}: {cmd}")
            except Exception as e:
                print(f"  Warning: Command failed: {cmd}: {e}")

    print("\n=== Build commands complete ===")

    persist_build_dirs(build_commands)


def persist_build_dirs(
    build_commands: list[dict[str, Any]],
    repo_deps_base: Path = Path("/tmp/repo-deps"),
    prebuilt_base: Path = Path("/opt/prebuilt-deps"),
) -> None:
    """Persist directories from build context into the Docker image.

    After build commands run, specified directories (e.g. node_modules) are
    copied to a persistent location so they survive the /tmp/repo-deps cleanup.
    They are restored into mounted repos at container startup by entrypoint.py.

    Args:
        build_commands: List of dicts with 'repo', 'commands', and 'persist_dirs' keys.
        repo_deps_base: Base path for repo build contexts (default: /tmp/repo-deps).
        prebuilt_base: Destination base for persisted directories (default: /opt/prebuilt-deps).
    """
    persist_count = 0
    for entry in build_commands:
        repo = entry["repo"]
        persist_dirs = entry.get("persist_dirs", [])
        if not persist_dirs:
            continue

        repo_dir_name = repo.replace("/", "--")
        work_dir = repo_deps_base / repo_dir_name
        dest_base = prebuilt_base / repo_dir_name

        for rel_dir in persist_dirs:
            src_dir = work_dir / rel_dir

            # Defense-in-depth: validate path stays within work_dir
            try:
                src_dir.resolve().relative_to(work_dir.resolve())
            except ValueError:
                print(f"  Warning: persist_dirs: {rel_dir} escapes build context, skipping")
                continue

            if not src_dir.is_dir():
                print(f"  Warning: persist_dirs: {rel_dir} does not exist after build, skipping")
                continue

            dest_dir = dest_base / rel_dir
            dest_dir.parent.mkdir(parents=True, exist_ok=True)
            print(f"  Persisting {repo}/{rel_dir} -> {dest_dir}")
            shutil.copytree(src_dir, dest_dir, symlinks=True)
            persist_count += 1

    # Persist system-level directories (absolute paths like /usr/local/go)
    for entry in build_commands:
        repo = entry["repo"]
        system_dirs = entry.get("persist_system_dirs", [])
        if not system_dirs:
            continue

        system_dest = prebuilt_base / "_system_"

        for abs_dir in system_dirs:
            abs_dir = str(abs_dir)
            if not abs_dir.startswith("/"):
                print(f"  Warning: persist_system_dirs: {abs_dir} is not absolute, skipping")
                continue

            src_dir = Path(abs_dir)
            if not src_dir.is_dir():
                print(
                    f"  Warning: persist_system_dirs: {abs_dir} does not exist "
                    f"after build ({repo}), skipping"
                )
                continue

            # Store under _system_/<abs_path> so it can be restored to the same location
            # Strip leading / for the destination path
            dest_dir = system_dest / abs_dir.lstrip("/")
            dest_dir.parent.mkdir(parents=True, exist_ok=True)
            print(f"  Persisting system dir {abs_dir} ({repo}) -> {dest_dir}")
            shutil.copytree(src_dir, dest_dir, symlinks=True)
            persist_count += 1

    if persist_count:
        print(f"\n=== Persisted {persist_count} directories ===")


def configure_system(distro: str) -> None:
    """Configure system settings"""
    print("\n=== Configuring system ===")

    # Increase inotify watchers for file watching tools
    print("Configuring inotify...")
    with open("/etc/sysctl.conf", "a") as f:
        f.write("\nfs.inotify.max_user_watches=524288\n")
    run(["sysctl", "-p"], check=False)


def main() -> None:
    """Main setup process"""
    if os.geteuid() != 0:
        print("This script must be run as root (for apt/dnf installs)")
        sys.exit(1)

    print("=" * 60)
    print("Docker Development Environment Setup")
    print("=" * 60)
    print()
    print("This script installs common development utilities.")
    print("Configure extra_packages in ~/.config/egg/repositories.yaml for more packages.")
    print()

    distro = detect_distro()
    arch = get_arch()

    print(f"Detected: {distro} on {arch}")
    print()

    if distro == "unknown":
        print("WARNING: Unknown distribution. This may not work correctly.")
        print("Supported: Ubuntu, Fedora")
        response = input("Continue anyway? (yes/no): ")
        if response.lower() != "yes":
            sys.exit(1)

    try:
        # Load config for extra packages
        config = load_config()
        apt_packages, dnf_packages = get_extra_packages(config, distro)

        # Fall back to manifest.json for extra_packages when repositories.yaml
        # is not available (e.g., during Docker builds)
        if not apt_packages and not dnf_packages:
            apt_packages, dnf_packages = load_extra_packages_manifest()

        # Install core packages
        install_core_packages(distro)

        # Install user-configured extra packages
        install_extra_packages(distro, apt_packages, dnf_packages)

        # Persist extra packages list for multi-stage Docker builds.
        # In a multi-stage build, apt packages installed here (Stage 1) don't
        # carry to the final image. Write the list so Stage 3 can reinstall them.
        prebuilt_dir = Path("/opt/prebuilt-deps")
        prebuilt_dir.mkdir(parents=True, exist_ok=True)
        if apt_packages:
            (prebuilt_dir / "extra-packages-apt.txt").write_text("\n".join(apt_packages) + "\n")
        if dnf_packages:
            (prebuilt_dir / "extra-packages-dnf.txt").write_text("\n".join(dnf_packages) + "\n")

        # System configuration
        configure_system(distro)

        # Run per-repo build commands (dependency installation)
        # Try config first, then fall back to manifest.json written by create_dockerfile()
        # (repositories.yaml is not available during Docker builds)
        build_commands = get_build_commands(config)
        if not build_commands:
            build_commands = load_build_commands_manifest()
        run_build_commands(build_commands)

        print()
        print("=" * 60)
        print("Setup complete!")
        print("=" * 60)
        print()
        print("Installed core utilities:")
        print("  ✓ git, curl, wget, jq, unzip")
        print("  ✓ Build tools (gcc, make, etc.)")
        print("  ✓ vim, htop, tree, lsof")

        if (distro == "ubuntu" and apt_packages) or (distro == "fedora" and dnf_packages):
            extra = apt_packages if distro == "ubuntu" else dnf_packages
            print("\nInstalled extra packages:")
            for pkg in extra:
                print(f"  ✓ {pkg}")

        if build_commands:
            print("\nRan build commands for:")
            for entry in build_commands:
                print(f"  ✓ {entry['repo']} ({len(entry['commands'])} commands)")

        print()
        print("To install additional packages, add to ~/.config/egg/repositories.yaml:")
        print("  docker_setup:")
        print("    extra_packages:")
        print("      apt:  # For Ubuntu/Debian")
        print("        - package-name")
        print("      dnf:  # For Fedora/RHEL")
        print("        - package-name")
        print()

    except Exception as e:
        print(f"\nError during setup: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
