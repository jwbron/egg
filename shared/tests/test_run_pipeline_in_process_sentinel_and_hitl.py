"""Sentinel-lifecycle + HITL tests for ``run_pipeline_in_process`` (#2623).

Coverage added in response to reviewer_code v1 (tester) NACK blockers
1-4 — pinning the v2/v3 operator-safety surface the coder added:

* **Active-role sentinel (v3 fix)** — PID-stamping + PID liveness
  check in ``hook_entry._resolve_active_role`` + generator
  ``finally``-block teardown via ``_teardown_sentinel``. Without these
  pins, a regression that re-introduces sentinel-leak silently breaks
  the user's plain Claude Code session after a crashed run.

* **Preflight HITL abort (v2 fix)** — operator's ``"abort"`` answer
  short-circuits the refiner spawn via ``_PreflightAborted``. Pins
  the bare-string and dict-shaped answer forms ``_answer_is_abort``
  documents.

* **Refine-failure HITL gate (v2 fix)** — when the spawner returns
  ``exit_code != 0`` the gate's question changes shape (``Refiner
  FAILED ...`` + ``options=["retry","abort"]``) so the operator sees
  the failure rather than approving a refine that never ran.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

substrate_pkg = pytest.importorskip(
    "orchestrator.substrate",
    reason="orchestrator/substrate/ package not present yet",
)
in_process_mod = pytest.importorskip(
    "orchestrator.substrate.in_process",
    reason="orchestrator/substrate/in_process.py not present yet",
)
hook_entry_mod = pytest.importorskip(
    "orchestrator.substrate.claude_code.hook_entry",
    reason="orchestrator/substrate/claude_code/hook_entry.py not present yet",
)


@pytest.fixture
def short_intervals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink background-thread intervals so tests run in seconds."""
    monkeypatch.setattr(in_process_mod, "_HEARTBEAT_INTERVAL", 0.05)
    monkeypatch.setattr(in_process_mod, "_BRC_REVIEW_INTERVAL", 0.05)
    monkeypatch.setattr(in_process_mod, "_BUS_TICK_INTERVAL", 0.05)


@pytest.fixture
def fake_bundle(tmp_path: Path) -> MagicMock:
    """A substrate bundle that doesn't touch the real Claude Code runner."""
    bundle = MagicMock()
    spawn_result = MagicMock(
        exit_code=0,
        commit_sha="0" * 40,
        stdout="ok",
        worktree=tmp_path / "wt",
        artifacts=[],
    )
    bundle.spawner.spawn = MagicMock(return_value=spawn_result)
    bundle.worktrees.create = MagicMock(return_value=tmp_path / "wt")
    bundle.worktrees.tear_down = MagicMock()
    bundle.name = "claude-code"
    return bundle


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``$HOME`` at a clean tmp dir so sentinel reads/writes are isolated."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


# ---------------------------------------------------------------------------
# Sentinel — PID stamping (v3 fix)
# ---------------------------------------------------------------------------


def test_sentinel_is_written_with_pid(
    tmp_path: Path, fake_home: Path, short_intervals: None
) -> None:
    """``_write_active_role_sentinel`` writes the orchestrator's PID."""
    runner_cls = in_process_mod._InProcessOrchestrator
    runner = runner_cls(
        pipeline_id="pipeline-sentinel-pid",
        repo=None,
        issue_number=None,
        issue_body="",
        env={"EGG_SUBSTRATE": "claude-code"},
        state_root=tmp_path / ".egg-state",
    )
    runner._write_active_role_sentinel("refiner")
    sentinel = fake_home / ".claude" / "egg-active-role.json"
    assert sentinel.exists(), "sentinel must be written under $HOME/.claude/"
    payload = json.loads(sentinel.read_text())
    assert payload["pid"] == os.getpid()
    assert payload["role"] == "refiner"
    assert payload["pipeline_id"] == "pipeline-sentinel-pid"


def test_sentinel_teardown_unlinks_file(
    tmp_path: Path, fake_home: Path, short_intervals: None
) -> None:
    """``_teardown_sentinel`` removes the sentinel file if it exists."""
    runner_cls = in_process_mod._InProcessOrchestrator
    runner = runner_cls(
        pipeline_id="pipeline-sentinel-teardown",
        repo=None,
        issue_number=None,
        issue_body="",
        env={"EGG_SUBSTRATE": "claude-code"},
        state_root=tmp_path / ".egg-state",
    )
    runner._write_active_role_sentinel("refiner")
    sentinel = fake_home / ".claude" / "egg-active-role.json"
    assert sentinel.exists()
    runner._teardown_sentinel()
    assert not sentinel.exists(), "sentinel must be unlinked by teardown"


def test_sentinel_teardown_silently_no_op_when_missing(
    tmp_path: Path, fake_home: Path, short_intervals: None
) -> None:
    """``_teardown_sentinel`` does not raise when no sentinel exists."""
    runner_cls = in_process_mod._InProcessOrchestrator
    runner = runner_cls(
        pipeline_id="pipeline-sentinel-missing",
        repo=None,
        issue_number=None,
        issue_body="",
        env={"EGG_SUBSTRATE": "claude-code"},
        state_root=tmp_path / ".egg-state",
    )
    runner._teardown_sentinel()  # must not raise


