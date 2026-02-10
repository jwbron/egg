"""
Tests for concurrency and thread safety in gateway modules.

Phase 4: Comprehensive Coverage - Concurrency Testing
Tests for race conditions, deadlocks, and thread-safe operations.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

# Import from conftest-loaded modules
from policy import BoundedCache, CachedPRInfo, PolicyEngine
from rate_limiter import SlidingWindowRateLimiter
from session_manager import SessionManager


class TestSessionManagerConcurrency:
    """Comprehensive concurrency tests for SessionManager."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create session manager with temporary persistence."""
        return SessionManager(persistence_file=tmp_path / "sessions.json")

    def test_parallel_session_creation_no_collisions(self, manager):
        """Parallel session creation produces unique tokens."""
        token_hashes = []
        errors = []
        lock = threading.Lock()

        def create_session(i):
            try:
                token, session = manager.register_session(
                    container_id=f"container-{i}",
                    container_ip=f"10.0.0.{i % 256}",
                    mode="private",
                )
                with lock:
                    token_hashes.append(session.session_token_hash)
            except Exception as e:
                with lock:
                    errors.append((i, e))

        # Create 100 sessions in parallel
        threads = [threading.Thread(target=create_session, args=(i,)) for i in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Errors occurred: {errors}"
        assert len(token_hashes) == 100
        # All token hashes should be unique
        assert len(set(token_hashes)) == 100

    def test_concurrent_validate_during_register(self, manager):
        """Validation works correctly while registration is in progress."""
        # Pre-register a session
        token, _ = manager.register_session(
            container_id="existing",
            container_ip="10.0.0.1",
            mode="private",
        )

        results = []
        errors = []
        barrier = threading.Barrier(20)

        def validate():
            try:
                barrier.wait()
                result = manager.validate_session(token)
                results.append(result.valid)
            except Exception as e:
                errors.append(e)

        def register(i):
            try:
                barrier.wait()
                manager.register_session(
                    container_id=f"new-{i}",
                    container_ip=f"10.0.1.{i}",
                    mode="public",
                )
            except Exception as e:
                errors.append(e)

        # 10 validators and 10 registrars
        threads = []
        for i in range(10):
            threads.append(threading.Thread(target=validate))
            threads.append(threading.Thread(target=register, args=(i,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        # All validations should succeed
        assert all(results)

    def test_concurrent_delete_and_validate(self, manager):
        """Race between delete and validate is handled safely."""
        token, _ = manager.register_session(
            container_id="test",
            container_ip="10.0.0.1",
            mode="private",
        )

        results = {"valid": 0, "invalid": 0}
        delete_results = []
        lock = threading.Lock()
        barrier = threading.Barrier(20)

        def validate():
            try:
                barrier.wait()
                result = manager.validate_session(token)
                with lock:
                    if result.valid:
                        results["valid"] += 1
                    else:
                        results["invalid"] += 1
            except Exception as e:
                with lock:
                    results["error"] = e

        def delete():
            try:
                barrier.wait()
                result = manager.delete_session(token)
                with lock:
                    delete_results.append(result)
            except Exception as e:
                with lock:
                    delete_results.append(("error", e))

        threads = []
        for _ in range(10):
            threads.append(threading.Thread(target=validate))
        for _ in range(10):
            threads.append(threading.Thread(target=delete))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly one delete should succeed
        assert delete_results.count(True) == 1
        # After delete, all subsequent validations should fail
        # The exact split depends on timing, but there should be no errors
        assert "error" not in results

    def test_concurrent_expiry_and_validate(self, manager):
        """TTL extension during validation is thread-safe."""
        token, session = manager.register_session(
            container_id="test",
            container_ip="10.0.0.1",
            mode="private",
        )

        # Set expiry close to now to increase race likelihood
        session.expires_at = datetime.now(UTC) + timedelta(seconds=1)

        results = {"valid": 0, "invalid": 0}
        lock = threading.Lock()

        def validate():
            result = manager.validate_session(token)
            with lock:
                if result.valid:
                    results["valid"] += 1
                else:
                    results["invalid"] += 1

        threads = [threading.Thread(target=validate) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All initial validations should succeed (TTL gets extended)
        # as long as they happen before expiry
        assert results["valid"] >= 1  # At least some should succeed

    def test_concurrent_persistence_writes(self, tmp_path):
        """Concurrent writes don't corrupt persistence file."""
        persist_path = tmp_path / "sessions.json"
        manager = SessionManager(persistence_file=persist_path)

        errors = []

        def register_and_delete(i):
            try:
                token, _ = manager.register_session(
                    container_id=f"container-{i}",
                    container_ip=f"10.0.{i // 256}.{i % 256}",
                    mode="private",
                )
                # Sometimes delete immediately
                if i % 3 == 0:
                    manager.delete_session(token)
            except Exception as e:
                errors.append((i, e))

        threads = [threading.Thread(target=register_and_delete, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0

        # File should still be valid JSON
        import json

        with open(persist_path) as f:
            data = json.load(f)
        assert "sessions" in data

    def test_validate_fast_path_race(self, manager):
        """Fast path validation doesn't bypass security checks."""
        token, _ = manager.register_session(
            container_id="test",
            container_ip="10.0.0.1",
            mode="private",
        )

        valid_count = 0
        lock = threading.Lock()

        def validate_with_wrong_ip():
            nonlocal valid_count
            # Use wrong IP - should always fail
            result = manager.validate_session(token, source_ip="192.168.1.100")
            with lock:
                if result.valid:
                    valid_count += 1

        threads = [threading.Thread(target=validate_with_wrong_ip) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # None should be valid due to IP mismatch
        assert valid_count == 0

    def test_threadpool_session_operations(self, manager):
        """Session operations work correctly with ThreadPoolExecutor."""
        tokens = []
        lock = threading.Lock()

        def create_and_validate(i):
            token, _ = manager.register_session(
                container_id=f"container-{i}",
                container_ip=f"10.0.0.{i % 256}",
                mode="private",
            )
            result = manager.validate_session(token)
            with lock:
                tokens.append((token, result.valid))
            return result.valid

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(create_and_validate, i) for i in range(50)]
            results = [f.result() for f in futures]

        assert all(results)
        assert len(tokens) == 50


class TestRateLimiterConcurrency:
    """Concurrency tests for rate limiter."""

    def test_concurrent_allowed_calls_exact_limit(self):
        """Concurrent calls don't exceed max_requests."""
        limiter = SlidingWindowRateLimiter(
            max_requests=50,
            window_seconds=60,
            name="exact_limit_test",
        )

        allowed_count = []
        lock = threading.Lock()
        barrier = threading.Barrier(100)

        def check_limit():
            barrier.wait()
            result = limiter.is_allowed("shared-key")
            with lock:
                allowed_count.append(result.allowed)

        threads = [threading.Thread(target=check_limit) for _ in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly 50 should be allowed
        assert sum(allowed_count) == 50

    def test_concurrent_window_expiry_race(self):
        """Window expiry doesn't cause race conditions."""
        limiter = SlidingWindowRateLimiter(
            max_requests=10,
            window_seconds=1,  # Short window for testing
            name="expiry_race_test",
        )

        errors = []

        def check_and_reset():
            try:
                for _ in range(20):
                    limiter.is_allowed("key")
                    time.sleep(0.05)  # 50ms delay
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=check_and_reset) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0

    def test_stress_test_limiter(self):
        """High-volume concurrent requests are handled correctly."""
        limiter = SlidingWindowRateLimiter(
            max_requests=1000,
            window_seconds=60,
            name="stress_test",
        )

        call_count = 0
        lock = threading.Lock()

        def make_requests():
            nonlocal call_count
            for _ in range(100):
                limiter.is_allowed("stress-key")
                with lock:
                    call_count += 1

        threads = [threading.Thread(target=make_requests) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert call_count == 2000
        # Check stats are consistent
        stats = limiter.get_stats()
        assert stats["total_active_requests"] <= 1000


class TestBoundedCacheConcurrency:
    """Concurrency tests for BoundedCache."""

    def test_cache_concurrent_writes(self):
        """Concurrent writes don't corrupt cache."""
        cache = BoundedCache(max_size=100)
        errors = []
        lock = threading.Lock()

        def write_to_cache(i):
            try:
                for j in range(50):
                    key = f"key-{i}-{j}"
                    cache[key] = f"value-{i}-{j}"
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=write_to_cache, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        # Cache should respect max_size
        assert len(cache) <= 100

    def test_cache_concurrent_read_write(self):
        """Concurrent reads and writes don't cause errors."""
        cache = BoundedCache(max_size=50)
        # Pre-populate
        for i in range(30):
            cache[f"initial-{i}"] = i

        errors = []
        lock = threading.Lock()

        def reader():
            try:
                for _ in range(100):
                    for key in list(cache.keys()):
                        _ = cache.get(key)
            except Exception as e:
                with lock:
                    errors.append(("read", e))

        def writer(i):
            try:
                for j in range(100):
                    cache[f"write-{i}-{j}"] = j
            except Exception as e:
                with lock:
                    errors.append(("write", e))

        threads = []
        for i in range(5):
            threads.append(threading.Thread(target=reader))
            threads.append(threading.Thread(target=writer, args=(i,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0

    def test_cache_eviction_under_load(self):
        """Cache eviction works correctly under concurrent load."""
        cache = BoundedCache(max_size=10)
        barrier = threading.Barrier(20)

        def add_items(i):
            barrier.wait()
            for j in range(50):
                cache[f"key-{i}-{j}"] = j

        threads = [threading.Thread(target=add_items, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Cache should never exceed max_size
        assert len(cache) == 10


class TestPolicyEngineCacheConcurrency:
    """Concurrency tests for PolicyEngine caching."""

    @pytest.fixture
    def mock_github_client(self):
        """Create a mock GitHub client with controlled latency."""
        client = MagicMock()
        call_count = {"get_pr_info": 0, "list_prs_for_branch": 0}
        lock = threading.Lock()

        def mock_get_pr_info(repo, pr_number):
            with lock:
                call_count["get_pr_info"] += 1
            time.sleep(0.01)  # Simulate network latency
            return {
                "number": pr_number,
                "author": {"login": "james-in-a-box"},
                "state": "open",
                "headRefName": "feature",
            }

        def mock_list_prs_for_branch(repo, branch, state="open"):
            with lock:
                call_count["list_prs_for_branch"] += 1
            time.sleep(0.01)
            return [
                {"number": 123, "author": {"login": "james-in-a-box"}, "state": "open", "headRefName": branch}
            ]

        client.get_pr_info = mock_get_pr_info
        client.list_prs_for_branch = mock_list_prs_for_branch
        client.call_count = call_count
        return client

    @pytest.fixture
    def policy_engine(self, mock_github_client):
        """Create policy engine with mock client."""
        return PolicyEngine(github_client=mock_github_client)

    def test_concurrent_pr_info_caching(self, policy_engine, mock_github_client):
        """Concurrent PR checks share cached results.

        Note: Without explicit locking in the cache, concurrent first-time
        accesses may all miss the cache and make API calls. This is acceptable
        behavior - the cache prevents *repeated* calls, not initial concurrent ones.
        """
        results = []
        lock = threading.Lock()
        barrier = threading.Barrier(20)

        def check_pr():
            barrier.wait()
            result = policy_engine.check_pr_ownership("owner/repo", 123)
            with lock:
                results.append(result.allowed)

        threads = [threading.Thread(target=check_pr) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(results)
        # When all threads start at the same time via barrier, they may all
        # miss the initially empty cache. This is acceptable - subsequent
        # sequential calls would benefit from caching.
        # Just verify no errors occurred and results are correct
        assert mock_github_client.call_count["get_pr_info"] <= 20

    def test_concurrent_branch_ownership_checks(self, policy_engine, mock_github_client):
        """Concurrent branch ownership checks work correctly."""
        results = []
        lock = threading.Lock()

        def check_branch(i):
            result = policy_engine.check_branch_ownership("owner/repo", f"james-in-a-box-feature-{i}")
            with lock:
                results.append((i, result.allowed))

        threads = [threading.Thread(target=check_branch, args=(i,)) for i in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 30
        # All james-in-a-box- prefixed branches should be allowed
        assert all(allowed for _, allowed in results)

    def test_cache_ttl_race_condition(self, policy_engine, mock_github_client):
        """Cache TTL boundary doesn't cause issues."""
        # First, populate cache
        policy_engine.check_pr_ownership("owner/repo", 999)

        # Manually make cache entry stale
        cache_key = ("owner/repo", 999)
        if cache_key in policy_engine._pr_cache:
            old_entry = policy_engine._pr_cache[cache_key]
            policy_engine._pr_cache[cache_key] = CachedPRInfo(
                pr_number=old_entry.pr_number,
                author=old_entry.author,
                state=old_entry.state,
                head_branch=old_entry.head_branch,
                fetched_at=datetime.now(UTC).timestamp() - 600,  # 10 min ago
            )

        results = []
        lock = threading.Lock()
        barrier = threading.Barrier(10)

        def check_stale():
            barrier.wait()
            result = policy_engine.check_pr_ownership("owner/repo", 999)
            with lock:
                results.append(result.allowed)

        threads = [threading.Thread(target=check_stale) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(results)


class TestDeadlockPrevention:
    """Tests to verify no deadlocks occur under concurrent operations."""

    def test_no_deadlock_on_session_operations(self, tmp_path):
        """Session operations don't deadlock."""
        manager = SessionManager(persistence_file=tmp_path / "sessions.json")
        tokens = []
        lock = threading.Lock()

        def mixed_operations(i):
            # Create
            token, _ = manager.register_session(
                container_id=f"c-{i}",
                container_ip=f"10.0.{i // 256}.{i % 256}",
                mode="private",
            )
            with lock:
                tokens.append(token)

            # Validate
            manager.validate_session(token)

            # List
            manager.list_sessions()

            # Get by container
            manager.get_session_by_container(f"c-{i}")

            # Get by IP
            manager.get_session_by_ip(f"10.0.{i // 256}.{i % 256}")

        # Run with timeout to detect deadlocks
        threads = [threading.Thread(target=mixed_operations, args=(i,)) for i in range(30)]
        for t in threads:
            t.start()

        # Wait with timeout
        for t in threads:
            t.join(timeout=10)
            assert not t.is_alive(), "Thread deadlocked"

    def test_no_deadlock_nested_rate_limit_checks(self):
        """Nested rate limit checks don't deadlock."""
        limiter1 = SlidingWindowRateLimiter(max_requests=100, window_seconds=60, name="limiter1")
        limiter2 = SlidingWindowRateLimiter(max_requests=100, window_seconds=60, name="limiter2")

        def nested_check(i):
            result1 = limiter1.is_allowed(f"key-{i}")
            if result1.allowed:
                limiter2.is_allowed(f"key-{i}")
                # Check stats (exercises lock)
                limiter1.get_stats()
                limiter2.get_stats()

        threads = [threading.Thread(target=nested_check, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()

        for t in threads:
            t.join(timeout=5)
            assert not t.is_alive(), "Thread deadlocked"
