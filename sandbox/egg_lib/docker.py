"""Docker image management for egg.

This module handles Docker image building, hash caching,
Dockerfile creation, and related utilities.
"""

import hashlib
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .config import EGG_ISOLATED_NETWORK, Config
from .output import error, get_quiet_mode, info, success, warn

# Label used to store build content hash on Docker image
BUILD_HASH_LABEL = "org.egg.build-hash"

# Global force rebuild flag (set by --rebuild)
_force_rebuild = False


def set_force_rebuild(force: bool) -> None:
    """Set the global force rebuild flag."""
    global _force_rebuild
    _force_rebuild = force


def get_force_rebuild() -> bool:
    """Get the current force rebuild setting."""
    return _force_rebuild


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


def create_dockerfile() -> None:
    """Create the Dockerfile for the container"""
    quiet = get_quiet_mode()

    # Ensure cache directory exists
    Config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Resolve symlinks to find the actual project directory
    script_dir = Path(__file__).resolve().parent.parent

    # Copy docker-setup.py to config directory
    setup_script = script_dir / "docker-setup.py"
    setup_dest = Config.CONFIG_DIR / "docker-setup.py"

    if setup_script.exists():
        shutil.copy(setup_script, setup_dest)
        setup_dest.chmod(0o755)
    else:
        warn("docker-setup.py not found, skipping dev tools installation")

    # Copy claude-commands directory to config directory
    # Use atomic copy with retry to handle race conditions when multiple
    # egg --exec instances run simultaneously
    commands_src = script_dir / "claude-commands"
    commands_dest = Config.CONFIG_DIR / "claude-commands"
    if commands_src.exists():
        _copy_directory_atomic(commands_src, commands_dest, "Claude commands", quiet)
    else:
        warn("claude-commands directory not found")

    # Copy claude-rules directory to build context
    # Use atomic copy with retry to handle race conditions
    rules_src = script_dir / "claude-rules"
    rules_dest = Config.CONFIG_DIR / "claude-rules"
    if rules_src.exists():
        _copy_directory_atomic(rules_src, rules_dest, "Claude rules", quiet)
    else:
        warn("claude-rules directory not found, skipping agent rules")

    # Copy egg-runtime directories to build context
    # These provide container-resident executables and tools
    # The bin/ directory contains symlinks to executables (added to PATH in container)
    sandbox_dest = Config.CONFIG_DIR / "sandbox"
    sandbox_dest.mkdir(parents=True, exist_ok=True)

    runtime_dirs = ["bin", "llm", "tools", "scripts"]
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
    entrypoint_dest = Config.CONFIG_DIR / "entrypoint.py"
    if entrypoint_src.exists():
        shutil.copy(entrypoint_src, entrypoint_dest)
        entrypoint_dest.chmod(0o755)
    else:
        error(f"entrypoint.py not found at {entrypoint_src}")
        error("Cannot build without entrypoint script")

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
        result = subprocess.run(
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
            return data.get("version")
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


def _hash_file(path: Path, hasher) -> None:
    """Add a single file's content to the hasher."""
    try:
        with open(path, "rb") as f:
            while chunk := f.read(8192):
                hasher.update(chunk)
    except OSError:
        pass


def _hash_directory(path: Path, hasher) -> None:
    """Recursively hash all files in a directory."""
    if not path.exists():
        return
    for item in sorted(path.rglob("*")):
        if item.is_file() and not item.name.startswith("."):
            # Include relative path in hash to detect renames/moves
            hasher.update(str(item.relative_to(path)).encode())
            _hash_file(item, hasher)


def compute_build_hash() -> str:
    """Compute a SHA256 hash of all files that affect the Docker image build.

    This includes:
    - Dockerfile
    - entrypoint.py
    - docker-setup.py
    - claude-commands/
    - claude-rules/
    - .claude/hooks/
    - bin/, llm/, tools/, scripts/
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
            _hash_file(path, hasher)

    # Directories in sandbox/
    container_dirs = [
        "claude-commands",
        "claude-rules",
        "bin",
        "llm",
        "tools",
        "scripts",
    ]
    for dir_name in container_dirs:
        dir_path = script_dir / dir_name
        if dir_path.exists():
            hasher.update(dir_name.encode())
            _hash_directory(dir_path, hasher)

    # .claude/hooks directory
    hooks_path = script_dir / ".claude" / "hooks"
    if hooks_path.exists():
        hasher.update(b".claude/hooks")
        _hash_directory(hooks_path, hasher)

    # shared/ directory from repo root
    shared_path = repo_root / "shared"
    if shared_path.exists():
        hasher.update(b"shared")
        _hash_directory(shared_path, hasher)
        # Include shared pyproject.toml
        shared_pyproject = shared_path / "pyproject.toml"
        if shared_pyproject.exists():
            _hash_file(shared_pyproject, hasher)

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
    """
    quiet = get_quiet_mode()

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
    return (
        subprocess.run(
            ["docker", "image", "inspect", Config.IMAGE_NAME], capture_output=True, check=False
        ).returncode
        == 0
    )


def ensure_egg_network() -> bool:
    """Create egg-network Docker network if it doesn't exist.

    Returns:
        True if network exists or was created, False on failure
    """
    # Check if network exists
    result = subprocess.run(
        ["docker", "network", "inspect", EGG_ISOLATED_NETWORK], capture_output=True, check=False
    )
    if result.returncode == 0:
        return True

    # Create the network
    result = subprocess.run(
        ["docker", "network", "create", EGG_ISOLATED_NETWORK],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        info(f"Created Docker network: {EGG_ISOLATED_NETWORK}")
        return True

    error(f"Failed to create Docker network: {result.stderr}")
    return False