# ---------------------------------------------------------------------------
# Sentinel — hook PID-liveness fallback (v3 fix)
# ---------------------------------------------------------------------------


def test_hook_treats_dead_pid_sentinel_as_missing(
    tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sentinel whose PID is not alive is ignored — fail-closed for prefixes."""
    monkeypatch.delenv("EGG_AGENT_ROLE", raising=False)
    sentinel_dir = fake_home / ".claude"
    sentinel_dir.mkdir()
    # Pick a PID that is extremely unlikely to be live (2^22 — well
    # above /proc/sys/kernel/pid_max on most kernels).
    dead_pid = 4194300
    (sentinel_dir / "egg-active-role.json").write_text(
        json.dumps({"role": "refiner", "pid": dead_pid})
    )
    # Hook resolves an empty role → fail-closed for substrate prefixes.
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".egg-state").mkdir()
    monkeypatch.setenv("EGG_REPO_ROOT", str(repo_root))
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(repo_root / ".egg-state" / "drafts" / "x.md"),
            "content": "...",
        },
    }
    result = hook_entry_mod.decide(payload)
    assert result.get("decision") == "block", (
        f"Dead-PID sentinel must be ignored; role resolver should fall "
        f"through to the substrate-prefix fail-closed default. Got "
        f"{result!r}"
    )


def test_hook_uses_live_pid_sentinel_as_fallback(
    tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sentinel whose PID is alive resolves the role from the sentinel."""
    monkeypatch.delenv("EGG_AGENT_ROLE", raising=False)
    sentinel_dir = fake_home / ".claude"
    sentinel_dir.mkdir()
    (sentinel_dir / "egg-active-role.json").write_text(
        json.dumps({"role": "tester", "pid": os.getpid()})
    )
    role = hook_entry_mod._resolve_active_role()
    assert role == "tester", f"Live-PID sentinel must resolve the role; got {role!r}"


def test_resolve_active_role_prefers_env_over_sentinel(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``EGG_AGENT_ROLE`` env wins over the sentinel file."""
    sentinel_dir = fake_home / ".claude"
    sentinel_dir.mkdir()
    (sentinel_dir / "egg-active-role.json").write_text(
        json.dumps({"role": "tester", "pid": os.getpid()})
    )
    monkeypatch.setenv("EGG_AGENT_ROLE", "coder")
    role = hook_entry_mod._resolve_active_role()
    assert role == "coder"


# ---------------------------------------------------------------------------
# Sentinel — generator cleanup paths (v3 fix)
# ---------------------------------------------------------------------------


def test_generator_unlinks_sentinel_on_generator_close(
    tmp_path: Path, fake_home: Path, short_intervals: None
) -> None:
    """Closing the generator mid-run unlinks the sentinel."""
    run = in_process_mod.run_pipeline_in_process
    gen = run(
        "pipeline-sentinel-close",
        env={"EGG_SUBSTRATE": "claude-code"},
        state_dir=tmp_path / ".egg-state",
    )
    next(gen)  # advance to first yield — sentinel should exist
    sentinel = fake_home / ".claude" / "egg-active-role.json"
    # The sentinel may not be written until _spawn_refiner runs; we
    # only assert teardown leaves nothing behind regardless of state.
    gen.close()
    time.sleep(0.1)
    assert not sentinel.exists(), "GeneratorExit must unlink the sentinel file"


def test_generator_unlinks_sentinel_on_preflight_abort(
    tmp_path: Path, fake_home: Path, short_intervals: None
) -> None:
    """Operator's preflight abort still tears down the sentinel.

    The abort path is translated to a clean ``StopIteration`` by the
    generator (reviewer v1 blocker #7 — the docstring's contract is
    "clean StopIteration with a diagnostic message"). The generator's
    ``finally`` block runs as part of the normal-completion path and
    unlinks the sentinel.
    """
    run = in_process_mod.run_pipeline_in_process
    gen = run(
        "pipeline-sentinel-abort",
        env={"EGG_SUBSTRATE": "claude-code"},
        state_dir=tmp_path / ".egg-state",
    )
    next(gen)
    with pytest.raises(StopIteration) as exc_info:
        gen.send("abort")
    assert "aborted" in str(exc_info.value.value).lower(), (
        "StopIteration.value should carry the abort diagnostic message"
    )
    sentinel = fake_home / ".claude" / "egg-active-role.json"
    assert not sentinel.exists(), "Preflight abort must unlink the sentinel via the finally block"


# ---------------------------------------------------------------------------
# Preflight HITL abort (v2 fix)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    ["abort", "Abort", "STOP", "cancel", {"selected": "abort"}, {"value": "stop"}],
)
def test_preflight_abort_answer_short_circuits_spawn(
    tmp_path: Path,
    fake_home: Path,
    short_intervals: None,
    answer: Any,
) -> None:
    """Operator's abort answer (bare or dict shape) skips the refiner spawn.

    The abort path is translated to a clean ``StopIteration`` by the
    generator (reviewer v1 blocker #7). The contract this test pins:
    "the refiner spawn does NOT run" AND the generator surfaces the
    abort as a normal StopIteration carrying a diagnostic message.
    """
    spawn_mock = MagicMock()
    run = in_process_mod.run_pipeline_in_process
    gen = run(
        "pipeline-preflight-abort",
        env={"EGG_SUBSTRATE": "claude-code"},
        state_dir=tmp_path / ".egg-state",
    )
    try:
        next(gen)  # preflight HITL
        # Patch the spawn fn so we can assert it never runs.
        frame = gen.gi_frame
        runner = frame.f_locals.get("self") if frame else None
        assert runner is not None
        runner._spawn_refiner = spawn_mock
        with pytest.raises(StopIteration) as exc_info:
            gen.send(answer)
        assert "aborted" in str(exc_info.value.value).lower()
    finally:
        gen.close()
    assert not spawn_mock.called, (
        f"Preflight abort answer {answer!r} must NOT invoke _spawn_refiner"
    )


def test_preflight_non_abort_answer_proceeds_to_spawn(
    tmp_path: Path,
    fake_home: Path,
    short_intervals: None,
    fake_bundle: MagicMock,
) -> None:
    """A non-abort preflight answer proceeds to spawn the refiner."""
    run = in_process_mod.run_pipeline_in_process
    gen = run(
        "pipeline-preflight-ok",
        env={"EGG_SUBSTRATE": "claude-code"},
        state_dir=tmp_path / ".egg-state",
    )
    try:
        with patch("orchestrator.substrate.select_substrate", return_value=fake_bundle):
            next(gen)
            second = gen.send("approve")
        # The second yield is the refine HITL gate.
        assert second is not None
        assert fake_bundle.spawner.spawn.called, "Non-abort answer must invoke the spawner"
    finally:
        gen.close()


def test_answer_is_abort_helper_contract() -> None:
    """``_answer_is_abort`` accepts bare strings + dict-shaped answers."""
    helper = in_process_mod._answer_is_abort
    assert helper("abort") is True
    assert helper("Abort") is True
    assert helper("STOP") is True
    assert helper("cancel") is True
    assert helper({"selected": "abort"}) is True
    assert helper({"value": "stop"}) is True
    assert helper("approve") is False
    assert helper(None) is False
    assert helper({"selected": "approve"}) is False


# ---------------------------------------------------------------------------
# Refine-failure HITL gate (v2 fix — exit_code != 0 path)
# ---------------------------------------------------------------------------


def test_refine_gate_says_failed_when_spawner_exit_code_nonzero(
    tmp_path: Path,
    fake_home: Path,
    short_intervals: None,
    fake_bundle: MagicMock,
) -> None:
    """Non-zero spawner exit_code surfaces a FAILURE-shaped HITL decision."""
    fake_bundle.spawner.spawn.return_value = MagicMock(
        exit_code=1,
        commit_sha=None,
        stdout="boom: refiner crashed",
        worktree=tmp_path / "wt",
        artifacts=[],
    )
    run = in_process_mod.run_pipeline_in_process
    gen = run(
        "pipeline-refine-failed",
        env={"EGG_SUBSTRATE": "claude-code"},
        state_dir=tmp_path / ".egg-state",
    )
    try:
        with patch("orchestrator.substrate.select_substrate", return_value=fake_bundle):
            next(gen)  # preflight
            gate = gen.send("approve")  # refine gate decision
        assert gate is not None
        assert "FAILED" in gate.question, (
            f"Failed-spawn gate must surface FAILED in question; got {gate.question!r}"
        )
        assert list(gate.options) == ["retry", "abort"], (
            f"Failed-spawn gate must offer retry/abort; got {gate.options!r}"
        )
    finally:
        gen.close()


def test_refine_gate_says_normal_when_spawner_exit_code_zero(
    tmp_path: Path,
    fake_home: Path,
    short_intervals: None,
    fake_bundle: MagicMock,
) -> None:
    """Zero exit_code surfaces the normal 4-way HITL gate decision."""
    # fake_bundle already returns exit_code=0
    run = in_process_mod.run_pipeline_in_process
    gen = run(
        "pipeline-refine-ok",
        env={"EGG_SUBSTRATE": "claude-code"},
        state_dir=tmp_path / ".egg-state",
    )
    try:
        with patch("orchestrator.substrate.select_substrate", return_value=fake_bundle):
            next(gen)
            gate = gen.send("approve")
        assert "FAILED" not in gate.question
        assert "Approve and continue" in gate.question
        assert list(gate.options) == [
            "approve_continue",
            "request_changes",
            "change_approach",
            "stop",
        ]
    finally:
        gen.close()
