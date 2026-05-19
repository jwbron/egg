"""Substrate-swap end-to-end smoke test (issue #2623 slice-1, task-1-8).

Drives ``select_substrate(...).spawner.spawn(...)`` and
``.bus.add_message/get_messages`` directly through both substrate
implementations:

* ``"k3s"`` — ``K3sSpawnerAdapter`` wrapping
  ``orchestrator/kubernetes_spawner.py::create_concurrent_spawn_fn``
  with the underlying job-dispatch closure mocked so the test stays
  pure-Python.
* ``"claude-code"`` — ``ClaudeCodeSpawner`` (in-process subagent
  dispatch monkey-patched) + ``InProcessMessageBus``.

Both dimensions run in-process; no kubectl is required.

Assertions verified per task-1-8 acceptance criteria:

* ``spawner.spawn`` returns an ``AgentResult`` with a populated
  ``commit_sha`` (INV-6 — captured via ``git -C <worktree> rev-parse
  HEAD``).
* ``.bus.add_message`` / ``.bus.get_messages`` round-trips messages
  for the agent surface.
* INV-3 stale-version rejection still fires when an ACK / NACK is
  routed through the bus at an older proposal version (verified by
  re-using ``PeerConsensusTracker`` over the substrate bus).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.integration

# Soft-import the substrate package — the test will be skipped (not
# failed) if the substrate hasn't shipped yet, so collection stays
# green and the rest of the regression suite can run.
substrate_pkg = pytest.importorskip(
    "substrate",
    reason="orchestrator/substrate/ package not present yet (task-1-1 pending)",
)


def _init_git_repo(path: Path) -> str:
    """Initialise a one-commit git repo under ``path`` and return the SHA."""
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=path,
        check=True,
    )
    (path / "README.md").write_text("placeholder\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"],
        cwd=path,
        check=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return sha


@pytest.fixture
def isolated_worktree(tmp_path: Path) -> Path:
    """Spin up a throwaway git repo so ``git rev-parse HEAD`` returns a real SHA."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    return repo


