"""Integration tests for SDLC pipeline circuit breaker.

Tests the circuit breaker escalation mechanism where:
1. Task cycle threshold triggers escalation
2. Phase cycle threshold triggers escalation
3. Pipeline total cycle threshold triggers escalation
4. Circuit breaker opens and closes correctly
5. Escalation summary provides useful information
"""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

# Add shared directory to path
_shared_path = Path(__file__).parent.parent.parent / "shared"
if str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from egg_contracts import (
    AuditRole,
    CircuitBreakerStatus,
    Contract,
    IssueInfo,
    Phase,
    PhaseStatus,
    PipelinePhase,
    Task,
    TaskStatus,
    check_all_thresholds,
    check_phase_threshold,
    check_pipeline_threshold,
    check_task_threshold,
    close_circuit_breaker,
    get_escalation_summary,
    increment_phase_cycle,
    increment_pipeline_cycle,
    increment_task_cycle,
    load_contract,
    open_circuit_breaker,
    save_contract,
    should_escalate,
)


@pytest.fixture
def temp_repo():
    """Create a temporary repository directory for testing."""
    with TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        contracts_dir = repo_path / ".egg-state" / "contracts"
        contracts_dir.mkdir(parents=True)
        yield repo_path


@pytest.fixture
def sample_issue_info():
    """Create sample issue info for testing."""
    return IssueInfo(
        number=300,
        title="Feature with escalation",
        url="https://github.com/test-owner/test-repo/issues/300",
    )


@pytest.fixture
def contract_with_tasks(sample_issue_info):
    """Create a contract with tasks for circuit breaker testing."""
    contract = Contract(
        schemaVersion="1.0",
        issue=sample_issue_info,
        current_phase=PipelinePhase.IMPLEMENT,
        phases=[
            Phase(
                id="phase-1",
                name="Core Implementation",
                status=PhaseStatus.IN_PROGRESS,
                tasks=[
                    Task(
                        id="task-1-1",
                        description="Implement feature",
                        status=TaskStatus.IN_PROGRESS,
                        max_cycles=3,
                    ),
                    Task(
                        id="task-1-2",
                        description="Add tests",
                        status=TaskStatus.PENDING,
                        max_cycles=3,
                    ),
                ],
            ),
        ],
    )
    return contract


class TestTaskThreshold:
    """Tests for per-task cycle threshold."""

    def test_task_under_threshold(self, contract_with_tasks):
        """Task under threshold does not trigger escalation."""
        task = contract_with_tasks.phases[0].tasks[0]
        task.review_cycles = 1

        result = check_task_threshold(task)
        assert not result.should_trip
        assert result.reason is None

    def test_task_at_threshold(self, contract_with_tasks):
        """Task at threshold triggers escalation."""
        task = contract_with_tasks.phases[0].tasks[0]
        task.review_cycles = 3  # max_cycles is 3

        result = check_task_threshold(task)
        assert result.should_trip
        assert "exceeded max cycles" in result.reason
        assert "task-1-1" in result.affected_tasks

    def test_task_threshold_recommendation(self, contract_with_tasks):
        """Escalation provides helpful recommendation."""
        task = contract_with_tasks.phases[0].tasks[0]
        task.review_cycles = 3

        result = check_task_threshold(task)
        assert result.recommendation is not None
        assert "Clarifying requirements" in result.recommendation

    def test_increment_task_cycle_below_threshold(
        self, temp_repo, contract_with_tasks
    ):
        """Incrementing cycle below threshold keeps circuit breaker closed."""
        save_contract(contract_with_tasks, temp_repo)

        contract = load_contract(300, temp_repo)
        updated, result = increment_task_cycle(contract, "phase-1", "task-1-1")

        assert not result.should_trip
        assert updated.phases[0].tasks[0].review_cycles == 1
        assert updated.circuit_breaker.status == CircuitBreakerStatus.CLOSED

    def test_increment_task_cycle_triggers_threshold(
        self, temp_repo, contract_with_tasks
    ):
        """Incrementing cycle at threshold opens circuit breaker."""
        contract_with_tasks.phases[0].tasks[0].review_cycles = 2  # Next will be 3
        save_contract(contract_with_tasks, temp_repo)

        contract = load_contract(300, temp_repo)
        updated, result = increment_task_cycle(contract, "phase-1", "task-1-1")

        assert result.should_trip
        assert updated.phases[0].tasks[0].review_cycles == 3
        assert updated.phases[0].tasks[0].escalated is True
        assert updated.circuit_breaker.status == CircuitBreakerStatus.OPEN


