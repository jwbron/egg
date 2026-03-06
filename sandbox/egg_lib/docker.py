"""Docker image management for egg.

This module handles Docker image building, hash caching,
Dockerfile creation, and related utilities.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import yaml

from .config import (
    Config,
)
from .context import AUTO, get_context
from .output import error, get_quiet_mode, info, success, warn

# Label used to store build content hash on Docker image
BUILD_HASH_LABEL = "org.egg.build-hash"

# Global force rebuild flag (set by --rebuild)
_force_rebuild = False


def set_force_rebuild(force: bool) -> None:
    """Set the global force rebuild flag."""
    global _force_rebuild
    _force_rebuild = force


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


def _copy_directory_atomic(src: Path, dest: Path, name: str, quiet: bool = False) -> bool:
    """Copy a directory atomically with retry logic for race conditions.

    When multiple egg --exec instances run simultaneously, they may all try to
    update the same build context directories. This function uses atomic operations
    to handle race conditions:
    1. Copy to a temporary directory
    2. Remove existing destination (with retry on ENOTEMPTY/ENOENT)
    3. Rename temp to destination (atomic on same filesystem)

    Args:
        src: Source directory to copy
        dest: Destination path
        name: Human-readable name for logging
        quiet: If True, suppress info messages

    Returns:
        True if successful, False otherwise
    """
    max_retries = 3
    retry_delay = 0.1  # seconds

    for attempt in range(max_retries):
        try:
            # Create a unique temp directory in the same parent (for atomic rename)
            temp_dir = dest.parent / f".tmp-{uuid.uuid4().hex[:8]}"

            # Copy source to temp location
            shutil.copytree(src, temp_dir)

            # Try to remove existing destination
            if dest.exists():
                try:
                    shutil.rmtree(dest)
                except FileNotFoundError:
                    # Another process already removed it - that's fine
                    pass
                except OSError:
                    # Directory not empty (ENOTEMPTY) - another process is writing
                    # Clean up temp and retry
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay * (attempt + 1))
                        continue
                    raise

            # Atomic rename from temp to destination
            try:
                temp_dir.rename(dest)
            except OSError:
                # Destination appeared between rmtree and rename - another process won
                # Clean up our temp and use their copy
                shutil.rmtree(temp_dir, ignore_errors=True)
                if dest.exists():
                    # Other process succeeded, we're done
                    if not quiet:
                        info(f"{name} directory ready (from another process)")
                    return True
                # Neither exists - retry
                if attempt < max_retries - 1:
                    time.sleep(retry_delay * (attempt + 1))
                    continue
                raise

            if not quiet:
                info(f"{name} copied to build context")
            return True

        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(retry_delay * (attempt + 1))
                continue
            warn(f"Failed to copy {name} directory after {max_retries} attempts: {e}")
            # Clean up temp if it exists
            if "temp_dir" in locals() and temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
            return False

    return False


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


def _copy_repo_watch_files(quiet: bool = False) -> None:
    """Copy watch files from local repos into the build context.

    For each repo with build_commands configured, copies the watch_files
    from the local repo directory into the build context at
    repo-deps/<repo-dir-name>/.

    This enables Docker layer caching: the COPY layer only invalidates
    when watch files change, triggering a rebuild of the dependency layer.
    """
    config = _load_repos_config()
    repo_settings = config.get("repo_settings", {})
    if not isinstance(repo_settings, dict):
        return

    repo_deps_dir = Config.CONFIG_DIR / "repo-deps"

    # Clean up old repo-deps to avoid stale files
    if repo_deps_dir.exists():
        shutil.rmtree(repo_deps_dir, ignore_errors=True)

    has_any = False

    for repo_name, settings in repo_settings.items():
        if not isinstance(settings, dict):
            continue
        build_cmds = settings.get("build_commands")
        if not isinstance(build_cmds, dict):
            continue
        watch_files = build_cmds.get("watch_files", [])
        commands = build_cmds.get("commands", [])
        if not isinstance(watch_files, list) or not isinstance(commands, list):
            continue
        if not commands:
            continue

        # Find the local repo path
        local_path = _get_local_repo_path(config, repo_name)
        if local_path is None:
            if not quiet:
                warn(f"build_commands: local path not found for {repo_name}, skipping watch files")
            continue

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
        watch_files = build_cmds.get("watch_files", [])
        if not isinstance(watch_files, list):
            watch_files = []
        build_commands_list.append(
            {
                "repo": repo_name,
                "watch_files": [str(f) for f in watch_files],
                "commands": [str(c) for c in commands],
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


def create_dockerfile() -> None:
    """Create the Dockerfile for the container"""
    quiet = get_quiet_mode()

    # Ensure cache directory exists
    Config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Resolve symlinks to find the actual project directory
    script_dir = Path(__file__).resolve().parent.parent

    # Copy docker-setup.py to sandbox subdirectory of build context
    # (Dockerfile references sandbox/docker-setup.py)
    sandbox_dest = Config.CONFIG_DIR / "sandbox"
    sandbox_dest.mkdir(parents=True, exist_ok=True)
    setup_script = script_dir / "docker-setup.py"
    setup_dest = sandbox_dest / "docker-setup.py"

    if setup_script.exists():
        shutil.copy(setup_script, setup_dest)
        setup_dest.chmod(0o755)
    else:
        warn("docker-setup.py not found, skipping dev tools installation")

    # Copy claude-commands directory to sandbox subdirectory of build context
    # (Dockerfile references sandbox/claude-commands/)
    # Use atomic copy with retry to handle race conditions when multiple
    # egg --exec instances run simultaneously
    commands_src = script_dir / "claude-commands"
    commands_dest = sandbox_dest / "claude-commands"
    if commands_src.exists():
        _copy_directory_atomic(commands_src, commands_dest, "Claude commands", quiet)
    else:
        warn("claude-commands directory not found")

    # Copy claude-rules directory to sandbox subdirectory of build context
    # (Dockerfile references sandbox/claude-rules/)
    # Use atomic copy with retry to handle race conditions
    rules_src = script_dir / "claude-rules"
    rules_dest = sandbox_dest / "claude-rules"
    if rules_src.exists():
        _copy_directory_atomic(rules_src, rules_dest, "Claude rules", quiet)
    else:
        warn("claude-rules directory not found, skipping agent rules")

    # Copy egg-runtime directories to build context
    # These provide container-resident executables and tools
    # The bin/ directory contains symlinks to executables (added to PATH in container)
    runtime_dirs = ["bin", "egg_lib", "llm", "tools", "scripts"]
    for dir_name in runtime_dirs:
        src = script_dir / dir_name
        dest = sandbox_dest / dir_name
        if src.exists():
            _copy_directory_atomic(src, dest, f"Runtime {dir_name}", quiet)
        else:
            warn(f"{dir_name} directory not found, skipping")

    # Copy shared directory from repo root to build context (sibling of sandbox)
    # Contains shared modules (egg_logging, egg_config, etc.)
    repo_root = script_dir.parent  # sandbox's parent is egg
    shared_src = repo_root / "shared"
    shared_dest = Config.CONFIG_DIR / "shared"
    if shared_src.exists():
        _copy_directory_atomic(shared_src, shared_dest, "Shared modules", quiet)
    else:
        warn("shared directory not found, container processors may fail imports")

    # Copy pyproject.toml files for pip-installable packages
    # These make claude (from sandbox) and shared modules pip-installable
    pyproject_files = [
        (script_dir / "pyproject.toml", sandbox_dest / "pyproject.toml"),
        (shared_src / "pyproject.toml", shared_dest / "pyproject.toml"),
    ]
    for src, dest in pyproject_files:
        if src.exists():
            shutil.copy(src, dest)
        else:
            warn(f"pyproject.toml not found at {src}")

    # Note: Claude credentials are mounted at runtime (not copied at build time)
    # This ensures the container always uses the host's CURRENT credentials
    # Avoids issues with stale/revoked OAuth tokens from previous builds
    if not quiet:
        info("Claude credentials will be mounted from host at runtime (see setup output above)")

    # Copy entrypoint.py from script directory
    entrypoint_src = script_dir / "entrypoint.py"
    entrypoint_dest = sandbox_dest / "entrypoint.py"
    if entrypoint_src.exists():
        shutil.copy(entrypoint_src, entrypoint_dest)
        entrypoint_dest.chmod(0o755)
    else:
        error(f"entrypoint.py not found at {entrypoint_src}")
        error("Cannot build without entrypoint script")

    # Copy watch files for build_commands (per-repo dependency caching)
    _copy_repo_watch_files(quiet)

    # Copy Dockerfile from script directory
    dockerfile_src = script_dir / "Dockerfile"
    if dockerfile_src.exists():
        shutil.copy(dockerfile_src, Config.DOCKERFILE)
        if not quiet:
            success("Build context prepared")
    else:
        error(f"Dockerfile not found at {dockerfile_src}")
        error("Cannot build without Dockerfile")


def get_installed_claude_version() -> str | None:
    """Get the Claude Code version installed in the current image.

    Returns:
        Version string (e.g., "2.1.7") or None if not available
    """
    if not image_exists():
        return None

    try:
        result = subprocess.run(  # noqa: EGG100 - extract version from sandbox image
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "cat",
                Config.IMAGE_NAME,
                "/opt/claude/VERSION",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            # Version output is like "claude 2.1.7" - extract just the number
            version_line = result.stdout.strip()
            parts = version_line.split()
            return parts[-1] if parts else None
        return None
    except Exception:
        return None


def get_latest_claude_version() -> str | None:
    """Get the latest Claude Code version from npm registry.

    Returns:
        Version string (e.g., "2.1.17") or None if check fails
    """
    import json
    import urllib.request

    try:
        url = "https://registry.npmjs.org/@anthropic-ai/claude-code/latest"
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read().decode())
            version: str | None = data.get("version")
            return version
    except Exception:
        return None


def check_claude_update() -> str | None:
    """Check if a Claude Code update is available.

    Returns:
        The new version string if update available, None otherwise
    """
    quiet = get_quiet_mode()
    installed = get_installed_claude_version()
    latest = get_latest_claude_version()

    if not latest:
        # Can't check, don't force update
        return None

    if not installed:
        # No version installed, use latest
        return latest

    # Compare versions
    if installed != latest:
        if not quiet:
            info(f"Claude Code update available: {installed} → {latest}")
        return latest

    return None


def hash_file(path: Path, hasher: Any) -> None:
    """Add a single file's content to the hasher."""
    try:
        with open(path, "rb") as f:
            while chunk := f.read(8192):
                hasher.update(chunk)
    except OSError:
        pass


