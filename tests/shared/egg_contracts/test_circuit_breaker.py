"""Tests for egg_contracts.circuit_breaker module."""

import pytest
from egg_contracts.circuit_breaker import (
    CircuitBreakerConfig,
    CircuitBreakerResult,
    check_all_thresholds,
    check_phase_threshold,
    check_pipeline_threshold,
    check_task_threshold,
    close_circuit_breaker,
    get_escalation_summary,
    increment_pipeline_cycle,
    increment_task_cycle,
    open_circuit_breaker,
    should_escalate,
)
from egg_contracts.models import (
    AuditRole,
    CircuitBreaker,
    CircuitBreakerStatus,
    Contract,
    IssueInfo,
    Phase,
    Task,
    TaskStatus,
)


def create_test_contract(
    total_cycles: int = 0,
    max_cycles: int = 10,
    cb_status: CircuitBreakerStatus = CircuitBreakerStatus.CLOSED,
) -> Contract:
    """Create a test contract with configurable circuit breaker."""
    return Contract(
        issue=IssueInfo(
            number=123,
            title="Test Issue",
            url="https://github.com/owner/repo/issues/123",
        ),
        circuit_breaker=CircuitBreaker(
            total_cycles=total_cycles,
            max_total_cycles=max_cycles,
            status=cb_status,
        ),
    )


def create_test_task(
    task_id: str = "task-1",
    review_cycles: int = 0,
    max_cycles: int = 3,
    escalated: bool = False,
    status: TaskStatus = TaskStatus.PENDING,
) -> Task:
    """Create a test task."""
    return Task(
        id=task_id,
        description="Test task",
        review_cycles=review_cycles,
        max_cycles=max_cycles,
        escalated=escalated,
        status=status,
    )


def create_test_phase(
    phase_id: str = "phase-1",
    review_cycles: int = 0,
    max_cycles: int = 3,
    escalated: bool = False,
    tasks: list[Task] | None = None,
    escalation_reason: str | None = None,
) -> Phase:
    """Create a test phase."""
    return Phase(
        id=phase_id,
        name="Test Phase",
        review_cycles=review_cycles,
        max_cycles=max_cycles,
        escalated=escalated,
        escalation_reason=escalation_reason,
        tasks=tasks or [],
    )


class TestCircuitBreakerResult:
    """Tests for CircuitBreakerResult."""

    def test_result_not_tripped(self):
        """Test result when circuit breaker should not trip."""
        result = CircuitBreakerResult(should_trip=False)
        assert result.should_trip is False
        assert result.reason is None
        assert result.affected_tasks == []
        assert result.affected_phases == []

    def test_result_tripped(self):
        """Test result when circuit breaker should trip."""
        result = CircuitBreakerResult(
            should_trip=True,
            reason="Test threshold exceeded",
            affected_tasks=["task-1", "task-2"],
            affected_phases=["phase-1"],
            recommendation="Fix the issue",
        )
        assert result.should_trip is True
        assert result.reason == "Test threshold exceeded"
        assert result.affected_tasks == ["task-1", "task-2"]
        assert result.affected_phases == ["phase-1"]

    def test_to_dict(self):
        """Test conversion to dictionary."""
        result = CircuitBreakerResult(
            should_trip=True,
            reason="Test",
            affected_tasks=["task-1"],
        )
        data = result.to_dict()
        assert data["should_trip"] is True
        assert data["reason"] == "Test"
        assert data["affected_tasks"] == ["task-1"]


class TestCheckTaskThreshold:
    """Tests for check_task_threshold."""

    def test_below_threshold(self):
        """Test task below threshold."""
        task = create_test_task(review_cycles=1, max_cycles=3)
        result = check_task_threshold(task)
        assert result.should_trip is False

    def test_at_threshold(self):
        """Test task at threshold."""
        task = create_test_task(review_cycles=3, max_cycles=3)
        result = check_task_threshold(task)
        assert result.should_trip is True
        assert "task-1" in result.affected_tasks

    def test_above_threshold(self):
        """Test task above threshold."""
        task = create_test_task(review_cycles=5, max_cycles=3)
        result = check_task_threshold(task)
        assert result.should_trip is True