class TestPhaseThreshold:
    """Tests for phase cycle threshold."""

    def test_phase_under_threshold(self, contract_with_tasks):
        """Phase under threshold does not trigger escalation."""
        phase = contract_with_tasks.phases[0]
        phase.review_cycles = 1

        result = check_phase_threshold(phase)
        assert not result.should_trip

    def test_phase_at_threshold(self, contract_with_tasks):
        """Phase at threshold triggers escalation."""
        phase = contract_with_tasks.phases[0]
        phase.review_cycles = 3
        phase.max_cycles = 3

        result = check_phase_threshold(phase)
        assert result.should_trip
        assert "phase-1" in result.affected_phases

    def test_increment_phase_cycle_triggers_threshold(
        self, temp_repo, contract_with_tasks
    ):
        """Incrementing phase cycle at threshold opens circuit breaker."""
        contract_with_tasks.phases[0].review_cycles = 2
        contract_with_tasks.phases[0].max_cycles = 3
        save_contract(contract_with_tasks, temp_repo)

        contract = load_contract(300, temp_repo)
        updated, result = increment_phase_cycle(contract, "phase-1")

        assert result.should_trip
        assert updated.phases[0].escalated is True
        assert updated.circuit_breaker.status == CircuitBreakerStatus.OPEN


class TestPipelineThreshold:
    """Tests for total pipeline cycle threshold."""

    def test_pipeline_under_threshold(self, contract_with_tasks):
        """Pipeline under threshold does not trigger escalation."""
        contract_with_tasks.circuit_breaker.total_cycles = 5
        contract_with_tasks.circuit_breaker.max_total_cycles = 10

        result = check_pipeline_threshold(contract_with_tasks)
        assert not result.should_trip

    def test_pipeline_at_threshold(self, contract_with_tasks):
        """Pipeline at threshold triggers escalation."""
        contract_with_tasks.circuit_breaker.total_cycles = 10
        contract_with_tasks.circuit_breaker.max_total_cycles = 10

        result = check_pipeline_threshold(contract_with_tasks)
        assert result.should_trip
        assert "Pipeline exceeded" in result.reason

    def test_increment_pipeline_cycle(self, temp_repo, contract_with_tasks):
        """Pipeline cycle count increments correctly."""
        save_contract(contract_with_tasks, temp_repo)

        contract = load_contract(300, temp_repo)
        initial_cycles = contract.circuit_breaker.total_cycles

        updated, result = increment_pipeline_cycle(contract)
        assert updated.circuit_breaker.total_cycles == initial_cycles + 1
        assert not result.should_trip  # Not at threshold yet

    def test_pipeline_threshold_collects_incomplete_tasks(self, contract_with_tasks):
        """Pipeline escalation lists all incomplete tasks."""
        contract_with_tasks.circuit_breaker.total_cycles = 10
        contract_with_tasks.circuit_breaker.max_total_cycles = 10
        contract_with_tasks.phases[0].tasks[0].status = TaskStatus.IN_PROGRESS
        contract_with_tasks.phases[0].tasks[1].status = TaskStatus.PENDING

        result = check_pipeline_threshold(contract_with_tasks)
        assert result.should_trip
        assert "task-1-1" in result.affected_tasks
        assert "task-1-2" in result.affected_tasks


class TestCheckAllThresholds:
    """Tests for comprehensive threshold checking."""

    def test_no_thresholds_exceeded(self, contract_with_tasks):
        """Returns false when no thresholds are exceeded."""
        result = check_all_thresholds(contract_with_tasks)
        assert not result.should_trip

    def test_task_threshold_found_first(self, contract_with_tasks):
        """Task threshold is checked before phase or pipeline."""
        contract_with_tasks.phases[0].tasks[0].review_cycles = 3  # Exceeds threshold
        contract_with_tasks.phases[0].review_cycles = 1  # Under threshold

        result = check_all_thresholds(contract_with_tasks)
        assert result.should_trip
        assert "task-1-1" in result.affected_tasks

    def test_already_open_circuit_breaker(self, contract_with_tasks):
        """Already open circuit breaker is detected."""
        contract_with_tasks.circuit_breaker.status = CircuitBreakerStatus.OPEN

        result = check_all_thresholds(contract_with_tasks)
        assert result.should_trip
        assert "already OPEN" in result.reason


class TestCircuitBreakerStateTransitions:
    """Tests for circuit breaker state transitions."""

    def test_open_circuit_breaker(self, contract_with_tasks):
        """Circuit breaker can be opened."""
        assert contract_with_tasks.circuit_breaker.status == CircuitBreakerStatus.CLOSED

        updated = open_circuit_breaker(
            contract_with_tasks,
            reason="Task threshold exceeded",
            actor="system",
        )

        assert updated.circuit_breaker.status == CircuitBreakerStatus.OPEN
        assert len(updated.audit_log) > 0

    def test_close_circuit_breaker(self, contract_with_tasks):
        """Circuit breaker can be closed by human."""
        contract_with_tasks.circuit_breaker.status = CircuitBreakerStatus.OPEN

        updated = close_circuit_breaker(
            contract_with_tasks,
            actor="human-reviewer",
            role=AuditRole.HUMAN,
            reason="Provided additional guidance",
        )

        assert updated.circuit_breaker.status == CircuitBreakerStatus.CLOSED
        assert len(updated.audit_log) > 0
        last_entry = updated.audit_log[-1]
        assert last_entry.role == AuditRole.HUMAN

    def test_audit_log_tracks_transitions(self, contract_with_tasks):
        """Audit log tracks circuit breaker state transitions."""
        # Open
        updated = open_circuit_breaker(
            contract_with_tasks,
            reason="Test open",
            actor="system",
        )

        # Close
        updated = close_circuit_breaker(
            updated,
            actor="human",
            reason="Test close",
        )

        # Verify audit entries
        transition_entries = [
            e for e in updated.audit_log
            if e.field_path == "circuit_breaker.status"
        ]
        assert len(transition_entries) == 2