def hash_directory(path: Path, hasher: Any) -> None:
    """Recursively hash all files in a directory."""
    if not path.exists():
        return
    for item in sorted(path.rglob("*")):
        if item.is_file() and not item.name.startswith(".") and "__pycache__" not in item.parts:
            # Include relative path in hash to detect renames/moves
            hasher.update(str(item.relative_to(path)).encode())
            hash_file(item, hasher)


def _hash_build_command_watch_files(hasher: Any) -> None:
    """Hash watch file contents from build_commands config.

    Reads the repositories.yaml config and hashes the contents of all
    watch_files from their local repo paths. This ensures that the build
    hash changes when dependency files (e.g., package-lock.json) change,
    triggering an automatic image rebuild.
    """
    config = _load_repos_config()
    repo_settings = config.get("repo_settings", {})
    if not isinstance(repo_settings, dict):
        return

    for repo_name in sorted(repo_settings.keys()):
        settings = repo_settings[repo_name]
        if not isinstance(settings, dict):
            continue
        build_cmds = settings.get("build_commands")
        if not isinstance(build_cmds, dict):
            continue
        watch_files = build_cmds.get("watch_files", [])
        commands = build_cmds.get("commands", [])
        if not isinstance(watch_files, list) or not isinstance(commands, list):
            continue
        if not commands:
            continue

        local_path = _get_local_repo_path(config, repo_name)
        if local_path is None:
            continue

        # Include the repo name and commands in the hash so changes to
        # the build_commands config itself also trigger rebuilds
        hasher.update(f"build_commands:{repo_name}".encode())
        for cmd in commands:
            hasher.update(f"cmd:{cmd}".encode())

        # Hash watch file contents
        for watch_file in sorted(str(f) for f in watch_files):
            src_file = local_path / watch_file

            # Defense-in-depth: validate path stays within repo boundary
            try:
                src_file.resolve().relative_to(local_path.resolve())
            except ValueError:
                warn(f"build_commands: watch file escapes repo boundary: {repo_name}/{watch_file}")
                continue

            if src_file.exists() and src_file.is_file():
                hasher.update(f"watch:{repo_name}/{watch_file}".encode())
                hash_file(src_file, hasher)


