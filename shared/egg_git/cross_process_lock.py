"""Cross-process serialization for git operations on a shared bare repo.

The gateway pod and orchestrator pod both run git against the same shared
bare repo at ``/home/egg/repos/<repo>/.git`` (the same hostPath is mounted
into each pod).  A ``threading.Lock`` only synchronises calls inside one
process — nothing stops the gateway's ``git worktree add`` from racing the
orchestrator's state-store commit on ``.git/config.lock`` (#2311).

This module wraps that file with an ``fcntl.flock`` so both processes
serialise on the same inode.  The lock file lives at
``<main_repo>/.git/.egg-cross-process.lock`` — inside the resource being
protected, on the shared mount, so any process that can run git against
the repo can also see and acquire the lock.

Worktree paths (where ``.git`` is a file pointing at
``<main>/.git/worktrees/<name>``) are accepted and silently collapsed
onto the underlying main repo, so all worktrees of the same repo share
one lock — the racing resource is the main repo's
``.git/config.lock``, regardless of which worktree triggered the git
operation (#2452).

Reentrancy: nested ``with bare_repo_lock(repo)`` blocks in the same
process do not block — the inner acquisitions just bump a depth counter.
This matches the orchestrator state-store's existing pattern, where a
compound operation (e.g. ``add → commit``) holds the lock across several
inner ``_run_git`` calls.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import threading
from collections.abc import Generator
from pathlib import Path
from typing import Final

LOCK_FILENAME: Final = ".egg-cross-process.lock"


def _resolve_main_repo(repo_path: Path) -> Path:
    """Resolve a worktree path to its main repo path.

    A worktree's ``.git`` is a *file* whose contents are
    ``gitdir: <main>/.git/worktrees/<name>``.  The main repo is the
    grand-grandparent of that admin dir.  This lets ``bare_repo_lock``
    accept either a main-repo path or any of its worktrees and end up
    flocking the same inode under the main repo's ``.git/`` — which is
    exactly the cross-process serialisation we want, because all
    worktrees share the same ``.git/config.lock`` (#2452).

    Returns ``repo_path`` unchanged if it is already a main repo
    (``.git`` is a directory) or if the ``.git`` file cannot be parsed.
    The latter is intentional — the subsequent ``mkdir`` will fail with
    a clear error rather than silently locking the wrong inode.

    Assumes the standard ``git worktree add`` layout where ``gitdir:``
    points at ``<main>/.git/worktrees/<name>``.  A repo created with
    ``git init --separate-git-dir`` produces a ``.git`` file pointing
    outside ``<main>/.git/``, so this helper falls through and returns
    ``repo_path`` unchanged — fine for the egg gateway (which never uses
    ``--separate-git-dir``), but a future caller in that context should
    not expect resolution to succeed.
    """
    git = repo_path / ".git"
    if not git.is_file():
        return repo_path
    try:
        content = git.read_text().strip()
    except OSError:
        return repo_path
    if not content.startswith("gitdir:"):
        return repo_path
    gitdir = Path(content.split("gitdir:", 1)[1].strip())
    if not gitdir.is_absolute():
        gitdir = (repo_path / gitdir).resolve()
    # gitdir = <main>/.git/worktrees/<name>; main = gitdir.parent.parent.parent
    if gitdir.parent.name == "worktrees" and gitdir.parent.parent.name == ".git":
        return gitdir.parent.parent.parent
    return repo_path


def lock_path_for_repo(repo_path: Path | str) -> Path:
    """Return the cross-process lock path for ``repo_path``.

    The path is ``<main_repo>/.git/<LOCK_FILENAME>``.  Both main-repo
    paths (with ``.git`` as a directory) and worktree paths (where
    ``.git`` is a file) are accepted; worktrees are resolved to the
    underlying main repo so all worktrees of the same repo lock against
    the same inode.
    """
    return _resolve_main_repo(Path(repo_path)) / ".git" / LOCK_FILENAME


class _RepoLockState:
    __slots__ = ("rlock", "fd", "depth")

    def __init__(self, fd: int) -> None:
        self.rlock = threading.RLock()
        self.fd = fd
        # Nesting depth, only mutated while ``rlock`` is held.
        self.depth = 0


# Per-process state, keyed by stringified repo path.  Each entry owns a
# long-lived file descriptor on the lock file plus a reentrant thread
# lock so multiple threads in this process queue locally rather than all
# bouncing on the kernel's flock queue.
_state_lock = threading.Lock()
_per_repo_state: dict[str, _RepoLockState] = {}


def _get_state(repo_path: Path) -> _RepoLockState:
    # Worktree paths (.git is a file) collapse onto their main repo so
    # every worktree of the same repo shares one lock — the kernel
    # flock is inode-keyed, and the main repo's ``.git/config.lock`` is
    # what we are actually racing.  Without this, ``mkdir`` below would
    # fail with EEXIST because a worktree's ``.git`` is a regular file
    # and ``mkdir(exist_ok=True)`` only ignores existing *directories*
    # (#2452).
    repo_path = _resolve_main_repo(repo_path)
    # Resolve so two equivalent path forms (abs vs rel, with/without
    # trailing slash, symlinks) hit the same cache entry.  flock keys
    # on the inode regardless, but a single fd avoids redundant state
    # and a self-deadlock risk if a future caller relies on RLock
    # reentrancy across forms.
    try:
        key = str(repo_path.resolve())
    except OSError:
        key = str(repo_path)
    with _state_lock:
        state = _per_repo_state.get(key)
        if state is None:
            lock_path = lock_path_for_repo(repo_path)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            # O_CLOEXEC: prevent inheritance into child git subprocesses.
            # flock(2) locks are tied to the open file description and are
            # inherited across fork+exec; without close-on-exec, a stuck
            # git child would keep the parent's flock held until it exits.
            fd = os.open(
                str(lock_path),
                os.O_CREAT | os.O_RDWR | os.O_CLOEXEC,
                0o644,
            )
            state = _RepoLockState(fd=fd)
            _per_repo_state[key] = state
        return state


@contextlib.contextmanager
def bare_repo_lock(repo_path: Path | str) -> Generator[None]:
    """Hold the cross-process bare-repo lock for ``repo_path``.

    Acquires, in order, a reentrant in-process thread lock and an
    ``fcntl.flock`` ``LOCK_EX`` on a sentinel file under ``.git/``.
    Releases both on exit.  Reentrant within a single process via a
    depth counter — only the outermost caller actually issues the
    ``flock`` syscall.
    """
    state = _get_state(Path(repo_path))
    state.rlock.acquire()
    try:
        if state.depth == 0:
            fcntl.flock(state.fd, fcntl.LOCK_EX)
        state.depth += 1
        try:
            yield
        finally:
            state.depth -= 1
            if state.depth == 0:
                fcntl.flock(state.fd, fcntl.LOCK_UN)
    finally:
        state.rlock.release()


def reset_for_tests() -> None:
    """Drop the per-repo state cache.  Test-only.

    Closes every cached file descriptor and clears the registry so a
    fresh test run starts from a clean slate.  Calling this while a
    lock is held in another thread is undefined behaviour.
    """
    with _state_lock:
        for state in _per_repo_state.values():
            try:
                os.close(state.fd)
            except OSError:
                pass
        _per_repo_state.clear()