class TestShouldEscalate:
    """Tests for the simplified escalation check."""

    def test_should_not_escalate_healthy_contract(self, contract_with_tasks):
        """Healthy contract does not need escalation."""
        escalate, reason = should_escalate(contract_with_tasks)
        assert not escalate
        assert reason is None

    def test_should_escalate_exceeded_threshold(self, contract_with_tasks):
        """Contract with exceeded threshold needs escalation."""
        contract_with_tasks.phases[0].tasks[0].review_cycles = 3

        escalate, reason = should_escalate(contract_with_tasks)
        assert escalate
        assert reason is not None


class TestEscalationSummary:
    """Tests for escalation summary generation."""

    def test_summary_with_no_issues(self, contract_with_tasks):
        """Summary shows healthy state when no issues."""
        summary = get_escalation_summary(contract_with_tasks)

        assert summary["circuit_breaker_status"] == CircuitBreakerStatus.CLOSED
        assert summary["total_cycles"] == 0
        assert len(summary["escalated_tasks"]) == 0
        assert not summary["requires_intervention"]

    def test_summary_with_escalated_task(self, contract_with_tasks):
        """Summary shows escalated task information."""
        contract_with_tasks.phases[0].tasks[0].escalated = True
        contract_with_tasks.phases[0].tasks[0].review_cycles = 3
        contract_with_tasks.circuit_breaker.status = CircuitBreakerStatus.OPEN

        summary = get_escalation_summary(contract_with_tasks)

        assert summary["circuit_breaker_status"] == CircuitBreakerStatus.OPEN
        assert summary["requires_intervention"] is True
        assert len(summary["escalated_tasks"]) == 1
        assert summary["escalated_tasks"][0]["id"] == "task-1-1"

    def test_summary_includes_incomplete_tasks(self, contract_with_tasks):
        """Summary lists incomplete but not escalated tasks."""
        contract_with_tasks.phases[0].tasks[0].status = TaskStatus.IN_PROGRESS
        contract_with_tasks.phases[0].tasks[1].status = TaskStatus.PENDING

        summary = get_escalation_summary(contract_with_tasks)

        assert len(summary["incomplete_tasks"]) == 2

    def test_summary_with_stuck_phase(self, contract_with_tasks):
        """Summary shows stuck phase information."""
        contract_with_tasks.phases[0].escalated = True
        contract_with_tasks.phases[0].escalation_reason = "Phase exceeded max cycles"
        contract_with_tasks.circuit_breaker.status = CircuitBreakerStatus.OPEN

        summary = get_escalation_summary(contract_with_tasks)

        assert len(summary["stuck_phases"]) == 1
        assert summary["stuck_phases"][0]["id"] == "phase-1"


class TestCircuitBreakerIntegration:
    """Integration tests for circuit breaker with contract persistence."""

    def test_escalation_persists_across_loads(self, temp_repo, contract_with_tasks):
        """Circuit breaker state persists when contract is saved/loaded."""
        contract_with_tasks.phases[0].tasks[0].review_cycles = 2
        save_contract(contract_with_tasks, temp_repo)

        # Increment to trigger threshold
        contract = load_contract(300, temp_repo)
        updated, _ = increment_task_cycle(contract, "phase-1", "task-1-1")
        save_contract(updated, temp_repo)

        # Reload and verify
        reloaded = load_contract(300, temp_repo)
        assert reloaded.circuit_breaker.status == CircuitBreakerStatus.OPEN
        assert reloaded.phases[0].tasks[0].escalated is True

    def test_human_closes_circuit_breaker_persists(
        self, temp_repo, contract_with_tasks
    ):
        """Human closing circuit breaker persists correctly."""
        contract_with_tasks.circuit_breaker.status = CircuitBreakerStatus.OPEN
        save_contract(contract_with_tasks, temp_repo)

        contract = load_contract(300, temp_repo)
        updated = close_circuit_breaker(
            contract,
            actor="reviewer",
            reason="Provided guidance",
        )
        save_contract(updated, temp_repo)

        reloaded = load_contract(300, temp_repo)
        assert reloaded.circuit_breaker.status == CircuitBreakerStatus.CLOSED
