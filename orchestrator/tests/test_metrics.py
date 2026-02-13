"""
Tests for the metrics system.
"""

import pytest

from metrics import (
    Counter,
    Gauge,
    Histogram,
    MetricsRegistry,
    OrchestratorMetrics,
    get_metrics,
    get_metrics_registry,
)


class TestCounter:
    """Tests for Counter metric."""

    def test_counter_default(self):
        """Test counter default value."""
        counter = Counter(name="test_counter")
        assert counter.get() == 0

    def test_counter_increment(self):
        """Test counter increment."""
        counter = Counter(name="test_counter")
        counter.inc()
        assert counter.get() == 1

    def test_counter_increment_by_amount(self):
        """Test counter increment by amount."""
        counter = Counter(name="test_counter")
        counter.inc(5)
        assert counter.get() == 5

    def test_counter_multiple_increments(self):
        """Test multiple increments."""
        counter = Counter(name="test_counter")
        counter.inc()
        counter.inc()
        counter.inc(3)
        assert counter.get() == 5

    def test_counter_with_labels(self):
        """Test counter with labels."""
        counter = Counter(name="test_counter", labels={"role": "coder"})
        counter.inc()
        assert counter.labels == {"role": "coder"}


class TestGauge:
    """Tests for Gauge metric."""

    def test_gauge_default(self):
        """Test gauge default value."""
        gauge = Gauge(name="test_gauge")
        assert gauge.get() == 0.0

    def test_gauge_set(self):
        """Test gauge set."""
        gauge = Gauge(name="test_gauge")
        gauge.set(42.5)
        assert gauge.get() == 42.5

    def test_gauge_increment(self):
        """Test gauge increment."""
        gauge = Gauge(name="test_gauge")
        gauge.set(10)
        gauge.inc()
        assert gauge.get() == 11.0

    def test_gauge_decrement(self):
        """Test gauge decrement."""
        gauge = Gauge(name="test_gauge")
        gauge.set(10)
        gauge.dec()
        assert gauge.get() == 9.0

    def test_gauge_inc_by_amount(self):
        """Test gauge increment by amount."""
        gauge = Gauge(name="test_gauge")
        gauge.inc(5.5)
        assert gauge.get() == 5.5


class TestHistogram:
    """Tests for Histogram metric."""

    def test_histogram_observe(self):
        """Test histogram observation."""
        histogram = Histogram(name="test_histogram")
        histogram.observe(1.0)
        histogram.observe(2.0)
        histogram.observe(3.0)

        assert histogram.get_count() == 3
        assert histogram.get_sum() == 6.0

    def test_histogram_buckets(self):
        """Test histogram bucket counts."""
        histogram = Histogram(
            name="test_histogram",
            buckets=[1.0, 5.0, 10.0],
        )

        histogram.observe(0.5)  # <= 1.0
        histogram.observe(3.0)  # <= 5.0
        histogram.observe(8.0)  # <= 10.0
        histogram.observe(15.0)  # > 10.0 (+Inf)

        counts = histogram.get_bucket_counts()
        assert counts[1.0] == 1
        assert counts[5.0] == 1
        assert counts[10.0] == 1
        assert counts[float("inf")] == 1


