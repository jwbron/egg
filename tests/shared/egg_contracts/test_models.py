"""Tests for egg_contracts.models module."""

from datetime import UTC, datetime

import pytest
from egg_contracts.models import (
    AuditAction,
    AuditEntry,
    AuditRole,
    CircuitBreaker,
    CircuitBreakerStatus,
    Contract,
    Decision,
    DecisionType,
    IssueInfo,
    Phase,
    PhaseStatus,
    PipelinePhase,
    Task,
    TaskStatus,
)
from pydantic import ValidationError


class TestIssueInfo:
    """Tests for IssueInfo model."""

    def test_valid_issue(self):
        """Test creating a valid issue info."""
        issue = IssueInfo(
            number=123,
            title="Test issue",
            url="https://github.com/owner/repo/issues/123",
        )
        assert issue.number == 123
        assert issue.title == "Test issue"

    def test_invalid_issue_number(self):
        """Test that issue number must be positive."""
        with pytest.raises(ValidationError):
            IssueInfo(
                number=0,
                title="Test",
                url="https://github.com/owner/repo/issues/0",
            )

    def test_empty_title_rejected(self):
        """Test that empty title is rejected."""
        with pytest.raises(ValidationError):
            IssueInfo(
                number=1,
                title="",
                url="https://github.com/owner/repo/issues/1",
            )


class TestTask:
    """Tests for Task model."""

    def test_valid_task(self):
        """Test creating a valid task."""
        task = Task(
            id="task-1",
            description="Implement feature",
        )
        assert task.id == "task-1"
        assert task.status == TaskStatus.PENDING
        assert task.commit is None
        assert task.notes == ""

    def test_task_with_commit(self):
        """Test task with commit SHA."""
        task = Task(
            id="task-1",
            description="Test",
            commit="abc1234",
        )
        assert task.commit == "abc1234"

    def test_invalid_task_id_pattern(self):
        """Test that task ID must match pattern."""
        with pytest.raises(ValidationError):
            Task(
                id="invalid-id",
                description="Test",
            )

    def test_invalid_commit_pattern(self):
        """Test that commit must be valid SHA pattern."""
        with pytest.raises(ValidationError):
            Task(
                id="task-1",
                description="Test",
                commit="not-a-sha",
            )

    def test_all_task_statuses(self):
        """Test all valid task statuses."""
        for status in TaskStatus:
            task = Task(
                id="task-1",
                description="Test",
                status=status,
            )
            assert task.status == status


class TestPhase:
    """Tests for Phase model."""

    def test_valid_phase(self):
        """Test creating a valid phase."""
        phase = Phase(
            id="phase-1",
            name="Setup",
        )
        assert phase.id == "phase-1"
        assert phase.name == "Setup"
        assert phase.status == PhaseStatus.PENDING
        assert phase.tasks == []

    def test_phase_with_tasks(self):
        """Test phase with nested tasks."""
        phase = Phase(
            id="phase-1",
            name="Setup",
            tasks=[
                Task(id="task-1", description="First task"),
                Task(id="task-2", description="Second task"),
            ],
        )
        assert len(phase.tasks) == 2
        assert phase.tasks[0].id == "task-1"

    def test_invalid_phase_id(self):
        """Test that phase ID must match pattern."""
        with pytest.raises(ValidationError):
            Phase(
                id="invalid",
                name="Test",
            )


class TestDecision:
    """Tests for Decision model."""

    def test_valid_hitl_decision(self):
        """Test creating a HITL decision."""
        decision = Decision(
            id="decision-1",
            question="Approve plan?",
            type=DecisionType.HITL,
        )
        assert decision.id == "decision-1"
        assert decision.type == DecisionType.HITL
        assert decision.resolved is False
        assert decision.resolution is None

    def test_resolved_decision(self):
        """Test a resolved decision."""
        decision = Decision(
            id="decision-1",
            question="Approve plan?",
            type=DecisionType.HITL,
            resolved=True,
            resolution="approved",
            resolved_by="human@example.com",
            resolved_at=datetime.now(UTC),
        )
        assert decision.resolved is True
        assert decision.resolution == "approved"


class TestCircuitBreaker:
    """Tests for CircuitBreaker model."""

    def test_default_circuit_breaker(self):
        """Test default circuit breaker state."""
        cb = CircuitBreaker()
        assert cb.total_cycles == 0
        assert cb.max_total_cycles == 10
        assert cb.status == CircuitBreakerStatus.CLOSED

    def test_open_circuit_breaker(self):
        """Test open circuit breaker."""
        cb = CircuitBreaker(
            total_cycles=5,
            status=CircuitBreakerStatus.OPEN,
        )
        assert cb.status == CircuitBreakerStatus.OPEN


class TestAuditEntry:
    """Tests for AuditEntry model."""

    def test_valid_audit_entry(self):
        """Test creating a valid audit entry."""
        entry = AuditEntry(
            timestamp=datetime.now(UTC),
            actor="egg",
            role=AuditRole.IMPLEMENTER,
            action=AuditAction.UPDATE,
            field_path="phases.0.tasks.0.commit",
            old_value=None,
            new_value="abc1234",
        )
        assert entry.actor == "egg"
        assert entry.role == AuditRole.IMPLEMENTER
        assert entry.action == AuditAction.UPDATE


