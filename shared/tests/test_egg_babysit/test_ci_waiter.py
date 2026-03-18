"""Tests for egg_babysit.ci_waiter — CI check polling loop."""

from unittest.mock import patch

from egg_babysit.ci_waiter import _aggregate_status, wait_for_ci
from egg_babysit.types import CICheckResult, CICheckStatus


def _make_check(name: str, status: CICheckStatus) -> CICheckResult:
    return CICheckResult(name=name, status=status, conclusion=status.value.upper())


class TestAggregateStatus:
    """Test _aggregate_status helper."""

    def test_empty_checks(self):
        assert _aggregate_status([]) == CICheckStatus.PENDING

    def test_all_passing(self):
        checks = [_make_check("a", CICheckStatus.PASSING), _make_check("b", CICheckStatus.PASSING)]
        assert _aggregate_status(checks) == CICheckStatus.PASSING

    def test_any_failing(self):
        checks = [_make_check("a", CICheckStatus.PASSING), _make_check("b", CICheckStatus.FAILING)]
        assert _aggregate_status(checks) == CICheckStatus.FAILING

    def test_mixed_pending_and_passing(self):
        checks = [_make_check("a", CICheckStatus.PASSING), _make_check("b", CICheckStatus.PENDING)]
        assert _aggregate_status(checks) == CICheckStatus.PENDING


class TestWaitForCI:
    """Test wait_for_ci with mocked fetch_ci_checks."""

    @patch("egg_babysit.ci_waiter.fetch_ci_checks")
    @patch("egg_babysit.ci_waiter.time.sleep", return_value=None)
    def test_wait_for_ci_all_pass(self, mock_sleep, mock_fetch):
        """Immediate pass on first poll."""
        mock_fetch.return_value = [
            _make_check("lint", CICheckStatus.PASSING),
            _make_check("test", CICheckStatus.PASSING),
        ]

        status, checks = wait_for_ci(42, "owner/repo", poll_interval=1, timeout=10)

        assert status == CICheckStatus.PASSING
        assert len(checks) == 2
        mock_sleep.assert_not_called()

    @patch("egg_babysit.ci_waiter.fetch_ci_checks")
    @patch("egg_babysit.ci_waiter.time.sleep", return_value=None)
    def test_wait_for_ci_eventual_pass(self, mock_sleep, mock_fetch):
        """Polls pending then passes."""
        pending = [
            _make_check("lint", CICheckStatus.PENDING),
            _make_check("test", CICheckStatus.PENDING),
        ]
        passing = [
            _make_check("lint", CICheckStatus.PASSING),
            _make_check("test", CICheckStatus.PASSING),
        ]
        mock_fetch.side_effect = [pending, passing]

        status, checks = wait_for_ci(42, "owner/repo", poll_interval=1, timeout=60)

        assert status == CICheckStatus.PASSING
        assert mock_sleep.call_count == 1

    @patch("egg_babysit.ci_waiter.fetch_ci_checks")
    @patch("egg_babysit.ci_waiter.time.sleep", return_value=None)
    def test_wait_for_ci_failure(self, mock_sleep, mock_fetch):
        """Detects failure immediately."""
        mock_fetch.return_value = [
            _make_check("lint", CICheckStatus.FAILING),
            _make_check("test", CICheckStatus.PASSING),
        ]

        status, checks = wait_for_ci(42, "owner/repo", poll_interval=1, timeout=10)

        assert status == CICheckStatus.FAILING

    @patch("egg_babysit.ci_waiter.fetch_ci_checks")
    @patch("egg_babysit.ci_waiter.time.monotonic")
    @patch("egg_babysit.ci_waiter.time.sleep", return_value=None)
    def test_wait_for_ci_timeout(self, mock_sleep, mock_monotonic, mock_fetch):
        """Times out when checks stay pending."""
        # Simulate time passing: first call at 0, second at timeout
        mock_monotonic.side_effect = [0.0, 100.0]
        mock_fetch.return_value = [
            _make_check("lint", CICheckStatus.PENDING),
        ]

        status, checks = wait_for_ci(42, "owner/repo", poll_interval=1, timeout=10)

        assert status == CICheckStatus.PENDING

    @patch("egg_babysit.ci_waiter.fetch_ci_checks")
    @patch("egg_babysit.ci_waiter.time.sleep", return_value=None)
    def test_wait_for_ci_no_checks_then_found(self, mock_sleep, mock_fetch):
        """No checks initially, then checks appear."""
        mock_fetch.side_effect = [
            [],  # No checks yet
            [_make_check("lint", CICheckStatus.PASSING)],
        ]

        status, checks = wait_for_ci(42, "owner/repo", poll_interval=1, timeout=60)

        assert status == CICheckStatus.PASSING
        assert mock_sleep.call_count == 1

    @patch("egg_babysit.ci_waiter.fetch_ci_checks")
    @patch("egg_babysit.ci_waiter.time.sleep", return_value=None)
    def test_wait_for_ci_fetch_error_handled(self, mock_sleep, mock_fetch):
        """Fetch errors are caught by _safe_fetch and treated as empty."""
        mock_fetch.side_effect = [
            Exception("Network error"),
            [_make_check("lint", CICheckStatus.PASSING)],
        ]

        status, checks = wait_for_ci(42, "owner/repo", poll_interval=1, timeout=60)

        assert status == CICheckStatus.PASSING
