"""User/UID/GID and repo-permission setup."""

from __future__ import annotations

import os
import subprocess
import time

from ._config import Config, Logger
from ._core import chown_recursive, run_cmd


def _resolve_gid_conflict(target_gid: int, container_user: str, logger: Logger) -> None:
    """Rename any existing group that holds target_gid (if it's not our group).

    On macOS, os.getgid() returns 20 ("staff"). Inside the Ubuntu container,
    GID 20 belongs to "dialout". When groupmod tries to assign GID 20 to the
    egg group, it fails because that GID is already taken.

    Fix: rename the conflicting group out of the way first.
    """
    import grp

    try:
        existing = grp.getgrnam(container_user)
        if existing.gr_gid == target_gid:
            return  # Already owns it
    except KeyError:
        pass

    try:
        conflicting = grp.getgrgid(target_gid)
    except KeyError:
        return  # No group holds this GID

    if conflicting.gr_name == container_user:
        return  # Already ours

    # Pick a rename target that doesn't already exist
    new_name = f"_orig_{conflicting.gr_name}"
    try:
        grp.getgrnam(new_name)
        # Name taken — append GID to disambiguate
        new_name = f"_orig_{conflicting.gr_name}_{conflicting.gr_gid}"
    except KeyError:
        pass  # Name is free

    logger.info(
        f"GID {target_gid} is held by '{conflicting.gr_name}', "
        f"renaming to '{new_name}' to avoid conflict"
    )
    try:
        run_cmd(["groupmod", "-n", new_name, conflicting.gr_name])
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Failed to resolve GID conflict: could not rename group "
            f"'{conflicting.gr_name}' (GID {target_gid}) to '{new_name}'. "
            f"groupmod exited with code {e.returncode}. "
            f"Manually reassign GID {target_gid} inside the container to fix this."
        ) from e


def _find_free_uid(start: int) -> int:
    """Find a free UID starting from ``start``, bounded to 100 attempts.

    Raises RuntimeError if no free UID is found within range.
    """
    import pwd

    uid = start
    for _ in range(100):
        try:
            pwd.getpwuid(uid)
            uid += 1
        except KeyError:
            return uid
    raise RuntimeError(f"No free UID found starting from {start}")


def _resolve_uid_conflict(target_uid: int, container_user: str, logger: Logger) -> None:
    """Reassign any existing user that holds target_uid to a high UID.

    Similar to the GID conflict: if the target UID is already taken by a
    different user, move that user out of the way first.
    """
    import pwd

    try:
        existing = pwd.getpwnam(container_user)
        if existing.pw_uid == target_uid:
            return  # Already owns it
    except KeyError:
        pass

    try:
        conflicting = pwd.getpwuid(target_uid)
    except KeyError:
        return  # No user holds this UID

    if conflicting.pw_name == container_user:
        return  # Already ours

    high_uid = _find_free_uid(60000 + target_uid)
    logger.info(
        f"UID {target_uid} is held by '{conflicting.pw_name}', "
        f"reassigning to UID {high_uid} to avoid conflict"
    )
    try:
        run_cmd(["usermod", "-u", str(high_uid), conflicting.pw_name])
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Failed to resolve UID conflict: could not reassign user "
            f"'{conflicting.pw_name}' (UID {target_uid}) to UID {high_uid}. "
            f"usermod exited with code {e.returncode}. "
            f"Manually reassign UID {target_uid} inside the container to fix this."
        ) from e


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

    # Resolve conflicts before adjusting (e.g. macOS GID 20 = "dialout" in Ubuntu)
    if current_gid != config.runtime_gid:
        _resolve_gid_conflict(config.runtime_gid, config.container_user, logger)
    if current_uid != config.runtime_uid:
        _resolve_uid_conflict(config.runtime_uid, config.container_user, logger)

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
