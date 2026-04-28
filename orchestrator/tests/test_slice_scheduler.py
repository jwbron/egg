"""Tests for ``orchestrator.slice_scheduler.SliceScheduler`` (#2137 TASK-3-5).

The scheduler is intentionally pure-Python, so these tests exercise the
state machine deterministically without any container / gateway / git
plumbing. Coverage:

* Construction: graph build from ``Contract.slices`` honours
  ``Slice.dependencies``; root slices start READY and dependent slices
  start PENDING.
* ``iter_ready`` yields under the parallel-slice cap and respects
  RUNNING in-flight count.
* ``mark_spawned`` / ``record_complete`` / ``record_failure`` flip the
  runtime state correctly.
* ``record_complete`` of a parent unblocks PENDING children to READY.
* ``record_cycle`` increments local + global counters and trips on
  either cap.
* ``record_failure`` arms a cascade with the configured grace seconds
  (including the zero-grace edge case the NACK called out).
* ``poll_cascades`` fires only when the grace window elapses, marks
  the downstream subtree BLOCKED_ON_FAILED_DEPENDENCY, and is
  idempotent (a fired cascade is not re-emitted on subsequent polls).
* ``cancel_cascade`` removes a pending cascade so HITL resolution
  prevents the lockout.
* ``teardown_slice`` / ``respawn_slice`` operate as the slice-
  addressable hooks promised for the #2199 follow-up.
* ``get_slice_status`` returns a copy (callers can't mutate
  scheduler state via the returned struct).
* ``all_done`` is True only when every slice is in a terminal state.
"""

from __future__ import annotations

import sys
from pathlib import Path

# sys.path setup — orchestrator + shared. Match the canonical pattern
# used by ``test_concurrent_executor_staging_branch.py`` so the test is
# importable from both pytest and ad-hoc runs.
_project_root = Path(__file__).parent.parent.parent
_orchestrator_path = _project_root / "orchestrator"
_shared_path = _project_root / "shared"
for _p in (_orchestrator_path, _shared_path):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from egg_contracts.models import Contract, IssueInfo, Slice  # noqa: E402
from slice_scheduler import (  # noqa: E402
    CascadeEvent,
    SchedulerSliceState,
    SliceScheduler,
)


def _contract_with(*slices: Slice) -> Contract:
    return Contract(
        issue=IssueInfo(number=2137, title="t", url="u"),
        slices=list(slices),
    )


def _slice(id_: str, deps: list[str] | None = None) -> Slice:
    return Slice(id=id_, name=f"s {id_}", dependencies=deps or [])


# ---------- Construction ----------


class TestInitialState:
    def test_root_slices_start_ready(self) -> None:
        contract = _contract_with(_slice("slice-1"))
        sched = SliceScheduler(contract)
        runtime = sched.get_slice_status("slice-1")
        assert runtime is not None
        assert runtime.state is SchedulerSliceState.READY
        assert runtime.parent_slice_id is None

    def test_dependent_slice_starts_pending(self) -> None:
        contract = _contract_with(_slice("slice-1"), _slice("slice-2", ["slice-1"]))
        sched = SliceScheduler(contract)
        assert sched.get_slice_status("slice-2").state is SchedulerSliceState.PENDING
        assert sched.get_slice_status("slice-2").parent_slice_id == "slice-1"

    def test_unknown_slice_returns_none(self) -> None:
        contract = _contract_with(_slice("slice-1"))
        sched = SliceScheduler(contract)
        assert sched.get_slice_status("slice-99") is None

    def test_list_slices_returns_copies(self) -> None:
        contract = _contract_with(_slice("slice-1"))
        sched = SliceScheduler(contract)
        runtime = sched.list_slices()[0]
        runtime.state = SchedulerSliceState.FAILED
        # Mutating the returned copy must NOT affect the scheduler's
        # internal state.
        assert sched.get_slice_status("slice-1").state is SchedulerSliceState.READY


# ---------- iter_ready / mark_spawned ----------


