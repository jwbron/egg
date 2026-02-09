"""Tests for egg_contracts.models module."""

from datetime import UTC, datetime

import pytest
from egg_contracts.models import (
    AuditAction,
    AuditEntry,
    AuditRole,
    CheckResult,
    CheckStatus,
    CircuitBreaker,
    CircuitBreakerStatus,
    Contract,
    Decision,
    DecisionType,
    HumanReviewMechanism,
    IntermediateCheck,
    IssueInfo,
    Phase,
    PhaseConfig,
    PhaseConfigMap,
    PhaseStatus,
    PipelinePhase,
    ReviewerVerdict,
    Task,
    TaskStatus,
    WorkLoopPhase,
    WorkLoopState,
    WorkLoopStep,
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


# =============================================================================
# Work Loop Model Tests
# =============================================================================


class TestIntermediateCheck:
    """Tests for IntermediateCheck model."""

    def test_valid_check(self):
        """Test creating a valid intermediate check."""
        check = IntermediateCheck(
            id="check-lint",
            name="Run Linter",
            command="make lint",
        )
        assert check.id == "check-lint"
        assert check.name == "Run Linter"
        assert check.command == "make lint"
        assert check.auto_fix is False
        assert check.depends_on == []
        assert check.required is True
        assert check.timeout_minutes == 30

    def test_check_with_auto_fix(self):
        """Test check with auto-fix enabled."""
        check = IntermediateCheck(
            id="check-lint",
            name="Run Linter",
            command="make lint",
            auto_fix=True,
            auto_fix_command="make fix",
        )
        assert check.auto_fix is True
        assert check.auto_fix_command == "make fix"

    def test_check_with_dependencies(self):
        """Test check with dependencies."""
        check = IntermediateCheck(
            id="check-test",
            name="Run Tests",
            command="make test",
            depends_on=["check-lint", "check-build"],
        )
        assert check.depends_on == ["check-lint", "check-build"]

    def test_invalid_check_id_pattern(self):
        """Test that check ID must match pattern."""
        with pytest.raises(ValidationError):
            IntermediateCheck(
                id="invalid-id",  # Missing 'check-' prefix
                name="Test",
                command="test",
            )

    def test_depends_on_accepts_any_strings(self):
        """Test that depends_on accepts any strings (schema validates pattern)."""
        # Note: Pattern validation is done at schema level, not Pydantic level
        # This test documents that Pydantic model accepts any strings
        check = IntermediateCheck(
            id="check-test",
            name="Test",
            command="test",
            depends_on=["check-lint", "check-build"],
        )
        assert check.depends_on == ["check-lint", "check-build"]

    def test_workflow_reference_command(self):
        """Test check with workflow reference as command."""
        check = IntermediateCheck(
            id="check-autofix",
            name="Run Autofix",
            command="workflow:reusable-autofix.yml",
        )
        assert check.command == "workflow:reusable-autofix.yml"


class TestCheckResult:
    """Tests for CheckResult model."""

    def test_pending_result(self):
        """Test creating a pending check result."""
        result = CheckResult(
            check_id="check-lint",
            status=CheckStatus.PENDING,
        )
        assert result.check_id == "check-lint"
        assert result.status == CheckStatus.PENDING
        assert result.started_at is None
        assert result.completed_at is None
        assert result.output == ""

    def test_completed_result(self):
        """Test creating a completed check result."""
        now = datetime.now(UTC)
        result = CheckResult(
            check_id="check-lint",
            status=CheckStatus.PASSED,
            started_at=now,
            completed_at=now,
            output="All checks passed",
        )
        assert result.status == CheckStatus.PASSED
        assert result.output == "All checks passed"

    def test_failed_with_auto_fix(self):
        """Test result with auto-fix attempt."""
        result = CheckResult(
            check_id="check-lint",
            status=CheckStatus.FIXED,
            auto_fix_attempted=True,
            auto_fix_commit="abc1234",
        )
        assert result.status == CheckStatus.FIXED
        assert result.auto_fix_attempted is True
        assert result.auto_fix_commit == "abc1234"

    def test_all_check_statuses(self):
        """Test all valid check statuses."""
        for status in CheckStatus:
            result = CheckResult(
                check_id="check-test",
                status=status,
            )
            assert result.status == status


class TestPhaseConfig:
    """Tests for PhaseConfig model."""

    def test_minimal_config(self):
        """Test creating minimal phase config."""
        config = PhaseConfig(
            producer_prompt_script="action/build-refine-prompt.sh",
            max_cycles=3,
        )
        assert config.producer_prompt_script == "action/build-refine-prompt.sh"
        assert config.max_cycles == 3
        assert config.reviewer_prompt_script is None
        assert config.intermediate_checks == []
        assert config.human_review_mechanism == HumanReviewMechanism.ISSUE_COMMENT

    def test_full_config(self):
        """Test creating full phase config."""
        check = IntermediateCheck(
            id="check-lint",
            name="Lint",
            command="make lint",
        )
        config = PhaseConfig(
            producer_prompt_script="action/build-implement-prompt.sh",
            producer_timeout_minutes=120,
            reviewer_prompt_script="action/build-review-prompt.sh",
            reviewer_timeout_minutes=45,
            max_cycles=5,
            intermediate_checks=[check],
            human_review_mechanism=HumanReviewMechanism.PR_REVIEW,
            output_artifact_path=".egg-state/drafts/{issue}-impl.md",
            post_producer_script="action/populate-contract-tasks.py",
        )
        assert config.producer_timeout_minutes == 120
        assert config.reviewer_timeout_minutes == 45
        assert len(config.intermediate_checks) == 1
        assert config.human_review_mechanism == HumanReviewMechanism.PR_REVIEW

    def test_invalid_max_cycles(self):
        """Test that max_cycles must be at least 1."""
        with pytest.raises(ValidationError):
            PhaseConfig(
                producer_prompt_script="test.sh",
                max_cycles=0,
            )


class TestPhaseConfigMap:
    """Tests for PhaseConfigMap model."""

    def test_empty_config_map(self):
        """Test creating empty config map."""
        config_map = PhaseConfigMap()
        assert config_map.refine is None
        assert config_map.plan is None
        assert config_map.implement is None

    def test_partial_config_map(self):
        """Test creating partial config map."""
        refine_config = PhaseConfig(
            producer_prompt_script="action/build-refine-prompt.sh",
            max_cycles=3,
        )
        config_map = PhaseConfigMap(refine=refine_config)
        assert config_map.refine is not None
        assert config_map.plan is None
        assert config_map.implement is None

    def test_get_config_method(self):
        """Test get_config helper method."""
        refine_config = PhaseConfig(
            producer_prompt_script="refine.sh",
            max_cycles=3,
        )
        plan_config = PhaseConfig(
            producer_prompt_script="plan.sh",
            max_cycles=2,
        )
        config_map = PhaseConfigMap(refine=refine_config, plan=plan_config)

        assert config_map.get_config(WorkLoopPhase.REFINE) == refine_config
        assert config_map.get_config(WorkLoopPhase.PLAN) == plan_config
        assert config_map.get_config(WorkLoopPhase.IMPLEMENT) is None


class TestWorkLoopState:
    """Tests for WorkLoopState model."""

    def test_initial_state(self):
        """Test creating initial work loop state."""
        state = WorkLoopState(
            phase=WorkLoopPhase.REFINE,
            cycle=1,
            step=WorkLoopStep.PRODUCER,
        )
        assert state.phase == WorkLoopPhase.REFINE
        assert state.cycle == 1
        assert state.step == WorkLoopStep.PRODUCER
        assert state.check_results == []
        assert state.last_reviewer_verdict is None
        assert state.human_feedback_pending is False

    def test_state_with_check_results(self):
        """Test state with intermediate check results."""
        result = CheckResult(
            check_id="check-lint",
            status=CheckStatus.PASSED,
        )
        state = WorkLoopState(
            phase=WorkLoopPhase.IMPLEMENT,
            cycle=2,
            step=WorkLoopStep.INTERMEDIATE_CHECKS,
            check_results=[result],
        )
        assert len(state.check_results) == 1
        assert state.check_results[0].check_id == "check-lint"

    def test_state_with_reviewer_verdict(self):
        """Test state with reviewer verdict."""
        state = WorkLoopState(
            phase=WorkLoopPhase.PLAN,
            cycle=1,
            step=WorkLoopStep.DECISION,
            last_reviewer_verdict=ReviewerVerdict.APPROVED,
            last_reviewer_feedback="Plan looks good",
        )
        assert state.last_reviewer_verdict == ReviewerVerdict.APPROVED
        assert state.last_reviewer_feedback == "Plan looks good"

    def test_invalid_cycle_number(self):
        """Test that cycle must be at least 1."""
        with pytest.raises(ValidationError):
            WorkLoopState(
                phase=WorkLoopPhase.REFINE,
                cycle=0,
                step=WorkLoopStep.PRODUCER,
            )

    def test_all_work_loop_steps(self):
        """Test all valid work loop steps."""
        for step in WorkLoopStep:
            state = WorkLoopState(
                phase=WorkLoopPhase.REFINE,
                cycle=1,
                step=step,
            )
            assert state.step == step

    def test_all_reviewer_verdicts(self):
        """Test all valid reviewer verdicts."""
        for verdict in ReviewerVerdict:
            state = WorkLoopState(
                phase=WorkLoopPhase.REFINE,
                cycle=1,
                step=WorkLoopStep.DECISION,
                last_reviewer_verdict=verdict,
            )
            assert state.last_reviewer_verdict == verdict


class TestContractWithWorkLoopFields:
    """Tests for Contract with work loop fields."""

    def test_contract_with_phase_config(self):
        """Test contract with phase_config field."""
        config_map = PhaseConfigMap(
            refine=PhaseConfig(
                producer_prompt_script="refine.sh",
                max_cycles=3,
            )
        )
        contract = Contract(
            issue=IssueInfo(
                number=430,
                title="Test",
                url="https://example.com",
            ),
            phase_config=config_map,
        )
        assert contract.phase_config is not None
        assert contract.phase_config.refine is not None

    def test_contract_with_work_loop_state(self):
        """Test contract with work_loop_state field."""
        state = WorkLoopState(
            phase=WorkLoopPhase.IMPLEMENT,
            cycle=2,
            step=WorkLoopStep.REVIEWER,
        )
        contract = Contract(
            issue=IssueInfo(
                number=430,
                title="Test",
                url="https://example.com",
            ),
            work_loop_state=state,
        )
        assert contract.work_loop_state is not None
        assert contract.work_loop_state.phase == WorkLoopPhase.IMPLEMENT
        assert contract.work_loop_state.cycle == 2

    def test_contract_serialization_with_work_loop_fields(self):
        """Test that work loop fields serialize correctly."""
        config_map = PhaseConfigMap(
            implement=PhaseConfig(
                producer_prompt_script="impl.sh",
                max_cycles=3,
                intermediate_checks=[
                    IntermediateCheck(
                        id="check-test",
                        name="Tests",
                        command="make test",
                    )
                ],
            )
        )
        state = WorkLoopState(
            phase=WorkLoopPhase.IMPLEMENT,
            cycle=1,
            step=WorkLoopStep.PRODUCER,
            started_at=datetime.now(UTC),
        )
        contract = Contract(
            issue=IssueInfo(
                number=430,
                title="Test",
                url="https://example.com",
            ),
            phase_config=config_map,
            work_loop_state=state,
        )

        # Serialize
        data = contract.model_dump(mode="json")
        assert "phase_config" in data
        assert data["phase_config"]["implement"]["intermediate_checks"][0]["id"] == "check-test"
        assert data["work_loop_state"]["phase"] == "implement"

        # Deserialize
        restored = Contract.model_validate(data)
        assert restored.phase_config is not None
        assert restored.phase_config.implement is not None
        assert len(restored.phase_config.implement.intermediate_checks) == 1
        assert restored.work_loop_state is not None
        assert restored.work_loop_state.phase == WorkLoopPhase.IMPLEMENT
