"""Tests for ``egg_git.cross_process_lock``.

Covers the per-repo flock primitive that synchronises git operations
between the gateway and orchestrator pods on a shared bare repo (#2311).
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
from egg_git.cross_process_lock import (
    LOCK_FILENAME,
    bare_repo_lock,
    lock_path_for_repo,
    reset_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_state():
    yield
    reset_for_tests()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A directory with an empty ``.git/`` so the lock file can land."""
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_lock_path_lives_inside_dot_git(tmp_path: Path) -> None:
    assert lock_path_for_repo(tmp_path) == tmp_path / ".git" / LOCK_FILENAME


def _make_main_and_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """Build a main-repo / worktree pair laid out the way git would.

    The worktree's ``.git`` is a regular file containing
    ``gitdir: <main>/.git/worktrees/<name>`` — exactly the layout the
    gateway encounters when an agent's worktree path is passed to the
    cross-process lock (#2452).
    """
    main_repo = tmp_path / "main"
    main_dot_git = main_repo / ".git"
    main_dot_git.mkdir(parents=True)
    worktree_admin = main_dot_git / "worktrees" / "wt"
    worktree_admin.mkdir(parents=True)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {worktree_admin}\n")
    return main_repo, worktree


def test_lock_path_for_worktree_resolves_to_main_repo(tmp_path: Path) -> None:
    """A worktree path locks against the main repo's ``.git/`` (#2452).

    Without this, ``bare_repo_lock(worktree)`` would try to ``mkdir``
    inside the worktree's ``.git`` — but ``.git`` is a *file* in a
    worktree, so that fails with ``FileExistsError [Errno 17]`` and
    silently breaks every checkpoint store driven from a worktree path.
    """
    main_repo, worktree = _make_main_and_worktree(tmp_path)
    assert lock_path_for_repo(worktree) == main_repo / ".git" / LOCK_FILENAME


def test_acquiring_lock_via_worktree_path_succeeds(tmp_path: Path) -> None:
    """``bare_repo_lock`` on a worktree path no longer raises EEXIST (#2452)."""
    main_repo, worktree = _make_main_and_worktree(tmp_path)
    expected = main_repo / ".git" / LOCK_FILENAME
    assert not expected.exists()
    with bare_repo_lock(worktree):
        assert expected.exists()


def test_main_and_worktree_share_one_lock(tmp_path: Path) -> None:
    """Same-process callers via main-repo and worktree paths block each other.

    The kernel flock keys on inode, and our state cache keys on the
    resolved main-repo path — so worktree and main-repo callers must
    share one ``_RepoLockState`` (RLock + fd), not two.
    """
    main_repo, worktree = _make_main_and_worktree(tmp_path)

    in_section: list[str] = []
    overlap = threading.Event()

    def hold(path: Path, label: str, duration: float) -> None:
        with bare_repo_lock(path):
            if in_section:
                overlap.set()
            in_section.append(label)
            try:
                time.sleep(duration)
            finally:
                in_section.pop()

    t1 = threading.Thread(target=hold, args=(main_repo, "main", 0.05))
    t2 = threading.Thread(target=hold, args=(worktree, "wt", 0.05))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not overlap.is_set(), "main-repo and worktree callers were not serialised"


def test_acquiring_creates_lock_file(repo: Path) -> None:
    expected = repo / ".git" / LOCK_FILENAME
    assert not expected.exists()
    with bare_repo_lock(repo):
        assert expected.exists()


def test_reentrant_within_process(repo: Path) -> None:
    # Three nested ``with`` blocks must not deadlock — only the outermost
    # actually issues an ``flock`` syscall.
    with bare_repo_lock(repo):
        with bare_repo_lock(repo):
            with bare_repo_lock(repo):
                pass


def test_threads_in_one_process_serialise(repo: Path) -> None:
    in_section: list[bool] = []
    overlap = threading.Event()

    def hold(duration: float) -> None:
        with bare_repo_lock(repo):
            if any(in_section):
                overlap.set()
            in_section.append(True)
            try:
                time.sleep(duration)
            finally:
                in_section.pop()

    threads = [threading.Thread(target=hold, args=(0.05,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not overlap.is_set(), "threads observed each other inside the lock"


def test_serialises_across_processes(tmp_path: Path) -> None:
    """A subprocess holding the lock blocks the parent until it releases.

    Without flock-on-shared-inode (the #2311 fix), the parent would
    proceed immediately and the assertion below would fail by a wide
    margin.
    """
    (tmp_path / ".git").mkdir()

    holder_script = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
        from egg_git.cross_process_lock import bare_repo_lock
        with bare_repo_lock({str(tmp_path)!r}):
            # Signal "lock held" by writing to a sentinel file.
            open({str(tmp_path / "held")!r}, "w").close()
            time.sleep(1.0)
        """
    )

    proc = subprocess.Popen(
        [sys.executable, "-c", holder_script],
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )

    try:
        # Wait until the child has actually acquired the lock.
        deadline = time.monotonic() + 5
        while not (tmp_path / "held").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (tmp_path / "held").exists(), "child never signalled lock acquisition"

        start = time.monotonic()
        with bare_repo_lock(tmp_path):
            wait = time.monotonic() - start
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)

    # The child held the lock for ~1.0s after writing the sentinel; the
    # parent should have waited at least half that time.  The 0.5s slack
    # absorbs the parent's sentinel-detection latency (the polling loop
    # ticks every 10ms) plus scheduler jitter on busy CI hosts.
    assert wait > 0.5, f"parent acquired lock too quickly ({wait:.3f}s) — flock did not block"
