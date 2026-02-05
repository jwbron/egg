#!/usr/bin/env python3
"""
Docker Development Environment Setup

Installs common development utilities in the Docker container.
For additional packages, configure extra_packages in ~/.config/egg/repositories.yaml.
"""

import os
import subprocess
import sys
from pathlib import Path

import yaml


def run(cmd: list[str], check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    """Run a command and return the result"""
    print(f"Running: {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, **kwargs)


def run_shell(cmd: str, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
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


def load_config() -> dict:
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


def get_extra_packages(config: dict, distro: str) -> tuple[list[str], list[str]]:
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


def configure_system(distro: str) -> None:
    """Configure system settings"""
    print("\n=== Configuring system ===")

    # Increase inotify watchers for file watching tools
    print("Configuring inotify...")
    with open("/etc/sysctl.conf", "a") as f:
        f.write("\nfs.inotify.max_user_watches=524288\n")
    run(["sysctl", "-p"], check=False)


def get_pip_packages(config: dict) -> list[str]:
    """Get pip packages to pre-install from config."""
    docker_setup = config.get("docker_setup", {})
    return docker_setup.get("pip", [])


def get_npm_packages(config: dict) -> list[str]:
    """Get npm packages to pre-install from config."""
    docker_setup = config.get("docker_setup", {})
    return docker_setup.get("npm", [])


def install_pip_packages(packages: list[str]) -> bool:
    """Install pip packages from config.

    Returns:
        True if all packages installed successfully, False otherwise.
    """
    if not packages:
        return True

    print(f"\n=== Pre-installing pip packages: {', '.join(packages)} ===")
    result = run(
        ["pip3", "install", "--no-cache-dir"] + packages,
        check=False,
    )
    if result.returncode != 0:
        print(f"WARNING: Some pip packages failed to install (exit code {result.returncode})")
        print("The container will still work, but some repo dependencies may be missing.")
        print("Check package names and version constraints in repositories.yaml docker_setup.pip")
        return False
    return True


def install_npm_packages(packages: list[str]) -> bool:
    """Install npm packages globally from config.

    Returns:
        True if all packages installed successfully, False otherwise.
    """
    if not packages:
        return True

    # Check if npm is available
    npm_check = run_shell("which npm", check=False, capture_output=True)
    if npm_check.returncode != 0:
        print("\n=== Skipping npm packages: npm not installed ===")
        print("To use npm packages, add nodejs to docker_setup.extra_packages first:")
        print("  docker_setup:")
        print("    extra_packages:")
        print("      apt:")
        print("        - nodejs")
        print("        - npm")
        return False

    print(f"\n=== Pre-installing npm packages: {', '.join(packages)} ===")
    result = run(
        ["npm", "install", "-g"] + packages,
        check=False,
    )
    if result.returncode != 0:
        print(f"WARNING: Some npm packages failed to install (exit code {result.returncode})")
        print("Check package names in repositories.yaml docker_setup.npm")
        return False
    return True


def install_dependencies(config_path: str | None = None) -> None:
    """Install user-configured pip and npm packages from repositories.yaml.

    This is called as a separate Docker build step (--install-deps flag)
    to install pip and npm packages specified in the config.
    Placed after base package installation for better Docker layer caching.
    System packages (apt/dnf) are handled by the main docker-setup.py run.

    Args:
        config_path: Explicit path to repositories.yaml. If None, uses default search.
    """
    print("=" * 60)
    print("Pre-installing user-configured dependencies")
    print("=" * 60)

    # Load config from explicit path or default search
    if config_path:
        config_file = Path(config_path)
        if config_file.exists():
            with config_file.open() as f:
                config = yaml.safe_load(f) or {}
        else:
            print(f"Config file not found: {config_path}")
            config = {}
    else:
        config = load_config()

    # Note: system packages (apt/dnf) are already installed by the main
    # docker-setup.py run earlier in the Dockerfile. This mode only handles
    # pip and npm packages to avoid redundant apt/dnf calls.

    # Install pip packages
    pip_packages = get_pip_packages(config)
    pip_ok = install_pip_packages(pip_packages)

    # Install npm packages
    npm_packages = get_npm_packages(config)
    npm_ok = install_npm_packages(npm_packages)

    # Summary
    print()
    print("=" * 60)
    print("Dependency pre-installation complete!")
    print("=" * 60)

    installed_any = False

    if pip_packages:
        status = "✓" if pip_ok else "⚠"
        label = "installed" if pip_ok else "had errors"
        print(f"\nPip packages ({label}):")
        for pkg in pip_packages:
            print(f"  {status} {pkg}")
        installed_any = True

    if npm_packages:
        status = "✓" if npm_ok else "⚠"
        label = "installed" if npm_ok else "had errors"
        print(f"\nNpm packages ({label}):")
        for pkg in npm_packages:
            print(f"  {status} {pkg}")
        installed_any = True

    if not installed_any:
        print("\nNo user-configured dependencies to install.")
        print("To pre-install dependencies, add to ~/.config/egg/repositories.yaml:")
        print("  docker_setup:")
        print("    pip:")
        print("      - package-name")
        print("    npm:")
        print("      - package-name")

    print()


def main():
    """Main setup process"""
    import argparse

    parser = argparse.ArgumentParser(description="Docker development environment setup")
    parser.add_argument(
        "--install-deps",
        action="store_true",
        help="Install user-configured pip and npm packages from config",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Explicit path to repositories.yaml config file",
    )
    args = parser.parse_args()

    if os.geteuid() != 0:
        print("This script must be run as root (for apt/dnf installs)")
        sys.exit(1)

    if args.install_deps:
        # Dependency-only mode: install pip/npm/system packages from config
        try:
            install_dependencies(args.config)
        except Exception as e:
            print(f"\nError during dependency installation: {e}", file=sys.stderr)
            import traceback

            traceback.print_exc()
            # Don't fail the build - missing deps are non-fatal
            print("Continuing despite errors (dependencies can be installed at runtime)")
        return

    # Default mode: install core system packages
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

        # Install core packages
        install_core_packages(distro)

        # Install user-configured extra packages
        install_extra_packages(distro, apt_packages, dnf_packages)

        # System configuration
        configure_system(distro)

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
