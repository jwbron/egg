"""Cross-process serialization for git operations on a shared bare repo.

The gateway pod and orchestrator pod both run git against the same shared
bare repo at ``/home/egg/repos/<repo>/.git`` (the same hostPath is mounted
into each pod).  A ``threading.Lock`` only synchronises calls inside one
process — nothing stops the gateway's ``git worktree add`` from racing the
orchestrator's state-store commit on ``.git/config.lock`` (#2311).

This module wraps that file with an ``fcntl.flock`` so both processes
serialise on the same inode.  The lock file lives at
``<repo_path>/.git/.egg-cross-process.lock`` — inside the resource being
protected, on the shared mount, so any process that can run git against
the repo can also see and acquire the lock.

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


def lock_path_for_repo(repo_path: Path | str) -> Path:
    """Return the cross-process lock path for ``repo_path``.

    The path is ``<repo_path>/.git/<LOCK_FILENAME>``.  ``repo_path`` must
    be the main repo (with ``.git`` as a directory).  Worktree paths
    (where ``.git`` is a file) are not valid arguments — callers should
    resolve to the main repo first.
    """
    return Path(repo_path) / ".git" / LOCK_FILENAME


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
    key = str(repo_path)
    with _state_lock:
        state = _per_repo_state.get(key)
        if state is None:
            lock_path = lock_path_for_repo(repo_path)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
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
