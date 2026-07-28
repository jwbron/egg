"""In-pod working-tree checkpoint at the session boundary (#3658).

When the wall-clock budget expires the SDK call is cancelled and the CLI
returns; before this, nothing committed, stashed, or flushed the tree on the way
out. #3644 makes the *next* respawn survivable, but the snapshot it takes is a
different process's, minutes later, with no marker of where the agent stopped.

These tests drive the real ``git`` against real temp repos: the value of this
helper is entirely in what it does to a git tree under adverse conditions, and a
mocked ``subprocess`` would pin the calls rather than the outcome.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from egg_agent.checkpoint import CHECKPOINT_ENV, checkpoint_working_tree


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo with one commit, so HEAD exists and diffs are meaningful."""
    _git("init", "-b", "main", cwd=tmp_path)
    _git("config", "user.name", "test", cwd=tmp_path)
    _git("config", "user.email", "test@localhost", cwd=tmp_path)
    (tmp_path / "seed.txt").write_text("seed\n")
    _git("add", "seed.txt", cwd=tmp_path)
    _git("commit", "-m", "seed", cwd=tmp_path)
    return tmp_path


def _head_message(repo: Path) -> str:
    return _git("log", "-1", "--pretty=%B", cwd=repo).stdout


def _head_author(repo: Path) -> str:
    return _git("log", "-1", "--pretty=%an <%ae>", cwd=repo).stdout.strip()


def test_commits_modified_and_untracked_work(repo: Path):
    (repo / "seed.txt").write_text("edited mid-turn\n")
    (repo / "new_module.py").write_text("# half-written\n")

    sha = checkpoint_working_tree(repo)

    assert sha and len(sha) == 40
    assert _git("status", "--porcelain", cwd=repo).stdout.strip() == ""
    tracked = _git("ls-tree", "-r", "--name-only", "HEAD", cwd=repo).stdout.split()
    assert "new_module.py" in tracked


def test_snapshot_carries_the_salvage_marker_and_identity(repo: Path):
    """One ``[salvage]`` grep must find every machine-made tree snapshot."""
    (repo / "seed.txt").write_text("edited\n")

    checkpoint_working_tree(repo)

    message = _head_message(repo)
    assert message.startswith("[salvage]")
    assert "#3658" in message
    # The message has to warn a reader that this is mid-edit content, not a
    # considered commit — otherwise the next agent builds on it as if it were.
    assert "machine commit" in message
    assert _head_author(repo) == "egg-salvage <egg-salvage@localhost>"


def test_clean_tree_is_a_no_op_not_a_failure(repo: Path):
    """The good case: an agent that committed before the deadline."""
    before = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    assert checkpoint_working_tree(repo) is None

    assert _git("rev-parse", "HEAD", cwd=repo).stdout.strip() == before


def test_non_repo_path_returns_none(tmp_path: Path):
    assert checkpoint_working_tree(tmp_path / "not-a-repo") is None


def test_disabled_leaves_the_tree_untouched(repo: Path, monkeypatch):
    (repo / "seed.txt").write_text("edited\n")
    monkeypatch.setenv(CHECKPOINT_ENV, "false")

    assert checkpoint_working_tree(repo) is None

    assert _git("status", "--porcelain", cwd=repo).stdout.strip() != ""


def test_resolves_the_repo_from_egg_repo_path(repo: Path, monkeypatch):
    """The pod sets EGG_REPO_PATH; the CLI calls this with no argument."""
    (repo / "seed.txt").write_text("edited\n")
    monkeypatch.setenv("EGG_REPO_PATH", str(repo))

    assert checkpoint_working_tree() is not None
    assert _git("status", "--porcelain", cwd=repo).stdout.strip() == ""


def test_commits_despite_a_repo_pre_commit_hook_that_fails(repo: Path):
    """A hook must not be able to veto the snapshot — that loses the work."""
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    (repo / "seed.txt").write_text("edited\n")

    assert checkpoint_working_tree(repo) is not None


def test_commits_despite_gpgsign_with_no_key(repo: Path):
    """A worktree inheriting commit.gpgsign=true must still checkpoint."""
    _git("config", "commit.gpgsign", "true", cwd=repo)
    _git("config", "user.signingkey", "0xNOSUCHKEY", cwd=repo)
    (repo / "seed.txt").write_text("edited\n")

    assert checkpoint_working_tree(repo) is not None


def test_never_raises_on_a_hostile_worktree(repo: Path, monkeypatch):
    """The contract is 'never raises' — an exit code must stay classifiable."""

    def _boom(*_a, **_kw):
        raise OSError("git is not on PATH")

    monkeypatch.setattr(subprocess, "run", _boom)

    assert checkpoint_working_tree(repo) is None