class TestIterReady:
    def test_yields_only_ready_slices(self) -> None:
        contract = _contract_with(_slice("slice-1"), _slice("slice-2", ["slice-1"]))
        sched = SliceScheduler(contract)
        ready = list(sched.iter_ready())
        # Only slice-1 is ready; slice-2 is PENDING.
        assert ready == [("slice-1", None)]

    def test_iter_ready_returns_parent(self) -> None:
        contract = _contract_with(_slice("slice-1"), _slice("slice-2", ["slice-1"]))
        sched = SliceScheduler(contract)
        sched.mark_spawned("slice-1")
        sched.record_complete("slice-1")
        # slice-2 is now READY with slice-1 as parent.
        ready = list(sched.iter_ready())
        assert ready == [("slice-2", "slice-1")]

    def test_iter_ready_caps_at_parallel_slices(self) -> None:
        # 5 disjoint roots, cap = 2 → only 2 yielded per tick.
        contract = _contract_with(*[_slice(f"slice-{i}") for i in range(1, 6)])
        sched = SliceScheduler(contract, max_parallel_slices=2)
        ready = list(sched.iter_ready())
        assert len(ready) == 2

    def test_iter_ready_respects_in_flight_running_count(self) -> None:
        contract = _contract_with(*[_slice(f"slice-{i}") for i in range(1, 6)])
        sched = SliceScheduler(contract, max_parallel_slices=2)
        # Spawn one slice — in-flight is now 1 — so only 1 more slot.
        first_batch = list(sched.iter_ready())
        sched.mark_spawned(first_batch[0][0])
        second_batch = list(sched.iter_ready())
        # cap = 2, in-flight = 1 → available = 1 → yield exactly one
        # additional slice (already-running one is excluded).
        assert len(second_batch) == 1

    def test_iter_ready_clamps_min_one(self) -> None:
        # max_parallel_slices=0 must clamp to 1 in __init__ (sanity).
        contract = _contract_with(_slice("slice-1"))
        sched = SliceScheduler(contract, max_parallel_slices=0)
        assert sched.max_parallel_slices >= 1

    def test_iter_ready_zero_when_cap_full(self) -> None:
        contract = _contract_with(_slice("slice-1"), _slice("slice-2"))
        sched = SliceScheduler(contract, max_parallel_slices=1)
        first = list(sched.iter_ready())
        sched.mark_spawned(first[0][0])
        # Cap = 1, in-flight = 1 → next call yields nothing.
        assert list(sched.iter_ready()) == []


# ---------- mark_spawned / record_complete / record_failure ----------


class TestMarkSpawned:
    def test_flips_state_to_running(self) -> None:
        contract = _contract_with(_slice("slice-1"))
        sched = SliceScheduler(contract)
        sched.mark_spawned("slice-1")
        assert sched.get_slice_status("slice-1").state is SchedulerSliceState.RUNNING

    def test_unknown_slice_is_silent(self) -> None:
        contract = _contract_with(_slice("slice-1"))
        sched = SliceScheduler(contract)
        # Should not raise.
        sched.mark_spawned("slice-99")


class TestRecordComplete:
    def test_marks_complete_and_unblocks_children(self) -> None:
        contract = _contract_with(
            _slice("slice-1"),
            _slice("slice-2", ["slice-1"]),
            _slice("slice-3", ["slice-1"]),
        )
        sched = SliceScheduler(contract)
        sched.mark_spawned("slice-1")
        sched.record_complete("slice-1")
        assert sched.get_slice_status("slice-1").state is SchedulerSliceState.COMPLETE
        # Both PENDING children are now READY.
        assert sched.get_slice_status("slice-2").state is SchedulerSliceState.READY
        assert sched.get_slice_status("slice-3").state is SchedulerSliceState.READY

    def test_grandchildren_remain_pending_until_their_parent_completes(self) -> None:
        contract = _contract_with(
            _slice("slice-1"),
            _slice("slice-2", ["slice-1"]),
            _slice("slice-3", ["slice-2"]),
        )
        sched = SliceScheduler(contract)
        sched.mark_spawned("slice-1")
        sched.record_complete("slice-1")
        # slice-2 promoted to READY. slice-3 should NOT be promoted
        # yet — slice-2 is its parent, not slice-1.
        assert sched.get_slice_status("slice-2").state is SchedulerSliceState.READY
        assert sched.get_slice_status("slice-3").state is SchedulerSliceState.PENDING

    def test_unblocks_blocked_on_failed_dependency_children(self) -> None:
        # Cascade-then-respawn-then-complete edge case (v2.1 bug fix):
        # a child slice that has been transitively blocked by a failed
        # parent must be promoted back to READY when the failure is
        # resolved (the failed parent is restarted and ultimately
        # reaches CONSENSUS_CONFIRMED). Without this transition the
        # child stays wedged in BLOCKED_ON_FAILED_DEPENDENCY forever
        # even after the parent recovers — a silent deadlock.
        contract = _contract_with(
            _slice("slice-1"),
            _slice("slice-2", ["slice-1"]),
        )
        clock = _FakeClock()
        sched = SliceScheduler(contract, failure_grace_seconds=0.0, time_fn=clock)
        # Fail slice-1, fire the cascade so slice-2 is BLOCKED.
        sched.record_failure("slice-1")
        sched.poll_cascades()
        assert (
            sched.get_slice_status("slice-2").state
            is SchedulerSliceState.BLOCKED_ON_FAILED_DEPENDENCY
        )
        # HITL respawns slice-1; it goes RUNNING → COMPLETE.
        sched.respawn_slice("slice-1")
        sched.mark_spawned("slice-1")
        sched.record_complete("slice-1")
        # slice-2 must be promoted back to READY by ``_unblock_children``.
        assert sched.get_slice_status("slice-2").state is SchedulerSliceState.READY


