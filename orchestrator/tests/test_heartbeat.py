"""Unit tests for ``HeartbeatCoordinator``.

Covers:

* ``should_fan_out_gateway_session`` (issue #2076 NB4) — the route-
  level integration tests in ``test_messages.py`` exercise the
  throttle through the HEARTBEAT endpoint, but a refactor of the
  coordinator is hard to do safely without targeted unit coverage of
  the disable case, the cooldown elapsed/not-elapsed branches, the
  ``clear()`` reset, and concurrent access.
* Per-slice independence of ``is_duplicate`` and ``check_rate_limit``
  (issue #2471) — two slices that share a role must not share dedup
  state or rate budgets.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

# Add orchestrator to path so bare imports resolve.
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

from heartbeat import (  # noqa: E402
    HeartbeatCoordinator,
    get_heartbeat_coordinator,
    reset_heartbeat_coordinator,
)

# Singleton reset between tests is handled by ``_reset_heartbeat_coordinator``
# in ``conftest.py`` (autouse-scoped to all orchestrator tests).


class TestShouldFanOutGatewaySession:
    """Unit tests for the per-(pipeline, slice, role) gateway-fan-out throttle."""

    def test_zero_interval_disables_throttle(self):
        """``min_interval_seconds == 0`` always returns True without recording."""
        coord = HeartbeatCoordinator()

        for _ in range(5):
            assert coord.should_fan_out_gateway_session("p1", None, "coder", 0.0) is True

        # Nothing was recorded, so a subsequent positive-interval call
        # sees no prior fan-out and fires.
        assert coord.should_fan_out_gateway_session("p1", None, "coder", 30.0) is True

    def test_negative_interval_disables_throttle(self):
        """Any non-positive value disables (matches the docstring contract)."""
        coord = HeartbeatCoordinator()

        for _ in range(3):
            assert coord.should_fan_out_gateway_session("p1", None, "coder", -1.0) is True

        # Same as the zero case — no recording happened.
        assert coord.should_fan_out_gateway_session("p1", None, "coder", 30.0) is True

    def test_first_call_fires_and_records(self):
        """First call fires AND records the new timestamp.

        The recording side-effect is verified directly: a second call
        inside the cooldown window can only return False if the first
        call wrote ``_last_fan_out[(p1, None, coder)]``.
        """
        coord = HeartbeatCoordinator()
        assert coord.should_fan_out_gateway_session("p1", None, "coder", 30.0) is True
        # Recording side-effect: second call within the window is suppressed.
        assert coord.should_fan_out_gateway_session("p1", None, "coder", 30.0) is False

    def test_second_call_within_cooldown_suppressed(self):
        """A second call inside the cooldown window returns False."""
        coord = HeartbeatCoordinator()
        assert coord.should_fan_out_gateway_session("p1", None, "coder", 30.0) is True
        # Immediately again — well inside 30s.
        assert coord.should_fan_out_gateway_session("p1", None, "coder", 30.0) is False

    def test_call_after_cooldown_fires(self):
        """Once the window elapses, the next call fires again."""
        coord = HeartbeatCoordinator()
        # 50ms cooldown + 200ms sleep — 150ms headroom keeps the test
        # robust against GC pauses or noisy CI scheduling. ``time.sleep``
        # is a guaranteed lower bound, so the cooldown is always elapsed
        # by the time the third assertion runs.
        assert coord.should_fan_out_gateway_session("p1", None, "coder", 0.05) is True
        assert coord.should_fan_out_gateway_session("p1", None, "coder", 0.05) is False
        time.sleep(0.2)
        assert coord.should_fan_out_gateway_session("p1", None, "coder", 0.05) is True

    def test_different_roles_throttled_independently(self):
        """Throttle key includes the role — different roles don't collide."""
        coord = HeartbeatCoordinator()
        assert coord.should_fan_out_gateway_session("p1", None, "coder", 30.0) is True
        assert coord.should_fan_out_gateway_session("p1", None, "tester", 30.0) is True
        # Both are now suppressed independently.
        assert coord.should_fan_out_gateway_session("p1", None, "coder", 30.0) is False
        assert coord.should_fan_out_gateway_session("p1", None, "tester", 30.0) is False

    def test_different_pipelines_throttled_independently(self):
        """Throttle key includes the pipeline — different pipelines don't collide."""
        coord = HeartbeatCoordinator()
        assert coord.should_fan_out_gateway_session("p1", None, "coder", 30.0) is True
        assert coord.should_fan_out_gateway_session("p2", None, "coder", 30.0) is True
        assert coord.should_fan_out_gateway_session("p1", None, "coder", 30.0) is False
        assert coord.should_fan_out_gateway_session("p2", None, "coder", 30.0) is False

    def test_different_slices_throttled_independently(self):
        """Throttle key includes the slice — sibling slices don't collide (#2471)."""
        coord = HeartbeatCoordinator()
        assert coord.should_fan_out_gateway_session("p1", "slice-2", "reviewer_code", 30.0) is True
        assert coord.should_fan_out_gateway_session("p1", "slice-3", "reviewer_code", 30.0) is True
        # Each sibling is suppressed inside its own window — they did not
        # share the throttle entry.
        assert coord.should_fan_out_gateway_session("p1", "slice-2", "reviewer_code", 30.0) is False
        assert coord.should_fan_out_gateway_session("p1", "slice-3", "reviewer_code", 30.0) is False

    def test_pipeline_level_and_sliced_dont_collide(self):
        """``slice_id=None`` is a distinct bucket from any ``slice-<N>`` (#2471)."""
        coord = HeartbeatCoordinator()
        # Pipeline-level agent fires.
        assert coord.should_fan_out_gateway_session("p1", None, "coder", 30.0) is True
        # Slice-scoped agent in the same pipeline + role still fires —
        # it lives in a separate bucket.
        assert coord.should_fan_out_gateway_session("p1", "slice-1", "coder", 30.0) is True
        # Both are now suppressed inside their own windows.
        assert coord.should_fan_out_gateway_session("p1", None, "coder", 30.0) is False
        assert coord.should_fan_out_gateway_session("p1", "slice-1", "coder", 30.0) is False

    def test_suppressed_call_does_not_advance_recorded_timestamp(self):
        """Hot-looping must not push the cooldown forward indefinitely.

        Uses a 50ms cooldown + 200ms sleep — 150ms headroom keeps the
        ``time.time()`` reads robust against scheduling jitter on CI.
        """
        coord = HeartbeatCoordinator()
        assert coord.should_fan_out_gateway_session("p1", None, "coder", 0.05) is True
        # Hammer the coordinator inside the window — every call returns
        # False, but the recorded timestamp stays at the initial fire.
        for _ in range(10):
            assert coord.should_fan_out_gateway_session("p1", None, "coder", 0.05) is False
        time.sleep(0.2)
        # Still fires once the original window elapses, which proves the
        # suppressed calls didn't reset the clock.
        assert coord.should_fan_out_gateway_session("p1", None, "coder", 0.05) is True

    def test_clear_drops_throttle_state_for_pipeline(self):
        """``clear(pipeline)`` lets the next call fire immediately."""
        coord = HeartbeatCoordinator()
        assert coord.should_fan_out_gateway_session("p1", None, "coder", 30.0) is True
        assert coord.should_fan_out_gateway_session("p1", None, "coder", 30.0) is False
        coord.clear("p1")
        assert coord.should_fan_out_gateway_session("p1", None, "coder", 30.0) is True

    def test_clear_only_affects_targeted_pipeline(self):
        """``clear(p1)`` must not drop p2's throttle entries."""
        coord = HeartbeatCoordinator()
        coord.should_fan_out_gateway_session("p1", None, "coder", 30.0)
        coord.should_fan_out_gateway_session("p2", None, "coder", 30.0)
        coord.clear("p1")
        # p1 reset, fires again.
        assert coord.should_fan_out_gateway_session("p1", None, "coder", 30.0) is True
        # p2 untouched, still suppressed.
        assert coord.should_fan_out_gateway_session("p2", None, "coder", 30.0) is False

    def test_clear_drops_all_slice_scoped_entries_for_pipeline(self):
        """``clear(p1)`` must sweep every slice's entry, not just ``slice_id=None`` (#2471)."""
        coord = HeartbeatCoordinator()
        coord.should_fan_out_gateway_session("p1", None, "coder", 30.0)
        coord.should_fan_out_gateway_session("p1", "slice-1", "coder", 30.0)
        coord.should_fan_out_gateway_session("p1", "slice-2", "coder", 30.0)
        coord.clear("p1")
        # All three buckets reset — first post-clear call in each fires.
        assert coord.should_fan_out_gateway_session("p1", None, "coder", 30.0) is True
        assert coord.should_fan_out_gateway_session("p1", "slice-1", "coder", 30.0) is True
        assert coord.should_fan_out_gateway_session("p1", "slice-2", "coder", 30.0) is True

    def test_concurrent_callers_only_one_wins(self):
        """Under contention, exactly one thread should see True per window."""
        coord = HeartbeatCoordinator()
        results: list[bool] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(20)

        def worker():
            barrier.wait()
            res = coord.should_fan_out_gateway_session("p1", None, "coder", 30.0)
            with results_lock:
                results.append(res)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly one thread crossed the throttle boundary first.
        assert sum(results) == 1
        assert len(results) == 20


