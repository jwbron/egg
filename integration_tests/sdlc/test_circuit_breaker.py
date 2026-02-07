"""Integration tests for circuit breaker functionality."""

import pytest
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "shared"))

from egg_contracts import Contract, Issue, Phase, Task, save_contract, load_contract
from egg_contracts.models import (
    CircuitBreaker,
    CircuitBreakerStatus,
    PhaseStatus,
    PipelinePhase,
    TaskStatus,
)
from egg_contracts.circuit_breaker import (
    check_circuit_breaker,
    get_circuit_status,
    get_stuck_tasks,
    increment_task_cycles,
    increment_total_cycles,
    is_circuit_open,
    reset_circuit_breaker,
    trip_circuit_breaker,
)


@pytest.fixture
def temp_repo():
    """Create temporary repository."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        (repo_root / ".egg" / "contracts").mkdir(parents=True)
        yield repo_root


@pytest.fixture
def contract_with_tasks(temp_repo):
    """Create contract with tasks for testing."""
    contract = Contract(
        issue=Issue(number=200, title="Test", url="https://example.com/200"),
        currentPhase=PipelinePhase.IMPLEMENT,
        branch="egg/issue-200",
        phases=[
            Phase(
                id="phase-1",
                name="Test Phase",
                status=PhaseStatus.IN_PROGRESS,
                tasks=[
                    Task(
                        id="task-1",
                        description="Task 1",
                        status=TaskStatus.INCOMPLETE,
                        review_cycles=0,
                        max_cycles=3,
                    ),
                    Task(
                        id="task-2",
                        description="Task 2",
                        status=TaskStatus.PENDING,
                        review_cycles=0,
                        max_cycles=3,
                    ),
                ],
            ),
        ],
        circuit_breaker=CircuitBreaker(
            total_cycles=0,
            max_total_cycles=10,
            status=CircuitBreakerStatus.CLOSED,
        ),
    )
    save_contract(contract, temp_repo)
    return contract


class TestCircuitBreakerCheck:
    """Tests for circuit breaker check logic."""

    def test_no_trip_within_limits(self, contract_with_tasks):
        """Test that breaker doesn't trip when within limits."""
        result = check_circuit_breaker(contract_with_tasks)
        assert result.tripped is False

    def test_trip_on_total_cycles_exceeded(self, contract_with_tasks):
        """Test breaker trips when total cycles exceeded."""
        contract_with_tasks.circuit_breaker.total_cycles = 10
        result = check_circuit_breaker(contract_with_tasks)
        assert result.tripped is True
        assert "total" in result.reason.lower()

    def test_trip_on_task_cycles_exceeded(self, contract_with_tasks):
        """Test breaker trips when task cycles exceeded."""
        contract_with_tasks.phases[0].tasks[0].review_cycles = 3
        result = check_circuit_breaker(contract_with_tasks)
        assert result.tripped is True
        assert "task-1" in result.reason

    def test_trip_on_escalated_task(self, contract_with_tasks):
        """Test breaker trips when task is escalated."""
        contract_with_tasks.phases[0].tasks[0].escalated = True
        result = check_circuit_breaker(contract_with_tasks)
        assert result.tripped is True
        assert "escalated" in result.reason.lower()


