"""Tests for egg_contracts.validator module."""

import warnings

import pytest
from egg_contracts.models import (
    AuditAction,
    Contract,
    IssueInfo,
    Phase,
    PipelinePhase,
    Task,
    TaskStatus,
)
from egg_contracts.roles import Role
from egg_contracts.validator import (
    apply_mutation,
    validate_mutation,
    validate_phase_mutation,
    validate_task_mutation,
)
from pydantic_core import PydanticSerializationUnexpectedValue


class TestValidateMutation:
    """Tests for validate_mutation function."""

    def test_valid_implementer_mutation(self):
        """Test that implementer can modify allowed fields."""
        result = validate_mutation(
            role=Role.IMPLEMENTER,
            field_path="phases.0.tasks.0.commit",
            new_value="abc1234",
        )
        assert result.valid is True
        assert result.message == "Mutation allowed"

    def test_implementer_can_modify_task_status(self):
        """Test that implementer can modify task status (shared ownership)."""
        result = validate_mutation(
            role=Role.IMPLEMENTER,
            field_path="phases.0.tasks.0.status",
            new_value="complete",
        )
        assert result.valid is True

    def test_valid_reviewer_mutation(self):
        """Test that reviewer can modify status."""
        result = validate_mutation(
            role=Role.REVIEWER,
            field_path="phases.0.tasks.0.status",
            new_value="complete",
        )
        assert result.valid is True

    def test_invalid_reviewer_mutation(self):
        """Test that reviewer cannot modify commit."""
        result = validate_mutation(
            role=Role.REVIEWER,
            field_path="phases.0.tasks.0.commit",
            new_value="abc1234",
        )
        assert result.valid is False
        assert result.required_role == "implementer"

    def test_human_can_modify_anything(self):
        """Test that human role can modify any field."""
        # Implementer field
        result = validate_mutation(
            role=Role.HUMAN,
            field_path="phases.0.tasks.0.commit",
            new_value="abc1234",
        )
        assert result.valid is True

        # Reviewer field
        result = validate_mutation(
            role=Role.HUMAN,
            field_path="phases.0.tasks.0.status",
            new_value="complete",
        )
        assert result.valid is True

        # Human field
        result = validate_mutation(
            role=Role.HUMAN,
            field_path="decisions.0.resolved",
            new_value=True,
        )
        assert result.valid is True


