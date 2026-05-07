"""Tests for egg_contracts.models module."""

import warnings
from datetime import UTC, datetime

import pytest
from egg_contracts.models import (
    AgentExecutionModel,
    AgentExecutionStatus,
    AgentRoleType,
    AuditAction,
    AuditEntry,
    AuditRole,
    CheckDefinition,
    CheckResult,
    CheckStatus,
    Contract,
    Decision,
    DecisionType,
    DeferredAction,
    EggContractBaseModel,
    Feedback,
    FeedbackQuestion,
    HumanReviewMechanism,
    IssueInfo,
    Phase,
    PhaseConfig,
    PhaseStatus,
    PipelinePhase,
    PRMetadata,
    ReviewFeedback,
    Slice,
    SliceStatus,
    Task,
    TaskStatus,
)
from pydantic import ValidationError
from pydantic_core import PydanticSerializationUnexpectedValue


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

    def test_task_role_defaults_to_none(self):
        """Test that role defaults to None when not provided."""
        task = Task(id="task-1", description="Implement feature")
        assert task.role is None

    def test_task_with_role_coder(self):
        """Test task with role set to coder."""
        task = Task(id="task-1", description="Test", role="coder")
        assert task.role == "coder"

    def test_task_with_role_tester(self):
        """Test task with role set to tester."""
        task = Task(id="task-1", description="Test", role="tester")
        assert task.role == "tester"

    def test_task_with_role_documenter(self):
        """Test task with role set to documenter."""
        task = Task(id="task-1", description="Test", role="documenter")
        assert task.role == "documenter"

    def test_task_role_arbitrary_string_accepted(self):
        """Test that any string is accepted for role (no enum validation)."""
        task = Task(id="task-1", description="Test", role="reviewer")
        assert task.role == "reviewer"

    def test_task_role_serialization_roundtrip(self):
        """Test that role is preserved through serialization roundtrip."""
        task = Task(id="task-1", description="Test", role="coder")
        data = task.model_dump()
        assert data["role"] == "coder"
        restored = Task(**data)
        assert restored.role == "coder"

    def test_task_role_none_serialization_roundtrip(self):
        """Test that role=None is preserved through serialization roundtrip."""
        task = Task(id="task-1", description="Test")
        data = task.model_dump()
        assert data["role"] is None
        restored = Task(**data)
        assert restored.role is None

    def test_existing_contract_without_role_deserializes(self):
        """Test backward compatibility: dict without role key deserializes with role=None."""
        data = {
            "id": "task-1",
            "description": "Legacy task",
            "status": "pending",
            "commit": None,
            "notes": "",
        }
        task = Task(**data)
        assert task.role is None
        assert task.description == "Legacy task"


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

    def test_phase_commit_field(self):
        """Test that phase can have a commit SHA linked."""
        phase = Phase(
            id="phase-1",
            name="Foundation",
            commit="abc1234def5678",
        )
        assert phase.commit == "abc1234def5678"

    def test_phase_commit_defaults_to_none(self):
        """Test that phase commit defaults to None."""
        phase = Phase(id="phase-1", name="Setup")
        assert phase.commit is None

    def test_phase_commit_empty_string_becomes_none(self):
        """Test that empty string commit is normalized to None."""
        phase = Phase(id="phase-1", name="Setup", commit="")
        assert phase.commit is None

    def test_phase_commit_invalid_pattern(self):
        """Test that invalid commit SHA is rejected."""
        with pytest.raises(ValidationError):
            Phase(id="phase-1", name="Setup", commit="not-a-sha")


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
            actor="james-in-a-box",
            role=AuditRole.IMPLEMENTER,
            action=AuditAction.UPDATE,
            field_path="phases.0.tasks.0.commit",
            old_value=None,
            new_value="abc1234",
        )
        assert entry.actor == "james-in-a-box"
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
            workflow_owner="my-org",
        )
        assert contract.workflow_owner == "my-org"

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


