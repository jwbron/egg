"""Tests for routes.resolve_worktree_repo_path (regression for #1749).

The historical pattern was:

    base_path / repo_name if not (base_path / ".git").exists() else base_path

A stray ``.git`` at ``base_path`` silently flipped meaning and caused
``_auto_create_pr`` to operate on the wrong tree.  The new helper
prefers the named subdirectory and fails fast when neither location is
a git repo.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

# Mock docker before importing routes (which transitively pulls in modules
# that touch docker at import time).
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from routes import resolve_worktree_repo_path


def _make_git_repo(path: Path) -> None:
    """Mark a directory as a git repo by creating its .git directory."""
    (path / ".git").mkdir(parents=True, exist_ok=True)


class TestResolveWorktreeRepoPath:
    def test_prefers_named_subdir_when_both_are_git_repos(self, tmp_path: Path) -> None:
        """Regression for #1749: when both base and base/repo_name contain .git,
        we must pick the named subdir (the historical bug was the reverse)."""
        base = tmp_path
        subdir = base / "egg"
        subdir.mkdir()
        _make_git_repo(base)
        _make_git_repo(subdir)

        result = resolve_worktree_repo_path(base, "egg")
        assert result == subdir

    def test_returns_named_subdir_when_only_subdir_is_git(self, tmp_path: Path) -> None:
        """Common case: parent dir contains many repo subdirs, none of which
        has a .git at the parent level."""
        base = tmp_path
        subdir = base / "egg"
        subdir.mkdir()
        _make_git_repo(subdir)

        assert resolve_worktree_repo_path(base, "egg") == subdir

    def test_falls_back_to_base_when_only_base_is_git(self, tmp_path: Path) -> None:
        """Nested case: EGG_REPO_PATH already points at the repo, the named
        subdir does not exist."""
        base = tmp_path
        _make_git_repo(base)

        assert resolve_worktree_repo_path(base, "egg") == base

    def test_falls_back_to_base_when_subdir_exists_without_git(self, tmp_path: Path) -> None:
        """Edge case: the subdir exists but isn't a git repo (e.g. partial
        clone, leftover state).  Fall back to base if it's a real repo."""
        base = tmp_path
        (base / "egg").mkdir()
        _make_git_repo(base)

        assert resolve_worktree_repo_path(base, "egg") == base

    def test_raises_when_neither_is_git_repo(self, tmp_path: Path) -> None:
        """Failing fast with a specific error beats silently resolving to
        the wrong path."""
        base = tmp_path
        (base / "egg").mkdir()  # subdir exists but no .git anywhere

        with pytest.raises(RuntimeError, match="neither a git repo"):
            resolve_worktree_repo_path(base, "egg")

    def test_raises_when_repo_name_is_empty_and_base_is_not_git(self, tmp_path: Path) -> None:
        """Empty repo_name guards against ``"" .split("/")[-1]`` callers
        without silently mis-resolving — base must still be a git repo."""
        base = tmp_path
        with pytest.raises(RuntimeError, match="neither a git repo"):
            resolve_worktree_repo_path(base, "")

    def test_returns_base_when_repo_name_is_empty_and_base_is_git(self, tmp_path: Path) -> None:
        """Empty repo_name is fine if base itself is the repo."""
        base = tmp_path
        _make_git_repo(base)
        assert resolve_worktree_repo_path(base, "") == base
