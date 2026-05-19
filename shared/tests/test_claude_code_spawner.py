"""Tests for ``ClaudeCodeSpawner`` (#2623 slice-1 task-1-2, task-1-8).

Acceptance criteria covered:

* ``ClaudeCodeSpawner`` conforms to the ``AgentSpawner`` Protocol
  (``isinstance`` check via ``@runtime_checkable``).
* ``spawn(...)`` returns an ``AgentResult`` whose ``commit_sha`` field
  is populated when the worktree contains a git checkout.
* ``build_system_prompt`` (``shared/egg_harness/prompt.py:24``) is
  invoked with the role's ``PromptSource`` list — the depth-gap fix
  from #2622 is structural here, not delegated to the runner.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

substrate_pkg = pytest.importorskip(
    "orchestrator.substrate",
    reason="orchestrator/substrate/ package not present yet",
)
spawner_mod = pytest.importorskip(
    "orchestrator.substrate.claude_code.spawner",
    reason="orchestrator/substrate/claude_code/spawner.py not present yet",
)


def _git_available() -> bool:
    """Return ``True`` when ``git init`` works in this environment."""
    try:
        out = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except OSError:
        return False
    return out.returncode == 0


def _init_git_repo_or_skip(path: Path) -> str | None:
    """Initialise a one-commit repo at ``path`` or skip if blocked."""
    try:
        proc = subprocess.run(
            ["git", "init", "-q", "-b", "main"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError as exc:
        pytest.skip(f"git unavailable: {exc}")
    if proc.returncode != 0:
        pytest.skip(f"git init blocked in this container: {proc.stderr.strip() or proc.stdout!r}")
    for args in (
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "test"],
    ):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def fake_role() -> Any:
    """Return a stand-in object with the AgentRole interface (``.value``)."""

    class _Role:
        value = "refiner"

        def __str__(self) -> str:  # pragma: no cover
            return self.value

    return _Role()


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_claude_code_spawner_conforms_to_agent_spawner_protocol() -> None:
    """``isinstance(spawner, AgentSpawner)`` succeeds (cq-4 AC)."""
    AgentSpawner = substrate_pkg.AgentSpawner
    ClaudeCodeSpawner = spawner_mod.ClaudeCodeSpawner
    spawner = ClaudeCodeSpawner()
    assert isinstance(spawner, AgentSpawner), (
        "ClaudeCodeSpawner must satisfy AgentSpawner Protocol (cq-4 / task-1-2 AC)"
    )


def test_claude_code_spawner_returns_agent_result(
    tmp_path: Path,
    fake_role: Any,
) -> None:
    """``spawn`` returns an ``AgentResult`` even when the harness is mocked."""
    ClaudeCodeSpawner = spawner_mod.ClaudeCodeSpawner
    AgentResult = substrate_pkg.AgentResult
    runner = MagicMock(return_value=MagicMock(stdout="ok", returncode=0))
    spawner = ClaudeCodeSpawner(run_agent_fn=runner)
    # No git init — commit_sha falls back to None, which is allowed.
    result = spawner.spawn(fake_role, "hello", {"X": "1"}, tmp_path)
    assert isinstance(result, AgentResult)
    assert result.stdout == "ok"
    assert result.exit_code == 0
    runner.assert_called_once()


def test_claude_code_spawner_captures_commit_sha_from_worktree(
    tmp_path: Path,
    fake_role: Any,
) -> None:
    """When the worktree is a git checkout, ``commit_sha`` is the 40-char HEAD."""
    sha = _init_git_repo_or_skip(tmp_path)
    if sha is None:
        pytest.skip("git init blocked")
    ClaudeCodeSpawner = spawner_mod.ClaudeCodeSpawner
    runner = MagicMock(return_value=MagicMock(stdout="ok", returncode=0))
    spawner = ClaudeCodeSpawner(run_agent_fn=runner)
    result = spawner.spawn(fake_role, "hello", {}, tmp_path)
    assert result.commit_sha == sha, (
        f"ClaudeCodeSpawner must capture the worktree HEAD; got "
        f"{result.commit_sha!r} expected {sha!r}"
    )


def test_claude_code_spawner_commit_sha_is_full_40_char_hex(
    tmp_path: Path,
    fake_role: Any,
) -> None:
    """The captured commit_sha must be the full 40-char hex SHA, not a short prefix."""
    sha = _init_git_repo_or_skip(tmp_path)
    if sha is None:
        pytest.skip("git init blocked")
    ClaudeCodeSpawner = spawner_mod.ClaudeCodeSpawner
    runner = MagicMock(return_value=MagicMock(stdout="", returncode=0))
    spawner = ClaudeCodeSpawner(run_agent_fn=runner)
    result = spawner.spawn(fake_role, "x", {}, tmp_path)
    assert result.commit_sha is not None
    assert len(result.commit_sha) == 40, (
        f"commit_sha must be 40-char SHA; got len={len(result.commit_sha)}"
    )
    assert all(c in "0123456789abcdef" for c in result.commit_sha.lower())


def test_claude_code_spawner_commit_sha_none_when_no_worktree(
    tmp_path: Path,
    fake_role: Any,
) -> None:
    """commit_sha is None when the worktree path doesn't exist or is empty."""
    ClaudeCodeSpawner = spawner_mod.ClaudeCodeSpawner
    runner = MagicMock(return_value=MagicMock(stdout="", returncode=0))
    spawner = ClaudeCodeSpawner(run_agent_fn=runner)
    non_repo = tmp_path / "empty"
    non_repo.mkdir()
    result = spawner.spawn(fake_role, "x", {}, non_repo)
    assert result.commit_sha is None