class TestValidateAssignmentSiblingModels:
    """Regression tests for #2490 — ``validate_assignment=True`` extended
    from ``Contract`` to every sibling model in the contract object graph
    via the shared ``EggContractBaseModel``.

    Each test asserts the two halves of the acceptance criterion: a
    string-compatible enum value coerces to the declared type, and an
    out-of-domain value raises ``pydantic.ValidationError`` (which
    ``apply_mutation`` then surfaces as ``MutationResult(success=False)``
    via the catch added in #2484).
    """

    def test_base_class_sets_validate_assignment(self):
        """The shared base flips ``validate_assignment`` on for everyone."""
        assert EggContractBaseModel.model_config.get("validate_assignment") is True
        # And every concrete model inherits the config — spot-check a
        # representative sample across enum / nested-model / scalar
        # field shapes.
        for cls in (
            Task,
            Slice,
            Decision,
            AgentExecutionModel,
            PRMetadata,
            Feedback,
            Contract,
        ):
            assert issubclass(cls, EggContractBaseModel)
            assert cls.model_config.get("validate_assignment") is True

    def test_task_status_string_coerces_to_enum(self):
        task = Task(id="task-1", description="x")
        # ``setattr`` (not ``=``) — this is the path ``apply_mutation``
        # / ``_set_value`` ultimately takes, and avoids tripping mypy on
        # str-to-enum assignments that are only legal at runtime thanks
        # to ``validate_assignment=True``.
        setattr(task, "status", "in_progress")  # noqa: B010
        assert type(task.status) is TaskStatus
        assert task.status is TaskStatus.IN_PROGRESS

    def test_task_invalid_status_raises(self):
        task = Task(id="task-1", description="x")
        with pytest.raises(ValidationError):
            setattr(task, "status", "garbage")  # noqa: B010
        # Original value unchanged.
        assert task.status is TaskStatus.PENDING

    def test_slice_status_string_coerces_to_enum(self):
        slice_ = Slice(id="slice-1", name="one")
        setattr(slice_, "status", "in_progress")  # noqa: B010
        assert type(slice_.status) is SliceStatus
        assert slice_.status is SliceStatus.IN_PROGRESS

    def test_slice_invalid_status_raises(self):
        slice_ = Slice(id="slice-1", name="one")
        with pytest.raises(ValidationError):
            setattr(slice_, "status", "garbage")  # noqa: B010
        assert slice_.status is SliceStatus.PENDING

    def test_decision_type_string_coerces_to_enum(self):
        decision = Decision(id="decision-1", question="q?", type=DecisionType.HITL)
        setattr(decision, "type", "auto")  # noqa: B010
        assert type(decision.type) is DecisionType
        assert decision.type is DecisionType.AUTO

    def test_decision_invalid_type_raises(self):
        decision = Decision(id="decision-1", question="q?", type=DecisionType.HITL)
        with pytest.raises(ValidationError):
            setattr(decision, "type", "garbage")  # noqa: B010
        assert decision.type is DecisionType.HITL

    def test_agent_execution_status_string_coerces_to_enum(self):
        execution = AgentExecutionModel(role=AgentRoleType.CODER)
        setattr(execution, "status", "running")  # noqa: B010
        assert type(execution.status) is AgentExecutionStatus
        assert execution.status is AgentExecutionStatus.RUNNING

    def test_agent_execution_invalid_status_raises(self):
        execution = AgentExecutionModel(role=AgentRoleType.CODER)
        with pytest.raises(ValidationError):
            setattr(execution, "status", "garbage")  # noqa: B010
        assert execution.status is AgentExecutionStatus.PENDING

    def test_agent_execution_invalid_role_raises(self):
        execution = AgentExecutionModel(role=AgentRoleType.CODER)
        with pytest.raises(ValidationError):
            setattr(execution, "role", "not-a-real-role")  # noqa: B010
        assert execution.role is AgentRoleType.CODER

    def test_review_feedback_status_string_coerces_to_enum(self):
        review = ReviewFeedback(
            timestamp=datetime.now(UTC),
            task_id="task-1",
            feedback="needs work",
        )
        setattr(review, "status", "complete")  # noqa: B010
        assert type(review.status) is TaskStatus
        assert review.status is TaskStatus.COMPLETE

    def test_phase_config_human_review_mechanism_coerces_to_enum(self):
        config = PhaseConfig()
        setattr(config, "human_review_mechanism", "PR_REVIEW")  # noqa: B010
        assert type(config.human_review_mechanism) is HumanReviewMechanism
        assert config.human_review_mechanism is HumanReviewMechanism.PR_REVIEW

    def test_check_result_status_string_coerces_to_enum(self):
        result = CheckResult(check_id="check-lint", status=CheckStatus.PASS)
        setattr(result, "status", "fail")  # noqa: B010
        assert type(result.status) is CheckStatus
        assert result.status is CheckStatus.FAIL

    def test_audit_entry_invalid_action_raises(self):
        entry = AuditEntry(
            timestamp=datetime.now(UTC),
            actor="actor",
            role=AuditRole.HUMAN,
            action=AuditAction.UPDATE,
            field_path="x",
        )
        with pytest.raises(ValidationError):
            setattr(entry, "action", "garbage")  # noqa: B010
        assert entry.action is AuditAction.UPDATE

    def test_feedback_phase_string_coerces_to_enum(self):
        feedback = Feedback(
            id="feedback-1",
            questions=[FeedbackQuestion(id="Q1", question="q?")],
        )
        setattr(feedback, "phase", "plan")  # noqa: B010
        assert type(feedback.phase) is PipelinePhase
        assert feedback.phase is PipelinePhase.PLAN

    def test_pr_metadata_invalid_deferred_actions_raises(self):
        """Field validation re-runs on assignment for nested-model fields too."""
        pr = PRMetadata(title="t")
        pr.deferred_actions = [DeferredAction(reviewer="r", condition="cond")]
        with pytest.raises(ValidationError):
            # ``resolved_in_diff`` has a hex-only pattern; non-hex must be rejected.
            pr.deferred_actions = [
                DeferredAction(reviewer="r", condition="cond", resolved_in_diff="not-hex!")
            ]

    def test_no_unexpected_serialization_warnings_after_sibling_assignments(self):
        """Round-trip after sibling-model setattrs emits no PydanticSerializationUnexpectedValue.

        Mirror of the second acceptance criterion: existing tests that
        round-trip a contract must not start emitting
        ``PydanticSerializationUnexpectedValue`` warnings after the
        sibling models adopt ``validate_assignment=True``.
        """
        contract = Contract(
            issue=IssueInfo(
                number=1,
                title="t",
                url="https://github.com/o/r/issues/1",
            ),
            slices=[
                Slice(
                    id="slice-1",
                    name="one",
                    tasks=[Task(id="task-1", description="x")],
                )
            ],
            decisions=[Decision(id="decision-1", question="q?", type=DecisionType.HITL)],
        )
        setattr(contract.slices[0].tasks[0], "status", "complete")  # noqa: B010
        setattr(contract.slices[0], "status", "complete")  # noqa: B010
        setattr(contract.decisions[0], "type", "auto")  # noqa: B010

        with warnings.catch_warnings():
            warnings.simplefilter("error", PydanticSerializationUnexpectedValue)
            contract.model_dump(mode="json")