# ---------- record_cycle and HITL escalation ----------


class TestRecordCycle:
    def test_local_cap_trips_after_three_cycles(self) -> None:
        contract = _contract_with(_slice("slice-1"))
        sched = SliceScheduler(contract, local_max_cycles=3, global_max_cycles=99)
        sched.mark_spawned("slice-1")
        assert sched.record_cycle("slice-1") is False  # 1
        assert sched.record_cycle("slice-1") is False  # 2
        assert sched.record_cycle("slice-1") is True  # 3 → tripped
        runtime = sched.get_slice_status("slice-1")
        assert runtime.local_cycles == 3

    def test_global_cap_trips_across_multiple_slices(self) -> None:
        contract = _contract_with(_slice("slice-1"), _slice("slice-2"), _slice("slice-3"))
        sched = SliceScheduler(contract, local_max_cycles=99, global_max_cycles=3)
        for sid in ("slice-1", "slice-2", "slice-3"):
            sched.mark_spawned(sid)
        # Across three slices, three cycles total → global cap hits.
        assert sched.record_cycle("slice-1") is False
        assert sched.record_cycle("slice-2") is False
        assert sched.record_cycle("slice-3") is True
        assert sched.global_cycles == 3

    def test_escalator_invoked_with_local_reason(self) -> None:
        captured: list[tuple[str, str]] = []
        contract = _contract_with(_slice("slice-1"))
        sched = SliceScheduler(
            contract,
            local_max_cycles=2,
            global_max_cycles=99,
            hitl_escalator=lambda sid, reason: captured.append((sid, reason)),
        )
        sched.record_cycle("slice-1")
        sched.record_cycle("slice-1")  # trips local cap
        assert len(captured) == 1
        assert captured[0][0] == "slice-1"
        assert "local cap" in captured[0][1]

    def test_escalator_failure_does_not_propagate(self) -> None:
        # The scheduler must not crash if the HITL escalator raises.
        contract = _contract_with(_slice("slice-1"))
        sched = SliceScheduler(
            contract,
            local_max_cycles=1,
            hitl_escalator=lambda *_: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        # Should not raise.
        assert sched.record_cycle("slice-1") is True

    def test_record_cycle_unknown_slice_returns_false(self) -> None:
        contract = _contract_with(_slice("slice-1"))
        sched = SliceScheduler(contract)
        assert sched.record_cycle("slice-99") is False
        assert sched.global_cycles == 0


# ---------- record_failure / poll_cascades / cancel_cascade ----------


class _FakeClock:
    """Deterministic time source for cascade tests."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestRecordFailure:
    def test_marks_failed_and_arms_cascade(self) -> None:
        contract = _contract_with(_slice("slice-1"))
        clock = _FakeClock()
        sched = SliceScheduler(contract, failure_grace_seconds=60.0, time_fn=clock)
        sched.record_failure("slice-1")
        runtime = sched.get_slice_status("slice-1")
        assert runtime.state is SchedulerSliceState.FAILED
        assert runtime.cascade_due_at == 60.0

    def test_unknown_slice_silent(self) -> None:
        contract = _contract_with(_slice("slice-1"))
        sched = SliceScheduler(contract)
        sched.record_failure("slice-99")  # no raise


class TestPollCascades:
    def _setup_failed_chain(self, *, grace: float) -> tuple[SliceScheduler, _FakeClock]:
        contract = _contract_with(
            _slice("slice-1"),
            _slice("slice-2", ["slice-1"]),
            _slice("slice-3", ["slice-2"]),
            _slice("slice-99"),  # independent, must NOT be blocked
        )
        clock = _FakeClock()
        sched = SliceScheduler(contract, failure_grace_seconds=grace, time_fn=clock)
        sched.mark_spawned("slice-1")
        sched.record_failure("slice-1")
        return sched, clock

    def test_no_event_before_grace_window_expires(self) -> None:
        sched, clock = self._setup_failed_chain(grace=60.0)
        clock.advance(59.0)
        assert sched.poll_cascades() == []

    def test_event_fires_after_grace_window(self) -> None:
        sched, clock = self._setup_failed_chain(grace=60.0)
        clock.advance(60.0)
        events = sched.poll_cascades()
        assert len(events) == 1
        evt = events[0]
        assert isinstance(evt, CascadeEvent)
        assert evt.failed_slice_id == "slice-1"
        # Subtree includes children + grandchildren but NOT the failed
        # slice itself.
        assert sorted(evt.blocked_subtree) == ["slice-2", "slice-3"]

    def test_blocked_subtree_marks_descendants(self) -> None:
        sched, clock = self._setup_failed_chain(grace=60.0)
        clock.advance(60.0)
        sched.poll_cascades()
        assert (
            sched.get_slice_status("slice-2").state
            is SchedulerSliceState.BLOCKED_ON_FAILED_DEPENDENCY
        )
        assert (
            sched.get_slice_status("slice-3").state
            is SchedulerSliceState.BLOCKED_ON_FAILED_DEPENDENCY
        )
        # Sibling root must remain READY — failure semantics
        # (refine-phase decision-2): only the failed slice's downstream
        # subtree is blocked.
        assert sched.get_slice_status("slice-99").state is SchedulerSliceState.READY

    def test_idempotent_does_not_refire(self) -> None:
        sched, clock = self._setup_failed_chain(grace=60.0)
        clock.advance(60.0)
        first = sched.poll_cascades()
        assert len(first) == 1
        # Polling again must NOT emit a duplicate event.
        clock.advance(120.0)
        assert sched.poll_cascades() == []

    def test_zero_grace_fires_on_next_poll_immediately(self) -> None:
        # Edge case called out in the NACK: ``failure_grace_seconds=0``
        # should trip on the very next poll, not divide by zero or hang.
        sched, clock = self._setup_failed_chain(grace=0.0)
        # Don't advance the clock — cascade is due at exactly t=0.
        events = sched.poll_cascades()
        assert len(events) == 1


class TestCancelCascade:
    def test_cancelled_cascade_does_not_fire(self) -> None:
        contract = _contract_with(_slice("slice-1"), _slice("slice-2", ["slice-1"]))
        clock = _FakeClock()
        sched = SliceScheduler(contract, failure_grace_seconds=60.0, time_fn=clock)
        sched.record_failure("slice-1")
        sched.cancel_cascade("slice-1")
        clock.advance(120.0)
        assert sched.poll_cascades() == []
        # slice-2 must remain in its prior state (PENDING) — not
        # promoted to BLOCKED_ON_FAILED_DEPENDENCY.
        assert sched.get_slice_status("slice-2").state is SchedulerSliceState.PENDING


# ---------- Slice-addressable hooks (#2199 surface) ----------


class TestTeardownAndRespawn:
    def test_teardown_then_respawn(self) -> None:
        contract = _contract_with(_slice("slice-1"))
        sched = SliceScheduler(contract)
        sched.mark_spawned("slice-1")
        assert sched.teardown_slice("slice-1") is True
        assert sched.get_slice_status("slice-1").state is SchedulerSliceState.TEARDOWN
        assert sched.respawn_slice("slice-1") is True
        assert sched.get_slice_status("slice-1").state is SchedulerSliceState.READY

    def test_respawn_refuses_already_running(self) -> None:
        contract = _contract_with(_slice("slice-1"))
        sched = SliceScheduler(contract)
        sched.mark_spawned("slice-1")
        # State is RUNNING; respawn must refuse.
        assert sched.respawn_slice("slice-1") is False
        assert sched.get_slice_status("slice-1").state is SchedulerSliceState.RUNNING

    def test_respawn_clears_pending_cascade(self) -> None:
        contract = _contract_with(_slice("slice-1"))
        clock = _FakeClock()
        sched = SliceScheduler(contract, failure_grace_seconds=60.0, time_fn=clock)
        sched.record_failure("slice-1")
        sched.respawn_slice("slice-1")
        # The pending cascade must be dropped — the slice has been
        # restarted, the failure is no longer authoritative.
        clock.advance(60.0)
        assert sched.poll_cascades() == []

    def test_teardown_unknown_slice_returns_false(self) -> None:
        contract = _contract_with(_slice("slice-1"))
        sched = SliceScheduler(contract)
        assert sched.teardown_slice("slice-99") is False
        assert sched.respawn_slice("slice-99") is False


# ---------- all_done ----------


class TestAllDone:
    def test_false_while_anything_pending(self) -> None:
        contract = _contract_with(_slice("slice-1"), _slice("slice-2", ["slice-1"]))
        sched = SliceScheduler(contract)
        assert sched.all_done() is False

    def test_true_when_all_complete(self) -> None:
        contract = _contract_with(_slice("slice-1"))
        sched = SliceScheduler(contract)
        sched.mark_spawned("slice-1")
        sched.record_complete("slice-1")
        assert sched.all_done() is True

    def test_true_when_failed_subtree_blocked(self) -> None:
        contract = _contract_with(_slice("slice-1"), _slice("slice-2", ["slice-1"]))
        clock = _FakeClock()
        sched = SliceScheduler(contract, failure_grace_seconds=0.0, time_fn=clock)
        sched.record_failure("slice-1")
        sched.poll_cascades()
        # slice-1 FAILED, slice-2 BLOCKED_ON_FAILED_DEPENDENCY → all
        # terminal.
        assert sched.all_done() is True
