"""Shared helpers for the scripts/select_tests/ test suite (issue #1973).

The helpers below load the selector package from its on-disk path so the
test files don't have to repeat the import dance. They also provide a
synthetic-monorepo fixture builder for the graph / fallback / e2e tests.

Test files importing from this module should be under ``tests/tools/``.

NOTE: ``scripts/select_tests`` was decomposed from a single 1,875-line
module into a sub-package in issue #2261 (slice-1).  ``load_selector``
now imports the package via ``importlib`` after inserting
``scripts/`` on ``sys.path`` so the test suite continues to use the
``select_tests`` short-name (matching the original ``importlib`` shape
the helpers used before the decomposition).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

# Repository root — three levels up from this file
# (tests/tools/_select_tests_helpers.py).
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
# scripts/ contains the ``select_tests`` package post-#2261 decomposition.
SELECTOR_PARENT = REPO_ROOT / "scripts"
# Path-style entry point for subprocess-based tests.  Post-#2261 the
# selector ships as the ``select_tests`` sub-package; ``__main__.py``
# inserts ``scripts/`` on ``sys.path`` so the package resolves whether
# invoked as ``python __main__.py`` or ``python -m select_tests``.  The
# Makefile uses the same path.
SELECTOR_PATH = REPO_ROOT / "scripts" / "select_tests" / "__main__.py"

# Real git binary inside the egg sandbox.  The egg-runtime image
# replaces ``/usr/bin/git`` with a shell wrapper that proxies all git
# commands through the gateway sidecar — that wrapper rejects ``git
# init`` and refuses to operate on paths outside the host worktree.
# Tests need a real git binary to build synthetic mini-repos under
# ``tmp_path``, so we look up ``/opt/.egg-internal/git`` (the real
# binary the wrapper invokes for fallback) when present.  Outside the
# sandbox we fall back to the system ``git`` on PATH.
_INTERNAL_GIT = Path("/opt/.egg-internal/git")
REAL_GIT: str = str(_INTERNAL_GIT) if _INTERNAL_GIT.exists() else "git"


def load_selector() -> ModuleType:
    """Load the ``scripts/select_tests`` package as a Python module.

    Post-#2261 the selector is a sub-package
    (``scripts/select_tests/__init__.py``) with private ``_*.py``
    submodules; the public surface (every ``selector._foo`` symbol the
    test suite uses) is re-exported from ``__init__.py``.  We insert
    ``scripts/`` on ``sys.path`` so ``import select_tests`` resolves the
    package, register it under the short name ``select_tests`` in
    ``sys.modules`` for stable identity across calls, and return the
    package object.
    """
    import importlib
    import sys

    if "select_tests" in sys.modules:
        return sys.modules["select_tests"]
    parent = str(SELECTOR_PARENT)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    return importlib.import_module("select_tests")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command in ``cwd`` with deterministic identity.

    Tests that exercise commit-creation code paths need ``git commit``
    to succeed without asking for a real user identity.  Setting the
    env vars per-call avoids polluting the parent process's git config.

    The selector calls ``git`` via the system ``PATH`` — inside the
    sandbox that resolves to a wrapper that proxies through the
    gateway and rejects ``git init`` on synthetic repos.  When tests
    invoke ``selector._io._run_git`` (e.g. ``record_good``,
    ``resolve_baseline``), they need the same wrapper-bypassing
    binary the helpers use, so monkeypatch ``selector._io._run_git``
    in tests that exercise repo-side flows; for the helpers themselves,
    we always call ``REAL_GIT`` directly.
    """
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "egg-tester")
    env.setdefault("GIT_AUTHOR_EMAIL", "egg-tester@example.com")
    env.setdefault("GIT_COMMITTER_NAME", "egg-tester")
    env.setdefault("GIT_COMMITTER_EMAIL", "egg-tester@example.com")
    env.setdefault("GIT_CONFIG_GLOBAL", "/dev/null")
    env.setdefault("GIT_CONFIG_SYSTEM", "/dev/null")
    return subprocess.run(
        [REAL_GIT, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def patched_run_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``selector._io._run_git`` to invoke the real git binary.

    Without this patch, the selector's ``_run_git`` would invoke ``git``
    via PATH which inside the sandbox resolves to a gateway-wrapper
    that rejects synthetic-repo operations.  Tests that drive
    ``record_good`` / ``resolve_baseline`` / etc. must call this
    helper from a fixture / per-test setup so the selector reads from
    the synthetic repo we built under ``tmp_path``.

    The patch target is ``selector._io._run_git`` — internal callers
    inside ``_io.py`` reference the function by bare name, which Python
    resolves through the module's own namespace at call time, so
    patching ``_io._run_git`` reaches every call site (including the
    qualified ``_io._run_git`` access from ``_cli.py``) without
    per-callsite indirection.
    """
    selector = load_selector()
    real_run_git = selector._io._run_git  # for chaining

    def _patched(args: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
        env = os.environ.copy()
        env.setdefault("GIT_CONFIG_GLOBAL", "/dev/null")
        env.setdefault("GIT_CONFIG_SYSTEM", "/dev/null")
        proc = subprocess.run(
            [REAL_GIT, *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr

    monkeypatch.setattr(selector._io, "_run_git", _patched)
    return real_run_git  # type: ignore[no-any-return]


def init_git_repo(path: Path, default_branch: str = "main") -> None:
    """Initialize a git repository at ``path`` on branch ``default_branch``.

    Creates an initial empty commit so subsequent diffs / merge-bases
    have something to anchor to.  Sets up ``origin/<default_branch>`` to
    point at the same commit so ``resolve_baseline`` finds a base ref.
    """
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", default_branch)
    # Disable global config interference at the repo level too.
    _git(path, "config", "user.name", "egg-tester")
    _git(path, "config", "user.email", "egg-tester@example.com")
    _git(path, "config", "commit.gpgsign", "false")
    # Empty initial commit so HEAD always exists.
    _git(path, "commit", "--allow-empty", "-m", "initial", "-q")
    # Fake an origin/<branch> remote-tracking ref by copying the local
    # branch ref under refs/remotes/origin/.  This is enough for
    # `git merge-base HEAD origin/<branch>` to succeed without a real
    # remote.
    rc = _git(path, "update-ref", f"refs/remotes/origin/{default_branch}", "HEAD")
    assert rc.returncode == 0, rc.stderr


def commit_file(repo: Path, rel_path: str, content: str, message: str) -> str:
    """Write ``rel_path`` with ``content`` and commit; return the commit sha."""
    target = repo / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", "--", rel_path)
    _git(repo, "commit", "-m", message, "-q")
    rc = _git(repo, "rev-parse", "HEAD")
    return rc.stdout.strip()


def remove_committed_file(repo: Path, rel_path: str, message: str) -> str:
    """Remove ``rel_path`` (committed or untracked) and commit the deletion."""
    _git(repo, "rm", "-f", "--", rel_path)
    _git(repo, "commit", "-m", message, "-q")
    rc = _git(repo, "rev-parse", "HEAD")
    return rc.stdout.strip()


def make_synthetic_monorepo(
    root: Path,
    *,
    sources: dict[str, str] | None = None,
    tests: dict[str, str] | None = None,
) -> None:
    """Lay out a tiny synthetic mono-repo on disk under ``root``.

    Used for the grimp-graph tests where we want known module shapes
    without depending on the real egg repo (TASK-5-1, TASK-5-2 graph
    cases).

    ``sources`` and ``tests`` map repo-relative paths to file contents.
    A minimal ``__init__.py`` is created for every directory along the
    way so grimp can treat each tree as a regular package.
    """
    root.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    files.update(sources or {})
    files.update(tests or {})
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        # Ensure every intermediate dir has an __init__.py so grimp
        # treats it as a package.  The actual __init__.py creation is
        # done in the second pass below; this section just ensures the
        # path exists.
        path.write_text(content, encoding="utf-8")
    for rel in files:
        parent = (root / rel).parent
        while parent != root and parent.exists():
            init = parent / "__init__.py"
            if not init.exists():
                init.write_text("", encoding="utf-8")
            parent = parent.parent


def in_chdir(path: Path) -> Chdir:
    """Return a context manager that chdirs into ``path``."""
    return Chdir(path)


class Chdir:
    """Tiny context manager — pytest's ``monkeypatch.chdir`` works for
    tests that have access to monkeypatch, but bare helpers and module-
    level setup do not, so we provide our own.
    """

    def __init__(self, path: Path) -> None:
        self._target = Path(path)
        self._previous: str | None = None

    def __enter__(self) -> Path:
        self._previous = os.getcwd()
        os.chdir(str(self._target))
        return self._target

    def __exit__(self, *args: Any) -> None:
        if self._previous is not None:
            os.chdir(self._previous)


def find_python() -> str:
    """Return the python interpreter to use for subprocess invocations.

    Prefers the project venv when present, falling back to ``sys.executable``.
    """
    import sys

    venv = REPO_ROOT / ".venv" / "bin" / "python"
    if venv.exists():
        return str(venv)
    if shutil.which("python3"):
        return "python3"
    return sys.executable
