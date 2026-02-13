"""
Metrics and monitoring for the orchestrator.

Provides:
- Pipeline execution metrics
- Container lifecycle metrics
- Event counters
- Health monitoring
"""

import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# Add shared directory to path
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


logger = get_logger("orchestrator.metrics")


@dataclass
class Counter:
    """Simple counter metric."""

    name: str
    value: int = 0
    labels: dict[str, str] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def inc(self, amount: int = 1) -> None:
        """Increment the counter."""
        with self._lock:
            self.value += amount

    def get(self) -> int:
        """Get current value."""
        with self._lock:
            return self.value


@dataclass
class Gauge:
    """Gauge metric (can go up and down)."""

    name: str
    value: float = 0.0
    labels: dict[str, str] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set(self, value: float) -> None:
        """Set the gauge value."""
        with self._lock:
            self.value = value

    def inc(self, amount: float = 1.0) -> None:
        """Increment the gauge."""
        with self._lock:
            self.value += amount

    def dec(self, amount: float = 1.0) -> None:
        """Decrement the gauge."""
        with self._lock:
            self.value -= amount

    def get(self) -> float:
        """Get current value."""
        with self._lock:
            return self.value


@dataclass
class Histogram:
    """Histogram metric for measuring distributions."""

    name: str
    buckets: list[float] = field(default_factory=lambda: [0.1, 0.5, 1.0, 5.0, 10.0, 30.0, 60.0])
    labels: dict[str, str] = field(default_factory=dict)
    _observations: list[float] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def observe(self, value: float) -> None:
        """Record an observation."""
        with self._lock:
            self._observations.append(value)

    def get_count(self) -> int:
        """Get observation count."""
        with self._lock:
            return len(self._observations)

    def get_sum(self) -> float:
        """Get sum of observations."""
        with self._lock:
            return sum(self._observations)

    def get_bucket_counts(self) -> dict[float, int]:
        """Get counts per bucket."""
        with self._lock:
            counts = dict.fromkeys(self.buckets, 0)
            counts[float("inf")] = 0

            for obs in self._observations:
                for bucket in self.buckets:
                    if obs <= bucket:
                        counts[bucket] += 1
                        break
                else:
                    counts[float("inf")] += 1

            return counts


class MetricsRegistry:
    """Registry for all metrics."""

    def __init__(self):
        """Initialize the registry."""
        self._counters: dict[str, Counter] = {}
        self._gauges: dict[str, Gauge] = {}
        self._histograms: dict[str, Histogram] = {}
        self._lock = threading.RLock()
        self._start_time = datetime.utcnow()

    def counter(
        self,
        name: str,
        labels: dict[str, str] | None = None,
    ) -> Counter:
        """Get or create a counter.

        Args:
            name: Metric name
            labels: Optional labels

        Returns:
            Counter instance
        """
        key = self._make_key(name, labels)
        with self._lock:
            if key not in self._counters:
                self._counters[key] = Counter(name=name, labels=labels or {})
            return self._counters[key]

    def gauge(
        self,
        name: str,
        labels: dict[str, str] | None = None,
    ) -> Gauge:
        """Get or create a gauge.

        Args:
            name: Metric name
            labels: Optional labels

        Returns:
            Gauge instance
        """
        key = self._make_key(name, labels)
        with self._lock:
            if key not in self._gauges:
                self._gauges[key] = Gauge(name=name, labels=labels or {})
            return self._gauges[key]

    def histogram(
        self,
        name: str,
        labels: dict[str, str] | None = None,
        buckets: list[float] | None = None,
    ) -> Histogram:
        """Get or create a histogram.

        Args:
            name: Metric name
            labels: Optional labels
            buckets: Bucket boundaries

        Returns:
            Histogram instance
        """
        key = self._make_key(name, labels)
        with self._lock:
            if key not in self._histograms:
                kwargs: dict[str, Any] = {"name": name, "labels": labels or {}}
                if buckets:
                    kwargs["buckets"] = buckets
                self._histograms[key] = Histogram(**kwargs)
            return self._histograms[key]

    def _make_key(
        self,
        name: str,
        labels: dict[str, str] | None,
    ) -> str:
        """Make a unique key for a metric."""
        if not labels:
            return name
        label_str = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{name}{{{label_str}}}"

    def get_all(self) -> dict[str, Any]:
        """Get all metrics as a dictionary."""
        with self._lock:
            result: dict[str, Any] = {
                "uptime_seconds": (datetime.utcnow() - self._start_time).total_seconds(),
                "counters": {},
                "gauges": {},
                "histograms": {},
            }

            for key, counter in self._counters.items():
                result["counters"][key] = {
                    "value": counter.get(),
                    "labels": counter.labels,
                }

            for key, gauge in self._gauges.items():
                result["gauges"][key] = {
                    "value": gauge.get(),
                    "labels": gauge.labels,
                }

            for key, histogram in self._histograms.items():
                result["histograms"][key] = {
                    "count": histogram.get_count(),
                    "sum": histogram.get_sum(),
                    "buckets": histogram.get_bucket_counts(),
                    "labels": histogram.labels,
                }

            return result


