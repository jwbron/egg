"""Overseer self-monitoring for tracking its own health metrics.

Tracks poll cycle timing, message volume, LLM call costs (now broken down
per-model, #2270 §5 cost-tracking gap), and classifier/advisor failure rates,
to ensure the overseer itself is operating within acceptable bounds.

#2270 §5 resolves the ``check_health`` *emit-vs-log* nuance: historically
``check_health`` only *returned* a dict that was folded into a completion-time
summary, so a self-health concern (cost breach, classifier failing) was never
surfaced as an alert in-flight. ``check_health`` now emits through an optional
``alert_sink`` the moment a NEW concern appears — deduped on the concern
signature so a persistent concern is not re-broadcast every cycle.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class _LLMCallRecord:
    """Record of a single LLM call."""

    model: str
    tokens: int
    cost: float
    timestamp: float


class OverseerSelfMonitor:
    """Tracks the overseer's own health metrics.

    Args:
        max_poll_delay_seconds: Maximum acceptable poll cycle duration.
        max_messages_per_cycle: Maximum messages to send in a single cycle.
        max_llm_cost_per_hour: Maximum LLM cost (USD) per hour.
        max_failure_rate: Classifier/advisor failure rate (0..1) above which a
            self-health concern is raised (once at least ``min_failure_samples``
            calls have been recorded).
        min_failure_samples: Minimum recorded calls before a failure rate is
            considered statistically meaningful.
        alert_sink: Optional callable invoked with a structured alert payload
            when a NEW self-health concern appears. When ``None`` (the default)
            ``check_health`` is pure (the legacy behaviour) — nothing is emitted.
        clock: Injectable time source (seconds) for testability.
    """

    def __init__(
        self,
        max_poll_delay_seconds: float = 60.0,
        max_messages_per_cycle: int = 10,
        max_llm_cost_per_hour: float = 5.0,
        max_failure_rate: float = 0.5,
        min_failure_samples: int = 5,
        alert_sink: Callable[[dict], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.max_poll_delay_seconds = max_poll_delay_seconds
        self.max_messages_per_cycle = max_messages_per_cycle
        self.max_llm_cost_per_hour = max_llm_cost_per_hour
        self.max_failure_rate = max_failure_rate
        self.min_failure_samples = min_failure_samples
        self._alert_sink = alert_sink
        self._clock = clock

        # Metrics storage (bounded to prevent unbounded memory growth)
        self._poll_durations: deque[float] = deque(maxlen=100)
        self._messages_this_cycle: int = 0
        self._total_messages: int = 0
        self._llm_calls: deque[_LLMCallRecord] = deque(maxlen=500)
        self._cycle_count: int = 0
        # Lifetime LLM-cost accumulators (#2270 §5 cost-tracking fix). The
        # ``_llm_calls`` deque is bounded (maxlen=500) and is used only for the
        # hourly time-window; lifetime totals must NOT be summed off it or they
        # silently undercount once >500 calls have been recorded. These never
        # evict.
        self._lifetime_cost: float = 0.0
        self._lifetime_tokens: int = 0
        self._lifetime_calls: int = 0
        self._lifetime_cost_by_model: dict[str, float] = {}
        # Classifier / advisor outcome ring buffers (True == success).
        self._classifier_results: deque[bool] = deque(maxlen=200)
        self._advisor_results: deque[bool] = deque(maxlen=200)
        # Signature of the last-emitted concern set, so a persistent concern is
        # not re-broadcast on every check_health call (the dedup half of the
        # emit-vs-log fix).
        self._last_emitted_signature: tuple[str, ...] | None = None

    # -----------------------------------------------------------------
    # Recording methods
    # -----------------------------------------------------------------

    def record_poll_cycle(self, duration_seconds: float) -> None:
        """Record the duration of a completed poll cycle.

        Args:
            duration_seconds: How long the cycle took.
        """
        self._poll_durations.append(duration_seconds)
        self._cycle_count += 1
        # Reset per-cycle message counter
        self._messages_this_cycle = 0

    def record_message_sent(self) -> None:
        """Record that a message was sent during the current cycle."""
        self._messages_this_cycle += 1
        self._total_messages += 1

    def record_llm_call(self, model: str, tokens: int, cost: float) -> None:
        """Record an LLM call with its cost.

        Args:
            model: The model used (e.g. ``"haiku"``, ``"sonnet"``).
            tokens: Total tokens consumed.
            cost: Cost in USD.
        """
        self._llm_calls.append(
            _LLMCallRecord(
                model=model,
                tokens=tokens,
                cost=cost,
                timestamp=self._clock(),
            )
        )
        # Lifetime accumulators (immune to the deque's maxlen eviction).
        self._lifetime_cost += cost
        self._lifetime_tokens += tokens
        self._lifetime_calls += 1
        self._lifetime_cost_by_model[model] = self._lifetime_cost_by_model.get(model, 0.0) + cost

    def record_classifier_result(self, success: bool) -> None:
        """Record the outcome of a classifier call (True == succeeded)."""
        self._classifier_results.append(bool(success))

    def record_advisor_result(self, success: bool) -> None:
        """Record the outcome of an advisor call (True == succeeded)."""
        self._advisor_results.append(bool(success))

    def record_classifier_failure(self) -> None:
        """Convenience: record a classifier failure."""
        self.record_classifier_result(False)

    def record_advisor_failure(self) -> None:
        """Convenience: record an advisor failure."""
        self.record_advisor_result(False)

    # -----------------------------------------------------------------
    # Health checks
    # -----------------------------------------------------------------

    def check_health(self) -> dict:
        """Evaluate the overseer's own health.

        Returns:
            A dict with keys:
                healthy: bool -- True if all metrics are within limits
                concerns: list[str] -- descriptions of any issues
                metrics: dict -- current metric values

        Side effect: when an ``alert_sink`` is configured and a NEW concern set
        is detected, emits a structured alert payload through it exactly once
        per distinct concern signature.
        """
        concerns: list[str] = []

        # Check poll cycle duration
        avg_poll = 0.0
        max_poll = 0.0
        if self._poll_durations:
            avg_poll = sum(self._poll_durations) / len(self._poll_durations)
            max_poll = max(self._poll_durations)
            if max_poll > self.max_poll_delay_seconds:
                concerns.append(
                    f"Poll cycle exceeded limit: {max_poll:.1f}s > "
                    f"{self.max_poll_delay_seconds:.1f}s max"
                )

        # Check message volume (current cycle)
        if self._messages_this_cycle > self.max_messages_per_cycle:
            concerns.append(
                f"Message volume exceeded: {self._messages_this_cycle} > "
                f"{self.max_messages_per_cycle} max per cycle"
            )

        # Check LLM cost (last hour)
        hourly_cost = self._get_hourly_llm_cost()
        if hourly_cost > self.max_llm_cost_per_hour:
            concerns.append(
                f"LLM cost exceeded: ${hourly_cost:.2f}/hr > "
                f"${self.max_llm_cost_per_hour:.2f}/hr max"
            )

        # Check classifier / advisor failure rates (#2270 §5 — wired to alerts).
        classifier_rate = self._failure_rate(self._classifier_results)
        advisor_rate = self._failure_rate(self._advisor_results)
        if (
            len(self._classifier_results) >= self.min_failure_samples
            and classifier_rate > self.max_failure_rate
        ):
            concerns.append(
                f"Classifier failure rate {classifier_rate:.0%} > "
                f"{self.max_failure_rate:.0%} max "
                f"(n={len(self._classifier_results)})"
            )
        if (
            len(self._advisor_results) >= self.min_failure_samples
            and advisor_rate > self.max_failure_rate
        ):
            concerns.append(
                f"Advisor failure rate {advisor_rate:.0%} > "
                f"{self.max_failure_rate:.0%} max (n={len(self._advisor_results)})"
            )

        metrics = {
            "cycle_count": self._cycle_count,
            "avg_poll_duration_seconds": round(avg_poll, 2),
            "max_poll_duration_seconds": round(max_poll, 2),
            "messages_this_cycle": self._messages_this_cycle,
            "total_messages": self._total_messages,
            # Lifetime-accurate (immune to the bounded recent-window deque).
            "total_llm_calls": self._lifetime_calls,
            "total_llm_tokens": self._lifetime_tokens,
            "total_llm_cost_usd": round(self._lifetime_cost, 4),
            "hourly_llm_cost_usd": round(hourly_cost, 4),
            # #2270 §5: per-model cost breakdown so a runaway tier is visible.
            "cost_by_model": self._cost_by_model(),
            "classifier_failure_rate": round(classifier_rate, 4),
            "advisor_failure_rate": round(advisor_rate, 4),
            "classifier_samples": len(self._classifier_results),
            "advisor_samples": len(self._advisor_results),
        }

        result = {
            "healthy": len(concerns) == 0,
            "concerns": concerns,
            "metrics": metrics,
        }
        self._maybe_emit(concerns, metrics)
        return result

    def should_self_report(self) -> bool:
        """Whether the overseer should include a self-report in its output.

        Returns True if any health concern has been detected.
        """
        health = self.check_health()
        return not health["healthy"]

    def build_alerts(self) -> list[dict]:
        """Return one structured alert per current health concern (#2270 §5).

        Resolves the emit-vs-log nuance with a *pull* surface: a healthy monitor
        returns ``[]`` (never cries wolf); an unhealthy one returns one alert per
        distinct concern, each shaped for the OVERSEER_ALERT path with an
        ``anomaly`` tag, a ``priority`` severity, and a human ``summary``.
        ``len(build_alerts()) == len(check_health()["concerns"])`` by construction.
        """
        health = self.check_health()
        alerts: list[dict] = []
        for concern in health["concerns"]:
            alerts.append(
                {
                    "anomaly": "overseer-self-health",
                    "priority": self._concern_priority(concern),
                    "summary": concern,
                }
            )
        return alerts

    @staticmethod
    def _concern_priority(concern: str) -> str:
        """Map a concern string to an OVERSEER_ALERT priority severity."""
        lowered = concern.lower()
        if "cost" in lowered or "failure rate" in lowered:
            return "high"
        return "medium"

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------

    def _get_hourly_llm_cost(self) -> float:
        """Sum LLM costs from the last hour."""
        cutoff = self._clock() - 3600
        return sum(c.cost for c in self._llm_calls if c.timestamp >= cutoff)

    def _cost_by_model(self) -> dict[str, float]:
        """Lifetime LLM cost per model (rounded USD; immune to deque eviction)."""
        return {model: round(cost, 4) for model, cost in self._lifetime_cost_by_model.items()}

    @staticmethod
    def _failure_rate(results: deque[bool]) -> float:
        """Fraction of recorded results that are failures (0.0 when empty)."""
        if not results:
            return 0.0
        failures = sum(1 for ok in results if not ok)
        return failures / len(results)

    @staticmethod
    def detect(snapshot: Any) -> Any | None:
        """Convenience alias for :func:`detect_overseer_self_health`."""
        return detect_overseer_self_health(snapshot)

    def _maybe_emit(self, concerns: list[str], metrics: dict) -> None:
        """Emit a structured alert through the sink on a NEW concern signature.

        Resolves the emit-vs-log nuance: a self-health concern is surfaced the
        moment it appears, but a *persistent* concern is not re-broadcast every
        cycle. When all concerns clear, the signature resets so the next
        recurrence emits again.
        """
        if self._alert_sink is None:
            return
        signature = tuple(concerns)
        if not concerns:
            self._last_emitted_signature = None
            return
        if signature == self._last_emitted_signature:
            return
        self._last_emitted_signature = signature
        try:
            self._alert_sink(
                {
                    "anomaly": "overseer-self-health",
                    "priority": "medium",
                    "summary": "; ".join(concerns),
                    "concerns": list(concerns),
                    "metrics": metrics,
                }
            )
        except Exception:  # noqa: BLE001 — self-monitoring must never crash the loop
            # A failing alert sink must not take down the overseer's own loop.
            self._last_emitted_signature = None


def _as_float(value: Any) -> float | None:
    """Coerce a numeric-looking value to float, returning None otherwise."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def detect_overseer_self_health(snapshot: Any) -> Any | None:
    """Detection-plane detector for overseer self-health (#2270 §5, slice-8).

    Fires when the overseer's own classifier or advisor failure rate exceeds the
    snapshot's ``self_health.failure_threshold`` — i.e. the overseer's reasoning
    substrate is itself failing and its verdicts can no longer be trusted. A
    healthy overseer (rates at/under threshold) stays silent.

    Pure ``snapshot -> Finding | None``: never raises, never calls an LLM,
    deterministic → ``requires_adjudication=False``. ``Finding``/``Severity`` are
    imported lazily so importing :class:`OverseerSelfMonitor` never requires the
    ``health_checks`` package to be importable.
    """
    from health_checks.types import Finding, Severity

    raw = getattr(snapshot, "raw", {}) or {}
    section = raw.get("self_health", {}) if isinstance(raw, dict) else {}
    if not isinstance(section, dict):
        return None

    classifier_rate = _as_float(section.get("classifier_failure_rate")) or 0.0
    advisor_rate = _as_float(section.get("advisor_failure_rate")) or 0.0
    threshold = _as_float(section.get("failure_threshold"))
    if threshold is None:
        threshold = 0.25

    if classifier_rate <= threshold and advisor_rate <= threshold:
        return None

    return Finding(
        finding_class="overseer_self_health",
        severity=Severity.MEDIUM,
        evidence={
            "classifier_failure_rate": classifier_rate,
            "advisor_failure_rate": advisor_rate,
            "failure_threshold": threshold,
        },
        recommended_action=(
            "The overseer's own classifier/advisor failure rate is over its "
            "threshold — its verdicts are unreliable. Surface to the operator; "
            "do not act on overseer adjudications until the substrate recovers."
        ),
        requires_adjudication=False,
        detector_key="overseer_self_health",
    )


detect_overseer_self_health.detector_key = "overseer_self_health"  # type: ignore[attr-defined]
detect_overseer_self_health.name = "overseer_self_health_detector"  # type: ignore[attr-defined]
