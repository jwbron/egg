"""Tests for egg_contracts.models module."""

from datetime import UTC, datetime

import pytest
from egg_contracts.models import (
    AuditAction,
    AuditEntry,
    AuditRole,
    CheckDefinition,
    CheckResult,
    CheckStatus,
    Contract,
    Decision,
    DecisionType,
    HumanReviewMechanism,
    IssueInfo,
    Phase,
    PhaseConfig,
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


class TestCheckStatus:
    """Tests for CheckStatus enum."""

    def test_all_check_status_values(self):
        """Test all CheckStatus enum values."""
        assert CheckStatus.PASS == "pass"
        assert CheckStatus.FAIL == "fail"
        assert CheckStatus.SKIP == "skip"

    def test_check_status_string_serialization(self):
        """Test that CheckStatus serializes as string."""
        assert str(CheckStatus.PASS) == "pass"
        assert str(CheckStatus.FAIL) == "fail"
        assert str(CheckStatus.SKIP) == "skip"


class TestHumanReviewMechanism:
    """Tests for HumanReviewMechanism enum."""

    def test_all_human_review_mechanism_values(self):
        """Test all HumanReviewMechanism enum values."""
        assert HumanReviewMechanism.ISSUE_CHECKBOX == "ISSUE_CHECKBOX"
        assert HumanReviewMechanism.PR_REVIEW == "PR_REVIEW"

    def test_human_review_mechanism_string_serialization(self):
        """Test that HumanReviewMechanism serializes as string."""
        assert str(HumanReviewMechanism.ISSUE_CHECKBOX) == "ISSUE_CHECKBOX"
        assert str(HumanReviewMechanism.PR_REVIEW) == "PR_REVIEW"


class TestCheckDefinition:
    """Tests for CheckDefinition model."""

    def test_valid_check_definition(self):
        """Test creating a valid check definition."""
        check = CheckDefinition(
            id="check-lint",
            name="Lint Check",
            script="lint_check.py",
        )
        assert check.id == "check-lint"
        assert check.name == "Lint Check"
        assert check.script == "lint_check.py"
        assert check.required is True
        assert check.retry_on_fail is False
        assert check.max_retries == 0

    def test_check_definition_with_all_fields(self):
        """Test check definition with all optional fields."""
        check = CheckDefinition(
            id="check-test-runner",
            name="Test Runner",
            script="test_runner.py",
            required=False,
            retry_on_fail=True,
            max_retries=3,
        )
        assert check.required is False
        assert check.retry_on_fail is True
        assert check.max_retries == 3

    def test_check_definition_invalid_id_pattern(self):
        """Test that check ID must match pattern."""
        with pytest.raises(ValidationError):
            CheckDefinition(
                id="invalid-id",
                name="Test",
                script="test.py",
            )

    def test_check_definition_id_pattern_uppercase_rejected(self):
        """Test that uppercase letters in ID are rejected."""
        with pytest.raises(ValidationError):
            CheckDefinition(
                id="check-Lint",
                name="Test",
                script="test.py",
            )

    def test_check_definition_id_with_numbers(self):
        """Test that check ID can contain numbers."""
        check = CheckDefinition(
            id="check-lint-v2",
            name="Lint V2",
            script="lint_v2.py",
        )
        assert check.id == "check-lint-v2"

    def test_check_definition_empty_name_rejected(self):
        """Test that empty name is rejected."""
        with pytest.raises(ValidationError):
            CheckDefinition(
                id="check-lint",
                name="",
                script="lint.py",
            )

    def test_check_definition_empty_script_rejected(self):
        """Test that empty script is rejected."""
        with pytest.raises(ValidationError):
            CheckDefinition(
                id="check-lint",
                name="Lint",
                script="",
            )


class TestCheckResult:
    """Tests for CheckResult model."""

    def test_valid_check_result(self):
        """Test creating a valid check result."""
        result = CheckResult(
            check_id="check-lint",
            status=CheckStatus.PASS,
        )
        assert result.check_id == "check-lint"
        assert result.status == CheckStatus.PASS
        assert result.message == ""
        assert result.details == {}
        assert result.fixable is False

    def test_check_result_with_all_fields(self):
        """Test check result with all optional fields."""
        result = CheckResult(
            check_id="check-lint",
            status=CheckStatus.FAIL,
            message="Linting failed: 3 errors",
            details={"errors": 3, "warnings": 5},
            fixable=True,
        )
        assert result.message == "Linting failed: 3 errors"
        assert result.details == {"errors": 3, "warnings": 5}
        assert result.fixable is True

    def test_check_result_all_statuses(self):
        """Test check result with all status values."""
        for status in CheckStatus:
            result = CheckResult(
                check_id="check-test",
                status=status,
            )
            assert result.status == status

    def test_check_result_invalid_check_id(self):
        """Test that check_id must match pattern."""
        with pytest.raises(ValidationError):
            CheckResult(
                check_id="invalid-id",
                status=CheckStatus.PASS,
            )

    def test_check_result_json_serialization(self):
        """Test that check result serializes to JSON correctly."""
        result = CheckResult(
            check_id="check-lint",
            status=CheckStatus.FAIL,
            message="Test message",
            details={"key": "value"},
            fixable=True,
        )
        data = result.model_dump(mode="json")
        assert data["check_id"] == "check-lint"
        assert data["status"] == "fail"
        assert data["message"] == "Test message"
        assert data["details"] == {"key": "value"}
        assert data["fixable"] is True


class TestPhaseConfig:
    """Tests for PhaseConfig model."""

    def test_default_phase_config(self):
        """Test creating a phase config with defaults."""
        config = PhaseConfig()
        assert config.checks == []
        assert config.max_review_cycles == 3
        assert config.human_review_mechanism == HumanReviewMechanism.ISSUE_CHECKBOX

    def test_phase_config_with_checks(self):
        """Test phase config with nested check definitions."""
        config = PhaseConfig(
            checks=[
                CheckDefinition(
                    id="check-lint",
                    name="Lint",
                    script="lint.py",
                ),
                CheckDefinition(
                    id="check-test",
                    name="Test",
                    script="test.py",
                ),
            ],
        )
        assert len(config.checks) == 2
        assert config.checks[0].id == "check-lint"
        assert config.checks[1].id == "check-test"

    def test_phase_config_with_pr_review(self):
        """Test phase config with PR review mechanism."""
        config = PhaseConfig(
            human_review_mechanism=HumanReviewMechanism.PR_REVIEW,
        )
        assert config.human_review_mechanism == HumanReviewMechanism.PR_REVIEW

    def test_phase_config_custom_max_cycles(self):
        """Test phase config with custom max review cycles."""
        config = PhaseConfig(max_review_cycles=5)
        assert config.max_review_cycles == 5

    def test_phase_config_invalid_max_cycles(self):
        """Test that max_review_cycles must be at least 1."""
        with pytest.raises(ValidationError):
            PhaseConfig(max_review_cycles=0)


class TestContractWithPhaseConfigs:
    """Tests for Contract with phase_configs field."""

    def test_contract_without_phase_configs(self):
        """Test that phase_configs defaults to None."""
        contract = Contract(
            issue=IssueInfo(
                number=123,
                title="Test",
                url="https://example.com",
            ),
        )
        assert contract.phase_configs is None

    def test_contract_with_phase_configs(self):
        """Test contract with phase configurations."""
        contract = Contract(
            issue=IssueInfo(
                number=123,
                title="Test",
                url="https://example.com",
            ),
            phase_configs={
                PipelinePhase.IMPLEMENT: PhaseConfig(
                    max_review_cycles=5,
                    human_review_mechanism=HumanReviewMechanism.PR_REVIEW,
                ),
            },
        )
        assert contract.phase_configs is not None
        assert PipelinePhase.IMPLEMENT in contract.phase_configs
        assert contract.phase_configs[PipelinePhase.IMPLEMENT].max_review_cycles == 5

    def test_contract_phase_configs_json_roundtrip(self):
        """Test that phase_configs serializes and deserializes correctly."""
        original = Contract(
            issue=IssueInfo(
                number=123,
                title="Test",
                url="https://example.com",
            ),
            phase_configs={
                PipelinePhase.REFINE: PhaseConfig(
                    checks=[
                        CheckDefinition(
                            id="check-draft",
                            name="Draft Check",
                            script="draft.py",
                        ),
                    ],
                ),
            },
        )

        data = original.model_dump(mode="json")
        assert "phase_configs" in data
        assert "refine" in data["phase_configs"]
        assert len(data["phase_configs"]["refine"]["checks"]) == 1

        restored = Contract.model_validate(data)
        assert restored.phase_configs is not None
        assert PipelinePhase.REFINE in restored.phase_configs
        assert len(restored.phase_configs[PipelinePhase.REFINE].checks) == 1

    def test_contract_backward_compatibility_no_phase_configs(self):
        """Test that existing contracts without phase_configs still work."""
        # Simulate an old contract that doesn't have phase_configs
        data = {
            "schemaVersion": "1.0",
            "issue": {
                "number": 123,
                "title": "Test",
                "url": "https://example.com",
            },
            "current_phase": "refine",
            "phases": [],
            "decisions": [],
            "audit_log": [],
        }
        contract = Contract.model_validate(data)
        assert contract.phase_configs is None