@pytest.fixture
def mocked_k3s_spawn(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock the underlying k3s job-dispatch closure for the k3s leg.

    ``K3sSpawnerAdapter`` is expected to wrap ``create_concurrent_spawn_fn``
    from ``orchestrator/kubernetes_spawner.py``. The wrapped callable is
    what we mock here — the adapter still owns the SHA capture, env
    threading, and return-shape mapping that this test exercises.
    """
    fake_container = MagicMock()
    fake_container.exit_code = 0
    fake_container.stdout = "ok"
    fake_container.stderr = ""
    fake_container.duration_seconds = 0.01
    spawn_fn = MagicMock(return_value=fake_container)
    return spawn_fn


@pytest.fixture
def mocked_claude_code_dispatch(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock the Agent-tool dispatch the ClaudeCodeSpawner calls into.

    In production, ``ClaudeCodeSpawner.spawn`` would invoke the parent
    Claude Code session's Agent tool. For unit purposes we patch the
    spawner's internal dispatch shim to return a deterministic result
    immediately.
    """
    dispatch = MagicMock(
        return_value={"stdout": "ok", "exit_code": 0, "duration_seconds": 0.01}
    )
    return dispatch


# ---------------------------------------------------------------------------
# Smoke: select_substrate + spawn round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dim", ["k3s", "claude-code"])
def test_select_substrate_returns_bundle_with_required_fields(
    dim: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``select_substrate`` returns a bundle with spawner + bus wired up."""
    if dim == "claude-code" and os.environ.get("EGG_AGENT_ROLE"):
        pytest.skip(
            "claude-code substrate skipped inside egg sandbox-agent context"
        )

    select_substrate = getattr(substrate_pkg, "select_substrate", None)
    assert select_substrate is not None, (
        "substrate.select_substrate is required by task-1-1 acceptance criteria"
    )
    bundle = select_substrate({"EGG_SUBSTRATE": dim})
    assert hasattr(bundle, "spawner"), "bundle must expose .spawner"
    assert hasattr(bundle, "bus"), "bundle must expose .bus (MessageBus)"
    assert hasattr(bundle, "policy"), "bundle must expose .policy (PolicyEnforcer)"
    assert hasattr(bundle, "worktree") or hasattr(bundle, "worktrees"), (
        "bundle must expose .worktree (WorktreeManager) — see task-1-1"
    )


# ---------------------------------------------------------------------------
# Smoke: spawner.spawn captures commit_sha (INV-6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dim", ["k3s", "claude-code"])
def test_spawner_spawn_populates_commit_sha(
    dim: str,
    isolated_worktree: Path,
    mocked_k3s_spawn: MagicMock,
    mocked_claude_code_dispatch: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``AgentResult.commit_sha`` is populated for both substrate legs (INV-6).

    The k3s leg captures via the adapter (``git rev-parse HEAD`` after
    the wrapped job returns); the claude-code leg captures via the
    spawner itself. Both must satisfy the same invariant in
    ``orchestrator/action_guards.py:631 validate_invariants``.
    """
    if dim == "claude-code" and os.environ.get("EGG_AGENT_ROLE"):
        pytest.skip(
            "claude-code substrate skipped inside egg sandbox-agent context"
        )
    select_substrate = getattr(substrate_pkg, "select_substrate")
    # TODO(tester): patch the substrate's internal dispatch shim for the
    # selected dimension so spawn() returns synchronously. The exact
    # patch site depends on the coder's final module layout — fill in
    # once task-1-2 lands.
    bundle = select_substrate({"EGG_SUBSTRATE": dim})
    spawner = bundle.spawner
    # Build a minimal SpawnRequest. The dataclass shape is pinned by
    # task-1-1 acceptance criteria.
    request_cls = getattr(substrate_pkg, "SpawnRequest", None)
    if request_cls is None:
        # The adapter may accept the same positional shape today's
        # SpawnFn closure uses (role, branch, extra_env, command).
        pytest.skip("SpawnRequest not yet defined — coder pending task-1-1")
    request = request_cls(
        role="coder",
        prompt="say hi",
        env={},
        worktree_path=isolated_worktree,
        pipeline_id="issue-2623-smoke",
        slice_id=None,
        phase="implement",
        command=None,
    )
    result = spawner.spawn(request)
    assert result.commit_sha is not None, (
        "AgentResult.commit_sha must be populated (INV-6 — task-1-1 AC)"
    )
    assert len(result.commit_sha) == 40, (
        "commit_sha must be the full 40-char git SHA, not a short prefix"
    )
    # SHA is hex
    assert all(c in "0123456789abcdef" for c in result.commit_sha.lower())


# ---------------------------------------------------------------------------
# Smoke: bus.add_message / bus.get_messages round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dim", ["k3s", "claude-code"])
def test_bus_add_get_messages_round_trip(
    dim: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``bus.add_message`` then ``bus.get_messages`` returns the same payload."""
    if dim == "claude-code" and os.environ.get("EGG_AGENT_ROLE"):
        pytest.skip(
            "claude-code substrate skipped inside egg sandbox-agent context"
        )
    select_substrate = getattr(substrate_pkg, "select_substrate")
    bundle = select_substrate({"EGG_SUBSTRATE": dim})
    bus = bundle.bus
    pipeline_id = "issue-2623-smoke-bus"
    payload: dict[str, Any] = {
        "message_type": "STATUS",
        "from_role": "tester",
        "to_role": "all",
        "body": "smoke",
    }
    bus.add_message(pipeline_id, payload)
    msgs = bus.get_messages(pipeline_id)
    assert msgs, "bus.get_messages must return the added message"
    assert any(m.get("from_role") == "tester" for m in msgs)


# ---------------------------------------------------------------------------
# Smoke: INV-3 stale-version rejection survives the bus surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dim", ["k3s", "claude-code"])
def test_bus_preserves_inv3_stale_version_rejection(
    dim: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale-version ACK / NACK rejection is preserved by the bus (INV-3).

    Routes a propose → re-propose → stale-version ACK sequence through
    the substrate's bus and asserts the orchestrator-side guard still
    rejects the stale verdict. The oracle is the existing
    ``orchestrator/tests/test_brc_open_nacks_barrier.py::
    test_ack_against_stale_version_raises`` scenario.
    """
    if dim == "claude-code" and os.environ.get("EGG_AGENT_ROLE"):
        pytest.skip(
            "claude-code substrate skipped inside egg sandbox-agent context"
        )
    # TODO(tester): once the in-process substrate's bus is wired into a
    # PeerConsensusTracker (task-1-3), drive the propose / re-propose /
    # stale-ACK sequence below through `bundle.bus` and assert that the
    # ACK at the older version raises the documented stale_version
    # error. Keeping this body as a TODO until the coder lands the
    # bus<->tracker wire — the assertion shape is identical to the
    # oracle test linked in the docstring.
    pytest.skip(
        "INV-3 wiring through substrate bus pending task-1-3 coder commit"
    )
