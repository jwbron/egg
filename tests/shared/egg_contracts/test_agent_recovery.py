"""Tests for egg_contracts.agent_recovery module.

Tests cover:
- AgentRetryManager: retry logic, backoff calculation, error classification
- ConflictDetector: merge conflict detection, file overlap detection
- AgentCircuitBreaker: state transitions, failure/success recording, reset
"""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from egg_contracts.agent_recovery import (
    AgentCircuitBreaker,
    AgentRetryConfig,
    AgentRetryManager,
    CircuitBreakerConfig,
    CircuitState,
    ConflictDetector,
    ConflictInfo,
    RetryDecision,
    RetryPolicy,
    create_circuit_breaker,
    create_retry_manager,
)
from egg_contracts.agent_roles import AgentRole


# ---------------------------------------------------------------------------
# AgentRetryManager
# ---------------------------------------------------------------------------


class TestAgentRetryManager:
    """Tests for AgentRetryManager."""

    def test_initial_retry_count_is_zero(self):
        mgr = AgentRetryManager()
        assert mgr.get_retry_count(AgentRole.CODER) == 0

    def test_record_failure_increments_count(self):
        mgr = AgentRetryManager()
        mgr.record_failure(AgentRole.CODER, "timeout error")
        assert mgr.get_retry_count(AgentRole.CODER) == 1
        mgr.record_failure(AgentRole.CODER, "timeout error")
        assert mgr.get_retry_count(AgentRole.CODER) == 2

    def test_record_success_resets_count(self):
        mgr = AgentRetryManager()
        mgr.record_failure(AgentRole.CODER, "timeout")
        mgr.record_failure(AgentRole.CODER, "timeout")
        mgr.record_success(AgentRole.CODER)
        assert mgr.get_retry_count(AgentRole.CODER) == 0

    def test_can_retry_within_limit(self):
        mgr = AgentRetryManager(AgentRetryConfig(max_retries=2))
        mgr.record_failure(AgentRole.CODER, "timeout")
        assert mgr.can_retry(AgentRole.CODER)

    def test_cannot_retry_at_limit(self):
        mgr = AgentRetryManager(AgentRetryConfig(max_retries=2))
        mgr.record_failure(AgentRole.CODER, "timeout")
        mgr.record_failure(AgentRole.CODER, "timeout")
        assert not mgr.can_retry(AgentRole.CODER)

    def test_should_retry_retryable_error(self):
        mgr = AgentRetryManager(AgentRetryConfig(max_retries=3))
        decision = mgr.should_retry(AgentRole.CODER, "timeout occurred")
        assert decision.should_retry
        assert decision.policy == RetryPolicy.BACKOFF
        assert decision.delay_seconds > 0

    def test_should_retry_non_retryable_error(self):
        mgr = AgentRetryManager()
        decision = mgr.should_retry(AgentRole.CODER, "syntax error in code")
        assert not decision.should_retry
        assert decision.policy == RetryPolicy.MANUAL

    def test_should_retry_exceeded_max(self):
        mgr = AgentRetryManager(AgentRetryConfig(max_retries=1))
        mgr.record_failure(AgentRole.CODER, "timeout")
        decision = mgr.should_retry(AgentRole.CODER, "timeout")
        assert not decision.should_retry
        assert "Exceeded max retries" in decision.reason

    def test_backoff_calculation(self):
        config = AgentRetryConfig(
            max_retries=5,
            initial_delay_seconds=10,
            backoff_multiplier=2.0,
            max_delay_seconds=100,
        )
        mgr = AgentRetryManager(config)

        # First retry: 10 * 2^0 = 10
        decision = mgr.should_retry(AgentRole.CODER, "timeout")
        assert decision.delay_seconds == 10

        mgr.record_failure(AgentRole.CODER, "timeout")
        # Second retry: 10 * 2^1 = 20
        decision = mgr.should_retry(AgentRole.CODER, "timeout")
        assert decision.delay_seconds == 20

        mgr.record_failure(AgentRole.CODER, "timeout")
        # Third retry: 10 * 2^2 = 40
        decision = mgr.should_retry(AgentRole.CODER, "timeout")
        assert decision.delay_seconds == 40

    def test_backoff_capped_at_max(self):
        config = AgentRetryConfig(
            max_retries=10,
            initial_delay_seconds=100,
            backoff_multiplier=3.0,
            max_delay_seconds=200,
        )
        mgr = AgentRetryManager(config)
        mgr.record_failure(AgentRole.CODER, "timeout")
        mgr.record_failure(AgentRole.CODER, "timeout")
        # 100 * 3^2 = 900, capped at 200
        decision = mgr.should_retry(AgentRole.CODER, "timeout")
        assert decision.delay_seconds == 200

    def test_reset_specific_role(self):
        mgr = AgentRetryManager()
        mgr.record_failure(AgentRole.CODER, "timeout")
        mgr.record_failure(AgentRole.TESTER, "timeout")
        mgr.reset(AgentRole.CODER)
        assert mgr.get_retry_count(AgentRole.CODER) == 0
        assert mgr.get_retry_count(AgentRole.TESTER) == 1

    def test_reset_all(self):
        mgr = AgentRetryManager()
        mgr.record_failure(AgentRole.CODER, "timeout")
        mgr.record_failure(AgentRole.TESTER, "timeout")
        mgr.reset()
        assert mgr.get_retry_count(AgentRole.CODER) == 0
        assert mgr.get_retry_count(AgentRole.TESTER) == 0

    def test_get_status(self):
        mgr = AgentRetryManager(AgentRetryConfig(max_retries=3))
        mgr.record_failure(AgentRole.CODER, "timeout")
        status = mgr.get_status()
        assert AgentRole.CODER.value in status
        assert status[AgentRole.CODER.value]["attempts"] == 1
        assert status[AgentRole.CODER.value]["can_retry"]
        assert len(status[AgentRole.CODER.value]["errors"]) == 1

    def test_roles_are_independent(self):
        mgr = AgentRetryManager()
        mgr.record_failure(AgentRole.CODER, "timeout")
        assert mgr.get_retry_count(AgentRole.TESTER) == 0
        assert mgr.can_retry(AgentRole.TESTER)

    def test_retryable_error_types(self):
        mgr = AgentRetryManager()
        for err_type in ["timeout", "rate_limit", "transient", "network"]:
            decision = mgr.should_retry(AgentRole.CODER, f"got {err_type} error")
            assert decision.should_retry, f"{err_type} should be retryable"


