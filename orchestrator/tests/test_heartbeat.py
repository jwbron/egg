"""Unit tests for ``HeartbeatCoordinator``.

Focused on ``should_fan_out_gateway_session`` (issue #2076 NB4) — the
route-level integration tests in ``test_messages.py`` exercise the
throttle through the HEARTBEAT endpoint, but a refactor of the
coordinator is hard to do safely without targeted unit coverage of the
disable case, the cooldown elapsed/not-elapsed branches, the ``clear()``
reset, and concurrent access.
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
    """Unit tests for the per-(pipeline, role) gateway-fan-out throttle."""

    def test_zero_interval_disables_throttle(self):
        """``min_interval_seconds == 0`` always returns True without recording."""
        coord = HeartbeatCoordinator()

        for _ in range(5):
            assert coord.should_fan_out_gateway_session("p1", "coder", 0.0) is True

        # Nothing was recorded, so a subsequent positive-interval call
        # sees no prior fan-out and fires.
        assert coord.should_fan_out_gateway_session("p1", "coder", 30.0) is True

    def test_negative_interval_disables_throttle(self):
        """Any non-positive value disables (matches the docstring contract)."""
        coord = HeartbeatCoordinator()

        for _ in range(3):
            assert coord.should_fan_out_gateway_session("p1", "coder", -1.0) is True

        # Same as the zero case — no recording happened.
        assert coord.should_fan_out_gateway_session("p1", "coder", 30.0) is True

    def test_first_call_fires_and_records(self):
        """First call fires AND records the new timestamp.

        The recording side-effect is verified directly: a second call
        inside the cooldown window can only return False if the first
        call wrote ``_last_fan_out[(p1, coder)]``.
        """
        coord = HeartbeatCoordinator()
        assert coord.should_fan_out_gateway_session("p1", "coder", 30.0) is True
        # Recording side-effect: second call within the window is suppressed.
        assert coord.should_fan_out_gateway_session("p1", "coder", 30.0) is False

    def test_second_call_within_cooldown_suppressed(self):
        """A second call inside the cooldown window returns False."""
        coord = HeartbeatCoordinator()
        assert coord.should_fan_out_gateway_session("p1", "coder", 30.0) is True
        # Immediately again — well inside 30s.
        assert coord.should_fan_out_gateway_session("p1", "coder", 30.0) is False

    def test_call_after_cooldown_fires(self):
        """Once the window elapses, the next call fires again."""
        coord = HeartbeatCoordinator()
        # 50ms cooldown + 200ms sleep — 150ms headroom keeps the test
        # robust against GC pauses or noisy CI scheduling. ``time.sleep``
        # is a guaranteed lower bound, so the cooldown is always elapsed
        # by the time the third assertion runs.
        assert coord.should_fan_out_gateway_session("p1", "coder", 0.05) is True
        assert coord.should_fan_out_gateway_session("p1", "coder", 0.05) is False
        time.sleep(0.2)
        assert coord.should_fan_out_gateway_session("p1", "coder", 0.05) is True

    def test_different_roles_throttled_independently(self):
        """Throttle key includes the role — different roles don't collide."""
        coord = HeartbeatCoordinator()
        assert coord.should_fan_out_gateway_session("p1", "coder", 30.0) is True
        assert coord.should_fan_out_gateway_session("p1", "tester", 30.0) is True
        # Both are now suppressed independently.
        assert coord.should_fan_out_gateway_session("p1", "coder", 30.0) is False
        assert coord.should_fan_out_gateway_session("p1", "tester", 30.0) is False

    def test_different_pipelines_throttled_independently(self):
        """Throttle key includes the pipeline — different pipelines don't collide."""
        coord = HeartbeatCoordinator()
        assert coord.should_fan_out_gateway_session("p1", "coder", 30.0) is True
        assert coord.should_fan_out_gateway_session("p2", "coder", 30.0) is True
        assert coord.should_fan_out_gateway_session("p1", "coder", 30.0) is False
        assert coord.should_fan_out_gateway_session("p2", "coder", 30.0) is False

    def test_suppressed_call_does_not_advance_recorded_timestamp(self):
        """Hot-looping must not push the cooldown forward indefinitely.

        Uses a 50ms cooldown + 200ms sleep — 150ms headroom keeps the
        ``time.time()`` reads robust against scheduling jitter on CI.
        """
        coord = HeartbeatCoordinator()
        assert coord.should_fan_out_gateway_session("p1", "coder", 0.05) is True
        # Hammer the coordinator inside the window — every call returns
        # False, but the recorded timestamp stays at the initial fire.
        for _ in range(10):
            assert coord.should_fan_out_gateway_session("p1", "coder", 0.05) is False
        time.sleep(0.2)
        # Still fires once the original window elapses, which proves the
        # suppressed calls didn't reset the clock.
        assert coord.should_fan_out_gateway_session("p1", "coder", 0.05) is True

    def test_clear_drops_throttle_state_for_pipeline(self):
        """``clear(pipeline)`` lets the next call fire immediately."""
        coord = HeartbeatCoordinator()
        assert coord.should_fan_out_gateway_session("p1", "coder", 30.0) is True
        assert coord.should_fan_out_gateway_session("p1", "coder", 30.0) is False
        coord.clear("p1")
        assert coord.should_fan_out_gateway_session("p1", "coder", 30.0) is True

    def test_clear_only_affects_targeted_pipeline(self):
        """``clear(p1)`` must not drop p2's throttle entries."""
        coord = HeartbeatCoordinator()
        coord.should_fan_out_gateway_session("p1", "coder", 30.0)
        coord.should_fan_out_gateway_session("p2", "coder", 30.0)
        coord.clear("p1")
        # p1 reset, fires again.
        assert coord.should_fan_out_gateway_session("p1", "coder", 30.0) is True
        # p2 untouched, still suppressed.
        assert coord.should_fan_out_gateway_session("p2", "coder", 30.0) is False

    def test_concurrent_callers_only_one_wins(self):
        """Under contention, exactly one thread should see True per window."""
        coord = HeartbeatCoordinator()
        results: list[bool] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(20)

        def worker():
            barrier.wait()
            res = coord.should_fan_out_gateway_session("p1", "coder", 30.0)
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
