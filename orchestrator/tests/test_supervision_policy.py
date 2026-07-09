"""Transient rate-limit paced-retry policy helpers + constants (#3364 PR C).

Pure-function coverage for the ``supervision_policy`` additions the supervisor
and executor consume:

* the rate-limit tuning constants exist and are DELIBERATELY separate from the
  abnormal ``SUPERVISION_BACKOFF_*`` policy (the 30s cap / streak-to-10 halt) so
  the two paths can never bleed into each other (AC-C2 / AC-C6);
* ``parse_rate_limit_reset_seconds`` extracts a reset hint from throttle text
  when present, returns ``None`` when absent (the common bare-exit-code case),
  and clamps an absurd hint to the pacing ceiling;
* ``rate_limit_backoff_seconds`` prefers the reset hint, else a bounded linear
  backoff capped WELL above the 30s abnormal cap;
* ``RateLimitFingerprint`` equality is structural over BOTH fields — the
  load-bearing "identical failure at the same progression point" test the
  deterministic-loop guard relies on.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

import supervision_policy as sp  # noqa: E402


class TestConstants:
    def test_rate_limit_constants_have_expected_values(self):
        assert sp.SUPERVISION_RATE_LIMIT_BACKOFF_FACTOR == 30
        assert sp.SUPERVISION_RATE_LIMIT_BACKOFF_CAP_SECONDS == 900
        assert sp.SUPERVISION_RATE_LIMIT_MAX_PACING_SECONDS == 3600
        assert sp.SUPERVISION_RATE_LIMIT_ALERT_THRESHOLD_SECONDS == 1800
        assert sp.SUPERVISION_RATE_LIMIT_LOOP_GUARD_REPEATS == 5

    def test_rate_limit_policy_is_separate_from_abnormal_policy(self):
        # The whole point of PR C: a cap wall must be paced on an hours-scale
        # cadence, NOT the 30s abnormal cap. The rate-limit backoff cap must sit
        # well above the abnormal one so the two are never confused.
        assert sp.SUPERVISION_RATE_LIMIT_BACKOFF_CAP_SECONDS > sp.SUPERVISION_BACKOFF_CAP_SECONDS
        assert sp.SUPERVISION_RATE_LIMIT_MAX_PACING_SECONDS > sp.SUPERVISION_BACKOFF_CAP_SECONDS


class TestParseResetSeconds:
    @pytest.mark.parametrize("text", [None, "", "some unrelated failure", "exit_code=69"])
    def test_no_hint_returns_none(self, text):
        assert sp.parse_rate_limit_reset_seconds(text) is None

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Retry-After: 120", 120.0),
            ("retry after 120s", 120.0),
            ("retry_after=120", 120.0),
            ("try again in 90 seconds", 90.0),
            ("retry in 45 seconds", 45.0),
            ("resets in 5 minutes", 300.0),
        ],
    )
    def test_parses_known_hint_shapes(self, text, expected):
        assert sp.parse_rate_limit_reset_seconds(text) == expected

    def test_minutes_are_scaled_to_seconds(self):
        assert sp.parse_rate_limit_reset_seconds("resets in 45 minutes") == 45 * 60

    def test_absurd_hint_is_clamped_to_the_pacing_ceiling(self):
        # A malformed / absurd reset must not park an arm effectively forever.
        assert (
            sp.parse_rate_limit_reset_seconds("resets in 9999 minutes")
            == sp.SUPERVISION_RATE_LIMIT_MAX_PACING_SECONDS
        )
        assert (
            sp.parse_rate_limit_reset_seconds("retry after 99999 seconds")
            == sp.SUPERVISION_RATE_LIMIT_MAX_PACING_SECONDS
        )


class TestBackoffSeconds:
    def test_bounded_linear_backoff_without_a_hint(self):
        assert sp.rate_limit_backoff_seconds(1) == 30
        assert sp.rate_limit_backoff_seconds(5) == 150
        # Never below one factor even for a zero/negative count.
        assert sp.rate_limit_backoff_seconds(0) == 30

    def test_backoff_is_capped(self):
        assert sp.rate_limit_backoff_seconds(30) == sp.SUPERVISION_RATE_LIMIT_BACKOFF_CAP_SECONDS
        assert sp.rate_limit_backoff_seconds(100) == sp.SUPERVISION_RATE_LIMIT_BACKOFF_CAP_SECONDS

    def test_reset_hint_takes_precedence_over_backoff(self):
        assert sp.rate_limit_backoff_seconds(1, reset_seconds=120) == 120
        # A large reset is clamped to the pacing ceiling.
        assert (
            sp.rate_limit_backoff_seconds(1, reset_seconds=99999)
            == sp.SUPERVISION_RATE_LIMIT_MAX_PACING_SECONDS
        )

    def test_zero_or_none_reset_falls_back_to_backoff(self):
        # ``reset_seconds`` must be > 0 to win; 0 / None fall back to the count.
        assert sp.rate_limit_backoff_seconds(3, reset_seconds=0) == 90
        assert sp.rate_limit_backoff_seconds(3, reset_seconds=None) == 90


class TestRateLimitFingerprint:
    def test_equal_iff_both_fields_match(self):
        a = sp.RateLimitFingerprint(signature="rate_limited", progression="round-1")
        b = sp.RateLimitFingerprint(signature="rate_limited", progression="round-1")
        assert a == b

    def test_differs_when_progression_advances(self):
        a = sp.RateLimitFingerprint(signature="rate_limited", progression="round-1")
        b = sp.RateLimitFingerprint(signature="rate_limited", progression="round-2")
        assert a != b

    def test_differs_when_signature_changes(self):
        a = sp.RateLimitFingerprint(signature="rate_limited", progression="round-1")
        b = sp.RateLimitFingerprint(signature="exit_code=69", progression="round-1")
        assert a != b

    def test_is_frozen_and_hashable(self):
        a = sp.RateLimitFingerprint(signature="s", progression="p")
        b = sp.RateLimitFingerprint(signature="s", progression="p")
        # Structural hash: equal fingerprints dedupe in a set.
        assert len({a, b}) == 1
        with pytest.raises((AttributeError, TypeError)):
            a.signature = "mutated"  # type: ignore[misc]