class TestRetryDecision:
    """Tests for RetryDecision factory methods."""

    def test_retry_now(self):
        d = RetryDecision.retry_now(retry_count=1, max_retries=3)
        assert d.should_retry
        assert d.policy == RetryPolicy.IMMEDIATE
        assert d.delay_seconds == 0

    def test_retry_with_backoff(self):
        d = RetryDecision.retry_with_backoff(delay=30, retry_count=2, max_retries=5)
        assert d.should_retry
        assert d.policy == RetryPolicy.BACKOFF
        assert d.delay_seconds == 30

    def test_no_retry(self):
        d = RetryDecision.no_retry("fatal error", retry_count=3, max_retries=3)
        assert not d.should_retry
        assert d.policy == RetryPolicy.MANUAL


# ---------------------------------------------------------------------------
# ConflictDetector
# ---------------------------------------------------------------------------


class TestConflictDetector:
    """Tests for ConflictDetector."""

    def test_check_file_overlap_detects_overlap(self):
        detector = ConflictDetector(repo_path=Path("/tmp/test-repo"))
        conflicts = detector.check_file_overlap(
            ["src/a.py", "src/b.py"], AgentRole.CODER,
            ["src/b.py", "tests/test_b.py"], AgentRole.TESTER,
        )
        assert len(conflicts) == 1
        assert "src/b.py" in conflicts[0].conflicting_files
        assert conflicts[0].conflict_type == "edit"
        assert AgentRole.CODER in conflicts[0].agents_involved
        assert AgentRole.TESTER in conflicts[0].agents_involved

    def test_check_file_overlap_no_overlap(self):
        detector = ConflictDetector(repo_path=Path("/tmp/test-repo"))
        conflicts = detector.check_file_overlap(
            ["src/a.py"], AgentRole.CODER,
            ["tests/test_a.py"], AgentRole.TESTER,
        )
        assert len(conflicts) == 0

    def test_detect_conflicts_from_outputs(self):
        detector = ConflictDetector(repo_path=Path("/tmp/test-repo"))
        outputs = {
            AgentRole.CODER: {"changed_files": ["src/a.py", "src/shared.py"]},
            AgentRole.TESTER: {"changed_files": ["tests/test.py", "src/shared.py"]},
        }
        conflicts = detector.detect_conflicts_from_outputs(outputs)
        assert len(conflicts) == 1
        assert "src/shared.py" in conflicts[0].conflicting_files

    def test_detect_conflicts_from_outputs_no_changed_files(self):
        detector = ConflictDetector(repo_path=Path("/tmp/test-repo"))
        outputs = {
            AgentRole.CODER: {"summary": "done"},
            AgentRole.TESTER: {"summary": "done"},
        }
        conflicts = detector.detect_conflicts_from_outputs(outputs)
        assert len(conflicts) == 0

    def test_has_conflicts(self):
        detector = ConflictDetector(repo_path=Path("/tmp/test-repo"))
        assert not detector.has_conflicts()
        detector.check_file_overlap(
            ["src/a.py"], AgentRole.CODER,
            ["src/a.py"], AgentRole.TESTER,
        )
        assert detector.has_conflicts()

    def test_clear_conflicts(self):
        detector = ConflictDetector(repo_path=Path("/tmp/test-repo"))
        detector.check_file_overlap(
            ["src/a.py"], AgentRole.CODER,
            ["src/a.py"], AgentRole.TESTER,
        )
        detector.clear_conflicts()
        assert not detector.has_conflicts()

    def test_get_all_conflicts_returns_copy(self):
        detector = ConflictDetector(repo_path=Path("/tmp/test-repo"))
        detector.check_file_overlap(
            ["src/a.py"], AgentRole.CODER,
            ["src/a.py"], AgentRole.TESTER,
        )
        conflicts = detector.get_all_conflicts()
        conflicts.clear()
        # Original should be unaffected
        assert detector.has_conflicts()

    def test_check_for_merge_conflicts_subprocess_error(self):
        """When git command raises SubprocessError, should return empty list."""
        import subprocess

        detector = ConflictDetector(repo_path=Path("/tmp/test-repo"))
        with patch("subprocess.run", side_effect=subprocess.SubprocessError("git failed")):
            conflicts = detector.check_for_merge_conflicts()
        assert conflicts == []

    def test_check_for_merge_conflicts_timeout(self):
        """When git command times out, should return empty list."""
        import subprocess

        detector = ConflictDetector(repo_path=Path("/tmp/test-repo"))
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 30)):
            conflicts = detector.check_for_merge_conflicts()
        assert conflicts == []

    def test_check_for_merge_conflicts_with_conflicts(self):
        """Mock git to return conflicting files."""
        detector = ConflictDetector(repo_path=Path("/tmp/test-repo"))
        mock_result = type("Result", (), {
            "returncode": 0,
            "stdout": "src/a.py\nsrc/b.py\n",
        })()
        with patch("subprocess.run", return_value=mock_result):
            conflicts = detector.check_for_merge_conflicts()
        assert len(conflicts) == 1
        assert conflicts[0].conflict_type == "merge"
        assert "src/a.py" in conflicts[0].conflicting_files
        assert "src/b.py" in conflicts[0].conflicting_files

    def test_check_for_merge_conflicts_clean(self):
        """Mock git to return no conflicts."""
        detector = ConflictDetector(repo_path=Path("/tmp/test-repo"))
        mock_result = type("Result", (), {
            "returncode": 0,
            "stdout": "",
        })()
        with patch("subprocess.run", return_value=mock_result):
            conflicts = detector.check_for_merge_conflicts()
        assert len(conflicts) == 0

    def test_multiple_pair_conflicts(self):
        """Detect conflicts across three agents."""
        detector = ConflictDetector(repo_path=Path("/tmp/test-repo"))
        outputs = {
            AgentRole.CODER: {"changed_files": ["shared.py"]},
            AgentRole.TESTER: {"changed_files": ["shared.py"]},
            AgentRole.DOCUMENTER: {"changed_files": ["shared.py"]},
        }
        conflicts = detector.detect_conflicts_from_outputs(outputs)
        # 3 pairs: coder-tester, coder-documenter, tester-documenter
        assert len(conflicts) == 3