class TestCircuitBreakerOperations:
    """Tests for circuit breaker state operations."""

    def test_trip_circuit_breaker(self, contract_with_tasks):
        """Test tripping the circuit breaker."""
        trip_circuit_breaker(contract_with_tasks, "Test trip", task_id="task-1")

        assert contract_with_tasks.circuit_breaker.status == CircuitBreakerStatus.OPEN
        assert contract_with_tasks.circuit_breaker.opened_reason == "Test trip"
        assert contract_with_tasks.phases[0].tasks[0].escalated is True

    def test_reset_circuit_breaker(self, contract_with_tasks):
        """Test resetting the circuit breaker."""
        # First trip it
        trip_circuit_breaker(contract_with_tasks, "Trip", task_id="task-1")
        contract_with_tasks.circuit_breaker.total_cycles = 5

        # Then reset
        reset_circuit_breaker(contract_with_tasks)

        assert contract_with_tasks.circuit_breaker.status == CircuitBreakerStatus.CLOSED
        assert contract_with_tasks.circuit_breaker.total_cycles == 0
        assert contract_with_tasks.phases[0].tasks[0].escalated is False

    def test_increment_total_cycles(self, contract_with_tasks):
        """Test incrementing total cycles."""
        new_count = increment_total_cycles(contract_with_tasks)
        assert new_count == 1
        assert contract_with_tasks.circuit_breaker.total_cycles == 1

        new_count = increment_total_cycles(contract_with_tasks)
        assert new_count == 2

    def test_increment_task_cycles(self, contract_with_tasks):
        """Test incrementing task cycles."""
        new_count = increment_task_cycles(contract_with_tasks, "task-1")
        assert new_count == 1

        new_count = increment_task_cycles(contract_with_tasks, "task-1")
        assert new_count == 2

    def test_increment_task_cycles_not_found(self, contract_with_tasks):
        """Test incrementing cycles for non-existent task."""
        result = increment_task_cycles(contract_with_tasks, "task-99")
        assert result is None


class TestStuckTasks:
    """Tests for stuck task detection."""

    def test_no_stuck_tasks(self, contract_with_tasks):
        """Test no stuck tasks when cycles low."""
        stuck = get_stuck_tasks(contract_with_tasks)
        assert len(stuck) == 0

    def test_detect_stuck_task(self, contract_with_tasks):
        """Test detection of stuck tasks."""
        # Set task-1 to near limit
        contract_with_tasks.phases[0].tasks[0].review_cycles = 2  # At limit - 1
        stuck = get_stuck_tasks(contract_with_tasks)

        assert len(stuck) == 1
        phase_id, task_id, cycles = stuck[0]
        assert task_id == "task-1"
        assert cycles == 2

    def test_stuck_task_only_counts_incomplete(self, contract_with_tasks):
        """Test that only incomplete tasks are counted as stuck."""
        # Complete task with high cycles
        contract_with_tasks.phases[0].tasks[0].status = TaskStatus.COMPLETE
        contract_with_tasks.phases[0].tasks[0].review_cycles = 10

        stuck = get_stuck_tasks(contract_with_tasks)
        assert len(stuck) == 0


class TestCircuitStatus:
    """Tests for circuit status reporting."""

    def test_get_status_closed(self, contract_with_tasks):
        """Test getting status when closed."""
        status = get_circuit_status(contract_with_tasks)
        assert status["status"] == "closed"
        assert status["total_cycles"] == 0

    def test_get_status_open(self, contract_with_tasks):
        """Test getting status when open."""
        trip_circuit_breaker(contract_with_tasks, "Test")
        status = get_circuit_status(contract_with_tasks)
        assert status["status"] == "open"
        assert status["opened_reason"] == "Test"

    def test_is_circuit_open(self, contract_with_tasks):
        """Test is_circuit_open helper."""
        assert is_circuit_open(contract_with_tasks) is False

        trip_circuit_breaker(contract_with_tasks, "Test")
        assert is_circuit_open(contract_with_tasks) is True


class TestCircuitBreakerPersistence:
    """Tests for circuit breaker state persistence."""

    def test_state_persists_across_load(self, temp_repo, contract_with_tasks):
        """Test that circuit breaker state persists after save/load."""
        # Trip the breaker
        trip_circuit_breaker(contract_with_tasks, "Persistence test", "task-1")
        contract_with_tasks.circuit_breaker.total_cycles = 5
        save_contract(contract_with_tasks, temp_repo)

        # Load and verify
        loaded = load_contract(temp_repo, 200)
        assert loaded.circuit_breaker.status == CircuitBreakerStatus.OPEN
        assert loaded.circuit_breaker.total_cycles == 5
        assert loaded.circuit_breaker.opened_reason == "Persistence test"
        assert loaded.phases[0].tasks[0].escalated is True