class TestCheckPhaseThreshold:
    """Tests for check_phase_threshold."""

    def test_below_threshold(self):
        """Test phase below threshold."""
        phase = create_test_phase(review_cycles=1, max_cycles=3)
        result = check_phase_threshold(phase)
        assert result.should_trip is False

    def test_at_threshold(self):
        """Test phase at threshold."""
        phase = create_test_phase(
            review_cycles=3,
            max_cycles=3,
            tasks=[create_test_task(status=TaskStatus.INCOMPLETE)],
        )
        result = check_phase_threshold(phase)
        assert result.should_trip is True
        assert "phase-1" in result.affected_phases

    def test_includes_incomplete_tasks(self):
        """Test that incomplete tasks are included in affected_tasks."""
        phase = create_test_phase(
            review_cycles=3,
            max_cycles=3,
            tasks=[
                create_test_task(task_id="task-1", status=TaskStatus.COMPLETE),
                create_test_task(task_id="task-2", status=TaskStatus.INCOMPLETE),
            ],
        )
        result = check_phase_threshold(phase)
        assert result.should_trip is True
        assert "task-2" in result.affected_tasks
        assert "task-1" not in result.affected_tasks


class TestCheckPipelineThreshold:
    """Tests for check_pipeline_threshold."""

    def test_below_threshold(self):
        """Test pipeline below threshold."""
        contract = create_test_contract(total_cycles=5, max_cycles=10)
        result = check_pipeline_threshold(contract)
        assert result.should_trip is False

    def test_at_threshold(self):
        """Test pipeline at threshold."""
        contract = create_test_contract(total_cycles=10, max_cycles=10)
        result = check_pipeline_threshold(contract)
        assert result.should_trip is True

    def test_includes_incomplete_tasks(self):
        """Test that incomplete tasks are collected."""
        contract = create_test_contract(total_cycles=10, max_cycles=10)
        contract.phases = [
            create_test_phase(tasks=[
                create_test_task(task_id="task-1", status=TaskStatus.COMPLETE),
                create_test_task(task_id="task-2", status=TaskStatus.IN_PROGRESS),
            ]),
        ]
        result = check_pipeline_threshold(contract)
        assert result.should_trip is True
        assert "task-2" in result.affected_tasks
        assert "task-1" not in result.affected_tasks


class TestCheckAllThresholds:
    """Tests for check_all_thresholds."""

    def test_already_open(self):
        """Test when circuit breaker is already open."""
        contract = create_test_contract(cb_status=CircuitBreakerStatus.OPEN)
        result = check_all_thresholds(contract)
        assert result.should_trip is True
        assert "already OPEN" in result.reason

    def test_task_threshold_first(self):
        """Test that task thresholds are checked before phase/pipeline."""
        contract = create_test_contract(total_cycles=0, max_cycles=10)
        contract.phases = [
            create_test_phase(
                review_cycles=0,
                max_cycles=3,
                tasks=[create_test_task(review_cycles=5, max_cycles=3)],
            ),
        ]
        result = check_all_thresholds(contract)
        assert result.should_trip is True
        assert "task-1" in result.reason

    def test_no_threshold_exceeded(self):
        """Test when no thresholds are exceeded."""
        contract = create_test_contract(total_cycles=1, max_cycles=10)
        contract.phases = [
            create_test_phase(
                review_cycles=1,
                max_cycles=3,
                tasks=[create_test_task(review_cycles=1, max_cycles=3)],
            ),
        ]
        result = check_all_thresholds(contract)
        assert result.should_trip is False


class TestShouldEscalate:
    """Tests for should_escalate."""

    def test_no_escalation(self):
        """Test when no escalation is needed."""
        contract = create_test_contract()
        should, reason = should_escalate(contract)
        assert should is False
        assert reason is None

    def test_escalation_needed(self):
        """Test when escalation is needed."""
        contract = create_test_contract(total_cycles=10, max_cycles=10)
        should, reason = should_escalate(contract)
        assert should is True
        assert reason is not None


class TestOpenCircuitBreaker:
    """Tests for open_circuit_breaker."""

    def test_opens_circuit_breaker(self):
        """Test that circuit breaker is opened."""
        contract = create_test_contract()
        assert contract.circuit_breaker.status == CircuitBreakerStatus.CLOSED

        contract = open_circuit_breaker(contract, "Test reason", "test-actor")
        assert contract.circuit_breaker.status == CircuitBreakerStatus.OPEN

    def test_adds_audit_entry(self):
        """Test that audit entry is added."""
        contract = create_test_contract()
        initial_audit_count = len(contract.audit_log)

        contract = open_circuit_breaker(contract, "Test reason")
        assert len(contract.audit_log) == initial_audit_count + 1
        assert contract.audit_log[-1].action.value == "transition"


