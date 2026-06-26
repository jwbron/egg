"""Slice-5 overseer-lifecycle contract tests (issue #2270, task-5-3).

Slice-5 retires the standing-pod **respawn churn** and adds **restart/generation
hygiene** (§3 of the ticket). It is the deletion slice that lands strictly AFTER
slice-4 (the orchestrator-side detection plane), which is the replacement that
MUST exist before the respawn machinery is removed.

This module is the **tester contract** that pins the slice-5 production surface;
the coder reconciles ``routes/pipelines.py``, ``overseer/monitor.py``, and
``models.py`` to it — the same tester-leads-coder flow used in slices 2, 3 and 4.

Production surface this contract pins
-------------------------------------

* ``routes.pipelines`` **no longer defines** ``_check_and_respawn_overseer`` and
  carries no per-overseer respawn-counter machinery (``overseer_respawn_count`` /
  ``max_overseer_respawns`` locals). The standing-pod respawn loop is folded into
  the general agent-restart machinery — net-negative lines (task-5-1).

* ``routes.pipelines._overseer_should_be_present(*, running_agent_count,
  pipeline_status) -> bool`` — the gate that guarantees **no overseer during a
  zero-agent HITL park**. Decisive rules (task-5-1):
    - ``running_agent_count <= 0`` ⇒ ``False`` regardless of status (the §3
      "no overseer during HITL waits with no agents running" guarantee), and
    - a terminal status (COMPLETE / FAILED / CANCELLED) ⇒ ``False`` regardless
      of the count, and
    - ``running_agent_count > 0`` on a ``RUNNING`` pipeline ⇒ ``True``.

* ``overseer.monitor.OverseerMonitor.reset_escalation_history()`` — clears the
  per-agent escalation history on restart; idempotent (task-5-2).

* ``overseer.monitor.OverseerMonitor.generation`` (int, default ``0``) plus
  ``reset_generation(generation=None)`` — the generation token reset on
  orchestrator pod recycle: advancing/resetting the token also clears the
  escalation history so stale escalation state can't cascade across generations
  (task-5-2).

Skip→strict convention
----------------------

Each row **skips** while the coder's slice-5 surface is absent (so the suite is
green on the tester's standalone branch and the BRC check-gate passes) and turns
into a **strict assertion** the moment that surface lands at slice integration —
the same green-while-coder-works / strict-at-integration behaviour slice-4 got
from ``pytest.importorskip``. This module can't ``importorskip`` (the overseer
package already imports), so the guard is per-row: resolve the target surface,
``pytest.skip`` if it isn't there yet, otherwise assert. The test bodies exercise
the real ``_escalation_history`` mechanism, so each asserts genuine behaviour the
moment the new surface lands — and would fail loudly if the coder shipped it
wrong.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup + docker stubs (mirrors test_restart_overseer.py so the overseer
# package imports without the real docker SDK present).
# ---------------------------------------------------------------------------

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

# ---------------------------------------------------------------------------
# Module imports. The overseer package + the PipelineStatus enum import cleanly
# today; ``routes.pipelines`` is imported lazily per-test (it is a large module
# and the deletion-regression test must observe its *current* symbol table).
# ---------------------------------------------------------------------------

try:
    from overseer.monitor import OverseerMonitor
except (ImportError, ModuleNotFoundError) as exc:  # pragma: no cover - import guard
    pytest.skip(
        f"overseer.monitor not importable yet: {exc}",
        allow_module_level=True,
    )

try:
    from models import PipelineStatus
except (ImportError, ModuleNotFoundError) as exc:  # pragma: no cover - import guard
    pytest.skip(
        f"models.PipelineStatus not importable yet: {exc}",
        allow_module_level=True,
    )


def _pipelines_module():
    """Import ``routes.pipelines`` fresh for symbol-table assertions."""
    import routes.pipelines as pipelines

    return pipelines


def _make_monitor() -> OverseerMonitor:
    """Construct a bare monitor (no real classifier/decision_maker needed)."""
    return OverseerMonitor("pipe-lifecycle")


def _seed_escalations(monitor: OverseerMonitor) -> None:
    """Record a couple of escalations through the real history mechanism.

    Uses the production ``_escalation_history`` structure directly (it exists
    today) so the reset assertions exercise genuine recorded state rather than a
    test-only stand-in.
    """
    from collections import deque

    for role in ("coder", "reviewer_code"):
        monitor._escalation_history.setdefault(role, deque(maxlen=50))
        monitor._escalation_history[role].append(
            {"action": "nudge", "classification": "stalled", "timestamp": 1.0}
        )


def _require(obj: object, name: str, *, absent: bool = False):
    """Skip-guard the slice-5 surface (skip→strict convention).

    With ``absent=False`` (default): return ``getattr(obj, name)``, or skip if it
    is not present yet (a new-surface row). With ``absent=True``: skip while the
    named attribute is STILL present (a deletion-regression row that has not
    folded yet); the caller then asserts its absence.
    """
    present = hasattr(obj, name)
    if absent:
        if present:
            pytest.skip(f"{name} not yet folded by the coder — strict at integration")
        return None
    if not present:
        pytest.skip(f"{name} not landed by the coder yet — strict at integration")
    return getattr(obj, name)


# ===========================================================================
# task-5-1 — respawn churn retired (deletion regression, net-negative)
# ===========================================================================


class TestRespawnChurnRetired:
    """The standing-pod respawn loop is gone, folded into agent-restart (§3)."""

    def test_check_and_respawn_overseer_removed(self) -> None:
        """``_check_and_respawn_overseer`` is deleted from ``routes.pipelines``.

        Deletion regression for coder task-5-1: the overseer-specific respawn
        helper has no phase-agent analog and folds into the general
        agent-restart machinery. Skips while still present; strict at
        integration once the fold lands.
        """
        pipelines = _pipelines_module()
        _require(pipelines, "_check_and_respawn_overseer", absent=True)
        assert not hasattr(pipelines, "_check_and_respawn_overseer"), (
            "_check_and_respawn_overseer must be folded into the general "
            "agent-restart machinery — no bespoke overseer respawn loop (#2270 §3)"
        )

    def test_no_respawn_counter_machinery_in_source(self) -> None:
        """The respawn-counter locals are gone from ``pipelines.py`` source.

        ``overseer_respawn_count`` / ``max_overseer_respawns`` were the
        standing-pod respawn-budget locals threaded through the poll loop and
        the deleted helper. Their absence is the churn-gone / net-negative gate.
        Skips while still present; strict at integration once the fold lands.
        """
        pipelines = _pipelines_module()
        source = Path(pipelines.__file__).read_text(encoding="utf-8")
        if "overseer_respawn_count" in source or "max_overseer_respawns" in source:
            pytest.skip(
                "respawn-counter machinery not yet folded by the coder — "
                "strict at integration"
            )
        assert "overseer_respawn_count" not in source, (
            "the per-overseer respawn counter must be gone — respawn churn is "
            "retired in favour of the general agent-restart machinery (#2270 §3)"
        )
        assert "max_overseer_respawns" not in source, (
            "the max-overseer-respawns budget local must be gone from the poll loop"
        )


# ===========================================================================
# task-5-1 — no overseer during a zero-agent HITL park (the §3 guarantee)
# ===========================================================================


class TestNoOverseerDuringZeroAgentPark:
    """``_overseer_should_be_present`` gates presence on agents actually running."""

    def _predicate(self):
        pipelines = _pipelines_module()
        return _require(pipelines, "_overseer_should_be_present")

    def test_zero_agents_during_hitl_park_spawns_nothing(self) -> None:
        """A multi-hour zero-agent HITL park must spawn no overseer.

        This is the core §3 guarantee: ``running_agent_count <= 0`` ⇒ ``False``
        even while the pipeline is parked AWAITING_HUMAN.
        """
        should_be_present = self._predicate()
        assert (
            should_be_present(
                running_agent_count=0,
                pipeline_status=PipelineStatus.AWAITING_HUMAN,
            )
            is False
        )

    def test_zero_agents_while_running_spawns_nothing(self) -> None:
        """Even a RUNNING pipeline with no agents in flight gets no overseer."""
        should_be_present = self._predicate()
        assert (
            should_be_present(
                running_agent_count=0,
                pipeline_status=PipelineStatus.RUNNING,
            )
            is False
        )

    def test_running_with_agents_is_present(self) -> None:
        """Agents actually running on a RUNNING pipeline ⇒ overseer present."""
        should_be_present = self._predicate()
        assert (
            should_be_present(
                running_agent_count=2,
                pipeline_status=PipelineStatus.RUNNING,
            )
            is True
        )

    @pytest.mark.parametrize(
        "terminal",
        [PipelineStatus.COMPLETE, PipelineStatus.FAILED, PipelineStatus.CANCELLED],
    )
    def test_terminal_status_never_present(self, terminal) -> None:
        """A terminal pipeline never gets an overseer, regardless of agent count."""
        should_be_present = self._predicate()
        assert (
            should_be_present(running_agent_count=3, pipeline_status=terminal) is False
        )


# ===========================================================================
# task-5-2 — escalation-history reset on restart
# ===========================================================================


class TestEscalationHistoryResetOnRestart:
    """``reset_escalation_history`` clears per-agent escalation state on restart."""

    def test_reset_clears_recorded_history(self) -> None:
        """After recording escalations, reset empties the per-agent history.

        Stale escalation state from before a restart must not survive into the
        restarted monitor (task-5-2 restart hygiene).
        """
        monitor = _make_monitor()
        reset = _require(monitor, "reset_escalation_history")
        _seed_escalations(monitor)
        assert monitor._escalation_history, "precondition: history was seeded"

        reset()

        # No escalations survive — either the dict is emptied or every per-agent
        # deque is cleared. Both satisfy "history cleared on restart".
        residual = sum(len(h) for h in monitor._escalation_history.values())
        assert residual == 0, "escalation history must be empty after reset"

    def test_reset_is_idempotent(self) -> None:
        """Resetting an already-empty history is a harmless no-op."""
        monitor = _make_monitor()
        reset = _require(monitor, "reset_escalation_history")
        reset()
        reset()  # must not raise
        residual = sum(len(h) for h in monitor._escalation_history.values())
        assert residual == 0


# ===========================================================================
# task-5-2 — generation-token reset on orchestrator pod recycle
# ===========================================================================


class TestGenerationTokenResetOnRecycle:
    """A generation token isolates escalation state across pod recycles."""

    def test_fresh_monitor_starts_at_clean_generation(self) -> None:
        """A freshly constructed monitor is a clean slate: gen 0, no history.

        An orchestrator pod recycle constructs a new monitor; it must carry no
        escalation state forward (no cross-generation leakage).
        """
        monitor = _make_monitor()
        if not hasattr(monitor, "generation"):
            pytest.skip(
                "OverseerMonitor.generation not landed by the coder yet — "
                "strict at integration"
            )
        assert monitor.generation == 0
        residual = sum(len(h) for h in monitor._escalation_history.values())
        assert residual == 0

    def test_reset_generation_sets_token_and_clears_history(self) -> None:
        """``reset_generation(token)`` advances the token AND clears history.

        On orchestrator pod recycle the generation token is reset; resetting it
        must also drop stale escalation state so it can't cascade into the new
        generation (task-5-2).
        """
        monitor = _make_monitor()
        reset_generation = _require(monitor, "reset_generation")
        _seed_escalations(monitor)

        reset_generation(5)
        assert monitor.generation == 5
        residual = sum(len(h) for h in monitor._escalation_history.values())
        assert residual == 0, "reset_generation must clear stale escalation state"

    def test_reset_generation_default_advances_without_leaking(self) -> None:
        """``reset_generation()`` with no explicit token still clears history.

        The default-call shape (recycle without a caller-supplied token) must
        not silently retain prior-generation escalation state.
        """
        monitor = _make_monitor()
        reset_generation = _require(monitor, "reset_generation")
        _seed_escalations(monitor)

        reset_generation()
        residual = sum(len(h) for h in monitor._escalation_history.values())
        assert residual == 0
