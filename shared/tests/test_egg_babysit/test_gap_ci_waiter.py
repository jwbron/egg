"""Gap tests for egg_babysit.ci_waiter — stale detection, edge cases."""

from unittest.mock import patch

from egg_babysit.ci_waiter import _STALE_THRESHOLD, _aggregate_status, wait_for_ci
from egg_babysit.types import CICheckResult, CICheckStatus


def _make_check(name: str, status: CICheckStatus) -> CICheckResult:
    return CICheckResult(name=name, status=status, conclusion=status.value.upper())


class TestStaleDetection:
    """Test stale check detection logic."""

    @patch("egg_babysit.ci_waiter.fetch_ci_checks")
    @patch("egg_babysit.ci_waiter.time.monotonic")
    @patch("egg_babysit.ci_waiter.time.sleep", return_value=None)
    def test_stale_after_threshold_polls(self, mock_sleep, mock_time, mock_fetch):
        """Checks become STALE after _STALE_THRESHOLD polls with no change."""
        # Simulate enough time for many polls without timeout
        poll_counter = [0]

        def monotonic_side_effect():
            poll_counter[0] += 1
            return float(poll_counter[0])  # 1 second per call

        mock_time.side_effect = monotonic_side_effect

        # Always return the same pending checks — no status change
        pending_checks = [_make_check("lint", CICheckStatus.PENDING)]
        mock_fetch.return_value = pending_checks

        status, checks = wait_for_ci(42, "owner/repo", poll_interval=1, timeout=10000)

        assert status == CICheckStatus.STALE
        # Should have polled at least _STALE_THRESHOLD times
        assert mock_sleep.call_count >= _STALE_THRESHOLD

    @patch("egg_babysit.ci_waiter.fetch_ci_checks")
    @patch("egg_babysit.ci_waiter.time.monotonic")
    @patch("egg_babysit.ci_waiter.time.sleep", return_value=None)
    def test_stale_counter_resets_on_change(self, mock_sleep, mock_time, mock_fetch):
        """Stale counter resets when check status changes."""
        poll_counter = [0]

        def monotonic_side_effect():
            poll_counter[0] += 1
            return float(poll_counter[0])

        mock_time.side_effect = monotonic_side_effect

        # First _STALE_THRESHOLD - 1 calls: same pending
        # Then one call with different status (resets counter)
        # Then another _STALE_THRESHOLD calls with new status
        pending_a = [_make_check("lint", CICheckStatus.PENDING)]
        pending_b = [
            _make_check("lint", CICheckStatus.PENDING),
            _make_check("test", CICheckStatus.PENDING),  # New check appears
        ]

        responses = (
            [pending_a] * (_STALE_THRESHOLD - 1) + [pending_b] + [pending_b] * _STALE_THRESHOLD
        )
        mock_fetch.side_effect = responses

        status, checks = wait_for_ci(42, "owner/repo", poll_interval=1, timeout=100000)

        assert status == CICheckStatus.STALE
        # Should have polled more than threshold since counter was reset
        assert mock_sleep.call_count > _STALE_THRESHOLD


class TestAggregateStatusEdgeCases:
    """Test _aggregate_status with edge cases."""

    def test_all_failing(self):
        """All checks failing returns FAILING."""
        checks = [
            _make_check("a", CICheckStatus.FAILING),
            _make_check("b", CICheckStatus.FAILING),
        ]
        assert _aggregate_status(checks) == CICheckStatus.FAILING

    def test_single_passing(self):
        """Single passing check returns PASSING."""
        assert _aggregate_status([_make_check("a", CICheckStatus.PASSING)]) == CICheckStatus.PASSING

    def test_single_failing(self):
        """Single failing check returns FAILING."""
        assert _aggregate_status([_make_check("a", CICheckStatus.FAILING)]) == CICheckStatus.FAILING

    def test_failing_takes_precedence_over_pending(self):
        """FAILING should take precedence over PENDING."""
        checks = [
            _make_check("a", CICheckStatus.PENDING),
            _make_check("b", CICheckStatus.FAILING),
        ]
        assert _aggregate_status(checks) == CICheckStatus.FAILING

    def test_stale_checks_in_aggregate(self):
        """STALE status is treated as not passing, not failing = PENDING."""
        checks = [
            _make_check("a", CICheckStatus.STALE),
            _make_check("b", CICheckStatus.PASSING),
        ]
        # STALE is neither PASSING nor FAILING, so aggregate is PENDING
        assert _aggregate_status(checks) == CICheckStatus.PENDING
