"""Docker image and network management for egg.

Image builds are driven by ``make build`` (which calls
``scripts/prepare-sandbox-build-context.py`` to populate ``./repo-deps/``
from ``repositories.yaml`` before ``docker build``). This module owns the
shared host-side helpers: build-context population, network setup,
docker availability checks, and image presence checks.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from .config import (
    Config,
)
from .context import AUTO, get_context
from .output import error, info, success, warn


def check_docker_permissions() -> bool:
    """Check if user has permission to run Docker commands"""
    result = subprocess.run(["docker", "ps"], capture_output=True, text=True, check=False)

    if result.returncode == 0:
        return True

    if "permission denied" in result.stderr.lower():
        error("Docker permission denied - you are not in the docker group")
        print()
        print("This usually means one of two things:")
        print("  1. You just installed Docker and need to log out/in for group membership")
        print("  2. You need to be added to the docker group")
        print()
        print("Solutions:")
        print()
        print("Option 1: Add yourself to docker group and re-login")
        print("  sudo usermod -aG docker $USER")
        print("  then LOG OUT and LOG BACK IN")
        print()
        print("Option 2: Run with sudo (temporary workaround)")
        print("  sudo $(which egg)")
        print()
        return False

    return False


def check_docker() -> bool:
    """Check if Docker is installed and offer to install if not"""
    from .config import get_platform

    platform_name = get_platform()

    if subprocess.run(["which", "docker"], capture_output=True, check=False).returncode != 0:
        error("Docker is not installed.")

        if platform_name == "macos":
            info("On macOS, please install Docker Desktop from:")
            info("  https://www.docker.com/products/docker-desktop")
            return False

        # Linux installation
        response = input("Install Docker now? (yes/no): ").strip().lower()
        if response == "yes":
            info("Installing Docker...")
            try:
                # Download installer
                subprocess.run(
                    ["curl", "-fsSL", "https://get.docker.com", "-o", "/tmp/get-docker.sh"],
                    check=True,
                )
                # Run installer
                subprocess.run(["sudo", "sh", "/tmp/get-docker.sh"], check=True)
                # Add user to docker group
                subprocess.run(["sudo", "usermod", "-aG", "docker", os.environ["USER"]], check=True)
                # Cleanup
                os.remove("/tmp/get-docker.sh")

                success("Docker installed successfully!")
                print()
                warn(
                    "IMPORTANT: You need to log out and back in for group membership to take effect."
                )
                print("After logging back in, run this script again.")
                sys.exit(0)
            except Exception as e:
                error(f"Docker installation failed: {e}")
                return False
        else:
            error("Docker is required")
            return False

    # Check Docker daemon is running and we have permissions
    return check_docker_permissions()


def is_dangerous_dir(path: Path) -> bool:
    """Check if a directory is dangerous to mount (contains credentials)"""
    for dangerous in Config.DANGEROUS_DIRS:
        try:
            # Check if path is dangerous or contains dangerous
            if path.resolve() == dangerous.resolve():
                return True
            if path.resolve() in dangerous.resolve().parents:
                return True
            if dangerous.resolve() in path.resolve().parents:
                return True
        except Exception:
            pass
    return False


def _load_repos_config() -> dict[str, Any]:
    """Load repositories.yaml for build_commands configuration.

    Returns:
        Parsed config dict, or empty dict if not found.
    """
    config_path = Config.REPOS_CONFIG_FILE
    if not config_path.exists():
        return {}
    try:
        with config_path.open() as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _get_local_repo_path(config: dict[str, Any], repo_name: str) -> Path | None:
    """Find the local path for a repo from local_repos.paths config.

    Matches by checking if the repo name appears as the last component(s) of
    the local path (e.g., /home/user/projects/org/repo matches org/repo).

    Args:
        config: Parsed repositories.yaml config
        repo_name: Repository in "owner/repo" format

    Returns:
        Path to the local repo directory, or None if not found.
    """
    local_repos = config.get("local_repos", {})
    if not isinstance(local_repos, dict):
        return None
    paths = local_repos.get("paths", [])
    if not isinstance(paths, list):
        return None

    # Normalize repo name for matching
    repo_parts = repo_name.lower().split("/")

    for path_str in paths:
        path = Path(str(path_str)).expanduser().resolve()
        if not path.exists() or not path.is_dir():
            continue
        # Check if the path ends with the repo name parts
        # e.g., /home/user/repos/org/repo -> parts [-2:] = ["org", "repo"]
        path_parts = [p.lower() for p in path.parts]
        if len(path_parts) >= len(repo_parts):
            if path_parts[-len(repo_parts) :] == repo_parts:
                return path
        # Also try matching just the repo name (without owner)
        if len(repo_parts) > 1 and path.name.lower() == repo_parts[-1]:
            return path

    return None


def populate_build_context(target_dir: Path, quiet: bool = False) -> None:
    """Populate ``target_dir`` with watch files + manifest for the sandbox build.

    For each repo with ``build_commands`` configured in ``repositories.yaml``:
    copies the declared ``watch_files`` from the local repo directory into
    ``<target_dir>/<owner--repo>/`` and writes a ``manifest.json`` describing
    the build commands and persist directories. ``docker-setup.py`` reads
    that manifest during the sandbox image build (Stage 1 of
    ``sandbox/Dockerfile``) to run per-repo build steps and persist their
    output (e.g. ``.venv``, ``node_modules``) into the image.

    Docker layer caching keys on the contents of ``target_dir``: the
    ``COPY repo-deps/`` layer only invalidates when watch files change,
    so unchanged dependency layers are reused across builds.

    When no repos declare ``build_commands`` and no ``extra_packages`` are
    configured, an ``.empty`` marker is written so the Dockerfile ``COPY``
    step still has a valid source.
    """
    config = _load_repos_config()
    repo_settings = config.get("repo_settings", {})
    if not isinstance(repo_settings, dict):
        repo_settings = {}

    repo_deps_dir = target_dir

    # Footgun guard: this function rmtrees its target. The Makefile passes
    # ``./repo-deps``, but the script is a public entry point and a stray
    # argument like ``/home/user/important-data`` would otherwise be wiped.
    # Refuse anything whose final segment isn't ``repo-deps``.
    if repo_deps_dir.name != "repo-deps":
        raise ValueError(
            f"populate_build_context refuses to operate on {repo_deps_dir!s}: "
            f"target directory must be named 'repo-deps' (got {repo_deps_dir.name!r})"
        )

    # Clean up old contents to avoid stale files
    if repo_deps_dir.exists():
        shutil.rmtree(repo_deps_dir, ignore_errors=True)

    has_any = False
    repos_with_local_path: set[str] = set()

    for repo_name, settings in repo_settings.items():
        if not isinstance(settings, dict):
            continue
        build_cmds = settings.get("build_commands")
        if not isinstance(build_cmds, dict):
            continue
        watch_files = build_cmds.get("watch_files", [])
        commands = build_cmds.get("commands", [])
        if not isinstance(watch_files, list) or not isinstance(commands, list):
            # Malformed yaml: tell the operator. Without this warn, a
            # repo with a typo in build_commands.watch_files / .commands
            # is silently dropped from both the watch-file copy step and
            # (via repos_with_local_path) the manifest, producing an
            # image with no per-repo build steps and no log line.
            #
            # Intentionally NOT gated on ``quiet``: this is operator
            # misconfiguration, not a recoverable per-file condition like
            # "watch file not found" or "local path not found". The other
            # warns in this function are quiet-gated because tests run with
            # quiet=True to suppress expected per-file noise; a malformed
            # build_commands block is a config bug we want surfaced
            # regardless.
            warn(f"build_commands: skipping {repo_name} — watch_files and commands must be lists")
            continue
        if not commands:
            continue

        # Find the local repo path
        local_path = _get_local_repo_path(config, repo_name)
        if local_path is None:
            if not quiet:
                warn(f"build_commands: local path not found for {repo_name}, skipping watch files")
            continue

        repos_with_local_path.add(repo_name)

        # Copy watch files
        repo_dir_name = repo_name.replace("/", "--")
        dest_dir = repo_deps_dir / repo_dir_name
        dest_dir.mkdir(parents=True, exist_ok=True)

        copied_any = False
        for watch_file in watch_files:
            src_file = local_path / str(watch_file)

            # Defense-in-depth: validate path stays within repo boundary
            try:
                src_file.resolve().relative_to(local_path.resolve())
            except ValueError:
                warn(f"build_commands: watch file escapes repo boundary: {repo_name}/{watch_file}")
                continue

            if not src_file.exists() or not src_file.is_file():
                if not quiet:
                    warn(f"build_commands: watch file not found: {repo_name}/{watch_file}")
                continue

            # Defense-in-depth: don't follow symlinks that point outside the repo
            if src_file.is_symlink():
                resolved = src_file.resolve()
                if not resolved.is_relative_to(local_path.resolve()):
                    warn(
                        f"build_commands: watch file symlink escapes repo boundary: {repo_name}/{watch_file}"
                    )
                    continue

            # Preserve directory structure within the watch file path
            dest_file = dest_dir / str(watch_file)

            # Validate dest path stays within dest_dir
            try:
                dest_file.resolve().relative_to(dest_dir.resolve())
            except ValueError:
                warn(
                    f"build_commands: watch file dest escapes build context: {repo_name}/{watch_file}"
                )
                continue

            dest_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest_file)
            copied_any = True

        if copied_any:
            has_any = True
            if not quiet:
                info(f"Copied watch files for {repo_name}")

    # Write a manifest.json so docker-setup.py can read it during the Docker build.
    # (repositories.yaml is not available in the build context)
    # Format: {"extra_packages": {"apt": [...], "dnf": [...]}, "build_commands": [...]}
    build_commands_list = []
    for repo_name, settings in repo_settings.items():
        if not isinstance(settings, dict):
            continue
        build_cmds = settings.get("build_commands")
        if not isinstance(build_cmds, dict):
            continue
        commands = build_cmds.get("commands", [])
        if not isinstance(commands, list) or not commands:
            continue
        # Skip repos whose local path wasn't found above. The host already
        # warned; emitting a manifest entry here would surface as a
        # downstream RuntimeError from docker-setup.py:run_build_commands
        # (watch files dir missing) which is just noise for the same root
        # cause. Keep the host warning as the single source of truth.
        if repo_name not in repos_with_local_path:
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
        build_commands_list.append(
            {
                "repo": repo_name,
                "watch_files": [str(f) for f in watch_files],
                "commands": [str(c) for c in commands],
                "persist_dirs": [str(d) for d in persist_dirs],
                "persist_system_dirs": [str(d) for d in persist_system_dirs],
            }
        )

    # Also include extra_packages so they're installed during the Docker build
    docker_setup_cfg = config.get("docker_setup", {})
    extra_pkgs = (
        docker_setup_cfg.get("extra_packages", {}) if isinstance(docker_setup_cfg, dict) else {}
    )
    if not isinstance(extra_pkgs, dict):
        extra_pkgs = {}
    apt_pkgs = extra_pkgs.get("apt", [])
    dnf_pkgs = extra_pkgs.get("dnf", [])
    generic_pkgs = extra_pkgs.get("packages", [])
    if not isinstance(apt_pkgs, list):
        apt_pkgs = []
    if not isinstance(dnf_pkgs, list):
        dnf_pkgs = []
    if not isinstance(generic_pkgs, list):
        generic_pkgs = []
    apt_pkgs = [str(p) for p in apt_pkgs + generic_pkgs]
    dnf_pkgs = [str(p) for p in dnf_pkgs + generic_pkgs]

    manifest_data: dict[str, Any] = {
        "extra_packages": {"apt": apt_pkgs, "dnf": dnf_pkgs},
        "build_commands": build_commands_list,
    }

    if build_commands_list or apt_pkgs or dnf_pkgs:
        repo_deps_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = repo_deps_dir / "manifest.json"
        with manifest_path.open("w") as f:
            json.dump(manifest_data, f, indent=2)
        if not quiet:
            info(
                f"Wrote build manifest ({len(build_commands_list)} repos, "
                f"{len(apt_pkgs)} apt pkgs, {len(dnf_pkgs)} dnf pkgs)"
            )
        has_any = True

    if not has_any:
        # Always create repo-deps with an empty marker so Dockerfile COPY doesn't fail
        repo_deps_dir.mkdir(parents=True, exist_ok=True)
        (repo_deps_dir / ".empty").touch()


def image_exists() -> bool:
    """Check if Docker image exists"""
    ctx = get_context()
    return (
        subprocess.run(
            ["docker", "image", "inspect", ctx.sandbox_image], capture_output=True, check=False
        ).returncode
        == 0
    )


def ensure_egg_network() -> bool:
    """Create egg-network Docker network if it doesn't exist.

    Returns:
        True if network exists or was created, False on failure
    """
    ctx = get_context()
    network = ctx.isolated_network

    # Check if network exists
    result = subprocess.run(
        ["docker", "network", "inspect", network],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return True

    # Create the network
    result = subprocess.run(
        ["docker", "network", "create", network],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        info(f"Created Docker network: {network}")
        return True

    error(f"Failed to create Docker network: {result.stderr}")
    return False


def _create_network(name: str, subnet: str, internal: bool = False) -> bool:
    """Create a Docker network with specific configuration.

    Args:
        name: Network name
        subnet: Network subnet (e.g., "172.32.0.0/24")
        internal: If True, create as internal network (no external route)

    Returns:
        True if network exists or was created successfully
    """
    # Check if network exists
    result = subprocess.run(
        ["docker", "network", "inspect", name],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return True

    # Build create command
    cmd = [
        "docker",
        "network",
        "create",
        "--driver",
        "bridge",
        "--subnet",
        subnet,
    ]

    if internal:
        cmd.append("--internal")

    cmd.append(name)

    # Create the network
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode == 0:
        info(f"Created Docker network: {name} (subnet: {subnet}, internal: {internal})")
        return True

    error(f"Failed to create Docker network {name}: {result.stderr}")
    return False


def _allocate_dynamic_subnet() -> str:
    """Find an unused 172.x.0.0/24 subnet in the Docker network space.

    Scans 172.28.0.0/24 through 172.63.255.0/24 for subnets not already
    claimed by existing Docker networks.

    Returns:
        A subnet string like ``"172.28.0.0/24"``

    Raises:
        RuntimeError: If no unused subnet can be found.
    """
    # Collect subnets already in use
    used: set[str] = set()
    try:
        result = subprocess.run(
            ["docker", "network", "ls", "--format", "{{.ID}}"],
            capture_output=True,
            text=True,
            check=True,
        )
        for net_id in result.stdout.strip().splitlines():
            if not net_id:
                continue
            inspect = subprocess.run(
                [
                    "docker",
                    "network",
                    "inspect",
                    net_id,
                    "--format",
                    "{{range .IPAM.Config}}{{.Subnet}}{{end}}",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            subnet = inspect.stdout.strip()
            if subnet:
                used.add(subnet)
    except Exception:
        pass  # Proceed with empty set — worst case we get a conflict

    for major in range(28, 64):
        for minor in range(0, 256):
            candidate = f"172.{major}.{minor}.0/24"
            if candidate not in used:
                return candidate

    raise RuntimeError("No unused subnet found in 172.28-63.x.0/24 range")


def ensure_gateway_networks() -> bool:
    """Create both gateway networks if they don't exist.

    Creates the dual-network architecture for gateway:
    - egg-isolated: Internal network (no external route) for egg containers
    - egg-external: Standard bridge network for gateway external access

    The gateway is dual-homed, connecting to both networks. Egg containers
    connect only to egg-isolated and route traffic through the gateway.

    When the context subnets are set to ``"auto"``, dynamically allocate
    unused subnets (used in GHA to avoid collisions between concurrent runs).
    The context is updated in-place with the actual values.

    Returns:
        True if both networks exist or were created, False on failure
    """
    ctx = get_context()

    # Resolve dynamic subnets if requested
    if ctx.isolated_subnet == AUTO:
        ctx.isolated_subnet = _allocate_dynamic_subnet()
        # Derive gateway IP from the allocated subnet (x.x.x.2)
        base = ctx.isolated_subnet.rsplit(".", 1)[0]  # e.g. "172.28.0"
        ctx.gateway_isolated_ip = f"{base}.2"

    # Create internal isolated network first so the next allocation sees it
    if not _create_network(ctx.isolated_network, ctx.isolated_subnet, internal=True):
        return False

    if ctx.external_subnet == AUTO:
        ctx.external_subnet = _allocate_dynamic_subnet()
        base = ctx.external_subnet.rsplit(".", 1)[0]
        ctx.gateway_external_ip = f"{base}.2"

    # Create external network (standard bridge)
    if not _create_network(ctx.external_network, ctx.external_subnet, internal=False):
        return False

    return True


def teardown_networks() -> None:
    """Remove ephemeral Docker networks created for this context.

    Called during cleanup of ephemeral (GHA) runs.
    """
    ctx = get_context()
    for network in [ctx.isolated_network, ctx.external_network]:
        subprocess.run(
            ["docker", "network", "rm", network],
            capture_output=True,
            check=False,
        )
