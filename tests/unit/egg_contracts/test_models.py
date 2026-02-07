"""Tests for contract Pydantic models."""

import pytest
from datetime import datetime, UTC

import sys
from pathlib import Path

# Add shared to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "shared"))

from egg_contracts.models import (
    AcceptanceCriterion,
    AuditAction,
    AuditEntry,
    CircuitBreaker,
    CircuitBreakerStatus,
    Contract,
    Decision,
    DecisionOption,
    DecisionType,
    Issue,
    Phase,
    PhaseStatus,
    PipelinePhase,
    ReviewFeedback,
    Task,
    TaskStatus,
)


class TestIssue:
    """Tests for Issue model."""

    def test_valid_issue(self):
        issue = Issue(
            number=123,
            title="Test issue",
            url="https://github.com/owner/repo/issues/123",
        )
        assert issue.number == 123
        assert issue.title == "Test issue"

    def test_issue_requires_positive_number(self):
        with pytest.raises(ValueError):
            Issue(number=0, title="Test", url="https://example.com")

    def test_issue_requires_title(self):
        with pytest.raises(ValueError):
            Issue(number=1, title="", url="https://example.com")


class TestTask:
    """Tests for Task model."""

    def test_valid_task(self):
        task = Task(
            id="task-1",
            description="Implement feature",
            status=TaskStatus.PENDING,
        )
        assert task.id == "task-1"
        assert task.status == TaskStatus.PENDING
        assert task.commit is None
        assert task.notes == ""

    def test_task_with_commit(self):
        task = Task(
            id="task-1",
            description="Implement feature",
            commit="abc1234",
        )
        assert task.commit == "abc1234"

    def test_task_invalid_id_pattern(self):
        with pytest.raises(ValueError):
            Task(id="invalid", description="Test")

    def test_task_invalid_commit_sha(self):
        with pytest.raises(ValueError):
            Task(id="task-1", description="Test", commit="not-a-sha")

    def test_task_status_values(self):
        for status in TaskStatus:
            task = Task(id="task-1", description="Test", status=status)
            assert task.status == status


class TestPhase:
    """Tests for Phase model."""

    def test_valid_phase(self):
        phase = Phase(
            id="phase-1",
            name="Implementation",
            tasks=[
                Task(id="task-1", description="First task"),
                Task(id="task-2", description="Second task"),
            ],
        )
        assert phase.id == "phase-1"
        assert len(phase.tasks) == 2
        assert phase.status == PhaseStatus.PENDING

    def test_phase_defaults(self):
        phase = Phase(id="phase-1", name="Test", tasks=[])
        assert phase.review_cycles == 0
        assert phase.max_cycles == 3
        assert phase.escalated is False
        assert phase.escalation_reason is None

    def test_phase_with_feedback(self):
        feedback = ReviewFeedback(
            timestamp=datetime.now(UTC),
            feedback="Needs more tests",
            cycle=1,
        )
        phase = Phase(
            id="phase-1",
            name="Test",
            tasks=[],
            review_feedback=[feedback],
        )
        assert len(phase.review_feedback) == 1


class TestDecision:
    """Tests for Decision model."""

    def test_valid_decision(self):
        decision = Decision(
            id="decision-1",
            question="Approve the plan?",
            type=DecisionType.HITL,
        )
        assert decision.id == "decision-1"
        assert decision.resolved is False

    def test_decision_with_options(self):
        decision = Decision(
            id="decision-1",
            question="Choose an approach",
            type=DecisionType.HITL,
            options=[
                DecisionOption(id="opt-1", label="Option A"),
                DecisionOption(id="opt-2", label="Option B"),
            ],
        )
        assert len(decision.options) == 2

    def test_resolved_decision(self):
        decision = Decision(
            id="decision-1",
            question="Approve?",
            type=DecisionType.HITL,
            resolved=True,
            resolution="approved",
            resolved_by="jwbron",
            resolved_at=datetime.now(UTC),
        )
        assert decision.resolved is True
        assert decision.resolution == "approved"


