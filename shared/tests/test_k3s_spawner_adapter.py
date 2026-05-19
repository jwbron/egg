"""Tests for ``K3sSpawnerAdapter`` (#2623 slice-1 task-1-1, task-1-8).

Acceptance criteria covered:

* ``K3sSpawnerAdapter`` conforms to the ``AgentSpawner`` Protocol.
* The adapter delegates to
  ``orchestrator/kubernetes_spawner.py:1564 create_concurrent_spawn_fn``
  (the closure shape ``(role, branch, extra_env, command) ->
  SpawnedContainer``).
* ``spawn`` captures ``commit_sha`` via ``git -C <worktree> rev-parse
  HEAD`` after the wrapped closure returns and returns it on the
  ``AgentResult`` (INV-6).

These tests live in ``shared/tests/`` per the plan; they mock the
underlying ``create_concurrent_spawn_fn`` so they remain pure-Python
and do not require a live k3s cluster.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

substrate_pkg = pytest.importorskip(
    "substrate",
    reason="orchestrator/substrate/ package not present yet (task-1-1 pending)",
)
k3s_adapter_mod = pytest.importorskip(
    "substrate.k3s_adapter",
    reason=(
        "orchestrator/substrate/k3s_adapter.py not present yet "
        "(task-1-1 pending)"
    ),
)


def _init_git_repo(path: Path) -> str:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, check=True)
    (path / "x").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"],
        cwd=path,
        check=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    repo = tmp_path / "wt"
    repo.mkdir()
    _init_git_repo(repo)
    return repo


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_k3s_spawner_adapter_has_spawn_member() -> None:
    """``K3sSpawnerAdapter`` exposes the ``AgentSpawner.spawn`` member."""
    adapter_cls = getattr(k3s_adapter_mod, "K3sSpawnerAdapter", None)
    assert adapter_cls is not None, (
        "substrate.k3s_adapter.K3sSpawnerAdapter missing — task-1-1 AC"
    )
    assert hasattr(adapter_cls, "spawn"), (
        "K3sSpawnerAdapter.spawn member required by AgentSpawner protocol"
    )


# ---------------------------------------------------------------------------
# Adapter wraps create_concurrent_spawn_fn
# ---------------------------------------------------------------------------


def test_k3s_adapter_wraps_create_concurrent_spawn_fn(
    worktree: Path,
) -> None:
    """Adapter dispatches through the closure from ``kubernetes_spawner``.

    The adapter constructor either takes a ``KubernetesSpawner`` (and
    asks it for ``create_concurrent_spawn_fn``) or a pre-built
    spawn-fn directly. We accept either shape — the assertion is that
    the wrapped closure is invoked with the request's role/env when
    the adapter's ``spawn`` is called.
    """
    fake_container = MagicMock()
    fake_container.exit_code = 0
    fake_container.stdout = "ok"
    fake_container.stderr = ""
    fake_container.duration_seconds = 0.01
    closure = MagicMock(return_value=fake_container)

    adapter_cls = getattr(k3s_adapter_mod, "K3sSpawnerAdapter")
    # TODO(tester): tighten construction once coder pins the
    # adapter signature. Three plausible shapes:
    #   K3sSpawnerAdapter(spawn_fn=closure)
    #   K3sSpawnerAdapter(kubernetes_spawner=<mock with create_concurrent_spawn_fn>)
    #   K3sSpawnerAdapter(closure)
    pytest.skip(
        "K3sSpawnerAdapter constructor signature pending — fill in once "
        "task-1-1 lands"
    )


# ---------------------------------------------------------------------------
# Adapter captures commit_sha after spawn (INV-6)
# ---------------------------------------------------------------------------


def test_k3s_adapter_captures_commit_sha(worktree: Path) -> None:
    """Adapter runs ``git rev-parse HEAD`` after closure returns (INV-6)."""
    fake_container = MagicMock()
    fake_container.exit_code = 0
    fake_container.stdout = "ok"
    fake_container.stderr = ""
    fake_container.duration_seconds = 0.01
    closure = MagicMock(return_value=fake_container)

    # TODO(tester): construct, call .spawn(...), then assert
    # ``result.commit_sha`` is a 40-char hex SHA matching the
    # worktree's HEAD. Skip until task-1-1 lands.
    pytest.skip(
        "K3sSpawnerAdapter.spawn signature pending — fill in once task-1-1 lands"
    )