def compute_build_hash() -> str:
    """Compute a SHA256 hash of all files that affect the Docker image build.

    This includes:
    - Dockerfile
    - entrypoint.py
    - docker-setup.py
    - claude-commands/
    - claude-rules/
    - .claude/hooks/
    - bin/, egg_lib/, llm/, tools/, scripts/
    - shared/ (from repo root)
    - pyproject.toml files
    - Host-services files that get copied to container

    Also includes the current user's UID/GID since these affect the build.

    Returns:
        Hex-encoded SHA256 hash string
    """
    script_dir = Path(__file__).resolve().parent.parent
    repo_root = script_dir.parent
    hasher = hashlib.sha256()

    # Include UID/GID in hash (affects build args)
    hasher.update(f"uid={os.getuid()},gid={os.getgid()}".encode())

    # Single files in sandbox/
    single_files = [
        script_dir / "Dockerfile",
        script_dir / "entrypoint.py",
        script_dir / "docker-setup.py",
        script_dir / "pyproject.toml",
    ]
    for path in single_files:
        if path.exists():
            hasher.update(path.name.encode())
            hash_file(path, hasher)

    # Directories in sandbox/
    container_dirs = [
        "claude-commands",
        "claude-rules",
        "bin",
        "egg_lib",
        "llm",
        "tools",
        "scripts",
    ]
    for dir_name in container_dirs:
        dir_path = script_dir / dir_name
        if dir_path.exists():
            hasher.update(dir_name.encode())
            hash_directory(dir_path, hasher)

    # .claude/hooks directory
    hooks_path = script_dir / ".claude" / "hooks"
    if hooks_path.exists():
        hasher.update(b".claude/hooks")
        hash_directory(hooks_path, hasher)

    # shared/ directory from repo root
    shared_path = repo_root / "shared"
    if shared_path.exists():
        hasher.update(b"shared")
        hash_directory(shared_path, hasher)
        # Include shared pyproject.toml
        shared_pyproject = shared_path / "pyproject.toml"
        if shared_pyproject.exists():
            hash_file(shared_pyproject, hasher)

    # Include watch file contents from build_commands config
    # This ensures the image rebuilds when dependency files change
    _hash_build_command_watch_files(hasher)

    return hasher.hexdigest()