class TestApplyMutation:
    """Tests for apply_mutation function."""

    @pytest.fixture
    def sample_contract(self):
        """Create a sample contract for testing."""
        return Contract(
            issue=IssueInfo(
                number=133,
                title="Test",
                url="https://example.com",
            ),
            phases=[
                Phase(
                    id="phase-1",
                    name="Setup",
                    tasks=[
                        Task(
                            id="task-1",
                            description="First task",
                            status=TaskStatus.PENDING,
                        ),
                    ],
                ),
            ],
        )

    def test_apply_valid_mutation(self, sample_contract):
        """Test applying a valid mutation."""
        result = apply_mutation(
            contract=sample_contract,
            role=Role.IMPLEMENTER,
            actor="james-in-a-box",
            field_path="phases.0.tasks.0.commit",
            new_value="abc1234",
            reason="Implementation complete",
        )
        assert result.success is True
        assert result.contract is not None
        assert result.contract.phases[0].tasks[0].commit == "abc1234"
        assert result.audit_entry is not None
        assert result.audit_entry.actor == "james-in-a-box"
        assert result.audit_entry.reason == "Implementation complete"

    def test_apply_invalid_mutation_rejected(self, sample_contract):
        """Test that invalid mutations are rejected."""
        result = apply_mutation(
            contract=sample_contract,
            role=Role.REVIEWER,
            actor="reviewer-agent",
            field_path="phases.0.tasks.0.commit",
            new_value="abc1234",
        )
        assert result.success is False
        assert "reviewer" in result.message.lower()
        assert result.contract is None
        assert result.audit_entry is None

    def test_implementer_can_set_task_status(self, sample_contract):
        """Test that implementer can mark task complete (shared ownership)."""
        result = apply_mutation(
            contract=sample_contract,
            role=Role.IMPLEMENTER,
            actor="james-in-a-box",
            field_path="phases.0.tasks.0.status",
            new_value="complete",
        )
        assert result.success is True
        assert result.contract is not None
        assert result.contract.phases[0].tasks[0].status == TaskStatus.COMPLETE

    def test_apply_reviewer_mutation(self, sample_contract):
        """Test reviewer can mark task complete."""
        result = apply_mutation(
            contract=sample_contract,
            role=Role.REVIEWER,
            actor="reviewer-agent",
            field_path="phases.0.tasks.0.status",
            new_value=TaskStatus.COMPLETE,
        )
        assert result.success is True
        assert result.contract.phases[0].tasks[0].status == TaskStatus.COMPLETE

    def test_audit_log_appended(self, sample_contract):
        """Test that audit log entry is appended."""
        initial_log_len = len(sample_contract.audit_log)

        result = apply_mutation(
            contract=sample_contract,
            role=Role.IMPLEMENTER,
            actor="james-in-a-box",
            field_path="phases.0.tasks.0.notes",
            new_value="Added implementation notes",
        )

        assert result.success is True
        assert len(result.contract.audit_log) == initial_log_len + 1
        assert result.contract.audit_log[-1].field_path == "phases.0.tasks.0.notes"

    def test_old_value_captured(self, sample_contract):
        """Test that old value is captured in audit log."""
        # First set a value
        sample_contract.phases[0].tasks[0].notes = "Original notes"

        result = apply_mutation(
            contract=sample_contract,
            role=Role.IMPLEMENTER,
            actor="james-in-a-box",
            field_path="phases.0.tasks.0.notes",
            new_value="Updated notes",
        )

        assert result.success is True
        assert result.audit_entry.old_value == "Original notes"
        assert result.audit_entry.new_value == "Updated notes"

    def test_current_phase_emits_transition_action(self, sample_contract):
        """current_phase mutations emit AuditAction.TRANSITION (not UPDATE)."""
        sample_contract.current_phase = PipelinePhase.REFINE

        result = apply_mutation(
            contract=sample_contract,
            role=Role.HUMAN,
            actor="human-reviewer",
            field_path="current_phase",
            new_value=PipelinePhase.PLAN.value,
            reason="Analysis approved",
        )

        assert result.success is True
        assert result.audit_entry is not None
        assert result.audit_entry.action == AuditAction.TRANSITION
        assert result.audit_entry.field_path == "current_phase"
        # Old/new values are normalised to the enum string form regardless
        # of whether the caller passed a string or a PipelinePhase instance.
        assert result.audit_entry.old_value == "refine"
        assert result.audit_entry.new_value == "plan"
        assert result.audit_entry.reason == "Analysis approved"

    def test_current_phase_normalises_enum_new_value(self, sample_contract):
        """Passing a PipelinePhase enum as new_value still produces a string-valued audit entry."""
        sample_contract.current_phase = PipelinePhase.PLAN

        result = apply_mutation(
            contract=sample_contract,
            role=Role.HUMAN,
            actor="human-reviewer",
            field_path="current_phase",
            new_value=PipelinePhase.IMPLEMENT,
            reason="Plan approved",
        )

        assert result.success is True
        assert result.audit_entry.action == AuditAction.TRANSITION
        # The normalisation in validator.py converts PipelinePhase -> str so
        # downstream consumers see a plain string. Asserting type() (rather than
        # equality) is required because PipelinePhase is a StrEnum and would
        # compare equal to "implement" even without normalisation.
        assert type(result.audit_entry.old_value) is str
        assert type(result.audit_entry.new_value) is str
        assert result.audit_entry.old_value == "plan"
        assert result.audit_entry.new_value == "implement"

    def test_current_phase_string_assignment_coerces_to_enum(self, sample_contract):
        """``apply_mutation`` with a raw string leaves ``current_phase`` as ``PipelinePhase``.

        Regression for #2465: before ``validate_assignment=True`` was set
        on ``Contract``, ``setattr(contract, "current_phase", "plan")``
        left the field as a plain ``str``, breaking ``.value`` reads and
        triggering ``PydanticSerializationUnexpectedValue`` on the next
        ``model_dump``.
        """
        sample_contract.current_phase = PipelinePhase.REFINE

        result = apply_mutation(
            contract=sample_contract,
            role=Role.HUMAN,
            actor="human-reviewer",
            field_path="current_phase",
            new_value="plan",
        )

        assert result.success is True
        assert type(result.contract.current_phase) is PipelinePhase
        assert result.contract.current_phase is PipelinePhase.PLAN

        # Re-serialising the contract must not emit
        # PydanticSerializationUnexpectedValue (the warning that #2465
        # called out as the second symptom of the bug).
        with warnings.catch_warnings():
            warnings.simplefilter("error", PydanticSerializationUnexpectedValue)
            result.contract.model_dump(mode="json")

    def test_invalid_enum_value_returns_failed_mutation(self, sample_contract):
        """Out-of-domain enum strings return ``success=False`` instead of raising.

        With ``validate_assignment=True`` on ``Contract`` (added for
        #2465), ``setattr(contract, "current_phase", "garbage")`` raises
        ``pydantic.ValidationError`` from inside the assignment.
        ``apply_mutation`` must catch that and surface it through the
        existing ``MutationResult(success=False, ...)`` channel so the
        contract /mutate route returns a structured 4xx instead of an
        opaque 500.
        """
        sample_contract.current_phase = PipelinePhase.REFINE

        result = apply_mutation(
            contract=sample_contract,
            role=Role.HUMAN,
            actor="human-reviewer",
            field_path="current_phase",
            new_value="invalid_phase_value",
        )

        assert result.success is False
        assert result.message is not None
        assert "current_phase" in result.message
        # The original value is unchanged.
        assert sample_contract.current_phase is PipelinePhase.REFINE

    def test_non_current_phase_field_still_emits_update_action(self, sample_contract):
        """Non-current_phase fields continue to emit AuditAction.UPDATE."""
        result = apply_mutation(
            contract=sample_contract,
            role=Role.IMPLEMENTER,
            actor="james-in-a-box",
            field_path="phases.0.tasks.0.commit",
            new_value="abc1234",
        )

        assert result.success is True
        assert result.audit_entry is not None
        assert result.audit_entry.action == AuditAction.UPDATE