class TestCloseCircuitBreaker:
    """Tests for close_circuit_breaker."""

    def test_closes_circuit_breaker(self):
        """Test that circuit breaker is closed."""
        contract = create_test_contract(cb_status=CircuitBreakerStatus.OPEN)
        assert contract.circuit_breaker.status == CircuitBreakerStatus.OPEN

        contract = close_circuit_breaker(contract, "human-user")
        assert contract.circuit_breaker.status == CircuitBreakerStatus.CLOSED

    def test_adds_audit_entry(self):
        """Test that audit entry is added."""
        contract = create_test_contract(cb_status=CircuitBreakerStatus.OPEN)
        initial_audit_count = len(contract.audit_log)

        contract = close_circuit_breaker(contract, "human-user")
        assert len(contract.audit_log) == initial_audit_count + 1
        assert contract.audit_log[-1].role == AuditRole.HUMAN


class TestIncrementTaskCycle:
    """Tests for increment_task_cycle."""

    def test_increments_cycle(self):
        """Test that task cycle is incremented."""
        contract = create_test_contract()
        task = create_test_task(review_cycles=1)
        contract.phases = [create_test_phase(tasks=[task])]

        contract, result = increment_task_cycle(contract, "phase-1", "task-1")
        assert contract.phases[0].tasks[0].review_cycles == 2
        assert result.should_trip is False

    def test_triggers_escalation_at_threshold(self):
        """Test that escalation is triggered at threshold."""
        contract = create_test_contract()
        task = create_test_task(review_cycles=2, max_cycles=3)  # Will be 3 after increment
        contract.phases = [create_test_phase(tasks=[task])]

        contract, result = increment_task_cycle(contract, "phase-1", "task-1")
        assert result.should_trip is True
        assert contract.phases[0].tasks[0].escalated is True
        assert contract.circuit_breaker.status == CircuitBreakerStatus.OPEN

    def test_raises_for_missing_task(self):
        """Test that ValueError is raised for missing task."""
        contract = create_test_contract()
        contract.phases = [create_test_phase()]

        with pytest.raises(ValueError, match="not found"):
            increment_task_cycle(contract, "phase-1", "task-99")


class TestIncrementPipelineCycle:
    """Tests for increment_pipeline_cycle."""

    def test_increments_cycle(self):
        """Test that pipeline cycle is incremented."""
        contract = create_test_contract(total_cycles=5, max_cycles=10)

        contract, result = increment_pipeline_cycle(contract)
        assert contract.circuit_breaker.total_cycles == 6
        assert result.should_trip is False

    def test_triggers_escalation_at_threshold(self):
        """Test that escalation is triggered at threshold."""
        contract = create_test_contract(total_cycles=9, max_cycles=10)

        contract, result = increment_pipeline_cycle(contract)
        assert result.should_trip is True
        assert contract.circuit_breaker.status == CircuitBreakerStatus.OPEN


class TestGetEscalationSummary:
    """Tests for get_escalation_summary."""

    def test_summary_with_escalated_tasks(self):
        """Test summary includes escalated tasks."""
        contract = create_test_contract(cb_status=CircuitBreakerStatus.OPEN)
        contract.phases = [
            create_test_phase(tasks=[
                create_test_task(task_id="task-1", escalated=True, review_cycles=3),
                create_test_task(task_id="task-2", status=TaskStatus.INCOMPLETE),
            ]),
        ]

        summary = get_escalation_summary(contract)
        assert summary["circuit_breaker_status"] == CircuitBreakerStatus.OPEN
        assert summary["requires_intervention"] is True
        assert len(summary["escalated_tasks"]) == 1
        assert summary["escalated_tasks"][0]["id"] == "task-1"
        assert len(summary["incomplete_tasks"]) == 1
        assert summary["incomplete_tasks"][0]["id"] == "task-2"

    def test_summary_with_stuck_phases(self):
        """Test summary includes stuck phases."""
        contract = create_test_contract()
        contract.phases = [
            create_test_phase(
                escalated=True,
                escalation_reason="Test reason",
            ),
        ]

        summary = get_escalation_summary(contract)
        assert len(summary["stuck_phases"]) == 1
        assert summary["stuck_phases"][0]["reason"] == "Test reason"


class TestCircuitBreakerConfig:
    """Tests for CircuitBreakerConfig."""

    def test_default_values(self):
        """Test default configuration values."""
        assert CircuitBreakerConfig.DEFAULT_TASK_MAX_CYCLES == 3
        assert CircuitBreakerConfig.DEFAULT_PIPELINE_MAX_CYCLES == 10
        assert CircuitBreakerConfig.DEFAULT_TASK_TIMEOUT_MINUTES == 60
