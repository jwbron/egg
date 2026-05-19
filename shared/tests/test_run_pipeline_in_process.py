"""Tests for ``run_pipeline_in_process`` (#2623 slice-1 task-1-6, task-1-8).

Acceptance criteria covered (TASK-1-6 R4 acceptance bullets):

* Raises ``NotImplementedError`` for ``EGG_SUBSTRATE=k3s`` — the
  in-process entry point is claude-code-only; the k3s leg keeps using
  the existing pipeline runner.
* Heartbeat thread liveness is preserved across HITL yields (the
  generator yields control back to the parent skill for HITL prompts,
  but the orchestrator's internal heartbeat thread keeps ticking so
  overseer-side stall detection still works).
* Background threads are dropped cleanly on ``GeneratorExit`` (caller
  closes the generator; no zombie threads survive).

The entry point shape is documented by the architect output as a
generator: the parent skill iterates it, ``HITLDecision`` objects
yielded by the generator surface to the skill (which renders them via
``AskUserQuestion``), and ``.send(answer)`` resumes the orchestrator.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

substrate_pkg = pytest.importorskip(
    "substrate",
    reason="orchestrator/substrate/ package not present yet (task-1-1 pending)",
)
in_process_mod = pytest.importorskip(
    "substrate.in_process",
    reason=(
        "orchestrator/substrate/in_process.py not present yet "
        "(task-1-6 pending)"
    ),
)


# ---------------------------------------------------------------------------
# AC (a): NotImplementedError for k3s
# ---------------------------------------------------------------------------


def test_run_pipeline_in_process_rejects_k3s_substrate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_pipeline_in_process`` refuses ``EGG_SUBSTRATE=k3s``.

    The in-process entry point is the claude-code substrate's
    orchestrator. ``EGG_SUBSTRATE=k3s`` must keep using the existing
    pipeline runner via the regular k8s spawner — the in-process
    entry point must not silently fall back.
    """
    run = getattr(in_process_mod, "run_pipeline_in_process", None)
    assert run is not None, (
        "substrate.in_process.run_pipeline_in_process missing — task-1-6 AC"
    )
    monkeypatch.setenv("EGG_SUBSTRATE", "k3s")
    with pytest.raises(NotImplementedError):
        # Generator-style or function-style — both must raise on first use.
        result = run()
        # If the entry is a generator, advance it to trigger the raise.
        if hasattr(result, "__next__"):
            next(result)


# ---------------------------------------------------------------------------
# AC (b): heartbeat-thread liveness across HITL yields
# ---------------------------------------------------------------------------


def test_heartbeat_thread_remains_alive_across_hitl_yield(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Heartbeat thread keeps ticking while a HITL decision is pending.

    Drives the generator until it yields a ``HITLDecision``, pauses
    (simulating the parent skill rendering the question to the user),
    waits long enough for ≥2 heartbeat intervals to elapse, and asserts
    the orchestrator's heartbeat counter advanced — i.e. the background
    thread didn't sleep on the same generator-level yield.
    """
    pytest.skip(
        "Heartbeat introspection surface pending — fill in once task-1-6 "
        "exposes a counter or test hook for the heartbeat thread"
    )


# ---------------------------------------------------------------------------
# AC (c): background threads dropped cleanly on GeneratorExit
# ---------------------------------------------------------------------------


def test_background_threads_dropped_on_generator_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing the generator joins / stops all orchestrator-side threads.

    Snapshots ``threading.enumerate()`` before / after the generator
    lifecycle and asserts no new threads survive the ``.close()``.
    """
    monkeypatch.setenv("EGG_SUBSTRATE", "claude-code")
    run = getattr(in_process_mod, "run_pipeline_in_process", None)
    if run is None:
        pytest.skip("run_pipeline_in_process missing — task-1-6 pending")

    before = {t.ident for t in threading.enumerate()}
    pytest.skip(
        "Generator construction signature pending — fill in once task-1-6 "
        "lands (need pipeline_id / mode / etc. params)"
    )
    # The shape we want once task-1-6 ships:
    # gen = run(pipeline_id="pipeline-test", mode="issue", issue=2623)
    # next(gen)        # advance past first yield
    # gen.close()      # caller asks to clean up
    # # Give threads a moment to wind down.
    # time.sleep(0.5)
    # after = {t.ident for t in threading.enumerate()}
    # assert after.issubset(before | {threading.main_thread().ident}), (
    #     "No new threads must survive generator close"
    # )