class TestValidateTaskMutation:
    """Tests for validate_task_mutation helper."""

    def test_implementer_commit_allowed(self):
        """Test implementer can set commit."""
        result = validate_task_mutation(
            role=Role.IMPLEMENTER,
            field="commit",
            new_value="abc1234",
        )
        assert result.valid is True

    def test_implementer_status_allowed(self):
        """Test implementer can set task status (shared ownership)."""
        result = validate_task_mutation(
            role=Role.IMPLEMENTER,
            field="status",
            new_value="complete",
        )
        assert result.valid is True


class TestValidatePhaseMutation:
    """Tests for validate_phase_mutation helper."""

    def test_reviewer_status_allowed(self):
        """Test reviewer can set phase status."""
        result = validate_phase_mutation(
            role=Role.REVIEWER,
            field="status",
            new_value="complete",
        )
        assert result.valid is True

    def test_implementer_status_allowed(self):
        """Test implementer can set phase status (shared ownership)."""
        result = validate_phase_mutation(
            role=Role.IMPLEMENTER,
            field="status",
            new_value="complete",
        )
        assert result.valid is True


class TestErrorMessages:
    """Tests for error message formatting."""

    def test_clear_error_message_format(self):
        """Test that error messages are clear and helpful."""
        result = validate_mutation(
            role=Role.IMPLEMENTER,
            field_path="acceptance_criteria.0.verified",
            new_value=True,
        )
        assert result.valid is False
        # Check error message contains key information
        assert "acceptance_criteria.*.verified" in result.message
        assert "implementer" in result.message.lower()
        assert "reviewer" in result.message.lower()

    def test_decision_field_error_message(self):
        """Test error message for decision field."""
        result = validate_mutation(
            role=Role.REVIEWER,
            field_path="decisions.0.resolved",
            new_value=True,
        )
        assert result.valid is False
        assert "human" in result.message.lower()


