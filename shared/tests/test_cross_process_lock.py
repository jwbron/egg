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
            time.sleep(0.5)
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

    # The child held the lock for ~0.5s after writing the sentinel; the
    # parent should have waited at least most of that time.  Allow a
    # generous slack for scheduler jitter on busy CI hosts.
    assert wait > 0.2, f"parent acquired lock too quickly ({wait:.3f}s) — flock did not block"