class TestIsDuplicate:
    """Per-(pipeline, slice, role) dedup memory (#2471)."""

    def test_first_observation_is_not_duplicate(self):
        coord = HeartbeatCoordinator()
        assert coord.is_duplicate("p1", None, "coder", "WORKING", None) is False

    def test_repeat_after_record_is_duplicate(self):
        coord = HeartbeatCoordinator()
        coord.record_state("p1", None, "coder", "WORKING", None)
        assert coord.is_duplicate("p1", None, "coder", "WORKING", None) is True

    def test_different_state_is_not_duplicate(self):
        coord = HeartbeatCoordinator()
        coord.record_state("p1", None, "coder", "WORKING", None)
        assert coord.is_duplicate("p1", None, "coder", "IDLE", None) is False

    def test_sibling_slices_have_independent_dedup(self):
        """slice-2 recording WORKING must not dedup slice-3's first WORKING (#2471).

        Without slice-scope, slice-2's record poisons slice-3's first
        non-exempt heartbeat for the same role into a silent dedup —
        downstream consumers (HealthMonitor, overseer, UI) see only one
        of the N siblings' state transitions even though all N are
        independent agents in independent pods.
        """
        coord = HeartbeatCoordinator()
        coord.record_state("p1", "slice-2", "reviewer_code", "WORKING", None)
        # Slice-3's first WORKING observation is fresh — different bucket.
        assert coord.is_duplicate("p1", "slice-3", "reviewer_code", "WORKING", None) is False
        # And after slice-3 records, it dedups in its own bucket only.
        coord.record_state("p1", "slice-3", "reviewer_code", "WORKING", None)
        assert coord.is_duplicate("p1", "slice-3", "reviewer_code", "WORKING", None) is True
        # Slice-2's bucket is unaffected by slice-3's writes.
        assert coord.is_duplicate("p1", "slice-2", "reviewer_code", "WORKING", None) is True

    def test_pipeline_level_does_not_collide_with_slice(self):
        """``slice_id=None`` and a real ``slice-<N>`` are different buckets."""
        coord = HeartbeatCoordinator()
        coord.record_state("p1", None, "coder", "WORKING", None)
        assert coord.is_duplicate("p1", "slice-1", "coder", "WORKING", None) is False

    def test_clear_resets_dedup_for_all_slices_in_pipeline(self):
        """Phase transitions must reset every slice's dedup memory."""
        coord = HeartbeatCoordinator()
        coord.record_state("p1", "slice-1", "coder", "WORKING", None)
        coord.record_state("p1", "slice-2", "coder", "WORKING", None)
        coord.record_state("p1", None, "coder", "WORKING", None)
        coord.clear("p1")
        assert coord.is_duplicate("p1", "slice-1", "coder", "WORKING", None) is False
        assert coord.is_duplicate("p1", "slice-2", "coder", "WORKING", None) is False
        assert coord.is_duplicate("p1", None, "coder", "WORKING", None) is False