# Singleton registry
_registry: MetricsRegistry | None = None


def get_metrics_registry() -> MetricsRegistry:
    """Get the singleton metrics registry."""
    global _registry
    if _registry is None:
        _registry = MetricsRegistry()
    return _registry


# Pre-defined metrics
class OrchestratorMetrics:
    """Pre-defined metrics for the orchestrator."""

    def __init__(self, registry: MetricsRegistry | None = None):
        """Initialize orchestrator metrics."""
        self.registry = registry or get_metrics_registry()

        # Pipeline counters
        self.pipelines_created = self.registry.counter("orchestrator_pipelines_created_total")
        self.pipelines_completed = self.registry.counter("orchestrator_pipelines_completed_total")
        self.pipelines_failed = self.registry.counter("orchestrator_pipelines_failed_total")

        # Container gauges
        self.containers_active = self.registry.gauge("orchestrator_containers_active")

        # Phase counters
        self.phases_started = self.registry.counter("orchestrator_phases_started_total")
        self.phases_completed = self.registry.counter("orchestrator_phases_completed_total")

        # Agent counters
        self.agents_started = self.registry.counter("orchestrator_agents_started_total")
        self.agents_completed = self.registry.counter("orchestrator_agents_completed_total")
        self.agents_failed = self.registry.counter("orchestrator_agents_failed_total")

        # Decision counters
        self.decisions_created = self.registry.counter("orchestrator_decisions_created_total")
        self.decisions_resolved = self.registry.counter("orchestrator_decisions_resolved_total")
        self.decisions_timeout = self.registry.counter("orchestrator_decisions_timeout_total")

        # Duration histograms
        self.pipeline_duration = self.registry.histogram(
            "orchestrator_pipeline_duration_seconds",
            buckets=[60, 300, 600, 1800, 3600, 7200, 14400],
        )
        self.agent_duration = self.registry.histogram(
            "orchestrator_agent_duration_seconds",
            buckets=[30, 60, 300, 600, 1800, 3600],
        )

    def record_pipeline_created(self, pipeline_id: str) -> None:
        """Record pipeline creation."""
        self.pipelines_created.inc()
        logger.debug("Metric: pipeline created", pipeline_id=pipeline_id)

    def record_pipeline_completed(self, pipeline_id: str, duration_seconds: float) -> None:
        """Record pipeline completion."""
        self.pipelines_completed.inc()
        self.pipeline_duration.observe(duration_seconds)
        logger.debug(
            "Metric: pipeline completed",
            pipeline_id=pipeline_id,
            duration_seconds=duration_seconds,
        )

    def record_pipeline_failed(self, pipeline_id: str) -> None:
        """Record pipeline failure."""
        self.pipelines_failed.inc()
        logger.debug("Metric: pipeline failed", pipeline_id=pipeline_id)

    def record_container_spawned(self) -> None:
        """Record container spawn."""
        self.containers_active.inc()

    def record_container_removed(self) -> None:
        """Record container removal."""
        self.containers_active.dec()

    def record_agent_started(self, agent_role: str) -> None:
        """Record agent start."""
        self.agents_started.inc()
        logger.debug("Metric: agent started", role=agent_role)

    def record_agent_completed(self, agent_role: str, duration_seconds: float) -> None:
        """Record agent completion."""
        self.agents_completed.inc()
        self.agent_duration.observe(duration_seconds)
        logger.debug(
            "Metric: agent completed",
            role=agent_role,
            duration_seconds=duration_seconds,
        )

    def record_agent_failed(self, agent_role: str) -> None:
        """Record agent failure."""
        self.agents_failed.inc()
        logger.debug("Metric: agent failed", role=agent_role)

    def record_decision_created(self) -> None:
        """Record decision creation."""
        self.decisions_created.inc()

    def record_decision_resolved(self) -> None:
        """Record decision resolution."""
        self.decisions_resolved.inc()

    def record_decision_timeout(self) -> None:
        """Record decision timeout."""
        self.decisions_timeout.inc()


# Singleton metrics
_metrics: OrchestratorMetrics | None = None


def get_metrics() -> OrchestratorMetrics:
    """Get the singleton orchestrator metrics."""
    global _metrics
    if _metrics is None:
        _metrics = OrchestratorMetrics()
    return _metrics