class TestMetricsRegistry:
    """Tests for MetricsRegistry."""

    @pytest.fixture
    def registry(self):
        """Create a fresh registry."""
        return MetricsRegistry()

    def test_get_counter(self, registry):
        """Test getting a counter."""
        counter = registry.counter("test_counter")
        assert isinstance(counter, Counter)
        assert counter.name == "test_counter"

    def test_counter_singleton(self, registry):
        """Test that same counter is returned."""
        counter1 = registry.counter("test_counter")
        counter2 = registry.counter("test_counter")
        assert counter1 is counter2

    def test_counter_with_different_labels(self, registry):
        """Test counters with different labels are different."""
        counter1 = registry.counter("test_counter", labels={"role": "coder"})
        counter2 = registry.counter("test_counter", labels={"role": "tester"})
        assert counter1 is not counter2

    def test_get_gauge(self, registry):
        """Test getting a gauge."""
        gauge = registry.gauge("test_gauge")
        assert isinstance(gauge, Gauge)

    def test_get_histogram(self, registry):
        """Test getting a histogram."""
        histogram = registry.histogram("test_histogram")
        assert isinstance(histogram, Histogram)

    def test_histogram_with_custom_buckets(self, registry):
        """Test histogram with custom buckets."""
        histogram = registry.histogram(
            "test_histogram",
            buckets=[1.0, 2.0, 3.0],
        )
        assert histogram.buckets == [1.0, 2.0, 3.0]

    def test_get_all(self, registry):
        """Test getting all metrics."""
        registry.counter("counter1").inc(5)
        registry.gauge("gauge1").set(42)
        registry.histogram("hist1").observe(1.5)

        result = registry.get_all()

        assert "uptime_seconds" in result
        assert "counters" in result
        assert "gauges" in result
        assert "histograms" in result

        assert result["counters"]["counter1"]["value"] == 5
        assert result["gauges"]["gauge1"]["value"] == 42
        assert result["histograms"]["hist1"]["count"] == 1


class TestOrchestratorMetrics:
    """Tests for OrchestratorMetrics."""

    @pytest.fixture
    def metrics(self):
        """Create orchestrator metrics with fresh registry."""
        registry = MetricsRegistry()
        return OrchestratorMetrics(registry)

    def test_record_pipeline_created(self, metrics):
        """Test recording pipeline creation."""
        metrics.record_pipeline_created("issue-123")
        assert metrics.pipelines_created.get() == 1

    def test_record_pipeline_completed(self, metrics):
        """Test recording pipeline completion."""
        metrics.record_pipeline_completed("issue-123", 120.5)
        assert metrics.pipelines_completed.get() == 1
        assert metrics.pipeline_duration.get_count() == 1

    def test_record_pipeline_failed(self, metrics):
        """Test recording pipeline failure."""
        metrics.record_pipeline_failed("issue-123")
        assert metrics.pipelines_failed.get() == 1

    def test_record_container_spawned(self, metrics):
        """Test recording container spawn."""
        metrics.record_container_spawned()
        metrics.record_container_spawned()
        assert metrics.containers_active.get() == 2

    def test_record_container_removed(self, metrics):
        """Test recording container removal."""
        metrics.record_container_spawned()
        metrics.record_container_spawned()
        metrics.record_container_removed()
        assert metrics.containers_active.get() == 1

    def test_record_agent_started(self, metrics):
        """Test recording agent start."""
        metrics.record_agent_started("coder")
        assert metrics.agents_started.get() == 1

    def test_record_agent_completed(self, metrics):
        """Test recording agent completion."""
        metrics.record_agent_completed("coder", 60.0)
        assert metrics.agents_completed.get() == 1
        assert metrics.agent_duration.get_count() == 1

    def test_record_agent_failed(self, metrics):
        """Test recording agent failure."""
        metrics.record_agent_failed("tester")
        assert metrics.agents_failed.get() == 1

    def test_record_decision_created(self, metrics):
        """Test recording decision creation."""
        metrics.record_decision_created()
        assert metrics.decisions_created.get() == 1

    def test_record_decision_resolved(self, metrics):
        """Test recording decision resolution."""
        metrics.record_decision_resolved()
        assert metrics.decisions_resolved.get() == 1

    def test_record_decision_timeout(self, metrics):
        """Test recording decision timeout."""
        metrics.record_decision_timeout()
        assert metrics.decisions_timeout.get() == 1


class TestSingletonMetrics:
    """Tests for singleton accessors."""

    def test_get_metrics_registry_singleton(self):
        """Test registry singleton."""
        import metrics

        metrics._registry = None

        registry1 = get_metrics_registry()
        registry2 = get_metrics_registry()
        assert registry1 is registry2

    def test_get_metrics_singleton(self):
        """Test metrics singleton."""
        import metrics

        metrics._metrics = None

        metrics1 = get_metrics()
        metrics2 = get_metrics()
        assert metrics1 is metrics2
