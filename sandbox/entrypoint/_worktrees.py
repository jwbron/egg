"""Worktree validation, prebuilt-deps restore, and the ~/egg symlink."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ._config import Config, Logger
from ._core import chown_recursive


def setup_worktrees(config: Config, logger: Logger) -> bool:
    """Validate gateway-managed worktree configuration.

    This implements the Gateway-Managed Worktrees architecture:
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


def restore_prebuilt_deps(
    config: Config,
    logger: Logger,
    prebuilt_base: Path | None = None,
) -> None:
    """Restore pre-built dependencies from Docker image into mounted repos.

    During Docker image build, build_commands may produce artifacts (e.g. node_modules)
    in /tmp/repo-deps/ which gets deleted. The persist_dirs config causes those
    directories to be saved to /opt/prebuilt-deps/ instead.

    This function copies them into the actual repo mounts at startup so that
    private-mode containers have dependencies available without network access.
    """
    if prebuilt_base is None:
        prebuilt_base = Path("/opt/prebuilt-deps")
    if not prebuilt_base.exists():
        return

    repos_dir = config.repos_dir
    if not repos_dir.exists():
        logger.info("  Repos directory does not exist, skipping prebuilt deps restore")
        return
    restored = 0

    for repo_dir in prebuilt_base.iterdir():
        if not repo_dir.is_dir():
            continue
        # __egg_system_dirs__ held system-level installs (e.g. /usr/local/go)
        # in pre-#2999 images; they now persist to /opt/egg-system-dirs (outside
        # this tree) and are restored by the Dockerfile either way. Keep the
        # skip so older images don't get system dirs restored into a repo.
        if repo_dir.name == "__egg_system_dirs__":
            continue
        # repo_dir is like /opt/prebuilt-deps/owner--repo
        # Convert back to repo name to find mount point
        # Try each mounted repo to find a match
        repo_dir_name = repo_dir.name  # e.g. "owner--repo"

        # Find the matching mounted repo directory
        target_repo = None
        for mounted in repos_dir.iterdir():
            if not mounted.is_dir():
                continue
            # The repo mount name is typically just the repo name (e.g. "webapp")
            # Match by checking if repo_dir_name ends with --<mount_name>
            if repo_dir_name.endswith(f"--{mounted.name}"):
                target_repo = mounted
                break

        if target_repo is None:
            logger.warn(f"No mounted repo found for prebuilt deps: {repo_dir_name}")
            continue

        # Copy prebuilt tree into repo, skipping files that already exist.
        # We use symlinks=False so that file-level entries (including file
        # symlinks) go through copy_function, where we can skip existing
        # files and handle symlinks manually. Directory symlinks are followed
        # and expanded into real directories — this is acceptable since
        # Node.js module resolution works the same either way.
        # Note: the persist side uses symlinks=True (preserving symlinks),
        # so directory symlinks become real directories after restore. This
        # increases disk usage slightly but avoids copytree's non-idempotent
        # os.symlink() calls which raise FileExistsError on repeat runs.
        def _copy_if_missing(src: str, dst: str, **kwargs: Any) -> None:
            if os.path.exists(dst) or os.path.islink(dst):
                return
            try:
                if os.path.islink(src):
                    linkto = os.readlink(src)
                    os.symlink(linkto, dst)
                else:
                    shutil.copy2(src, dst, **kwargs)
            except OSError as e:
                logger.warn(f"  Failed to restore {dst}: {e}")

        try:
            shutil.copytree(
                repo_dir,
                target_repo,
                copy_function=_copy_if_missing,
                dirs_exist_ok=True,
                symlinks=False,
            )
        except shutil.Error as e:
            logger.warn(f"  Some files could not be restored for {repo_dir_name}: {e}")

        # Fix ownership of restored files. The copytree runs as root so
        # restored files are root-owned, but the container agent runs as
        # the runtime user.  Only chown the top-level subdirectories that
        # came from the prebuilt snapshot (e.g. .venv, node_modules) to
        # avoid an expensive recursive chown of the entire repo.
        for prebuilt_subdir in repo_dir.iterdir():
            if not prebuilt_subdir.is_dir():
                continue
            restored_path = target_repo / prebuilt_subdir.name
            if restored_path.exists():
                try:
                    chown_recursive(restored_path, config.runtime_uid, config.runtime_gid)
                except subprocess.CalledProcessError as e:
                    logger.warn(f"  Failed to chown {restored_path}: {e.stderr}")

        restored += 1
        logger.info(f"  Restored prebuilt deps for {repo_dir_name} -> {target_repo}")

    if restored:
        logger.success(f"Restored prebuilt dependencies for {restored} repo(s)")


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