class TestConflictInfo:
    """Tests for ConflictInfo serialization."""

    def test_to_dict(self):
        info = ConflictInfo(
            conflicting_files=["a.py"],
            agents_involved=[AgentRole.CODER, AgentRole.TESTER],
            conflict_type="edit",
            resolution_hint="Fix it",
        )
        d = info.to_dict()
        assert d["conflicting_files"] == ["a.py"]
        assert d["agents_involved"] == ["coder", "tester"]
        assert d["conflict_type"] == "edit"
        assert "detected_at" in d


# ---------------------------------------------------------------------------
# AgentCircuitBreaker
# ---------------------------------------------------------------------------


class TestAgentCircuitBreaker:
    """Tests for AgentCircuitBreaker."""

    def test_initial_state_is_closed(self):
        cb = AgentCircuitBreaker()
        assert cb.state == CircuitState.CLOSED
        assert cb.can_execute()
        assert not cb.is_open()

    def test_opens_after_threshold_failures(self):
        cb = AgentCircuitBreaker(CircuitBreakerConfig(failure_threshold=3))
        cb.record_failure(AgentRole.CODER)
        cb.record_failure(AgentRole.TESTER)
        assert cb.state == CircuitState.CLOSED
        cb.record_failure(AgentRole.DOCUMENTER)
        assert cb.state == CircuitState.OPEN
        assert not cb.can_execute()
        assert cb.is_open()

    def test_transitions_to_half_open_after_timeout(self):
        cb = AgentCircuitBreaker(CircuitBreakerConfig(
            failure_threshold=1,
            reset_timeout_seconds=60,
        ))
        cb.record_failure(AgentRole.CODER)
        assert cb.state == CircuitState.OPEN

        # Simulate time passing
        cb._opened_at = datetime(2020, 1, 1, tzinfo=UTC)
        assert cb.can_execute()  # triggers transition check
        assert cb.state == CircuitState.HALF_OPEN

    def test_half_open_closes_on_success_threshold(self):
        cb = AgentCircuitBreaker(CircuitBreakerConfig(
            failure_threshold=1,
            reset_timeout_seconds=0,  # immediate transition
            success_threshold=2,
        ))
        cb.record_failure(AgentRole.CODER)
        assert cb.state == CircuitState.OPEN

        # Trigger transition to half-open
        cb.can_execute()
        assert cb.state == CircuitState.HALF_OPEN

        cb.record_success(AgentRole.CODER)
        assert cb.state == CircuitState.HALF_OPEN  # need 2 successes
        cb.record_success(AgentRole.TESTER)
        assert cb.state == CircuitState.CLOSED

    def test_half_open_reopens_on_failure(self):
        cb = AgentCircuitBreaker(CircuitBreakerConfig(
            failure_threshold=1,
            reset_timeout_seconds=0,
        ))
        cb.record_failure(AgentRole.CODER)
        cb.can_execute()  # transition to half-open
        assert cb.state == CircuitState.HALF_OPEN

        cb.record_failure(AgentRole.TESTER)
        assert cb.state == CircuitState.OPEN

    def test_state_property_is_side_effect_free(self):
        """Reading .state should not mutate the breaker's state."""
        cb = AgentCircuitBreaker(CircuitBreakerConfig(
            failure_threshold=1,
            reset_timeout_seconds=0,
        ))
        cb.record_failure(AgentRole.CODER)
        assert cb.state == CircuitState.OPEN

        # Reading state repeatedly should stay OPEN (transition only
        # happens via can_execute/is_open/record_*)
        for _ in range(5):
            assert cb.state == CircuitState.OPEN

    def test_reset(self):
        cb = AgentCircuitBreaker(CircuitBreakerConfig(failure_threshold=1))
        cb.record_failure(AgentRole.CODER)
        assert cb.state == CircuitState.OPEN
        cb.reset()
        assert cb.state == CircuitState.CLOSED
        assert cb.can_execute()

    def test_failed_agents_tracked(self):
        cb = AgentCircuitBreaker()
        cb.record_failure(AgentRole.CODER)
        cb.record_failure(AgentRole.TESTER)
        status = cb.get_status()
        assert "coder" in status["failed_agents"]
        assert "tester" in status["failed_agents"]

    def test_success_removes_from_failed_agents(self):
        cb = AgentCircuitBreaker()
        cb.record_failure(AgentRole.CODER)
        cb.record_success(AgentRole.CODER)
        status = cb.get_status()
        assert "coder" not in status["failed_agents"]

    def test_duplicate_failures_tracked_once(self):
        cb = AgentCircuitBreaker()
        cb.record_failure(AgentRole.CODER)
        cb.record_failure(AgentRole.CODER)
        status = cb.get_status()
        assert status["failed_agents"].count("coder") == 1
        assert status["failure_count"] == 2

    def test_get_failure_summary_no_failures(self):
        cb = AgentCircuitBreaker()
        assert cb.get_failure_summary() == "No agent failures recorded"

    def test_get_failure_summary_with_failures(self):
        cb = AgentCircuitBreaker()
        cb.record_failure(AgentRole.CODER)
        summary = cb.get_failure_summary()
        assert "coder" in summary
        assert "1 failures" in summary

    def test_get_status_fields(self):
        cb = AgentCircuitBreaker()
        status = cb.get_status()
        assert "state" in status
        assert "failure_count" in status
        assert "failure_threshold" in status
        assert "success_count" in status
        assert "last_failure" in status
        assert "opened_at" in status
        assert "failed_agents" in status
        assert "can_execute" in status


# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------


class TestFactoryFunctions:
    """Tests for create_retry_manager and create_circuit_breaker."""

    def test_create_retry_manager_defaults(self):
        mgr = create_retry_manager()
        assert mgr.config.max_retries == 2
        assert mgr.config.initial_delay_seconds == 30

    def test_create_retry_manager_custom(self):
        mgr = create_retry_manager(max_retries=5, initial_delay=10)
        assert mgr.config.max_retries == 5
        assert mgr.config.initial_delay_seconds == 10

    def test_create_circuit_breaker_defaults(self):
        cb = create_circuit_breaker()
        assert cb.config.failure_threshold == 3
        assert cb.config.reset_timeout_seconds == 300

    def test_create_circuit_breaker_custom(self):
        cb = create_circuit_breaker(failure_threshold=5, reset_timeout=60)
        assert cb.config.failure_threshold == 5
        assert cb.config.reset_timeout_seconds == 60