class TestArrayAppend:
    """Tests for array append functionality in _set_value."""

    @pytest.fixture
    def contract_with_decisions(self):
        """Create a contract with an empty decisions list."""
        return Contract(
            issue=IssueInfo(
                number=133,
                title="Test",
                url="https://example.com",
            ),
            phases=[
                Phase(
                    id="phase-1",
                    name="Setup",
                    tasks=[
                        Task(
                            id="task-1",
                            description="First task",
                            status=TaskStatus.PENDING,
                        ),
                    ],
                ),
            ],
            decisions=[],
        )

    def test_append_to_empty_array(self, contract_with_decisions):
        """Test appending to an empty decisions array."""
        decision = {
            "id": "decision-1",
            "question": "Test question",
            "resolved": False,
        }
        result = apply_mutation(
            contract=contract_with_decisions,
            role=Role.IMPLEMENTER,
            actor="james-in-a-box",
            field_path="decisions.0",
            new_value=decision,
            reason="Created decision",
        )
        assert result.success is True
        assert len(result.contract.decisions) == 1
        assert result.contract.decisions[0]["id"] == "decision-1"

    def test_append_to_existing_array(self, contract_with_decisions):
        """Test appending a second item to an array."""
        # First add one decision
        decision1 = {"id": "decision-1", "question": "Q1", "resolved": False}
        contract_with_decisions.decisions.append(decision1)

        # Now append another
        decision2 = {"id": "decision-2", "question": "Q2", "resolved": False}
        result = apply_mutation(
            contract=contract_with_decisions,
            role=Role.IMPLEMENTER,
            actor="james-in-a-box",
            field_path="decisions.1",
            new_value=decision2,
            reason="Created second decision",
        )
        assert result.success is True
        assert len(result.contract.decisions) == 2
        assert result.contract.decisions[1]["id"] == "decision-2"

    def test_overwrite_existing_array_element(self, contract_with_decisions):
        """Test overwriting an existing array element."""
        # Add a decision
        decision1 = {"id": "decision-1", "question": "Q1", "resolved": False}
        contract_with_decisions.decisions.append(decision1)

        # Overwrite it (IMPLEMENTER can modify decisions.*)
        updated = {"id": "decision-1", "question": "Updated Q1", "resolved": False}
        result = apply_mutation(
            contract=contract_with_decisions,
            role=Role.IMPLEMENTER,
            actor="james-in-a-box",
            field_path="decisions.0",
            new_value=updated,
            reason="Updated decision",
        )
        assert result.success is True
        assert result.contract.decisions[0]["question"] == "Updated Q1"

    def test_append_with_gap_fails(self, contract_with_decisions):
        """Test that appending with a gap in indices fails."""
        decision = {"id": "decision-2", "question": "Q2", "resolved": False}
        result = apply_mutation(
            contract=contract_with_decisions,
            role=Role.IMPLEMENTER,
            actor="james-in-a-box",
            field_path="decisions.2",  # Gap: no index 0 or 1
            new_value=decision,
            reason="Created decision",
        )
        assert result.success is False
        assert "out of range" in result.message.lower()
