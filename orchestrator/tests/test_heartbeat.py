"""Unit tests for ``HeartbeatCoordinator``.

Covers:

* Per-slice independence of ``is_duplicate`` and ``check_rate_limit``
  (issue #2471) — two slices that share a role must not share dedup
  state or rate budgets.

The gateway-session fan-out throttle (``should_fan_out_gateway_session``)
and the orchestrator-mode guard were removed in #3164 along with the
in-pod wait arm; the coordinator no longer has any gateway side effect.
"""

from __future__ import annotations

import sys
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


# ---------------------------------------------------------------------------
# Orchestrator-mode heartbeat semantics (#3064 / #3164)
# ---------------------------------------------------------------------------
# Under the orchestrator-owned event loop, agents are spawned per-event and
# exit; between events there is no sender, so a heartbeat gap is normal. The
# gateway-session fan-out and the coordinator's mode flag were removed in
# #3164 — the coordinator now only tracks dedup/rate state and raises no
# alert itself. The remaining test pins that the coordinator can be queried
# without error for a key that has never sent a heartbeat.
# ---------------------------------------------------------------------------


class TestModeGuard:
    """Coordinator has no gateway/alert side effect (#3064 / #3164)."""

    def test_absent_sender_in_orchestrator_mode_does_not_trip(self):
        """In orchestrator mode, an absent sender's silence is normal.

        In orchestator mode, agents are spawned per-event and exit. Between
        events there is no sender, so the absence of a heartbeat from any
        role between spawns should not trip any alert. The coordinator
        itself does not raise alerts — this test documents the contract so
        callers (health_monitor, routes/messages.py) know they must check
        ownership before treating a heartbeat gap as anomalous.

        The actual alert-silence logic lives in the health monitor; this
        test ensures the coordinator exposes enough information for the
        monitor to make that decision.
        """
        coord = HeartbeatCoordinator()
        # The coordinator's role here is purely structural: it tracks
        # per-key state. The orchestrator-mode decision is made by the
        # health monitor (see TestOwnershipModeHeartbeatMatrix). This test
        # confirms that the coordinator can be queried without error for
        # keys that have never sent a heartbeat.
        assert coord.is_duplicate("p1", "slice-5", "coder", "WORKING", None) is False
