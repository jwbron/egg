"""Tests for ``ClaudeCodeSpawner`` (#2623 slice-1 task-1-2, task-1-8).

Acceptance criteria covered:

* ``ClaudeCodeSpawner`` conforms to the ``AgentSpawner`` Protocol
  (``isinstance`` check via runtime-checkable protocol or duck-typing
  on the ``spawn`` member).
* ``spawn(...)`` returns an ``AgentResult`` whose ``commit_sha`` field
  is a 40-char hex SHA captured from ``git -C <worktree> rev-parse
  HEAD`` immediately after the subagent returns.
* ``build_system_prompt`` (``shared/egg_harness/prompt.py:24``) is
  invoked with the role's ``PromptSource`` list — the depth-gap fix
  from #2622 is structural here, not delegated to the runner.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

substrate_pkg = pytest.importorskip(
    "substrate",
    reason="orchestrator/substrate/ package not present yet (task-1-1 pending)",
)
claude_code_pkg = pytest.importorskip(
    "substrate.claude_code",
    reason="orchestrator/substrate/claude_code/ package not present yet (task-1-2 pending)",
)


def _init_git_repo(path: Path) -> str:
    """Create a one-commit repo under ``path`` and return its HEAD SHA."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, check=True)
    (path / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"],
        cwd=path,
        check=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return sha


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    """A throwaway git repo so ``rev-parse HEAD`` returns a real SHA."""
    repo = tmp_path / "wt"
    repo.mkdir()
    _init_git_repo(repo)
    return repo


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_claude_code_spawner_conforms_to_agent_spawner_protocol() -> None:
    """``ClaudeCodeSpawner`` exposes the ``AgentSpawner.spawn`` member.

    Either via ``isinstance`` against a runtime-checkable Protocol or
    via duck-typing on the method name. The latter is the lower bar;
    the test accepts either.
    """
    spawner_cls = getattr(claude_code_pkg, "ClaudeCodeSpawner", None)
    assert spawner_cls is not None, (
        "substrate.claude_code.ClaudeCodeSpawner missing — task-1-2 AC"
    )
    assert hasattr(spawner_cls, "spawn"), (
        "ClaudeCodeSpawner.spawn member required by AgentSpawner protocol"
    )


# ---------------------------------------------------------------------------
# commit_sha capture (INV-6, task-1-2 AC)
# ---------------------------------------------------------------------------


def test_spawn_captures_commit_sha_from_worktree(
    worktree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``spawn`` runs ``git rev-parse HEAD`` and records the 40-char SHA."""
    spawner_cls = getattr(claude_code_pkg, "ClaudeCodeSpawner")
    spawner = spawner_cls()  # default-construct — DI shape TBD by coder

    # TODO(tester): once the coder pins the Agent-tool dispatch shim's
    # symbol (likely ``_dispatch_subagent`` or similar), patch it here
    # to return a deterministic stdout / exit_code without spawning a
    # real subagent. Skip until the symbol stabilizes.
    pytest.skip(
        "ClaudeCodeSpawner internal dispatch shim symbol pending — "
        "fill in once task-1-2 lands"
    )


# ---------------------------------------------------------------------------
# build_system_prompt is invoked with the role's PromptSource list (#2622 fix)
# ---------------------------------------------------------------------------


def test_spawn_invokes_build_system_prompt() -> None:
    """``build_system_prompt`` is called by the spawner — structural depth fix."""
    spawner_cls = getattr(claude_code_pkg, "ClaudeCodeSpawner")
    spawner = spawner_cls()

    # TODO(tester): once task-1-2 commits, patch
    # ``shared.egg_harness.prompt.build_system_prompt`` and assert it
    # was called with the role's PromptSource list. The exact import
    # path inside the spawner module is what gets patched (the spawner
    # imports the function — we patch the spawner's binding, not the
    # source module).
    pytest.skip(
        "build_system_prompt call-site verification pending coder commit"
    )
