"""Tests for ``egg_overseer.priority`` (issue #1962, TASK-1-2)."""

from __future__ import annotations

import pytest
from egg_overseer.priority import alert_to_label, label_to_alert


class TestAlertToLabel:
    @pytest.mark.parametrize(
        "alert, expected",
        [
            ("low", "p3"),
            ("medium", "p2"),
            ("high", "p1"),
        ],
    )
    def test_known_alert_priorities(self, alert: str, expected: str) -> None:
        assert alert_to_label(alert) == expected  # type: ignore[arg-type]

    def test_alert_to_label_never_returns_p0(self) -> None:
        # No alert priority maps to p0 — p0 is opt-in human escalation only.
        for alert in ("low", "medium", "high"):
            assert alert_to_label(alert) != "p0"  # type: ignore[arg-type]

    def test_unknown_priority_raises(self) -> None:
        with pytest.raises(ValueError, match="unrecognised alert priority"):
            alert_to_label("critical")  # type: ignore[arg-type]

    def test_empty_priority_raises(self) -> None:
        with pytest.raises(ValueError):
            alert_to_label("")  # type: ignore[arg-type]


class TestLabelToAlert:
    @pytest.mark.parametrize(
        "label, expected",
        [
            ("p0", "high"),
            ("p1", "high"),
            ("p2", "medium"),
            ("p3", "low"),
        ],
    )
    def test_known_label_priorities(self, label: str, expected: str) -> None:
        assert label_to_alert(label) == expected  # type: ignore[arg-type]

    def test_p0_collapses_to_high(self) -> None:
        # Documented behaviour: the alert dimension does not carry a
        # dedicated p0 tier, so p0 maps to "high".
        assert label_to_alert("p0") == "high"  # type: ignore[arg-type]

    def test_unknown_label_raises(self) -> None:
        with pytest.raises(ValueError, match="unrecognised label priority"):
            label_to_alert("p9")  # type: ignore[arg-type]


class TestRoundTrip:
    @pytest.mark.parametrize(
        "alert",
        ["low", "medium", "high"],
    )
    def test_alert_round_trip_preserves_alert(self, alert: str) -> None:
        # alert -> label -> alert is identity (no information loss).
        assert label_to_alert(alert_to_label(alert)) == alert  # type: ignore[arg-type]

    def test_label_round_trip_information_loss_documented(self) -> None:
        # label -> alert -> label is NOT identity for p0 (collapses to
        # high -> p1) — captured here so any future refactor that
        # changes this contract triggers an explicit test failure.
        assert alert_to_label(label_to_alert("p0")) == "p1"  # type: ignore[arg-type]