class TestContract:
    """Tests for Contract model."""

    def test_minimal_contract(self):
        """Test creating a minimal contract."""
        contract = Contract(
            issue=IssueInfo(
                number=133,
                title="Test issue",
                url="https://github.com/owner/repo/issues/133",
            ),
        )
        assert contract.schemaVersion == "1.0"
        assert contract.issue.number == 133
        assert contract.current_phase == PipelinePhase.REFINE
        assert contract.phases == []
        assert contract.decisions == []
        assert contract.workflow_owner is None

    def test_contract_with_workflow_owner(self):
        """Test creating a contract with workflow_owner field."""
        contract = Contract(
            issue=IssueInfo(
                number=133,
                title="Test issue",
                url="https://github.com/owner/repo/issues/133",
            ),
            workflow_owner="jwbron",
        )
        assert contract.workflow_owner == "jwbron"

    def test_contract_workflow_owner_null(self):
        """Test that workflow_owner can be explicitly set to None."""
        contract = Contract(
            issue=IssueInfo(
                number=133,
                title="Test issue",
                url="https://github.com/owner/repo/issues/133",
            ),
            workflow_owner=None,
        )
        assert contract.workflow_owner is None

    def test_full_contract(self):
        """Test creating a contract with all fields."""
        contract = Contract(
            issue=IssueInfo(
                number=133,
                title="Test issue",
                url="https://github.com/owner/repo/issues/133",
            ),
            current_phase=PipelinePhase.IMPLEMENT,
            phases=[
                Phase(
                    id="phase-1",
                    name="Setup",
                    tasks=[
                        Task(id="task-1", description="First task"),
                    ],
                ),
            ],
            decisions=[
                Decision(
                    id="decision-1",
                    question="Approve?",
                    type=DecisionType.HITL,
                ),
            ],
        )
        assert contract.current_phase == PipelinePhase.IMPLEMENT
        assert len(contract.phases) == 1
        assert len(contract.decisions) == 1

    def test_get_task(self):
        """Test get_task helper method."""
        contract = Contract(
            issue=IssueInfo(number=1, title="Test", url="https://example.com"),
            phases=[
                Phase(
                    id="phase-1",
                    name="Setup",
                    tasks=[
                        Task(id="task-1", description="First"),
                        Task(id="task-2", description="Second"),
                    ],
                ),
            ],
        )
        task = contract.get_task("phase-1", "task-2")
        assert task is not None
        assert task.description == "Second"

        # Non-existent task
        assert contract.get_task("phase-1", "task-99") is None
        assert contract.get_task("phase-99", "task-1") is None

    def test_get_phase(self):
        """Test get_phase helper method."""
        contract = Contract(
            issue=IssueInfo(number=1, title="Test", url="https://example.com"),
            phases=[
                Phase(id="phase-1", name="First"),
                Phase(id="phase-2", name="Second"),
            ],
        )
        phase = contract.get_phase("phase-2")
        assert phase is not None
        assert phase.name == "Second"

        assert contract.get_phase("phase-99") is None

    def test_get_decision(self):
        """Test get_decision helper method."""
        contract = Contract(
            issue=IssueInfo(number=1, title="Test", url="https://example.com"),
            decisions=[
                Decision(id="decision-1", question="First?", type=DecisionType.HITL),
            ],
        )
        decision = contract.get_decision("decision-1")
        assert decision is not None
        assert decision.question == "First?"

        assert contract.get_decision("decision-99") is None


class TestContractSerialization:
    """Tests for contract serialization."""

    def test_json_roundtrip(self):
        """Test that contract can be serialized and deserialized."""
        original = Contract(
            issue=IssueInfo(
                number=133,
                title="Test",
                url="https://example.com",
            ),
            phases=[
                Phase(
                    id="phase-1",
                    name="Setup",
                    tasks=[Task(id="task-1", description="Test")],
                ),
            ],
        )

        # Serialize
        data = original.model_dump(mode="json")
        assert isinstance(data, dict)

        # Deserialize
        restored = Contract.model_validate(data)
        assert restored.issue.number == original.issue.number
        assert len(restored.phases) == 1
        assert restored.phases[0].tasks[0].id == "task-1"

    def test_json_roundtrip_with_workflow_owner(self):
        """Test that workflow_owner serializes and deserializes correctly."""
        original = Contract(
            issue=IssueInfo(
                number=133,
                title="Test",
                url="https://example.com",
            ),
            workflow_owner="testuser",
        )

        # Serialize
        data = original.model_dump(mode="json")
        assert data["workflow_owner"] == "testuser"

        # Deserialize
        restored = Contract.model_validate(data)
        assert restored.workflow_owner == "testuser"

    def test_json_roundtrip_with_null_workflow_owner(self):
        """Test that null workflow_owner serializes correctly."""
        original = Contract(
            issue=IssueInfo(
                number=133,
                title="Test",
                url="https://example.com",
            ),
            workflow_owner=None,
        )

        # Serialize
        data = original.model_dump(mode="json")
        assert data["workflow_owner"] is None

        # Deserialize
        restored = Contract.model_validate(data)
        assert restored.workflow_owner is None