# ---------------------------------------------------------------------------
# build_system_prompt is invoked with the role's PromptSource list (#2622 fix)
# ---------------------------------------------------------------------------


def test_spawn_invokes_build_system_prompt(
    tmp_path: Path,
    fake_role: Any,
) -> None:
    """``build_system_prompt`` is called by the spawner — structural depth fix.

    The depth-gap fix from #2622 lives in build_system_prompt; the
    spawner is expected to route through it so the role rubric is
    assembled the same way the existing harness does. Patch the
    spawner module's binding (it imports the symbol) and assert the
    call.
    """
    ClaudeCodeSpawner = spawner_mod.ClaudeCodeSpawner
    runner = MagicMock(return_value=MagicMock(stdout="", returncode=0))

    with patch.object(
        spawner_mod,
        "build_system_prompt",
        wraps=spawner_mod.build_system_prompt,
    ) as wrapped:
        spawner = ClaudeCodeSpawner(
            run_agent_fn=runner,
            role_rubric_loader=lambda role: f"rubric for {role.value}",
        )
        spawner.spawn(fake_role, "task body", {}, tmp_path)
    assert wrapped.called, (
        "ClaudeCodeSpawner.spawn must route prompt assembly through "
        "shared/egg_harness/prompt.py::build_system_prompt (#2622 "
        "structural depth fix)"
    )
    # The call must have been passed a list whose first element is the
    # role rubric — i.e. the spawner does inject the role rubric.
    call_args = wrapped.call_args
    sources = call_args.args[0] if call_args.args else call_args.kwargs.get("sources")
    assert sources, "build_system_prompt called with empty sources"
    assert "refiner" in (sources[0] if isinstance(sources[0], str) else sources[0]())


# ---------------------------------------------------------------------------
# Spawner sets EGG_AGENT_ROLE / EGG_WORKTREE_ROOT on the subagent env
# ---------------------------------------------------------------------------


def test_spawn_threads_egg_agent_role_and_worktree_root_into_env(
    tmp_path: Path,
    fake_role: Any,
) -> None:
    """The spawner injects ``EGG_AGENT_ROLE`` + ``EGG_WORKTREE_ROOT`` into env.

    The PreToolUseHookPolicy reads ``EGG_AGENT_ROLE`` to decide
    whether to enforce write restrictions; without it the hook
    fail-opens. The spawner must therefore ensure the env carries it.
    """
    ClaudeCodeSpawner = spawner_mod.ClaudeCodeSpawner
    runner = MagicMock(return_value=MagicMock(stdout="", returncode=0))
    spawner = ClaudeCodeSpawner(run_agent_fn=runner)
    spawner.spawn(fake_role, "task", {"CALLER_X": "y"}, tmp_path)
    # The runner is the harness shim; the spawner wraps its caller's
    # env with the canonical keys.
    call_kwargs = runner.call_args.kwargs
    env = call_kwargs.get("env", {})
    assert env.get("EGG_AGENT_ROLE") == "refiner"
    assert env.get("EGG_WORKTREE_ROOT") == str(tmp_path)
    assert env.get("CALLER_X") == "y", "Caller env vars must be preserved"
