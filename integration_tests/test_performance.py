"""
Tests for performance regression in gateway operations.

Phase 4: Comprehensive Coverage - Performance Testing
Tests latency, throughput, and scalability characteristics.
"""

import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from tests.utils.gateway_client import docker_available


@pytest.mark.integration
@pytest.mark.timeout(60)
class TestPerformanceBaselines:
    """Performance baseline tests for gateway operations."""

    def test_health_check_latency(self, egg_stack):
        """Health check latency should be under 100ms."""
        latencies = []

        for _ in range(10):
            start = time.perf_counter()
            result = egg_stack.health_check(timeout=5)
            latency = (time.perf_counter() - start) * 1000  # ms
            latencies.append(latency)

        avg_latency = statistics.mean(latencies)
        p95_latency = sorted(latencies)[int(len(latencies) * 0.95)]

        assert avg_latency < 100, f"Average health check latency {avg_latency:.2f}ms exceeds 100ms"
        assert p95_latency < 200, f"P95 health check latency {p95_latency:.2f}ms exceeds 200ms"

    def test_session_validation_latency(self, egg_stack, gateway_session):
        """Session validation latency should be under 50ms."""
        token = gateway_session.get("session_token")
        latencies = []

        for _ in range(20):
            start = time.perf_counter()
            result = egg_stack.validate_session(token, timeout=5)
            latency = (time.perf_counter() - start) * 1000  # ms
            latencies.append(latency)
            assert result.get("valid")

        avg_latency = statistics.mean(latencies)
        p95_latency = sorted(latencies)[int(len(latencies) * 0.95)]

        assert avg_latency < 50, f"Average validation latency {avg_latency:.2f}ms exceeds 50ms"
        assert p95_latency < 100, f"P95 validation latency {p95_latency:.2f}ms exceeds 100ms"

    def test_session_creation_latency(self, egg_stack):
        """Session creation latency should be under 200ms."""
        latencies = []
        tokens = []

        for i in range(10):
            start = time.perf_counter()
            result = egg_stack.create_session(
                container_id=f"perf-test-{i}",
                mode="private",
            )
            latency = (time.perf_counter() - start) * 1000  # ms
            latencies.append(latency)

            if result.get("success"):
                token = result.get("data", result).get("session_token")
                if token:
                    tokens.append(token)

        # Cleanup
        for token in tokens:
            try:
                egg_stack.delete_session(token)
            except Exception:
                pass

        avg_latency = statistics.mean(latencies)
        assert avg_latency < 200, f"Average creation latency {avg_latency:.2f}ms exceeds 200ms"