class TestCircuitBreaker:
    """Tests for CircuitBreaker model."""

    def test_default_circuit_breaker(self):
        cb = CircuitBreaker()
        assert cb.total_cycles == 0
        assert cb.max_total_cycles == 10
        assert cb.status == CircuitBreakerStatus.CLOSED

    def test_open_circuit_breaker(self):
        cb = CircuitBreaker(
            status=CircuitBreakerStatus.OPEN,
            opened_at=datetime.now(UTC),
            opened_reason="Per-task threshold exceeded",
        )
        assert cb.status == CircuitBreakerStatus.OPEN


class TestAuditEntry:
    """Tests for AuditEntry model."""

    def test_valid_audit_entry(self):
        entry = AuditEntry(
            timestamp=datetime.now(UTC),
            actor="implementer",
            role="implementer",
            action=AuditAction.UPDATE,
            field_path="phases.0.tasks.0.commit",
            old_value=None,
            new_value="abc1234",
        )
        assert entry.action == AuditAction.UPDATE

    def test_blocked_audit_entry(self):
        entry = AuditEntry(
            timestamp=datetime.now(UTC),
            actor="implementer",
            action=AuditAction.BLOCKED,
            field_path="phases.0.tasks.0.status",
            new_value="complete",
            reason="Role 'implementer' cannot modify status",
        )
        assert entry.action == AuditAction.BLOCKED
        assert entry.reason is not None


class TestContract:
    """Tests for Contract model."""

    def test_minimal_contract(self):
        contract = Contract(
            issue=Issue(
                number=123,
                title="Test issue",
                url="https://github.com/owner/repo/issues/123",
            ),
        )
        assert contract.schemaVersion == "1.0"
        assert contract.currentPhase == PipelinePhase.REFINE
        assert len(contract.phases) == 0

    def test_full_contract(self):
        contract = Contract(
            issue=Issue(number=123, title="Test", url="https://example.com/123"),
            currentPhase=PipelinePhase.IMPLEMENT,
            branch="egg/issue-123",
            phases=[
                Phase(
                    id="phase-1",
                    name="Setup",
                    tasks=[Task(id="task-1", description="Create files")],
                )
            ],
            decisions=[
                Decision(id="decision-1", question="Approve?", type=DecisionType.HITL)
            ],
            circuit_breaker=CircuitBreaker(),
            audit_log=[],
        )
        assert contract.currentPhase == PipelinePhase.IMPLEMENT
        assert len(contract.phases) == 1

    def test_get_phase(self):
        contract = Contract(
            issue=Issue(number=1, title="T", url="u"),
            phases=[
                Phase(id="phase-1", name="First", tasks=[]),
                Phase(id="phase-2", name="Second", tasks=[]),
            ],
        )
        phase = contract.get_phase("phase-2")
        assert phase is not None
        assert phase.name == "Second"

        missing = contract.get_phase("phase-99")
        assert missing is None

    def test_get_task(self):
        contract = Contract(
            issue=Issue(number=1, title="T", url="u"),
            phases=[
                Phase(
                    id="phase-1",
                    name="First",
                    tasks=[
                        Task(id="task-1", description="A"),
                        Task(id="task-2", description="B"),
                    ],
                ),
            ],
        )
        phase, task = contract.get_task("task-2")
        assert phase is not None
        assert task is not None
        assert task.description == "B"

        phase, task = contract.get_task("task-99")
        assert phase is None
        assert task is None

    def test_next_task_id(self):
        contract = Contract(
            issue=Issue(number=1, title="T", url="u"),
            phases=[
                Phase(
                    id="phase-1",
                    name="First",
                    tasks=[
                        Task(id="task-1", description="A"),
                        Task(id="task-5", description="B"),
                    ],
                ),
            ],
        )
        assert contract.next_task_id() == "task-6"

    def test_next_phase_id(self):
        contract = Contract(
            issue=Issue(number=1, title="T", url="u"),
            phases=[
                Phase(id="phase-1", name="First", tasks=[]),
                Phase(id="phase-3", name="Third", tasks=[]),
            ],
        )
        assert contract.next_phase_id() == "phase-4"

    def test_next_decision_id(self):
        contract = Contract(
            issue=Issue(number=1, title="T", url="u"),
            decisions=[
                Decision(id="decision-1", question="A", type=DecisionType.HITL),
                Decision(id="decision-2", question="B", type=DecisionType.HITL),
            ],
        )
        assert contract.next_decision_id() == "decision-3"