class TestCheckRateLimit:
    """Per-(pipeline, slice, role) rate budgets (#2471)."""

    def test_allows_up_to_limit(self):
        coord = HeartbeatCoordinator()
        for _ in range(3):
            assert coord.check_rate_limit("p1", None, "coder", limit_per_minute=3).allowed is True

    def test_rejects_after_limit(self):
        coord = HeartbeatCoordinator()
        for _ in range(3):
            coord.check_rate_limit("p1", None, "coder", limit_per_minute=3)
        decision = coord.check_rate_limit("p1", None, "coder", limit_per_minute=3)
        assert decision.allowed is False
        assert decision.retry_after_seconds >= 1

    def test_sibling_slices_have_independent_budgets(self):
        """slice-2 saturating its budget must not drop slice-3's beats (#2471).

        ``EGG_HEARTBEAT_RATE_LIMIT`` defaults to 20/min. Under wide
        fan-out, a popular role (e.g. reviewer-code in 4 slices each
        emitting wait-loop's 1/min plus other beats) can plausibly hit
        the per-role ceiling under the old key shape, silently dropping
        slice-X's beat because slice-Y filled the window. Slice-scoping
        the key gives each sibling its own bucket.
        """
        coord = HeartbeatCoordinator()
        # Saturate slice-2 at the limit.
        for _ in range(3):
            assert (
                coord.check_rate_limit("p1", "slice-2", "reviewer_code", limit_per_minute=3).allowed
                is True
            )
        # slice-2 is now over the line.
        assert (
            coord.check_rate_limit("p1", "slice-2", "reviewer_code", limit_per_minute=3).allowed
            is False
        )
        # slice-3 is in a separate bucket — it can still get its full budget.
        for _ in range(3):
            assert (
                coord.check_rate_limit("p1", "slice-3", "reviewer_code", limit_per_minute=3).allowed
                is True
            )

    def test_pipeline_level_does_not_collide_with_slice(self):
        coord = HeartbeatCoordinator()
        for _ in range(3):
            coord.check_rate_limit("p1", None, "coder", limit_per_minute=3)
        # slice-scoped agent in the same pipeline + role still gets a
        # fresh budget.
        assert coord.check_rate_limit("p1", "slice-1", "coder", limit_per_minute=3).allowed is True

    def test_clear_drops_rate_state_for_all_slices_in_pipeline(self):
        coord = HeartbeatCoordinator()
        for _ in range(3):
            coord.check_rate_limit("p1", "slice-1", "coder", limit_per_minute=3)
            coord.check_rate_limit("p1", "slice-2", "coder", limit_per_minute=3)
        coord.clear("p1")
        # Both slices' windows reset.
        assert coord.check_rate_limit("p1", "slice-1", "coder", limit_per_minute=3).allowed is True
        assert coord.check_rate_limit("p1", "slice-2", "coder", limit_per_minute=3).allowed is True


class TestSingletonAccessor:
    def test_get_returns_same_instance(self):
        a = get_heartbeat_coordinator()
        b = get_heartbeat_coordinator()
        assert a is b

    def test_reset_replaces_instance(self):
        a = get_heartbeat_coordinator()
        reset_heartbeat_coordinator()
        b = get_heartbeat_coordinator()
        assert a is not b