@pytest.mark.integration
@pytest.mark.timeout(120)
class TestConcurrentPerformance:
    """Tests for performance under concurrent load."""

    def test_concurrent_validations(self, egg_stack, gateway_session):
        """Concurrent validations should complete without significant degradation."""
        token = gateway_session.get("session_token")
        latencies = []
        errors = []
        lock = threading.Lock()

        def validate():
            start = time.perf_counter()
            try:
                result = egg_stack.validate_session(token, timeout=10)
                latency = (time.perf_counter() - start) * 1000
                with lock:
                    latencies.append(latency)
                return result.get("valid")
            except Exception as e:
                with lock:
                    errors.append(e)
                return False

        # Run 50 concurrent validations
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(validate) for _ in range(50)]
            results = [f.result() for f in as_completed(futures)]

        assert len(errors) == 0, f"Errors during concurrent validation: {errors}"
        assert all(results), "Some validations failed"

        avg_latency = statistics.mean(latencies)
        p99_latency = sorted(latencies)[int(len(latencies) * 0.99)]

        # Under load, latency should still be reasonable
        assert avg_latency < 200, f"Average concurrent latency {avg_latency:.2f}ms exceeds 200ms"
        assert p99_latency < 500, f"P99 concurrent latency {p99_latency:.2f}ms exceeds 500ms"

    def test_concurrent_session_creation(self, egg_stack):
        """Concurrent session creation should complete without errors."""
        tokens = []
        errors = []
        lock = threading.Lock()

        def create_session(i):
            try:
                result = egg_stack.create_session(
                    container_id=f"concurrent-{i}",
                    mode="private",
                )
                if result.get("success"):
                    token = result.get("data", result).get("session_token")
                    if token:
                        with lock:
                            tokens.append(token)
                        return True
                return False
            except Exception as e:
                with lock:
                    errors.append(e)
                return False

        # Create 30 sessions concurrently
        with ThreadPoolExecutor(max_workers=15) as executor:
            futures = [executor.submit(create_session, i) for i in range(30)]
            results = [f.result() for f in as_completed(futures)]

        # Cleanup
        for token in tokens:
            try:
                egg_stack.delete_session(token)
            except Exception:
                pass

        assert len(errors) == 0, f"Errors during concurrent creation: {errors}"
        success_count = sum(results)
        assert success_count >= 25, f"Only {success_count}/30 sessions created successfully"

    def test_mixed_operations_under_load(self, egg_stack):
        """Mixed create/validate/delete operations should work under load."""
        active_tokens = []
        lock = threading.Lock()
        errors = []
        operation_counts = {"create": 0, "validate": 0, "delete": 0}

        def random_operation(i):
            op = i % 3
            try:
                if op == 0:  # Create
                    result = egg_stack.create_session(
                        container_id=f"mixed-{i}",
                        mode="private",
                    )
                    if result.get("success"):
                        token = result.get("data", result).get("session_token")
                        if token:
                            with lock:
                                active_tokens.append(token)
                                operation_counts["create"] += 1

                elif op == 1:  # Validate
                    with lock:
                        token = active_tokens[-1] if active_tokens else None
                    if token:
                        egg_stack.validate_session(token)
                        with lock:
                            operation_counts["validate"] += 1

                elif op == 2:  # Delete
                    with lock:
                        token = active_tokens.pop() if active_tokens else None
                    if token:
                        egg_stack.delete_session(token)
                        with lock:
                            operation_counts["delete"] += 1

            except Exception as e:
                with lock:
                    errors.append(e)

        # Run 60 mixed operations
        with ThreadPoolExecutor(max_workers=10) as executor:
            list(executor.map(random_operation, range(60)))

        # Cleanup remaining tokens
        for token in active_tokens:
            try:
                egg_stack.delete_session(token)
            except Exception:
                pass

        # Allow some errors under concurrent load, but not too many
        assert len(errors) < 5, f"Too many errors: {errors}"


@pytest.mark.integration
@pytest.mark.timeout(30)
class TestScalability:
    """Tests for scalability characteristics."""

    def test_many_active_sessions(self, egg_stack):
        """Gateway handles many active sessions efficiently."""
        tokens = []

        # Create 50 sessions
        for i in range(50):
            result = egg_stack.create_session(
                container_id=f"scale-{i}",
                mode="private",
            )
            if result.get("success"):
                token = result.get("data", result).get("session_token")
                if token:
                    tokens.append(token)

        try:
            # Health check should still be fast
            start = time.perf_counter()
            health = egg_stack.health_check(timeout=10)
            latency = (time.perf_counter() - start) * 1000

            assert health.get("status") == "healthy"
            assert latency < 200, f"Health check degraded with many sessions: {latency:.2f}ms"

            # Validation should still be fast
            if tokens:
                start = time.perf_counter()
                result = egg_stack.validate_session(tokens[0])
                latency = (time.perf_counter() - start) * 1000

                assert result.get("valid")
                assert latency < 100, f"Validation degraded with many sessions: {latency:.2f}ms"

        finally:
            # Cleanup
            for token in tokens:
                try:
                    egg_stack.delete_session(token)
                except Exception:
                    pass

    def test_validation_throughput(self, egg_stack, gateway_session):
        """Measure validation throughput."""
        token = gateway_session.get("session_token")
        count = 0
        errors = 0
        duration = 5  # seconds

        start = time.perf_counter()
        while time.perf_counter() - start < duration:
            try:
                result = egg_stack.validate_session(token, timeout=2)
                if result.get("valid"):
                    count += 1
                else:
                    errors += 1
            except Exception:
                errors += 1

        throughput = count / duration
        error_rate = errors / (count + errors) if (count + errors) > 0 else 1

        # Should achieve at least 20 validations/second with low error rate
        assert throughput >= 20, f"Throughput {throughput:.2f} req/s below 20 req/s"
        assert error_rate < 0.01, f"Error rate {error_rate:.2%} exceeds 1%"
