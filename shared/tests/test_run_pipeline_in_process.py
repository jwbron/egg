"""Tests for ``run_pipeline_in_process`` (#2623 slice-1 task-1-6, task-1-8).

Acceptance criteria covered (TASK-1-6 R4 acceptance bullets):

* Raises ``NotImplementedError`` for ``EGG_SUBSTRATE=k3s``.
* Heartbeat thread keeps ticking across HITL yields — background
  thread liveness during HITL pauses.
* Background threads are dropped cleanly on ``GeneratorExit`` (the
  caller's ``.close()`` joins every thread).
"""

from __future__ import annotations

import time
from pathlib import Path
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


# ---------------------------------------------------------------------------
# AC (a): NotImplementedError for EGG_SUBSTRATE=k3s
# ---------------------------------------------------------------------------


def test_run_pipeline_in_process_rejects_k3s_substrate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_pipeline_in_process`` refuses ``EGG_SUBSTRATE=k3s``.

    The in-process entry point is claude-code-only for the spike.
    ``EGG_SUBSTRATE=k3s`` keeps using the existing pipeline runner via
    ``orchestrator.cli.cmd_serve``; the in-process entry must not
    silently fall back.
    """
    run = in_process_mod.run_pipeline_in_process
    with pytest.raises(NotImplementedError) as excinfo:
        run(
            "pipeline-test",
            env={"EGG_SUBSTRATE": "k3s"},
            state_dir=tmp_path / ".egg-state",
        )
    # The error message should reference the substrate / follow-up
    # issue so operators can route the request correctly.
    msg = str(excinfo.value).lower()
    assert "k3s" in msg or "claude-code" in msg or "substrate" in msg, (
        f"NotImplementedError should reference the substrate boundary; got: {excinfo.value!r}"
    )


# ---------------------------------------------------------------------------
# AC (b): heartbeat-thread liveness across HITL yields
# ---------------------------------------------------------------------------


def test_heartbeat_thread_remains_alive_across_hitl_yield(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Heartbeat thread keeps ticking while a HITL decision is pending.

    Drives the generator until it yields a ``HITLDecision``, pauses
    (simulating the parent skill rendering the question), waits long
    enough for ≥2 heartbeat intervals, and asserts the heartbeat
    counter advanced — i.e. the background thread didn't stop while
    the generator body was paused at a yield.
    """
    # Shrink the heartbeat interval so the test runs in seconds, not
    # the production 5s tick.
    monkeypatch.setattr(in_process_mod, "_HEARTBEAT_INTERVAL", 0.05)
    monkeypatch.setattr(in_process_mod, "_BRC_REVIEW_INTERVAL", 0.05)
    monkeypatch.setattr(in_process_mod, "_BUS_TICK_INTERVAL", 0.05)

    run = in_process_mod.run_pipeline_in_process
    gen = run(
        "pipeline-hb-test",
        env={"EGG_SUBSTRATE": "claude-code"},
        state_dir=tmp_path / ".egg-state",
    )
    try:
        # Advance to the first HITL yield.
        first = next(gen)
        # First yield must be a HITLDecision-shaped object.
        assert first is not None, "first yield should be a HITLDecision"
        # Grab the orchestrator instance via the generator's frame to
        # observe the heartbeat counter. The runner is the
        # ``_InProcessOrchestrator``'s ``run()`` method; the instance
        # is in ``gi_frame.f_locals["self"]``.
        frame = gen.gi_frame
        assert frame is not None, "generator must have a live frame after yield"
        runner = frame.f_locals.get("self")
        assert runner is not None
        baseline = runner._heartbeat_ticks
        # Sleep through several heartbeat intervals.
        time.sleep(0.25)  # ≥4 intervals of 0.05s
        assert runner._heartbeat_ticks > baseline, (
            f"Heartbeat thread must keep ticking across HITL yields; "
            f"baseline={baseline} current={runner._heartbeat_ticks}"
        )
    finally:
        gen.close()


# ---------------------------------------------------------------------------
# AC (c): background threads dropped cleanly on GeneratorExit
# ---------------------------------------------------------------------------


def test_background_threads_dropped_on_generator_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing the generator joins background threads cleanly.

    Snapshots ``threading.enumerate()`` before / after the generator
    lifecycle and asserts every egg-inproc thread the orchestrator
    started has terminated within the bounded join window.
    """
    monkeypatch.setattr(in_process_mod, "_HEARTBEAT_INTERVAL", 0.05)
    monkeypatch.setattr(in_process_mod, "_BRC_REVIEW_INTERVAL", 0.05)
    monkeypatch.setattr(in_process_mod, "_BUS_TICK_INTERVAL", 0.05)

    run = in_process_mod.run_pipeline_in_process
    gen = run(
        "pipeline-thread-test",
        env={"EGG_SUBSTRATE": "claude-code"},
        state_dir=tmp_path / ".egg-state",
    )
    # Advance to the first yield to ensure the background threads
    # have started.
    next(gen)
    frame = gen.gi_frame
    runner = frame.f_locals.get("self") if frame else None
    assert runner is not None
    started_threads = list(runner._threads)
    assert started_threads, "background threads should have been started"
    # Close the generator — the finally block must join the threads.
    gen.close()
    # Give the threads a moment to wind down.
    time.sleep(0.3)
    leaked = [t for t in started_threads if t.is_alive()]
    assert leaked == [], (
        f"GeneratorExit must drop background threads; leaked: {[t.name for t in leaked]}"
    )


def test_run_pipeline_in_process_returns_artifact_path_on_normal_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the operator picks a terminal answer, the generator returns the artifact path.

    The walking-skeleton scope-fences anything past refine, but if
    the operator answers the refine-gate with anything other than
    ``approve_continue``, the generator terminates and returns the
    artifact path string.
    """
    monkeypatch.setattr(in_process_mod, "_HEARTBEAT_INTERVAL", 0.05)
    monkeypatch.setattr(in_process_mod, "_BRC_REVIEW_INTERVAL", 0.05)
    monkeypatch.setattr(in_process_mod, "_BUS_TICK_INTERVAL", 0.05)

    # Stub out the spawner so we don't fan into the real harness.
    fake_bundle = MagicMock()
    fake_bundle.spawner.spawn = MagicMock(
        return_value=MagicMock(
            exit_code=0,
            commit_sha="0" * 40,
            stdout="ok",
            worktree=tmp_path / "wt",
            artifacts=[],
        )
    )
    fake_bundle.worktrees.create = MagicMock(return_value=tmp_path / "wt")
    fake_bundle.worktrees.tear_down = MagicMock()
    # reviewer_code non-blocking: wrap the select_substrate patch around
    # both yields so the bundle is observed before _spawn_refiner runs.
    # The previous outer ``patch.object`` was a no-op (wraps without
    # substitution) and is gone.
    with patch("orchestrator.substrate.select_substrate", return_value=fake_bundle):
        run = in_process_mod.run_pipeline_in_process
        gen = run(
            "pipeline-end2end",
            env={"EGG_SUBSTRATE": "claude-code"},
            state_dir=tmp_path / ".egg-state",
        )
        # Advance to first HITL (preflight).
        next(gen)
        try:
            # Send "approve" through preflight — non-abort answer.
            second = gen.send("approve")
            # The generator must yield the refine-gate decision next.
            assert second is not None
            # Send "stop" through the refine gate — non-fence terminal.
            try:
                gen.send("stop")
                pytest.fail("Expected StopIteration on terminal answer")
            except StopIteration as stop:
                # The return value is the artifact path string.
                assert isinstance(stop.value, str)
                assert stop.value.endswith("-analysis.md")
        finally:
            gen.close()
