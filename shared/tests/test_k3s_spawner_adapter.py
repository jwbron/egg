"""Tests for ``K3sSpawnerAdapter`` (#2623 slice-1 task-1-1, task-1-8).

Acceptance criteria covered:

* ``K3sSpawnerAdapter`` conforms to the ``AgentSpawner`` Protocol.
* The adapter delegates to a callable shaped like
  ``orchestrator/kubernetes_spawner.py:1564 create_concurrent_spawn_fn``
  (signature ``(role, branch, extra_env, command) -> SpawnedContainer``).
* ``spawn`` captures ``commit_sha`` via ``git -C <worktree> rev-parse
  HEAD`` after the wrapped closure returns and returns it on the
  ``AgentResult`` (INV-6).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

substrate_pkg = pytest.importorskip(
    "orchestrator.substrate",
    reason="orchestrator/substrate/ package not present yet",
)
adapter_mod = pytest.importorskip(
    "orchestrator.substrate.k3s_adapter",
    reason="orchestrator/substrate/k3s_adapter.py not present yet",
)


def _init_git_repo_or_skip(path: Path) -> str | None:
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
    class _Role:
        value = "coder"

        def __str__(self) -> str:  # pragma: no cover
            return self.value

    return _Role()


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_k3s_spawner_adapter_satisfies_protocol() -> None:
    """``isinstance(adapter, AgentSpawner)`` succeeds."""
    AgentSpawner = substrate_pkg.AgentSpawner
    K3sSpawnerAdapter = adapter_mod.K3sSpawnerAdapter
    adapter = K3sSpawnerAdapter(MagicMock())
    assert isinstance(adapter, AgentSpawner), (
        "K3sSpawnerAdapter must satisfy AgentSpawner Protocol (cq-4 / task-1-1 AC)"
    )


# ---------------------------------------------------------------------------
# Delegates to create_concurrent_spawn_fn-shaped closure
# ---------------------------------------------------------------------------


def test_adapter_invokes_wrapped_closure_with_role_and_env(
    tmp_path: Path,
    fake_role: Any,
) -> None:
    """The adapter routes ``role`` + ``env`` into the wrapped closure."""
    K3sSpawnerAdapter = adapter_mod.K3sSpawnerAdapter
    spawned = MagicMock(stdout="ok", exit_code=0)
    closure = MagicMock(return_value=spawned)
    adapter = K3sSpawnerAdapter(closure)
    adapter.spawn(fake_role, "task body", {"EGG_PIPELINE_ID": "pipeline-test"}, tmp_path)
    assert closure.called
    call_kwargs = closure.call_args.kwargs
    assert call_kwargs.get("role") is fake_role
    extra_env = call_kwargs.get("extra_env") or {}
    assert extra_env.get("EGG_PIPELINE_ID") == "pipeline-test"


def test_adapter_returns_agent_result_with_legacy_fields(
    tmp_path: Path,
    fake_role: Any,
) -> None:
    """``spawn`` returns an ``AgentResult`` populated from the legacy container."""
    AgentResult = substrate_pkg.AgentResult
    K3sSpawnerAdapter = adapter_mod.K3sSpawnerAdapter
    spawned = MagicMock(stdout="container stdout", exit_code=0)
    closure = MagicMock(return_value=spawned)
    adapter = K3sSpawnerAdapter(closure)
    result = adapter.spawn(fake_role, "task", {}, tmp_path)
    assert isinstance(result, AgentResult)
    assert result.stdout == "container stdout"
    assert result.exit_code == 0
    assert result.duration_seconds >= 0.0


# ---------------------------------------------------------------------------
# commit_sha captured from worktree (INV-6)
# ---------------------------------------------------------------------------


def test_adapter_captures_commit_sha_from_worktree(
    tmp_path: Path,
    fake_role: Any,
) -> None:
    """Adapter runs ``git rev-parse HEAD`` after the closure returns (INV-6)."""
    sha = _init_git_repo_or_skip(tmp_path)
    if sha is None:
        pytest.skip("git init blocked")
    K3sSpawnerAdapter = adapter_mod.K3sSpawnerAdapter
    closure = MagicMock(return_value=MagicMock(stdout="", exit_code=0))
    adapter = K3sSpawnerAdapter(closure)
    result = adapter.spawn(fake_role, "x", {}, tmp_path)
    assert result.commit_sha == sha


def test_adapter_commit_sha_none_when_worktree_missing(
    tmp_path: Path,
    fake_role: Any,
) -> None:
    """``commit_sha`` is None when the worktree path doesn't exist."""
    K3sSpawnerAdapter = adapter_mod.K3sSpawnerAdapter
    closure = MagicMock(return_value=MagicMock(stdout="", exit_code=0))
    adapter = K3sSpawnerAdapter(closure)
    nonexistent = tmp_path / "nope"
    result = adapter.spawn(fake_role, "x", {}, nonexistent)
    assert result.commit_sha is None