def get_image_build_hash() -> str | None:
    """Get the build hash stored in the Docker image label.

    Returns:
        Hash string if image exists and has the label, None otherwise
    """
    if not image_exists():
        return None

    try:
        result = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                f'{{{{index .Config.Labels "{BUILD_HASH_LABEL}"}}}}',
                Config.IMAGE_NAME,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            hash_value = result.stdout.strip()
            # Docker returns empty string or "<no value>" if label doesn't exist
            if hash_value and hash_value != "<no value>":
                return hash_value
        return None
    except Exception:
        return None


def should_rebuild_image() -> tuple[bool, str]:
    """Check if the Docker image needs to be rebuilt.

    Returns:
        Tuple of (should_rebuild, reason)
    """
    # Force rebuild if --rebuild flag is set
    if _force_rebuild:
        return True, "forced rebuild (--rebuild flag)"

    if not image_exists():
        return True, "image does not exist"

    current_hash = compute_build_hash()
    stored_hash = get_image_build_hash()

    if stored_hash is None:
        return True, "no build hash stored (legacy image)"

    if current_hash != stored_hash:
        return True, "build files changed"

    # Check for Claude Code updates (even if files haven't changed)
    claude_version = check_claude_update()
    if claude_version:
        return True, f"Claude Code update available ({claude_version})"

    return False, "build hash matches (skipping rebuild)"


def build_image() -> bool:
    """Build the Docker image, skipping if nothing has changed.

    Uses content hashing to detect when build files change. If the image
    exists and its stored hash matches the current files, the build is
    skipped entirely (~25 seconds saved).

    When ``ctx.skip_build`` is True (GHA), the image is pre-built and
    pulled from a registry, so this function short-circuits immediately.
    """
    ctx = get_context()
    quiet = get_quiet_mode()

    # In GHA, images are pre-pulled from GHCR — no build needed
    if ctx.skip_build:
        return True

    # Check if rebuild is needed
    needs_rebuild, reason = should_rebuild_image()

    if not needs_rebuild:
        if not quiet:
            info(f"Docker image up-to-date: {reason}")
        return True

    # Show rebuild reason - use warn() so it's visible in quiet mode for --rebuild
    if _force_rebuild:
        warn(f"Building Docker image: {reason}")
    elif not quiet:
        info(f"Building Docker image: {reason}")

    # Sync files to build context
    create_dockerfile()

    # Check for Claude Code updates
    claude_version = check_claude_update()

    # Compute the build hash to store as a label
    build_hash = compute_build_hash()

    try:
        cmd = [
            "docker",
            "build",
            "--build-arg",
            f"USER_NAME={os.environ['USER']}",
            "--build-arg",
            f"USER_UID={os.getuid()}",
            "--build-arg",
            f"USER_GID={os.getgid()}",
            "--label",
            f"{BUILD_HASH_LABEL}={build_hash}",
            "-t",
            Config.IMAGE_NAME,
            "-f",
            str(Config.DOCKERFILE),
            str(Config.CONFIG_DIR),
        ]

        # Pass Claude version to bust cache if update available
        if claude_version:
            cmd.insert(2, "--build-arg")
            cmd.insert(3, f"CLAUDE_CODE_VERSION={claude_version}")

        # Force no-cache when --rebuild flag is set
        if _force_rebuild:
            cmd.insert(2, "--no-cache")

        # In quiet mode, suppress Docker build output
        if quiet:
            cmd.insert(2, "--quiet")
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if result.returncode != 0:
                # Show error output if build failed
                error("Docker build failed")
                if result.stderr:
                    print(result.stderr, file=sys.stderr)
                return False
        else:
            # Docker automatically uses cache for unchanged layers
            subprocess.run(cmd, check=True)
        return True
    except subprocess.CalledProcessError:
        error("Docker build failed")
        return False


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
