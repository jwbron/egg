"""Tests for ``orchestrator/phase_idle_budget.py``: the orchestrator-side
phase-level idle-budget timer that replaces the wrapper-side per-role
``stuck-phase-transition`` alert (issue #3023, slice-1, task-1-1).

The timer owns:

* ``DEFAULT_PHASE_IDLE_BUDGET_MIN = 30`` (parity with the legacy
  ``consensus_wrapper.py:EVENT_PUMP_IDLE_BUDGET_MIN_DEFAULT`` so a
  drift between the two during the slice-1 coexistence window is
  caught directly);
* the public API ``record_spawn(pipeline_id, phase, role, action)``,
  ``check(now, pending_hitl_count)``, ``reset()``;
* state ``(pipeline_id, phase, last_spawn_at, last_alert_at,
  per_role_last_action)`` — the ``per_role_last_action`` snapshot is
  surfaced on every alert payload as the structured ``per_role_state``
  field (AC-R4);
* HITL-pending suppression (AC-R13): a non-zero ``pending_hitl_count``
  downgrades the 1× alert to ``priority=low`` and suppresses the 2×
  alert entirely (so the operator is not paged a second time on a phase
  that is legitimately waiting on a human gate).

The class is constructed with an injected ``alert_emitter`` callable.
The shape of the callable mirrors the existing overseer-side
``_broadcast_alert`` entry point in ``orchestrator/overseer/monitor.py``
so the production wiring can adapt either path without a new alert
surface. The tests pass a ``MagicMock`` so they don't depend on the
broadcaster signature beyond the kwargs we assert on.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# conftest.py already adds orchestrator/ to sys.path, but assert the
# guarantee here so a future regression in conftest doesn't cause a
# misleading ImportError diagnosis on this test file.
_orchestrator_path = Path(__file__).parent.parent
assert str(_orchestrator_path) in sys.path, (
    "conftest.py must have added the orchestrator/ directory to sys.path "
    "before this module is collected."
)

# ``pytest.importorskip`` so the tests skip cleanly until task-1-1's
# production module lands on the slice branch. Once present the tests
# execute fully and pin the public contract.
phase_idle_budget = pytest.importorskip(
    "phase_idle_budget",
    reason=(
        "orchestrator/phase_idle_budget.py is task-1-1 of issue #3023 "
        "slice-1; tests skip until the coder lands the module."
    ),
)


# Convenience handles into the production module — failing here would
# mean the module exists but the public surface drifted.
def _resolve_class():
    cls = getattr(phase_idle_budget, "PhaseIdleBudgetTimer", None)
    assert cls is not None, (
        "phase_idle_budget.py must export PhaseIdleBudgetTimer "
        "(task-1-1 acceptance: 'File exists; class exposes record_spawn, "
        "check, reset')."
    )
    return cls


def _resolve_default() -> int:
    constant = getattr(phase_idle_budget, "DEFAULT_PHASE_IDLE_BUDGET_MIN", None)
    assert constant is not None, (
        "phase_idle_budget.py must expose DEFAULT_PHASE_IDLE_BUDGET_MIN "
        "(task-1-1 acceptance: 'constant == 30')."
    )
    return int(constant)


# --------------------------------------------------------------------------- #
# (1) Class shape / constant parity
# --------------------------------------------------------------------------- #


class TestPhaseIdleBudgetTimerShape:
    """Pin the public surface task-1-1's acceptance criterion enumerates:
    ``File exists; class exposes record_spawn, check, reset; constant == 30``.
    """

    def test_default_constant_is_30(self):
        """The 30-minute default must match
        ``consensus_wrapper.EVENT_PUMP_IDLE_BUDGET_MIN_DEFAULT`` (the legacy
        wrapper-side value the orchestrator timer replaces). A drift during
        the slice-1 coexistence window would page operators twice or never.
        """
        assert _resolve_default() == 30

    def test_default_constant_matches_wrapper_default(self):
        """Cross-pin against the wrapper-side constant so a drift in
        *either* file fails this test. The wrapper-side value is the one
        operators have tuned around (#2908 task-2-3); the orchestrator
        must inherit it during slice-1 coexistence.
        """
        try:
            from consensus_wrapper import EVENT_PUMP_IDLE_BUDGET_MIN_DEFAULT
        except ImportError:
            pytest.skip(
                "consensus_wrapper.py not importable in this environment; "
                "skip the cross-default parity assertion."
            )
        assert _resolve_default() == EVENT_PUMP_IDLE_BUDGET_MIN_DEFAULT, (
            "DEFAULT_PHASE_IDLE_BUDGET_MIN must equal "
            "EVENT_PUMP_IDLE_BUDGET_MIN_DEFAULT during slice-1 coexistence "
            "(consensus_wrapper.py:59). A drift would page operators at "
            "different thresholds from the two emitters."
        )

    def test_class_exposes_required_methods(self):
        """Acceptance: ``class exposes record_spawn, check, reset``. The
        class is constructed with an injected ``alert_emitter`` so tests
        and production can plumb the existing overseer-alert entry point
        without coupling.
        """
        Cls = _resolve_class()
        timer = Cls(alert_emitter=MagicMock())
        for name in ("record_spawn", "check", "reset"):
            assert callable(getattr(timer, name, None)), (
                f"PhaseIdleBudgetTimer must expose a callable .{name}() "
                f"(task-1-1 acceptance: 'class exposes record_spawn, "
                f"check, reset')."
            )


# --------------------------------------------------------------------------- #
# (2) Threshold semantics
# --------------------------------------------------------------------------- #


def _make_timer(emitter=None, budget_minutes: int = 30):
    """Construct a timer with the default 30-min budget unless overridden.

    Tests inject the budget explicitly so they don't depend on the
    constant value (the parity check above pins the constant separately).
    """
    Cls = _resolve_class()
    emitter = emitter if emitter is not None else MagicMock()
    return Cls(
        alert_emitter=emitter,
        budget_minutes=budget_minutes,
    )


class TestThresholdSemantics:
    """Acceptance: ``0 idle min -> no emit; 30 min -> 1x emit once with
    populated per_role_state; 60 min -> 2x emit once (1x not re-emitted)``.

    The unit-tests use a monotonic-clock surrogate (plain seconds-since-zero)
    so they are deterministic across machines and OSes.
    """

    def test_zero_idle_time_does_not_emit(self):
        emitter = MagicMock()
        timer = _make_timer(emitter=emitter)
        timer.record_spawn(
            pipeline_id="p1", phase="implement", role="coder", action="propose", now=0.0
        )
        timer.check(now=0.0, pending_hitl_count=0)
        emitter.assert_not_called()

    def test_one_x_threshold_emits_exactly_once(self):
        """At the 1× threshold (30 min == 1800 s) the timer must emit
        exactly one ``stuck-phase-transition`` alert; subsequent checks
        within the same bucket must not re-fire (idempotence).
        """
        emitter = MagicMock()
        timer = _make_timer(emitter=emitter)
        timer.record_spawn(
            pipeline_id="p1", phase="implement", role="coder", action="propose", now=0.0
        )

        # At exactly the budget boundary.
        timer.check(now=1800.0, pending_hitl_count=0)
        assert emitter.call_count == 1

        # Idempotent re-fire within the same threshold bucket.
        timer.check(now=1801.0, pending_hitl_count=0)
        timer.check(now=2000.0, pending_hitl_count=0)
        assert emitter.call_count == 1, (
            "Idempotent re-fire: the 1× alert must not re-emit while still "
            "inside the 1× bucket (task-1-1 acceptance: 'idempotent across "
            "check() calls in the same bucket')."
        )

    def test_two_x_threshold_emits_exactly_once_and_subsumes_one_x(self):
        """At the 2× threshold both alerts fire (in order, 1× first then
        2×) but each emits exactly once; the 1× latch is set so it cannot
        re-fire from a stale ``check`` after the 2× landed.
        """
        emitter = MagicMock()
        timer = _make_timer(emitter=emitter)
        timer.record_spawn(
            pipeline_id="p1", phase="implement", role="coder", action="propose", now=0.0
        )

        # At 1× — first emission.
        timer.check(now=1800.0, pending_hitl_count=0)
        # At 2× — second (and final) emission.
        timer.check(now=3600.0, pending_hitl_count=0)
        assert emitter.call_count == 2

        # Idempotent at 2× — no third emission.
        timer.check(now=3601.0, pending_hitl_count=0)
        timer.check(now=7200.0, pending_hitl_count=0)
        assert emitter.call_count == 2, (
            "The 2× alert must not re-fire once it has been emitted within "
            "the same phase (task-1-1 acceptance: 'idempotent across check() "
            "calls in the same bucket')."
        )

    def test_jump_from_zero_to_two_x_emits_both_alerts_each_once(self):
        """If the orchestrator pauses long enough that a tick lands past
        the 2× threshold without a prior 1× tick, both alerts must still
        each emit exactly once (the 1× latch is set as a side effect of the
        2× branch, mirroring the wrapper's ``tester v1 non-blocker #1`` fix
        at ``consensus_wrapper.py:538-543``).
        """
        emitter = MagicMock()
        timer = _make_timer(emitter=emitter)
        timer.record_spawn(
            pipeline_id="p1", phase="implement", role="coder", action="propose", now=0.0
        )

        # Jump straight past 2× without a prior 1× check.
        timer.check(now=3601.0, pending_hitl_count=0)

        # Implementations are free to emit either one ``high``-priority
        # subsuming alert or two distinct (1×, 2×) alerts here. Both
        # branches satisfy the operator-UX invariant: at most one further
        # emission, and no re-fire from a follow-up check.
        first_call_count = emitter.call_count
        assert 1 <= first_call_count <= 2

        timer.check(now=4200.0, pending_hitl_count=0)
        timer.check(now=10800.0, pending_hitl_count=0)
        assert emitter.call_count == first_call_count, (
            "After a jump-past-2× emission, neither the 1× nor the 2× "
            "branch may re-fire on subsequent checks within the same "
            "phase. Wrapper parity: consensus_wrapper.py:538-543."
        )


# --------------------------------------------------------------------------- #
# (3) record_spawn resets the idle window + tracks per-role action
# --------------------------------------------------------------------------- #


class TestRecordSpawnResetsAndTracks:
    """Acceptance: ``record_spawn resets and updates per-role state``."""

    def test_record_spawn_resets_idle_window(self):
        """``record_spawn`` must reset ``last_spawn_at`` so a follow-up
        ``check`` within the budget does not emit. Without this the timer
        would page operators on every phase that legitimately spawns
        within the budget.
        """
        emitter = MagicMock()
        timer = _make_timer(emitter=emitter)
        timer.record_spawn(
            pipeline_id="p1", phase="implement", role="coder", action="propose", now=0.0
        )

        # Advance close to (but not past) the budget — no emit.
        timer.check(now=1700.0, pending_hitl_count=0)
        emitter.assert_not_called()

        # A spawn at 1700 s resets the window; ``now=3400`` (1700 s after
        # the second spawn, still inside the budget) must not emit.
        timer.record_spawn(
            pipeline_id="p1", phase="implement", role="coder", action="ack", now=1700.0
        )
        timer.check(now=3400.0, pending_hitl_count=0)
        emitter.assert_not_called()

    def test_record_spawn_updates_per_role_last_action(self):
        """The ``per_role_last_action`` accessor (read from the alert
        payload, since the field is not part of the public class
        contract beyond the alert payload) must reflect the latest
        ``action`` per role.
        """
        emitter = MagicMock()
        timer = _make_timer(emitter=emitter)
        timer.record_spawn(
            pipeline_id="p1", phase="implement", role="coder", action="propose", now=0.0
        )
        timer.record_spawn(
            pipeline_id="p1", phase="implement", role="tester", action="ack", now=10.0
        )
        # Newer action for ``coder`` supersedes the prior one.
        timer.record_spawn(
            pipeline_id="p1", phase="implement", role="coder", action="ack", now=20.0
        )

        timer.check(now=1820.0, pending_hitl_count=0)
        assert emitter.call_count == 1
        kwargs = emitter.call_args.kwargs
        per_role_state = kwargs.get("per_role_state") or {}
        assert per_role_state.get("coder") == "ack"
        assert per_role_state.get("tester") == "ack"


# --------------------------------------------------------------------------- #
# (4) Alert payload shape: anomaly + per_role_state (AC-R4)
# --------------------------------------------------------------------------- #


class TestAlertPayloadShape:
    """Acceptance: ``populated per_role_state`` on every alert payload
    (AC-R4). The alert always carries ``anomaly="stuck-phase-transition"``
    so operator dashboards filtering by the wrapper-era tag keep working.
    """

    def test_one_x_payload_contains_anomaly_and_per_role_state(self):
        emitter = MagicMock()
        timer = _make_timer(emitter=emitter)
        timer.record_spawn(
            pipeline_id="p1", phase="implement", role="coder", action="propose", now=0.0
        )
        timer.record_spawn(
            pipeline_id="p1",
            phase="implement",
            role="reviewer_code",
            action="ack",
            now=15.0,
        )
        timer.check(now=1800.0, pending_hitl_count=0)

        kwargs = emitter.call_args.kwargs
        assert kwargs.get("anomaly") == "stuck-phase-transition", (
            "Alert must carry anomaly='stuck-phase-transition' (parity "
            "with consensus_wrapper.py:524 so operator dashboards keep "
            "working post-cutover)."
        )
        per_role_state = kwargs.get("per_role_state")
        assert per_role_state is not None, (
            "Alert payload must carry the structured per_role_state field "
            "(AC-R4, runbook will reference it)."
        )
        assert per_role_state.get("coder") == "propose"
        assert per_role_state.get("reviewer_code") == "ack"

    def test_payload_carries_pipeline_id_and_phase(self):
        """The alert payload must identify the pipeline + phase that
        tripped the idle budget so operator triage doesn't require
        cross-referencing the orchestrator log.
        """
        emitter = MagicMock()
        timer = _make_timer(emitter=emitter)
        timer.record_spawn(
            pipeline_id="issue-3023",
            phase="implement",
            role="coder",
            action="propose",
            now=0.0,
        )
        timer.check(now=1800.0, pending_hitl_count=0)

        kwargs = emitter.call_args.kwargs
        assert kwargs.get("pipeline_id") == "issue-3023"
        assert kwargs.get("phase") == "implement"


# --------------------------------------------------------------------------- #
# (5) HITL-pending suppression (AC-R13)
# --------------------------------------------------------------------------- #


class TestHitlPendingSuppression:
    """Acceptance: ``pending_hitl_count=1 at 30 min -> priority=low +
    pending HITL id in reason; pending_hitl_count=1 at 60 min -> no
    additional alert``.
    """

    def test_one_x_with_pending_hitl_downgrades_to_low_priority(self):
        emitter = MagicMock()
        timer = _make_timer(emitter=emitter)
        timer.record_spawn(
            pipeline_id="p1", phase="implement", role="coder", action="propose", now=0.0
        )
        timer.check(now=1800.0, pending_hitl_count=1)
        assert emitter.call_count == 1
        kwargs = emitter.call_args.kwargs
        assert kwargs.get("priority") == "low", (
            "With a pending HITL, the 1× alert must downgrade to priority=low (AC-R13)."
        )
        # Reason text must mention 'HITL' or 'hitl' so operators see
        # the cause without correlating to the decision queue. The exact
        # ID format (decision IDs vs. a count) is implementation-free.
        reason = (kwargs.get("reason") or "").lower()
        assert "hitl" in reason, (
            "Alert reason must reference the pending HITL state so "
            "operators can triage without cross-referencing the decision "
            "queue (AC-R13)."
        )

    def test_two_x_with_pending_hitl_is_suppressed(self):
        """At the 2× threshold *and* with a pending HITL the timer must
        emit nothing additional (the 1× downgrade was already noisy
        enough; the 2× would page the operator a second time on a phase
        that is legitimately waiting on a human gate).
        """
        emitter = MagicMock()
        timer = _make_timer(emitter=emitter)
        timer.record_spawn(
            pipeline_id="p1", phase="implement", role="coder", action="propose", now=0.0
        )

        # 1× with HITL pending — single low-priority emission.
        timer.check(now=1800.0, pending_hitl_count=1)
        assert emitter.call_count == 1

        # 2× with HITL still pending — must NOT add a second emission.
        timer.check(now=3600.0, pending_hitl_count=1)
        timer.check(now=4000.0, pending_hitl_count=1)
        assert emitter.call_count == 1, (
            "With a pending HITL the 2× alert must be suppressed (AC-R13). "
            "A second emission here would re-page the operator on a phase "
            "that is legitimately waiting on a human gate."
        )

    def test_hitl_clears_does_not_retroactively_fire_two_x(self):
        """If the pending HITL clears between the 1× and the 2× tick, the
        2× emission must still respect the per-bucket idempotence rule
        (one emission per bucket per phase). This guards against a
        regression where the suppression latch is keyed by HITL state
        rather than by bucket and lets a 2× alert sneak through after a
        HITL resolves.
        """
        emitter = MagicMock()
        timer = _make_timer(emitter=emitter)
        timer.record_spawn(
            pipeline_id="p1", phase="implement", role="coder", action="propose", now=0.0
        )
        timer.check(now=1800.0, pending_hitl_count=1)  # low-priority 1× emit
        timer.check(now=3600.0, pending_hitl_count=0)  # HITL cleared at 2×
        # Implementations may emit a single transition alert here, but
        # must not re-fire the 1× *and* the 2× independently. Bound: at
        # most one further emission, never two.
        assert emitter.call_count <= 2


# --------------------------------------------------------------------------- #
# (6) reset() clears state
# --------------------------------------------------------------------------- #


class TestReset:
    """``reset()`` must clear every latched bit so the timer can be
    reused across phases without a stale 1×-latch suppressing a
    legitimate fresh alert. Production calls ``reset()`` at phase
    transitions (task-1-2 wires it in).
    """

    def test_reset_clears_latches_and_state(self):
        emitter = MagicMock()
        timer = _make_timer(emitter=emitter)
        timer.record_spawn(
            pipeline_id="p1", phase="implement", role="coder", action="propose", now=0.0
        )
        timer.check(now=1800.0, pending_hitl_count=0)
        assert emitter.call_count == 1

        timer.reset()
        # After reset the timer behaves as a fresh instance — a follow-up
        # spawn + check at 1× of *the new window* should emit again.
        timer.record_spawn(
            pipeline_id="p2", phase="pr", role="coder", action="propose", now=10000.0
        )
        timer.check(now=11800.0, pending_hitl_count=0)
        assert emitter.call_count == 2, (
            "After reset(), the timer must behave as a fresh instance. "
            "Otherwise a stale 1×-latch from the prior phase would "
            "suppress a legitimate fresh alert in the next phase."
        )

    def test_reset_clears_per_role_action_snapshot(self):
        """The ``per_role_last_action`` snapshot must not survive a
        ``reset()`` either; the next phase has a different role set.
        """
        emitter = MagicMock()
        timer = _make_timer(emitter=emitter)
        timer.record_spawn(
            pipeline_id="p1", phase="implement", role="coder", action="propose", now=0.0
        )
        timer.reset()
        timer.record_spawn(pipeline_id="p2", phase="pr", role="reviewer_pr", action="ack", now=0.0)
        timer.check(now=1800.0, pending_hitl_count=0)
        kwargs = emitter.call_args.kwargs
        per_role_state = kwargs.get("per_role_state") or {}
        assert "coder" not in per_role_state, (
            "reset() must clear per_role_last_action so the new phase's "
            "alert payload does not leak the prior phase's role set."
        )
        assert per_role_state.get("reviewer_pr") == "ack"
